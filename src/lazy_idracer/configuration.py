"""Interactive configuration and pending-job application workflows."""
import json
import re
import time
from urllib.parse import urlsplit
import httpx
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from . import ui
from .ui import CONSOLE, PROGRESS, ask, parallel, error_detail
from .constants import DELL, SYSTEM_PATH, SELECTIONS_PATH
from .jobs import Job, normalize_job
from .server import DellServer, registry_value, registry_equal
from .power import PowerMonitor, stagger_reboots
from .storage import save, snapshot


class Configurer:
    """Keep selections and undo history for one authenticated fleet session."""
    def __init__(self, servers, options):
        self.servers = servers
        self.options = options
        self.selections = []
        self.undo_history = []
        self.options.undo_history = self.undo_history

    def run(self):
        if self.options.setting is not None:
            setting_loop(self.servers, self.options, self.selections)
        else:
            PROGRESS.update(ui.OVERALL_TASK, visible=True)
            iterate(self.servers, self.options, self.selections)
            if self.options.discover_pending_reboot:
                apply_discovered_pending(self.servers, self.options)


def parse_selection(answer, choices):
    if answer.startswith('#'):
        try:
            index = int(answer[1:])
        except ValueError:
            raise ValueError('Use # followed by a choice number') from None
        if not 1 <= index <= len(choices):
            raise ValueError(f'Choice must be between #1 and #{len(choices)}')
        return choices[index - 1]
    # Strings can be entered directly; JSON quotes allow literal q or # prefixes.
    if all(isinstance(value, str) for value in choices):
        return json.loads(answer) if answer.startswith('"') else answer
    try:
        return json.loads(answer)
    except ValueError:
        raise ValueError('Enter a JSON value of the displayed type, or # followed by a choice number') from None


def show_attribute(hosts, present, pending, choices, path, key, fresh=None):
    CONSOLE.rule(Text(key, style='bold cyan'))
    CONSOLE.print(Text(f'{path} :: {key}', style='dim'))
    descriptions = {}
    for host, _ in present:
        entry = host.resources[path].get('metadata', {}).get(key, {})
        signature = json.dumps({field: entry.get(field) for field in
            ('DisplayName', 'HelpText', 'Description', 'WarningText', 'Value',
             'LowerBound', 'UpperBound', 'MinLength', 'MaxLength')}, sort_keys=True)
        descriptions.setdefault(signature, []).append(host.name)
    for signature, names in descriptions.items():
        entry = json.loads(signature)
        blocks = []
        for field in ('HelpText', 'Description'):
            if entry.get(field):
                blocks.append(Markdown(re.sub(r'[ \t]+', ' ', str(entry[field])).strip()))
        if entry.get('WarningText'):
            blocks.append(Text('Warning: ' + str(entry['WarningText']), style='yellow'))
        if not blocks:
            blocks.append(Text('No description supplied by firmware.', style='dim'))
        limits = [f'{field}: {entry[field]}' for field in
                  ('LowerBound', 'UpperBound', 'MinLength', 'MaxLength') if entry.get(field) is not None]
        if limits:
            blocks.append(Text(' · '.join(limits), style='dim'))
        allowed = Table('Registry value (JSON)', 'Display label', 'Meaning', box=None,
                        expand=True, show_header=True, header_style='bold')
        for option in entry.get('Value') or []:
            if isinstance(option, dict):
                allowed.add_row(Text(json.dumps(option.get('ValueName', ''))),
                                Text(str(option.get('ValueDisplayName', ''))),
                                Text(str(option.get('ValueHelpText', ''))))
        if allowed.row_count:
            blocks.append(allowed)
        title = Text(str(entry.get('DisplayName') or key), style='bold')
        if len(descriptions) > 1:
            CONSOLE.print(Text('Registry description for: ' + ', '.join(name
                                                                       for name in names), style='dim'))
        CONSOLE.print(Panel(Group(*blocks), title=title, border_style='blue', expand=True))
    options = Table('Choice', 'Observed API value (JSON)', 'Source', box=None, header_style='bold')
    for index, value in enumerate(choices, 1):
        sources = []
        if any(type(value) is type(observed) and value == observed for _, observed in present):
            sources.append('active')
        if any(type(value) is type(observed) and value == observed for _, observed in pending):
            sources.append('pending')
        options.add_row(Text(f'#{index}', style='cyan'), Text(json.dumps(value)), Text(' + '.join(sources)))
    CONSOLE.print(options)
    CONSOLE.print(Text('Tables show raw JSON. Unique registry display labels map to registry values on write; mappings are printed explicitly.', style='dim'))
    states = Table('Host', 'Raw active → raw pending (JSON)', 'Status', box=None, expand=True, header_style='bold')
    current_by_host = dict(present)
    pending_by_host = dict(pending)
    for host in hosts:
        status = ''
        if host in current_by_host:
            allowed_values = [option['ValueName'] for option in host.resources[path].get('metadata', {}).get(key, {}).get('Value', [])
                              if isinstance(option, dict) and 'ValueName' in option]
            if allowed_values and not any(type(current_by_host[host]) is type(value) and current_by_host[host] == value
                                          for value in allowed_values):
                status = 'Active API value differs from registry encoding'
        if host in current_by_host and host.exclusion_reason(path, key) == 'Registry marks read-only':
            status = (status + '; ' if status else '') + host.explain_restriction(path, key, 'Registry marks read-only') + '; will attempt selected value'
        if host not in current_by_host:
            reason = host.exclusion_reason(path, key)
            if reason is None:
                reason = 'Active/pending read failed' if fresh is not None and host not in fresh else 'Attribute unavailable after refresh'
            status = host.explain_restriction(path, key, reason)
            cached = host.resources.get(path, {}).get('values', {})
            state = Text(json.dumps(cached[key]) if key in cached else '—', style='dim')
            if fresh is not None and host not in fresh and reason == 'Active/pending read failed':
                status += ' (shown value is cached)'
        elif host in pending_by_host:
            state = Text(json.dumps(current_by_host[host]), style='red')
            state.append(' → ', style='dim')
            state.append(json.dumps(pending_by_host[host]), style='green')
            state.append(' (pending)', style='green')
        else:
            state = Text(json.dumps(current_by_host[host]))
        states.add_row(Text(host.name), state, Text(status, style='yellow' if status else ''))
    CONSOLE.print(states)


def show_write_results(path, key, desired, observed, results, read_only=False):
    table = Table('Host', 'Before (JSON)', 'Requested → wire (JSON)', 'Active → pending (JSON)',
                  'Result', box=None, expand=True, title=Text(key))
    for host, requested in desired.items():
        resource = host.resources[path]
        metadata = resource.get('metadata', {}).get(key, {})
        wire = registry_value(metadata, requested)
        target = Text(json.dumps(requested, ensure_ascii=False))
        if type(wire) is not type(requested) or wire != requested:
            target.append(' → ' + json.dumps(wire, ensure_ascii=False), style='cyan')
        active = resource['values'].get(key)
        current = Text(json.dumps(active, ensure_ascii=False))
        if key in resource.get('pending', {}):
            current.stylize('red')
            current.append(' → ' + json.dumps(resource['pending'][key], ensure_ascii=False), style='green')
        outcome = 'Read-only; no write' if read_only else results.get(host, 'Failed; see error above')
        table.add_row(Text(host.name), Text(json.dumps(observed[host], ensure_ascii=False)), target,
                      current, Text(outcome, style='green' if host in results else 'dim' if read_only else 'red'))
    CONSOLE.print(table)


def apply_with_history(args, path, key, desired, observed, description):
    previous = {host: host.resources[path].get('pending', {}).get(key, observed[host]) for host in observed}
    results = parallel(list(observed), description,
                       lambda host: host.apply(path, key, desired[host], observed[host]))
    history = [{'host': host, 'path': path, 'key': key, 'before': previous[host], 'after': desired[host]}
               for host in results if not results[host].startswith('already') and not registry_equal(
                   host.resources[path].get('metadata', {}).get(key, {}), previous[host], desired[host])]
    if history:
        if not hasattr(args, 'undo_history'):
            args.undo_history = []
        args.undo_history.append(history)
    show_write_results(path, key, desired, observed, results)
    return results


def undo_last(args, selections):
    history = getattr(args, 'undo_history', [])
    if not history:
        CONSOLE.print('No successful operation to undo in this session.')
        return
    if args.read_only:
        CONSOLE.print('Read-only: undo cannot write.')
        return
    operation = history[-1]
    entries = {entry['host']: entry for entry in operation}

    def restore(host):
        entry = entries[host]
        path, key = entry['path'], entry['key']
        data = host.read_setting(path)
        active = data['Attributes'][key]
        effective = host.resources[path].get('pending', {}).get(key, active)
        metadata = host.resources[path].get('metadata', {}).get(key, {})
        if not registry_equal(metadata, effective, entry['after']):
            raise ValueError(f'{key} changed since the last operation; undo refused')
        return host.apply(path, key, entry['before'], active)

    results = parallel(list(entries), 'Undoing last operation and reading back', restore)
    remaining = []
    for entry in operation:
        host = entry['host']
        if host in results:
            selections.append({'resource': entry['path'], 'attribute': entry['key'],
                               'value': entry['before'], 'hosts': [host.name], 'action': 'undo'})
        else:
            remaining.append(entry)
    if remaining:
        history[-1] = remaining
        args.failed = True
    else:
        history.pop()
    first = operation[0]
    show_write_results(first['path'], first['key'],
                       {entry['host']: entry['before'] for entry in operation},
                       {entry['host']: entry['after'] for entry in operation}, results)
    save(SELECTIONS_PATH, selections)


def normalize_command(answer):
    # s/S intentionally differ: prefix versus entire resource.
    return answer.lower() if len(answer) == 1 and answer.lower() in 'qiudpkcr' else answer


def compare(hosts, args, path, selections):
    keys = sorted({key for host in hosts if path in host.resources
                   for key in host.resources[path]['values']
                   if host.writable(path, key, allow_readonly=bool(getattr(args, 'search', None)))})
    start_field = getattr(args, 'start_field', '')
    if start_field and path == getattr(args, 'start_from', None):
        if start_field not in keys:
            args.failed = True
            CONSOLE.print(f'Start field {start_field} is absent or ineligible in {path}; skipping this resource.', markup=False)
            return True
        keys = keys[keys.index(start_field):]
    skipped_groups = set()
    prompt_positions = []
    cursor = 0
    force_position = None
    while cursor < len(keys):
        force_prompt = cursor == force_position
        force_position = None
        key = keys[cursor]
        cursor += 1
        if getattr(args, 'setting', None) and key != args.setting[1]:
            continue
        group = key.rsplit('.', 1)[0] + '.' if '.' in key else re.sub(r'\d+$', '', key)
        if any(key.startswith(prefix) for prefix in skipped_groups):
            continue
        label = f'{path} :: {key}'
        if getattr(args, 'search', None) and args.search.lower() not in key.lower():
            continue
        if args.filter and args.filter.lower() not in label.lower():
            continue
        eligible = [host for host in hosts if not host.blocked and path in host.resources
                    and key in host.resources[path]['values'] and host.writable(path, key, allow_readonly=True)]
        # Resource discovery already read the group. Refresh only candidates
        # that can produce a prompt, instead of fetching every matching field.
        cached = [host.resources[path]['values'][key] for host in eligible]
        has_pending = any(key in host.resources[path].get('pending', {}) for host in eligible)
        if has_pending and args.skip_pending:
            continue
        if (not force_prompt and not args.all and not has_pending and len(eligible) == len(hosts) and cached
                and all(type(value) is type(cached[0]) and value == cached[0] for value in cached)):
            continue
        fresh = parallel(eligible, f'Reading active/pending {key}', lambda host: host.read_setting(path))
        snapshot(hosts, args)
        present = [(host, fresh[host]['Attributes'][key]) for host in hosts if host in fresh
                   and key in fresh[host].get('Attributes', {}) and host.writable(path, key, allow_readonly=True)]
        if not present:
            continue
        pending = [(host, host.resources[path]['pending'][key]) for host, _ in present
                   if key in host.resources[path].get('pending', {})]
        if pending and args.skip_pending:
            CONSOLE.print(f'Skipping {key}: pending on {", ".join(h.name for h, _ in pending)}', markup=False)
            continue
        choices = []
        for _, value in present + pending:
            if not any(type(value) is type(v) and value == v for v in choices):
                choices.append(value)
        if not force_prompt and not args.all and not pending and len(choices) < 2 and len(present) == len(hosts):
            continue
        show_attribute(hosts, present, pending, choices, path, key, fresh=fresh)
        prompt_positions.append(cursor - 1)
        PROGRESS.update(ui.OPERATION_TASK, description='Waiting for your choice')
        answer = ask('Enter value or #choice; blank=skip, s=skip prefix, '
                     'S=skip resource, i=Individual, u=Undo, d=Clear, p=Previous, k=skip all pending, c=cancel pending, q=quit', default='', show_default=False).strip()
        answer = normalize_command(answer)
        while answer == 'u':
            undo_last(args, selections)
            fresh = parallel(eligible, f'Refreshing {key}', lambda host: host.read_setting(path))
            present = [(host, fresh[host]['Attributes'][key]) for host in eligible if host in fresh
                       and key in fresh[host].get('Attributes', {})]
            pending = [(host, host.resources[path]['pending'][key]) for host, _ in present
                       if key in host.resources[path].get('pending', {})]
            choices = []
            for _, value in present + pending:
                if not any(type(value) is type(v) and value == v for v in choices):
                    choices.append(value)
            show_attribute(hosts, present, pending, choices, path, key, fresh=fresh)
            snapshot(hosts, args)
            answer = ask('Enter value or #choice; blank=skip, s=skip prefix, S=skip resource, '
                         'i=Individual, u=Undo, d=Clear, p=Previous, k=skip all pending, c=cancel pending, q=quit',
                         default='', show_default=False).strip()
            answer = normalize_command(answer)
        if answer == 'q':
            return False
        if answer in ('S', 'r'):
            return True
        if answer == 'p':
            current_position = prompt_positions.pop()
            cursor = prompt_positions.pop() if prompt_positions else current_position
            force_position = cursor
            if cursor == current_position:
                CONSOLE.print('Already at the first attribute prompt in this resource.')
            continue
        if answer == 'i':
            for host, old in present:
                while True:
                    individual = ask(f'{host.name}: {key} (Enter=current value, q=quit)',
                                     default=json.dumps(old, ensure_ascii=False)).strip()
                    if individual.lower() == 'q':
                        return False
                    try:
                        value = parse_selection(individual, [old])
                        if type(value) is not type(old):
                            raise ValueError('Value type differs from current value')
                    except (ValueError, IndexError) as exc:
                        CONSOLE.print(f'Invalid value: {exc}', markup=False)
                        continue
                    break
                pending_value = host.resources[path].get('pending', {})
                if value == old and (key not in pending_value or pending_value[key] == value):
                    CONSOLE.print(f'{host.name}: already matches; no write needed', markup=False)
                    continue
                selections.append({'resource': path, 'attribute': key, 'value': value, 'hosts': [host.name]})
                save(SELECTIONS_PATH, selections)
                if args.read_only:
                    show_write_results(path, key, {host: value}, {host: old}, {}, read_only=True)
                    continue
                results = apply_with_history(args, path, key, {host: value}, {host: old},
                                             f'Applying {key} on {host.name} and reading back')
                if host not in results:
                    args.failed = True
                snapshot(hosts, args)
            continue
        if answer == 'c':
            if not pending:
                CONSOLE.print('No pending change for this attribute.')
                continue
            active = dict(present)
            targets = [host for host, _ in pending]
            for host in targets:
                selections.append({'resource': path, 'attribute': key, 'value': active[host],
                                   'hosts': [host.name], 'action': 'cancel-pending'})
            save(SELECTIONS_PATH, selections)
            if args.read_only:
                show_write_results(path, key, {host: active[host] for host in targets},
                                   {host: active[host] for host in targets}, {}, read_only=True)
                continue
            results = apply_with_history(args, path, key, {host: active[host] for host in targets},
                                         {host: active[host] for host in targets}, f'Cancelling pending {key}')
            if len(results) != len(targets):
                args.failed = True
            parallel(targets, f'Refreshing {key} after cancellation', lambda host: host.read_setting(path))
            snapshot(hosts, args)
            continue
        if answer == 'k':
            args.skip_pending = True
            CONSOLE.print('Skipping this attribute and all subsequent attributes with pending changes.')
            continue
        if answer == 's':
            prefix = ask(f'Prefix to skip in this resource (example: {group}); blank=skip only this attribute',
                         default='', show_default=False).strip()
            if prefix:
                skipped_groups.add(prefix)
                CONSOLE.print(f'Skipping remaining {prefix}* attributes in this resource.', markup=False)
            continue
        if not answer:
            continue
        clearing = answer == 'd'
        if clearing:
            answer = ask('Clear value: enter "" for empty string or null for JSON null; q=cancel',
                         default='""' if all(isinstance(old, str) for _, old in present) else 'q').strip()
            if answer.lower() == 'q':
                continue
            if answer not in ('""', 'null'):
                CONSOLE.print('Clear accepts only "" or null; no write made.')
                continue
            if ask(f'Type CLEAR to set {key} to {answer} on all eligible hosts', default='') != 'CLEAR':
                continue
        try:
            value = json.loads(answer) if clearing else parse_selection(answer, choices)
            if not (clearing and value is None) and any(type(value) is not type(old) for _, old in present):
                raise ValueError('Value type differs between hosts or from selection')
        except (ValueError, IndexError) as exc:
            CONSOLE.print(f'Skipped: {exc}', markup=False)
            continue
        selection = {'resource': path, 'attribute': key, 'value': value,
                     'hosts': [h.name for h, _ in present]}
        selections.append(selection)
        save(SELECTIONS_PATH, selections)
        changes = [(h, old) for h, old in present if old != value or
                   (key in h.resources[path].get('pending', {}) and h.resources[path]['pending'][key] != value)]
        if not changes:
            continue
        if args.read_only:
            show_write_results(path, key, {host: value for host, _ in changes}, dict(changes), {}, read_only=True)
            continue
        old_values = dict(changes)
        results = apply_with_history(args, path, key, {host: value for host in old_values},
                                     old_values, f'Applying {key} and reading back')
        if len(results) != len(changes):
            args.failed = True
        snapshot(hosts, args)
    return True


def iterate(hosts, args, selections=None):
    queue = ['/redfish/v1/Systems', '/redfish/v1/Managers']
    queue += [DELL + name for name in ('iDRAC.Embedded.1', 'LifecycleController.Embedded.1', 'System.Embedded.1')]
    seen = set()
    selections = [] if selections is None else selections
    start_from = getattr(args, 'start_from', None)
    prompting = start_from is None
    save(SELECTIONS_PATH, selections)
    CONSOLE.print('Read-only: selections saved without writes.' if args.read_only else
                  'Values apply immediately on Enter, with read-back verification.')
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        PROGRESS.update(ui.OVERALL_TASK, description='Overall resources discovered',
                        total=len(seen | set(queue)), completed=len(seen) - 1)
        active = [host for host in hosts if host.session_path and not host.blocked]
        data_by_host = parallel(active, f'Discovering {path.rsplit("/", 1)[-1]}',
                                lambda host: host.read_resource(path))
        for data in data_by_host.values():
            for key in ('Members', 'Bios', 'Attributes', 'DellAttributes',
                        'Storage', 'Controllers', 'NetworkAdapters', 'NetworkDeviceFunctions'):
                DellServer.links(data.get(key), queue)
            dell = data.get('Oem', {}).get('Dell', {})
            for key in ('DellAttributes', 'Attributes'):
                DellServer.links(dell.get(key), queue)
        PROGRESS.update(ui.OVERALL_TASK, total=len(seen | set(queue)), completed=len(seen))
        snapshot(hosts, args)
        if not prompting and path == start_from and data_by_host:
            prompting = True
            CONSOLE.print(f'Starting prompts at {path}', markup=False)
        if prompting and not args.discover_pending_reboot and not compare(hosts, args, path, selections):
            return
    if not prompting:
        args.failed = True
        CONSOLE.print(f'Start category was not successfully discovered: {start_from}; no attribute prompts were shown.', markup=False)
        return
    eligible = sum(host.writable(path, key) for host in hosts for path, resource in host.resources.items()
                   for key in resource['values'])
    if not eligible:
        CONSOLE.print('No writable attributes found. See registry errors in the snapshot.', markup=False)
    elif not args.discover_pending_reboot:
        CONSOLE.print('Finished comparing discovered writable attributes.', markup=False)


def existing_configuration_job(host, target):
    collection = '/redfish/v1/Managers/iDRAC.Embedded.1/Jobs'
    matches = []
    seen = set()
    while collection and collection not in seen:
        seen.add(collection)
        data = host.request('GET', collection).json()
        for member in data.get('Members', []):
            path = member['@odata.id']
            job = normalize_job(host.request('GET', path).json())
            if job.get('JobState') not in ('New', 'Scheduled', 'Running', 'Starting', 'Pending'):
                continue
            uri = job.get('TargetSettingsURI')
            # Some Dell firmware omits the target on BIOS jobs.
            bios = (not uri and target == SYSTEM_PATH + '/Bios/Settings'
                    and job.get('JobType') == 'BIOSConfiguration')
            if uri == target or bios:
                matches.append((path, job))
        collection = data.get('Members@odata.nextLink')
    if len(matches) > 1:
        raise ValueError(f'Multiple active jobs for {target}; inspect job queue before restarting')
    return matches[0] if matches else None


def show_pending_diff(host, entries):
    blocks = []
    for path, (target, changes) in entries.items():
        blocks.append(Text(path, style='bold cyan'))
        blocks.append(Text(f'Apply target: {target}', style='dim'))
        table = Table('Attribute', 'From: active (JSON)', 'To: requested (JSON)', 'Source',
                      box=None, expand=True, header_style='bold')
        resource = host.resources.get(path, {})
        active = resource.get('values', {})
        pending = resource.get('pending', {})
        for key, desired in sorted(changes.items()):
            changed = key not in active or active[key] != desired
            before = json.dumps(active[key], ensure_ascii=False) if key in active else 'Unavailable'
            source = 'Pending settings' if key in pending else 'Saved committed-job selection'
            table.add_row(Text(key), Text(before, style='red' if changed else 'dim'),
                          Text(json.dumps(desired, ensure_ascii=False), style='green' if changed else 'dim'),
                          Text(source if changed else source + ' (already active)', style='dim'))
        blocks.append(table)
    CONSOLE.print(Panel(Group(*blocks), title=Text(host.name), border_style='blue', expand=True))


def apply_discovered_pending(hosts, args):
    saved_records = {}
    if args.reboot_state.exists():
        try:
            saved_records = json.loads(args.reboot_state.read_text())
        except (ValueError, OSError):
            CONSOLE.print('Previous reboot state could not be read; committed-job recovery unavailable.')
    if any(host.errors for host in hosts):
        CONSOLE.print('Discovery has gaps; this preview includes only successfully read pending settings. '
                      'See snapshot errors.', markup=False)
    def inspect(host):
        paths = list(host.resources)
        entries = {}
        targets = set()
        for path in paths:
            data = host.read_resource(path)
            target = data.get('@Redfish.Settings', {}).get('SettingsObject', {}).get('@odata.id')
            if not target:
                continue
            if target in targets:
                continue
            targets.add(target)
            pending = host.request('GET', target).json().get('Attributes', {})
            current = data.get('Attributes', {})
            changes = {k: v for k, v in pending.items() if k in current and not registry_equal(
                host.resources[path].get('metadata', {}).get(k, {}), v, current[k])}
            if changes:
                entries[path] = (target, changes)
            else:
                saved = saved_records.get(host.name, {}).get('resources', {}).get(path)
                if saved and saved[0] == target:
                    expected = saved[1]
                    unresolved = {key: value for key, value in expected.items()
                                  if key in current and not registry_equal(
                                      host.resources[path].get('metadata', {}).get(key, {}), current[key], value)}
                    if unresolved and existing_configuration_job(host, target):
                        entries[path] = (target, expected)
        return entries

    inspected = parallel([h for h in hosts if h.session_path and not h.blocked], 'Inspecting pending settings', inspect)
    args.failed = args.failed or len(inspected) != len(hosts)
    ready = {h: entry for h, entry in inspected.items() if entry}
    for host in hosts:
        if host in ready:
            show_pending_diff(host, ready[host])
    if not ready:
        CONSOLE.print('No pending differences or recoverable committed jobs found; no jobs or reboot requested.')
        return
    if args.read_only:
        return
    states = parallel(list(ready), 'Reading original power states',
                      lambda host: host.request('GET', SYSTEM_PATH).json().get('PowerState'))
    if len(states) != len(ready) or any(state not in ('On', 'Off') for state in states.values()):
        args.failed = True
        CONSOLE.print('Cannot establish stable On/Off starting states; no jobs or reboot requested.')
        return
    records = {host.name: {'original_power': states[host], 'triggered': False, 'status': 'preview',
                           'resources': ready[host]} for host in ready}
    for host in ready:
        CONSOLE.print(f'{host.name}: starts {states[host]}; will return to {states[host]} after applying', markup=False)
    if ask('Type APPLY to schedule ALL displayed changes; reboot/start confirmation will be requested only for jobs needing it',
           default='', show_default=False) != 'APPLY':
        return
    save(args.reboot_state, records)
    for host in ready:
        host.read_retry_seconds = args.unavailable_grace

    def schedule(host):
        if inspect(host) != ready[host]:
            raise ValueError('Pending changes changed after preview; no job created')
        jobs = []
        needs_reboot = False
        reboot_types = []
        for target, _ in ready[host].values():
            existing = existing_configuration_job(host, target)
            if existing:
                job, existing_data = existing
                CONSOLE.print(f'{host.name}: reusing {existing_data.get("JobState")} configuration job {job}', markup=False)
            else:
                try:
                    response = host.request('POST', '/redfish/v1/Managers/iDRAC.Embedded.1/Jobs',
                                            json={'TargetSettingsURI': target, 'StartTime': 'TIME_NOW', 'EndTime': 'TIME_NA'})
                    job = response.headers.get('Location')
                except httpx.HTTPStatusError as exc:
                    # Another client may have committed the settings after our lookup.
                    if 'SYS011' not in error_detail(exc):
                        raise
                    existing = existing_configuration_job(host, target)
                    if not existing:
                        raise
                    job, existing_data = existing
                    CONSOLE.print(f'{host.name}: pending settings already committed; reusing job {job}', markup=False)
            if not job:
                raise ValueError('No job Location returned; inspect job queue before retrying')
            data = Job(host, job).wait(180, clock=time, interval=2,
                                       until_states={'Scheduled', 'Completed', 'Running', 'Starting'},
                                       timeout_message=f'Job {job} not scheduled within 180s; no reboot sent')
            state = data['JobState']
            jobs.append(job)
            job_type = data.get('JobType') or data.get('Oem', {}).get('Dell', {}).get('JobType')
            if state == 'Scheduled' and job_type != 'RealTimeNoRebootConfiguration':
                needs_reboot = True
                reboot_types.append(job_type or 'Unknown (firmware did not declare reboot-free application)')
        return {'jobs': jobs, 'needs_reboot': needs_reboot, 'reboot_types': reboot_types}

    # Commit only when this host reaches its turn to start. Running jobs
    # returned by schedule() go directly to monitoring without a reboot.
    results = {host: {'jobs': [], 'needs_reboot': not all(
                host.resources[path].get('supported_apply_times') == ['Immediate']
                for path in ready[host])} for host in ready}
    meter = PowerMonitor(hosts, args) if not args.no_power_monitor else None
    if meter:
        CONSOLE.print('Power display covers reported draw of selected hosts; shared chassis overhead and other circuit loads may be excluded.', markup=False)
        meter.start()
    try:
        stagger_reboots(hosts, results, ready, args, records, power=meter, schedule_host=schedule, confirm_reboot=True)
    finally:
        if meter:
            meter.stop()
    return


def setting_loop(hosts, args, selections=None):
    default_resource = '/redfish/v1/Systems/System.Embedded.1/Bios/Settings'
    selected = args.setting
    selections = [] if selections is None else selections
    args.all = True
    while True:
        if not selected:
            CONSOLE.print(Panel(Text('Example resource: /redfish/v1/Systems/System.Embedded.1/Bios/Settings\n'
                                    'Example attribute: BankXor\nu undoes the last operation; q quits; sessions stay open.'), title='Choose one setting'))
            resource = ask('Resource path', default=default_resource).strip()
            if resource.lower() == 'q':
                return
            if resource == 'u':
                undo_last(args, selections)
                snapshot(hosts, args)
                continue
            attribute = ask('Attribute name (example: BankXor)').strip()
            if attribute.lower() == 'q':
                return
            selected = [resource, attribute]
        resource, key = selected
        parsed = urlsplit(resource)
        if not resource.startswith('/redfish/') or parsed.netloc or parsed.query or parsed.fragment or not key:
            CONSOLE.print('Enter a local /redfish/ resource path and a nonempty attribute name.')
            selected = None
            continue
        default_resource = resource
        path = resource.rstrip('/')
        if path.endswith('/Settings'):
            path = path[:-len('/Settings')]
        args.setting = [path, key]
        available = parallel([host for host in hosts if host.session_path and not host.blocked],
                             f'Reading {key}', lambda host: host.read_setting(path))
        snapshot(hosts, args)
        if any(host.writable(path, key) for host in available):
            if not compare(hosts, args, path, selections):
                return
        else:
            for host in hosts:
                CONSOLE.print(f'{host.name}: {key}: {host.exclusion_reason(path, key) or "Unavailable"}', markup=False)
        selected = None
