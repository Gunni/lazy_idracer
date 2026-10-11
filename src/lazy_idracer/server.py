"""One Dell server's persistent HTTPS session, registry and configuration state."""
import json
import time
from threading import Lock
from urllib.parse import urlsplit
import httpx
from .ui import CONSOLE
from .retry import telemetry_retry_delay

def registry_value(entry, value):
    options = [option for option in entry.get('Value', []) if isinstance(option, dict) and 'ValueName' in option]
    if any(type(value) is type(option['ValueName']) and value == option['ValueName'] for option in options):
        return value
    matches = [option['ValueName'] for option in options
               if type(value) is type(option.get('ValueDisplayName')) and value == option.get('ValueDisplayName')]
    return matches[0] if len(matches) == 1 else value


def registry_equal(entry, left, right):
    left, right = registry_value(entry, left), registry_value(entry, right)
    return type(left) is type(right) and left == right


class DellServer:
    def __init__(self, name, client):
        self.name, self.client = name, client
        self.resources = {}
        self.errors = {}
        self.session_path = None
        self.registries = {}
        self.blocked = False
        self.login_debug = False
        self.verbose = False
        self.read_retry_seconds = 0
        self.renew_session = None
        self.session_lock = Lock()
        self.diagnostics = None
        self.cancel_reads = None

    def load_registry(self, reference):
        base = reference.split('.v', 1)[0]
        if reference.startswith('/redfish/'):
            candidates = [reference]
        else:
            candidates = ['/redfish/v1/Registries/' + reference,
                          '/redfish/v1/Registries/' + base,
                          f'/redfish/v1/Registries/{base}/{reference}.json']
            if base == 'ManagerAttributeRegistry':
                candidates.append('/redfish/v1/Registries/ManagerAttributeRegistry/ManagerAttributeRegistry.v1_0_0.json')
        visited = set()
        collection_checked = False
        while candidates or not collection_checked:
            if not candidates:
                collection_checked = True
                collection = self.request('GET', '/redfish/v1/Registries').json()
                candidates.extend(member['@odata.id'] for member in collection.get('Members', [])
                                  if base in member.get('@odata.id', ''))
                continue
            uri = candidates.pop(0)
            if uri in visited:
                continue
            visited.add(uri)
            try:
                registry = self.request('GET', uri).json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                raise
            if isinstance(registry.get('RegistryEntries', {}).get('Attributes'), list):
                return registry
            locations = sorted(registry.get('Location', []), key=lambda loc: loc.get('Language') != 'en')
            candidates = [loc['Uri'] for loc in locations
                          if loc.get('Uri', '').startswith('/redfish/')] + candidates
        raise ValueError(f'No local registry document found for {reference}')

    def attribute_metadata(self, path, data):
        reference = data.get('AttributeRegistry')
        if not reference and '/DellAttributes/' in path:
            reference = 'ManagerAttributeRegistry'
        if not reference:
            return {}
        if isinstance(reference, dict):
            reference = reference.get('@odata.id')
        if not isinstance(reference, str):
            return {}
        if reference in self.registries:
            return self.registries[reference]
        metadata = {}
        try:
            registry = self.load_registry(reference)
            entries = registry.get('RegistryEntries', {}).get('Attributes')
            if not isinstance(entries, list):
                raise ValueError('Missing registry attribute entries')
            metadata = {entry['AttributeName']: entry for entry in entries if 'AttributeName' in entry}
        except (httpx.HTTPError, ValueError) as exc:
            self.errors[f'registry:{reference}'] = str(exc)
            CONSOLE.print(f'{self.name}: registry unavailable: {reference}: {exc}. '
                          'Attributes will not be offered for writes.', markup=False)
        self.registries[reference] = metadata
        return metadata

    def writable(self, path, key, allow_readonly=False):
        reason = self.exclusion_reason(path, key)
        return reason is None or (allow_readonly and reason == 'Registry marks read-only')

    def exclusion_reason(self, path, key):
        if not self.session_path and 'login' in self.errors:
            return 'Login failed'
        if self.blocked:
            return 'Authentication / access rejected'
        if path not in self.resources:
            return 'Resource not read: ' + self.errors.get(path, 'unavailable on this host')
        if key not in self.resources[path]['values']:
            return 'Attribute absent from resource'
        entry = self.resources[path].get('metadata', {}).get(key, {})
        if not entry:
            return 'Attribute absent from registry (or registry could not be loaded)'
        readonly_flags = [entry[name] for name in ('ReadOnly', 'Readonly') if name in entry]
        if not readonly_flags or any(type(flag) is not bool for flag in readonly_flags):
            return 'Registry does not declare writability'
        if entry.get('Immutable', False):
            return 'Registry marks immutable'
        if entry.get('IsSystemUniqueProperty', False):
            return 'Registry marks system-unique'
        if entry.get('Type') == 'Password':
            return 'Password field excluded'
        if any(readonly_flags):
            return 'Registry marks read-only'
        return None

    def explain_restriction(self, path, key, reason):
        # Dell documents FRMPercent as inactive outside FaultResilientMode.
        # Keep this explanatory text separate from eligibility decisions.
        if key == 'FRMPercent' and path.endswith('/Bios') and reason in (
                'Registry marks immutable', 'Registry marks read-only'):
            mode = self.resources.get(path, {}).get('values', {}).get('MemOpMode')
            if mode is not None and mode != 'FaultResilientMode':
                return (f'{reason}: MemOpMode={mode}; FRM percentage is inactive '
                        'unless FaultResilientMode is selected')
        return reason

    def login(self, user, password):
        started = time.monotonic()
        def trace(event, info):
            # Never print info: response headers contain the session token.
            CONSOLE.print(f'{self.name}: login +{time.monotonic() - started:.2f}s {event}', markup=False)
        extensions = {'trace': trace} if self.login_debug else {}
        try:
            response = self.request('POST', '/redfish/v1/SessionService/Sessions',
                                    json={'UserName': user, 'Password': password}, extensions=extensions)
        except httpx.HTTPError as exc:
            CONSOLE.print(f'{self.name}: login failed after {time.monotonic() - started:.1f}s '
                          f'({type(exc).__name__})', markup=False)
            raise
        token = response.headers.get('X-Auth-Token')
        location = response.headers.get('Location')
        if location:
            parsed = urlsplit(location)
            if parsed.netloc:
                if parsed.scheme != 'https' or parsed.netloc != self.client.base_url.netloc.decode():
                    raise ValueError('Session location points to another host')
                location = parsed.path
            if not location.startswith('/redfish/'):
                raise ValueError('Invalid session location')
        if not token or not location:
            raise ValueError('Session login did not return X-Auth-Token and Location')
        self.client.headers['X-Auth-Token'] = token
        self.session_path = location
        if self.verbose:
            CONSOLE.print(f'{self.name}: logged in after {time.monotonic() - started:.1f}s '
                          f'using {response.http_version}', markup=False)

    def logout(self):
        if not self.session_path:
            return
        try:
            response = self.client.delete(self.session_path, timeout=5)
            response.raise_for_status()
            return True
        except httpx.HTTPError:
            return False
        except KeyboardInterrupt:
            print('  Logout interrupted; remaining sessions will expire automatically.', flush=True)
            raise
        finally:
            self.client.headers.pop('X-Auth-Token', None)
            self.session_path = None

    def request(self, method, path, **kwargs):
        # Reject absolute/off-host URLs, including network-path references.
        if not isinstance(path, str) or not path.startswith('/redfish/') or urlsplit(path).netloc:
            raise ValueError(f'Invalid local Redfish URL: {path!r}')
        deadline = time.monotonic() + self.read_retry_seconds
        announced = False
        reconnect_attempts = 0
        session_renewed = False
        while True:
            if method == 'GET' and self.cancel_reads is not None and self.cancel_reads.is_set():
                raise ValueError('Read cancelled on exit')
            attempt_started = time.monotonic()
            request_token = getattr(self.client, 'headers', {}).get('X-Auth-Token')
            try:
                response = self.client.request(method, path, **kwargs)
                response.raise_for_status()
                self.log_request(method, path, attempt_started, response=response)
                return response
            except httpx.HTTPError as exc:
                self.log_request(method, path, attempt_started, error=exc)
                if method == 'GET' and self.cancel_reads is not None and self.cancel_reads.is_set():
                    raise ValueError('Read cancelled on exit') from exc
                renew = getattr(self, 'renew_session', None)
                if (method == 'GET' and renew and not session_renewed
                        and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401):
                    session_renewed = True
                    with self.session_lock:
                        # Concurrent job reads can fail with the same expired
                        # token. Reuse a session another reader already renewed.
                        if getattr(self.client, 'headers', {}).get('X-Auth-Token') == request_token:
                            CONSOLE.print(f'{self.name}: session expired after update; logging in again', markup=False)
                            renew()
                    continue
                if method == 'GET' and isinstance(exc, httpx.TransportError) and not self.read_retry_seconds and reconnect_attempts < 2:
                    reconnect_attempts += 1
                    CONSOLE.print(f'{self.name}: read connection interrupted; reconnecting '
                                  f'(attempt {reconnect_attempts}/2)', markup=False)
                    time.sleep(1)
                    continue
                retryable = (isinstance(exc, httpx.TransportError) or
                             isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504))
                if method != 'GET' or not retryable or time.monotonic() >= deadline:
                    raise
                if not announced:
                    CONSOLE.print(f'{self.name}: iDRAC temporarily unavailable; retrying reads '
                                  f'for up to {self.read_retry_seconds}s', markup=False)
                    announced = True
                delay = telemetry_retry_delay(exc) or 5
                remaining = deadline - time.monotonic()
                if delay >= remaining:
                    raise
                time.sleep(delay)

    def log_request(self, method, path, started, response=None, error=None):
        # Login/renewal never enters diagnostics; payloads and headers are never
        # inspected. Strip query strings as they can contain credentials.
        resource = urlsplit(path).path
        if self.diagnostics is None or resource.startswith('/redfish/v1/SessionService'):
            return
        if response is None and error is not None:
            response = getattr(error, 'response', None)
        token = getattr(self.client, 'headers', {}).get('X-Auth-Token')
        entry = {'event': 'request', 'host': self.name, 'method': method, 'path': resource,
                 'status': getattr(response, 'status_code', None),
                 'elapsed_ms': round((time.monotonic() - started) * 1000, 1)}
        if error is not None:
            entry['error_type'] = type(error).__name__
        try:
            self.diagnostics.record(entry, secrets=[token])
        except OSError:
            self.diagnostics = None
            CONSOLE.print(f'{self.name}: diagnostic log unavailable; logging disabled.', markup=False)

    def read_resource(self, path):
        response = self.request('GET', path)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        if isinstance(data.get('Attributes'), dict):
            settings = data.get('@Redfish.Settings', {}).get('SettingsObject', {}).get('@odata.id')
            self.resources[path] = {'values': data['Attributes'], 'settings': settings,
                                    'supported_apply_times': data.get('@Redfish.Settings', {}).get('SupportedApplyTimes', []),
                                    'etag': response.headers.get('etag'),
                                    'metadata': self.attribute_metadata(path, data)}
            pending = self.request('GET', settings).json().get('Attributes', {}) if settings else {}
            self.resources[path]['pending'] = {key: value for key, value in pending.items()
                                               if key in data['Attributes'] and not registry_equal(
                                                   self.resources[path]['metadata'].get(key, {}), value, data['Attributes'][key])}
        return data

    def read_setting(self, path):
        return self.read_resource(path)

    @staticmethod
    def links(value, queue):
        if isinstance(value, list):
            for item in value:
                DellServer.links(item, queue)
        elif isinstance(value, dict):
            if '@odata.id' in value:
                queue.append(value['@odata.id'])
            else:
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        DellServer.links(item, queue)

    def apply(self, path, key, value, observed):
        resource = self.resources[path]
        entry = resource.get('metadata', {}).get(key, {})
        requested = value
        value = registry_value(entry, value)
        if self.verbose and (type(value) is not type(requested) or value != requested):
            CONSOLE.print(f'{self.name}: registry mapping {json.dumps(requested)} → {json.dumps(value)} (value sent)', markup=False)
        if not self.writable(path, key, allow_readonly=True):
            raise ValueError('Registry does not establish this attribute as writable and shared')
        if self.verbose and self.exclusion_reason(path, key) == 'Registry marks read-only':
            CONSOLE.print(f'{self.name}: attempting write despite registry read-only flag', markup=False)
        if self.verbose:
            CONSOLE.print(f'{self.name}: setting {key} = {json.dumps(value)}', markup=False)
        current_response = self.request('GET', path)
        current = current_response.json()['Attributes']
        if key not in current:
            raise ValueError('Attribute disappeared')
        if current[key] != observed:
            raise ValueError('Value changed since comparison; rediscover before retrying')
        target = resource['settings'] or path
        target_response = self.request('GET', target) if target != path else current_response
        target_data = target_response.json()
        pending = {}
        if target != path:
            pending = target_data.get('Attributes', {})
        self._remember_readback(resource, current, pending)
        if target != path:
            if key in pending and registry_equal(entry, pending[key], value):
                return 'already pending; no write needed'
            # PATCH only the chosen attribute; unrelated staged settings remain.
        if registry_equal(entry, current[key], value) and (key not in pending or registry_equal(entry, pending[key], value)):
            return 'already matches; no conflicting pending value'
        entry = resource.get('metadata', {}).get(key, {})
        allowed = [option['ValueName'] for option in entry.get('Value', [])
                   if isinstance(option, dict) and 'ValueName' in option]
        if allowed and value not in allowed:
            raise ValueError(f'{key}: {value!r} not allowed by this host registry; allowed: {allowed}')
        etag = target_response.headers.get('etag') or target_data.get('@odata.etag')
        headers = {'If-Match': etag} if etag else {}
        result = self.request('PATCH', target, json={'Attributes': {key: value}}, headers=headers)
        live = self.request('GET', path).json().get('Attributes', {})
        if self.verbose:
            CONSOLE.print(f'{self.name}: active read-back {key} = {json.dumps(live.get(key))}', markup=False)
        if target == path:
            self._remember_readback(resource, live, {})
        if target != path:
            staged = self.request('GET', target).json().get('Attributes', {})
            self._remember_readback(resource, live, staged)
            if self.verbose:
                CONSOLE.print(f'{self.name}: pending read-back {key} = {json.dumps(staged.get(key))}', markup=False)
            if key in staged and registry_equal(entry, staged[key], value):
                return ('verified active; pending agrees' if registry_equal(entry, live.get(key), value) else
                        'verified pending; firmware job/apply/reboot may be required')
            if key in staged and staged[key] != value:
                raise ValueError(f'Pending read-back still conflicts: {staged[key]!r}; wanted {value!r}')
        if registry_equal(entry, live.get(key), value):
            return 'verified active; no conflicting pending value'
        location = result.headers.get('location')
        raise ValueError(f'Accepted HTTP {result.status_code}, but value not verified; task={location!r}. Inspect before retrying')

    @staticmethod
    def _remember_readback(resource, active, staged):
        resource['values'] = active
        resource['pending'] = {key: value for key, value in staged.items()
                               if key in active and not registry_equal(
                                   resource.get('metadata', {}).get(key, {}), value, active[key])}
