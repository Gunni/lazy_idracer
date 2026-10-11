"""Fleet power monitoring, startup throttling and restoration."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event, Lock, Thread
import math
import time
from urllib.parse import urlsplit
import httpx
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from . import ui
from .ui import CONSOLE, PROGRESS, ask, parallel, error_detail
from .constants import SYSTEM_PATH
from .jobs import Job, JobGroup, JobFailure
from .server import registry_equal
from .retry import telemetry_retry_delay
from .storage import save

def power_action(host, reset):
    system = host.request('GET', SYSTEM_PATH).json()
    action = system.get('Actions', {}).get('#ComputerSystem.Reset', {})
    if reset not in action.get('ResetType@Redfish.AllowableValues', []):
        raise ValueError(f'{reset} not advertised; no power action sent')
    try:
        host.request('POST', action['target'], json={'ResetType': reset})
    except httpx.HTTPError as exc:
        ambiguous = (isinstance(exc, httpx.TransportError) or
                     isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (500, 502, 503, 504))
        if not ambiguous:
            raise
        CONSOLE.print(f'{host.name}: {reset} response lost/unavailable; checking state without resending', markup=False)


def wait_power(host, expected, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if host.request('GET', SYSTEM_PATH).json().get('PowerState') == expected:
            return
        time.sleep(3)
    raise ValueError(f'Power state did not reach {expected} within {timeout}s')


def restore_power(host, original):
    current = host.request('GET', SYSTEM_PATH).json().get('PowerState')
    if current == original:
        return
    CONSOLE.print(f'{host.name}: restoring original power state {original}', markup=False)
    if original == 'On':
        power_action(host, 'On')
        wait_power(host, 'On')
    else:
        system = host.request('GET', SYSTEM_PATH).json()
        allowed = system.get('Actions', {}).get('#ComputerSystem.Reset', {}).get('ResetType@Redfish.AllowableValues', [])
        if 'GracefulShutdown' in allowed:
            power_action(host, 'GracefulShutdown')
            try:
                wait_power(host, 'Off', timeout=120)
                return
            except ValueError:
                CONSOLE.print(f'{host.name}: graceful shutdown did not reach Off; requesting ForceOff', markup=False)
        power_action(host, 'ForceOff')
        wait_power(host, 'Off')


class PowerMonitor:
    """Poll host-associated chassis power; never treat missing readings as zero."""
    def __init__(self, hosts, args):
        self.hosts = hosts
        self.args = args
        self.sources = {}
        self.samples = {}
        self.errors = {}
        self.retry_at = {}
        self.lock = Lock()
        self.stop_event = Event()
        self.peak = 0
        self.not_before = 0
        self.task = PROGRESS.add_task('Reported host power: discovering telemetry', total=args.power_budget, unit='watts')
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(hosts)))
        self.thread = Thread(target=self.run, name='idrac-power-monitor', daemon=True)

    def read(self, host):
        sample_started = time.monotonic()
        # Telemetry gets its own bounded reads; no outage-retry loop holds up a sample.
        def get(path):
            if not isinstance(path, str) or not path.startswith('/redfish/') or urlsplit(path).netloc:
                raise ValueError('Invalid local power telemetry URL')
            started = time.monotonic()
            try:
                response = host.client.get(path, timeout=5)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                host.log_request('GET', path, started, error=exc)
                raise
            host.log_request('GET', path, started, response=response)
            return response.json()
        if host not in self.sources:
            system = get(SYSTEM_PATH)
            chassis = system.get('Links', {}).get('Chassis', [])
            if len(chassis) != 1:
                raise ValueError('Cannot identify one host-associated chassis power source')
            path = chassis[0]['@odata.id']
            info = get(path)
            if info.get('ChassisType') in ('Enclosure', 'Rack'):
                raise ValueError('Host link points to shared enclosure; host total would double-count shared power')
            power_path = info.get('Power', {}).get('@odata.id') or path + '/Power'
            identity = info.get('SerialNumber') or info.get('UUID') or host.name
            self.sources[host] = (power_path, identity)
        path, identity = self.sources[host]
        data = get(path)
        controls = [item for item in data.get('PowerControl', [])
                    if isinstance(item.get('PowerConsumedWatts'), (int, float))
                    and not isinstance(item.get('PowerConsumedWatts'), bool)]
        if len(controls) != 1:
            raise ValueError('No single unambiguous PowerConsumedWatts reading')
        watts = controls[0]['PowerConsumedWatts']
        if not math.isfinite(watts) or watts < 0:
            raise ValueError('Invalid power reading')
        return {'watts': watts, 'identity': identity, 'sampled': sample_started}

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=12)
        self.pool.shutdown(wait=True)
        PROGRESS.update(self.task, description=f'Reported host power: final peak {self.peak:.0f} W')

    def total(self):
        with self.lock:
            samples = dict(self.samples)
            errors = dict(self.errors)
            not_before = self.not_before
        fresh = {host: sample for host, sample in samples.items()
                 if time.monotonic() - sample['sampled'] <= 15 and sample['sampled'] >= not_before and host not in errors}
        unique = {sample['identity']: sample['watts'] for sample in fresh.values()}
        valid = len(fresh) == len(self.hosts) and len(unique) == len(self.hosts)
        return sum(unique.values()), valid, len(fresh)

    def run(self):
        while not self.stop_event.is_set():
            futures = {self.pool.submit(self.read, host): host for host in self.hosts
                       if time.monotonic() >= self.retry_at.get(host, 0)}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    sample = future.result()
                    with self.lock:
                        self.samples[host] = sample
                        self.errors.pop(host, None)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    delay = telemetry_retry_delay(exc)
                    message = (f'temporarily unavailable; retrying in {delay} seconds'
                               if delay is not None else error_detail(exc))
                    if delay is not None:
                        self.retry_at[host] = time.monotonic() + delay
                    with self.lock:
                        previous = self.errors.get(host)
                        self.errors[host] = message
                    if previous != message:
                        CONSOLE.print(f'{host.name}: power telemetry unavailable: {message}', markup=False)
            watts, valid, count = self.total()
            if valid:
                self.peak = max(self.peak, watts)
            label = (f'Reported host draw: {watts:.0f} W · peak {self.peak:.0f} W' if valid else
                     ('Power source scope ambiguous / duplicated; throttled starts blocked' if count == len(self.hosts) else
                      f'Reported host draw incomplete: {count}/{len(self.hosts)} fresh readings'))
            PROGRESS.update(self.task, description=label, completed=watts if valid else 0)
            self.stop_event.wait(5)

    def wait_headroom(self, failed):
        stable_since = None
        deadline = time.monotonic() + self.args.power_wait_timeout
        threshold = self.args.power_budget - self.args.power_reserve
        while not failed.is_set():
            watts, valid, _ = self.total()
            now = time.monotonic()
            if valid and watts <= threshold:
                stable_since = now if stable_since is None else stable_since
                elapsed = int(now - stable_since)
                PROGRESS.update(ui.OPERATION_TASK, description=f'Power headroom: {watts:.0f} W ≤ {threshold:.0f} W; settling (seconds)',
                                completed=min(self.args.power_settle, elapsed), total=self.args.power_settle)
                if now - stable_since >= self.args.power_settle:
                    return
            else:
                stable_since = None
                PROGRESS.update(ui.OPERATION_TASK, description='Waiting for fresh power readings / startup headroom',
                                completed=0, total=self.args.power_settle)
            if now >= deadline:
                with self.lock:
                    details = '; '.join(f'{host.name}: {message}' for host, message in self.errors.items())
                raise ValueError('No verified power headroom before timeout. ' + details)
            time.sleep(1)
        raise ValueError('New starts stopped because another host failed')


def stagger_reboots(hosts, scheduled, ready, args, records, power=None, schedule_host=None, confirm_reboot=False):
    targets = [host for host in hosts if host in scheduled]
    PROGRESS.update(ui.OVERALL_TASK, visible=True, description='Overall hosts applied / restored',
                    completed=0, total=len(targets))
    lock = Lock()
    failed = Event()
    tasks = {host: PROGRESS.add_task(f'{host.name}: awaiting start', total=len(scheduled[host]['jobs']))
             for host in targets}

    def record_update(host, **fields):
        with lock:
            records[host.name].update(fields)
            save(args.reboot_state, records)

    def monitor(host):
        entry = scheduled[host]
        original = records[host.name]['original_power']
        completed = False
        try:
            group = JobGroup(Job(host, path) for path in entry['jobs'])
            def progress(current):
                value, total, unit = current.progress
                PROGRESS.update(tasks[host], completed=value, total=total, unit=unit,
                                description=f'{host.name}: {current.state_text}' +
                                (' (average job progress)' if unit == 'percent' and len(current.jobs) > 1 else ''))
            try:
                group.wait(args.job_timeout, clock=time, on_progress=progress,
                           timeout_message=f'Configuration jobs did not complete within {args.job_timeout}s')
            except JobFailure as exc:
                completed = True
                raise ValueError('Configuration job failed: ' + str(exc)) from exc
            completed = True
            PROGRESS.update(tasks[host], description=f'{host.name}: restoring power / verifying')
            if records[host.name].get('triggered', True):
                restore_power(host, original)
            mismatches = []
            for path, (_, changes) in ready[host].items():
                data = host.read_setting(path)
                for key, wanted in changes.items():
                    actual = data.get('Attributes', {}).get(key)
                    if not registry_equal(host.resources[path].get('metadata', {}).get(key, {}), actual, wanted) or key in host.resources[path].get('pending', {}):
                        mismatches.append(f'{key}: wanted {wanted!r}, active {actual!r}')
            if mismatches:
                raise ValueError('Active verification failed: ' + '; '.join(mismatches))
            record_update(host, status='verified-complete')
            CONSOLE.print(f'{host.name}: settings verified active' +
                          (f'; power restored to {original}' if records[host.name].get('triggered', True) else '; no host power action needed'), markup=False)
            PROGRESS.update(tasks[host], description=f'{host.name}: verified / {original}', completed=100, total=100, unit='percent')
            PROGRESS.advance(ui.OVERALL_TASK)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            args.failed = True
            record_update(host, status='failed', error=error_detail(exc))
            if completed and records[host.name].get('triggered', True):
                try:
                    restore_power(host, original)
                except (httpx.HTTPError, ValueError, KeyError) as restore_error:
                    record_update(host, restore_error=error_detail(restore_error))
            PROGRESS.update(tasks[host], description=f'{host.name}: failed; inspect state file')
            CONSOLE.print(Panel(Text(error_detail(exc)), title=Text(host.name), border_style='red'))
            CONSOLE.print(f'{host.name}: failure recorded; other hosts will continue.', markup=False)
        finally:
            host.read_retry_seconds = 0

    last_trigger = None
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futures = []
        for host in targets:
            if failed.is_set():
                break
            host.read_retry_seconds = args.unavailable_grace
            try:
                job_scheduled = False
                if schedule_host is not None and not scheduled[host]['needs_reboot']:
                    scheduled[host] = schedule_host(host)
                    record_update(host, jobs=scheduled[host]['jobs'], status='scheduled')
                    job_scheduled = True
                if scheduled[host]['needs_reboot']:
                    if power is not None and args.power_throttle:
                        power.wait_headroom(failed)
                    elif last_trigger is not None:
                        deadline = last_trigger + 60
                        PROGRESS.update(ui.OPERATION_TASK, description='Next host start countdown (seconds)', total=60, completed=0)
                        while time.monotonic() < deadline and not failed.is_set():
                            PROGRESS.update(ui.OPERATION_TASK, completed=min(60, int(time.monotonic() - last_trigger)))
                            time.sleep(min(1, max(0, deadline - time.monotonic())))
                    if failed.is_set():
                        break
                    if schedule_host is not None and not job_scheduled:
                        scheduled[host] = schedule_host(host)
                        record_update(host, jobs=scheduled[host]['jobs'], status='scheduled')
                        PROGRESS.update(tasks[host], total=len(scheduled[host]['jobs']))
                        if not scheduled[host]['needs_reboot']:
                            futures.append(pool.submit(monitor, host))
                            continue
                    if confirm_reboot:
                        CONSOLE.print(f'{host.name}: scheduled jobs need a host start/reset: ' +
                                      ', '.join(scheduled[host].get('reboot_types', ['Unknown job type'])), markup=False)
                        if ask(f'Type REBOOT to start/reset {host.name}; blank=leave scheduled', default='') != 'REBOOT':
                            record_update(host, status='scheduled-awaiting-user-reboot')
                            PROGRESS.update(tasks[host], description=f'{host.name}: left scheduled; power unchanged')
                            continue
                    current = host.request('GET', SYSTEM_PATH).json().get('PowerState')
                    original = records[host.name]['original_power']
                    if current != original:
                        raise ValueError(f'Power changed since preview ({current}); no reboot sent')
                    reset = 'On' if current == 'Off' else 'GracefulRestart'
                    PROGRESS.update(ui.OPERATION_TASK, description=f'{host.name}: {reset}', total=1, completed=0)
                    record_update(host, status='power-action-about-to-send')
                    power_action(host, reset)
                    last_trigger = time.monotonic()
                    if power is not None and args.power_throttle:
                        # Do not reuse low readings from before the latest startup.
                        with power.lock:
                            power.samples.clear()
                            power.not_before = last_trigger
                    record_update(host, triggered=True, status='waiting-for-jobs')
                    CONSOLE.print(f'{host.name}: {reset} requested', markup=False)
                futures.append(pool.submit(monitor, host))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                failed.set()
                args.failed = True
                record_update(host, status='failed', error=error_detail(exc))
                CONSOLE.print(Panel(Text(error_detail(exc)), title=Text(host.name), border_style='red'))
                break
        PROGRESS.update(ui.OPERATION_TASK, description='Monitoring started hosts', total=len(futures), completed=0)
        for future in as_completed(futures):
            future.result()
            PROGRESS.advance(ui.OPERATION_TASK)
    if failed.is_set():
        CONSOLE.print('Further host starts stopped; started hosts were monitored through completion/failure. '
                      'Check the reboot-state file for unresolved application or power state.', markup=False)


def change_power_state(hosts, args):
    available = [host for host in hosts if host.session_path and not host.blocked]
    states = parallel(available, 'Reading power states',
                      lambda host: host.request('GET', SYSTEM_PATH).json().get('PowerState'))
    table = Table('Host', 'Power state', expand=True)
    for host in hosts:
        table.add_row(host.name, str(states.get(host, 'Unavailable')))
    CONSOLE.print(table)
    if args.read_only:
        return
    desired = ask('Fleet power state: On, Off, or q=quit', choices=['On', 'Off', 'q'], default='q')
    if desired == 'q':
        return
    targets = [host for host, state in states.items() if state in ('On', 'Off') and state != desired]
    if not targets:
        CONSOLE.print('No power changes needed.')
        return
    CONSOLE.print('Affected hosts: ' + ', '.join(host.name for host in targets), markup=False)
    if ask(f'Type {desired.upper()} to change these hosts to {desired}', default='') != desired.upper():
        return
    PROGRESS.update(ui.OVERALL_TASK, visible=True, description=f'Hosts verified {desired}', total=len(targets), completed=0)
    stopped = Event()
    meter = PowerMonitor(available, args) if desired == 'On' and not args.no_power_monitor else None
    tasks = {host: PROGRESS.add_task(f'{host.name}: awaiting power change', total=1) for host in targets}

    def verify(host):
        try:
            wait_power(host, desired, timeout=args.job_timeout)
            PROGRESS.update(tasks[host], description=f'{host.name}: {desired} verified', completed=1)
            PROGRESS.advance(ui.OVERALL_TASK)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            args.failed = True
            PROGRESS.update(tasks[host], description=f'{host.name}: verification failed')
            CONSOLE.print(Panel(Text(error_detail(exc)), title=Text(host.name), border_style='red'))
        finally:
            host.read_retry_seconds = 0

    if meter:
        meter.start()
    last_start = None
    try:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = []
            for host in targets:
                try:
                    if desired == 'On':
                        if meter and args.power_throttle:
                            meter.wait_headroom(stopped)
                        elif last_start is not None:
                            PROGRESS.update(ui.OPERATION_TASK, description='Next host start countdown (seconds)', total=60, completed=0)
                            while time.monotonic() - last_start < 60:
                                PROGRESS.update(ui.OPERATION_TASK, completed=min(60, int(time.monotonic() - last_start)))
                                time.sleep(1)
                    host.read_retry_seconds = args.unavailable_grace
                    current = host.request('GET', SYSTEM_PATH).json().get('PowerState')
                    if current == desired:
                        futures.append(pool.submit(verify, host))
                        continue
                    if current != states[host]:
                        raise ValueError(f'Power changed since preview ({current}); no power command sent')
                    power_action(host, 'On' if desired == 'On' else 'GracefulShutdown')
                    PROGRESS.update(tasks[host], description=f'{host.name}: waiting for {desired}')
                    if desired == 'On':
                        last_start = time.monotonic()
                        if meter and args.power_throttle:
                            with meter.lock:
                                meter.samples.clear()
                                meter.not_before = last_start
                    futures.append(pool.submit(verify, host))
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    args.failed = True
                    host.read_retry_seconds = 0
                    CONSOLE.print(Panel(Text(error_detail(exc)), title=Text(host.name), border_style='red'))
                    CONSOLE.print('Further power commands stopped; monitoring hosts already triggered.')
                    break
            for future in as_completed(futures):
                future.result()
    finally:
        if meter:
            meter.stop()
