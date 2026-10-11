"""Repository discovery and serial firmware updates for idrac_compare."""
import json
import signal
from datetime import datetime, timezone
import time
from threading import Event, Lock, current_thread, main_thread
from types import SimpleNamespace
import httpx
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from urllib.parse import urljoin, urlsplit, unquote
from xml.etree import ElementTree as ET
from .jobs import TERMINAL, job_state, normalize_job
from .jobs import job_progress, Job, JobGroup, JobFailure
from . import ui
from .constants import SYSTEM_PATH
from .power import PowerMonitor, power_action, restore_power
from .storage import save
from .diagnostics import redact, append_log


SERVICE = '/redfish/v1/Systems/System.Embedded.1/Oem/Dell/DellSoftwareInstallationService'
JOBS = '/redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/Jobs'


class UpdateFailure(ValueError):
    """A known terminal firmware failure; other packages may be attempted."""


class _StopRequested(Exception):
    """Leave an input prompt before submitting any work."""


class UpdateStop:
    """Drain accepted jobs on first SIGINT; leave them running on the second."""
    def __init__(self, api, hosts=()):
        self.api = api
        self.requested = Event()
        self.prompting = False
        self.previous_handler = None
        self.force_requested = Event()
        self.hosts = hosts

    def __enter__(self):
        if current_thread() is main_thread():
            self.previous_handler = signal.signal(signal.SIGINT, self.handle)
        return self

    def handle(self, signum, frame):
        first = not self.requested.is_set()
        self.requested.set()
        if not first:
            self.force_requested.set()
            for host in self.hosts:
                host.cancel_reads = self.force_requested
            self.api.CONSOLE.print('Exiting now: updates left running; logging out. '
                                   'Unresolved power restoration remains in the state file.', markup=False)
            raise KeyboardInterrupt()
        self.api.CONSOLE.print('Stop requested: no further updates will be submitted. '
                               'Waiting for accepted jobs to finish, then restoring original power states. '
                               'Press Ctrl+C again to log out and exit without waiting.', markup=False)
        if self.prompting:
            raise _StopRequested()

    def ask(self, *args, **kwargs):
        if self.requested.is_set():
            raise _StopRequested()
        self.prompting = True
        try:
            return self.api.ask(*args, **kwargs)
        finally:
            self.prompting = False

    def __exit__(self, *exc):
        if self.previous_handler is not None:
            signal.signal(signal.SIGINT, self.previous_handler)


def local_path(location, host):
    parsed = urlsplit(location or '')
    if parsed.netloc and parsed.netloc != urlsplit(str(host.client.base_url)).netloc:
        raise ValueError('Update job location points to another host')
    if not parsed.path.startswith('/redfish/') or parsed.query or parsed.fragment:
        raise ValueError('Invalid update job location; inspect queue before retrying')
    return parsed.path


def package_url(repository, package):
    path = package.get('PackagePath', '')
    if not path:
        raise ValueError('Update report has no PackagePath')
    if '..' in unquote(urlsplit(path).path).split('/'):
        raise ValueError('Invalid package path traversal')
    base = urlsplit(repository)
    url = urljoin(repository.rstrip('/') + '/', path)
    parsed = urlsplit(url)
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc or parsed.username or parsed.password:
        raise ValueError('PackagePath points outside the selected repository')
    if parsed.query or parsed.fragment or '..' in unquote(parsed.path).split('/'):
        raise ValueError('Invalid package URL')
    if not parsed.path.startswith(base.path.rstrip('/') + '/'):
        raise ValueError('PackagePath points outside the selected repository directory')
    # BaseLocation can refer to Dell downloads even in a local mirror. Fetch
    # only PackagePath from the explicitly selected local repository.
    return url


def parse_packages(xml):
    if not isinstance(xml, str) or not xml.strip():
        raise ValueError('Missing PackageList; cannot establish that no updates remain')
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('Unexpected declarations in repository XML')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f'Invalid repository PackageList XML: {exc}') from exc
    if root.tag.rsplit('}', 1)[-1] != 'CIM':
        raise ValueError('Unknown PackageList format; refusing to report zero updates')
    if any(node.tag.rsplit('}', 1)[-1] == 'ERROR' for node in root.iter()):
        raise ValueError('Repository XML contains an error, not an empty update list')
    packages = []
    for instance in root.iter():
        if instance.tag.rsplit('}', 1)[-1] != 'INSTANCENAME':
            continue
        if instance.get('CLASSNAME') != 'DCIM_RepoUpdateSWID':
            raise ValueError('Unknown repository report instance type')
        package = {}
        for prop in instance:
            name = prop.get('NAME')
            if not name:
                continue
            values = [(value.text or '').strip() for value in prop.iter()
                      if value.tag.rsplit('}', 1)[-1] == 'VALUE']
            package[name] = values if prop.tag.rsplit('}', 1)[-1] == 'PROPERTY.ARRAY' else (values[0] if values else '')
        if not package.get('PackagePath') or not package.get('PackageVersion'):
            raise ValueError('Incomplete package identity in repository report')
        packages.append(package)
    # A DUP can update multiple matching devices. Submit the package once.
    unique = {}
    for package in packages:
        unique.setdefault(package_id(package), package)
    return list(unique.values())


def package_id(package):
    return package['PackagePath'], package['PackageVersion']


def no_updates(exc, api):
    if not isinstance(exc, api.httpx.HTTPStatusError):
        return False
    try:
        body = exc.response.json()
        entries = body.get('error', {}).get('@Message.ExtendedInfo', [])
        return any(str(entry.get('MessageId', '')).split('.')[-1] == 'SUP029'
                   or entry.get('Message', '').rstrip('.') ==
                   'Firmware versions on server match catalog, applicable updates are not present in the repository'
                   for entry in entries)
    except (ValueError, AttributeError, TypeError):
        return False


def read_jobs(host):
    jobs = {}
    path = JOBS
    seen = set()
    while path and path not in seen:
        seen.add(path)
        data = host.request('GET', path).json()
        for member in data.get('Members', []):
            job_path = member['@odata.id']
            jobs[job_path] = normalize_job(host.request('GET', job_path).json())
        path = data.get('Members@odata.nextLink')
    return jobs


def require_idle(host):
    busy = [path for path, job in read_jobs(host).items() if job_state(job) not in TERMINAL]
    if busy:
        raise ValueError('Existing active/scheduled jobs; complete them before firmware updates: ' + ', '.join(busy))


def offer_existing_jobs(host, jobs, args, api, task, record, start, stop=None, observe=None):
    busy = {path: job for path, job in jobs.items() if job_state(job) not in TERMINAL}
    if not busy:
        return True
    table = api.Table('Job', 'Type', 'State', 'Progress', 'Message', expand=True)
    for path, job in busy.items():
        table.add_row(api.Text(path.rsplit('/', 1)[-1]), api.Text(str(job.get('JobType', 'Unknown'))),
                      api.Text(str(job_state(job))), api.Text(str(job.get('PercentComplete', '?')) + '%'),
                      api.Text(str(job.get('Message', ''))))
    api.CONSOLE.print(api.Panel(table, title=api.Text(host.name + ': existing jobs')))
    unsupported = [path for path, job in busy.items() if job_state(job) not in
                   ('New', 'Scheduled', 'Running', 'Starting', 'Pending')]
    if unsupported:
        api.CONSOLE.print(f'{host.name}: existing jobs are not running or scheduled: ' +
                          ', '.join(f'{path.rsplit("/", 1)[-1]} ({job_state(busy[path])})' for path in unsupported) +
                          '. Downloaded packages need scheduling; automatic resumption is unsupported. '
                          'Skipping this host instead of waiting for an action the script cannot perform.', markup=False)
        return False
    if args.read_only:
        return False
    ask = stop.ask if stop else api.ask
    if ask(f'{host.name}: type APPLY to finish existing jobs (restart if scheduled), or Enter to skip this host', default='') != 'APPLY':
        return False
    group = JobGroup(Job(host, path, data) for path, data in busy.items())
    triggered = False
    original = None
    reboot_types = {'BIOSConfiguration', 'RAIDConfiguration', 'NICConfiguration',
                    'RebootPowerCycle', 'RebootForce', 'RebootNoForce'}

    def progress(current):
        if observe:
            for job in current.jobs:
                observe(host, job.path, job.data)
        value, total, unit = current.progress
        api.PROGRESS.update(task, description=f'{host.name}: existing jobs: {current.state_text} ' +
                            '; '.join(job.message for job in current.jobs if job.message),
                            completed=value, total=total, unit=unit)

    def needs_start(current):
        # A scheduled job must not restart another job that is already flashing.
        return not any(job.state in ('Running', 'Starting') for job in current.jobs) and any(
            job.state == 'Scheduled' and
            (job.data.get('JobType') in reboot_types or job.data.get('RebootRequired') is True)
            for job in current.jobs)

    def apply_existing():
        nonlocal original, triggered
        original = host.request('GET', api.SYSTEM_PATH).json().get('PowerState')
        if original not in ('On', 'Off'):
            raise ValueError('Cannot establish original power state for existing jobs')
        record(host, original_power=original, existing_jobs=list(busy))
        start(host)
        triggered = True

    try:
        group.wait(args.update_timeout, clock=api.time, reader=job_data, on_progress=progress,
                   on_ready=apply_existing, ready_predicate=needs_start,
                   timeout_message='Existing jobs did not finish; no firmware updates submitted')
    except JobFailure as exc:
        api.CONSOLE.print(f'{host.name}: existing jobs failed: {exc}', markup=False)
        args.failed = True
    if triggered:
        api.restore_power(host, original)
        record(host, restore_power_needed=False)
    return True  # Terminal failures no longer block repository discovery.


def action(host, name):
    data = host.request('GET', SERVICE).json()
    for key, value in data.get('Actions', {}).items():
        if key.endswith('.' + name) and isinstance(value, dict) and value.get('target'):
            return value['target']
    raise ValueError(f'iDRAC does not advertise {name}')


def job_data(host, path):
    return normalize_job(host.request('GET', path).json())


def wait_job(host, path, api, task, timeout, on_scheduled=None, observe=None):
    job = Job(host, path)
    def progress(group):
        if observe:
            observe(host, path, job.data)
        percent, total, unit = group.progress
        api.PROGRESS.update(task, description=f'{host.name}: {job.state}: {job.message}',
                            completed=percent, total=total, unit=unit, busy=True)
    try:
        return job.wait(timeout, clock=api.time, reader=job_data, on_progress=progress,
                        on_ready=on_scheduled, timeout_message=f'{path}: update timed out after {timeout}s; '
                        'no further packages will be submitted to this host')
    except JobFailure as exc:
        raise UpdateFailure(str(exc)) from exc


def wait_firmware_job(host, path, api, task, timeout, on_scheduled=None, observe=None):
    result = wait_job(host, path, api, task, timeout, on_scheduled=on_scheduled, observe=observe)
    # Dell can expose the same update through TaskService and its OEM queue.
    # Task completion alone does not establish that the queue has caught up.
    prefix = '/redfish/v1/TaskService/Tasks/'
    if path.startswith(prefix):
        job_id = path[len(prefix):]
        if job_id.startswith('JID_') and '/' not in job_id:
            result = wait_job(host, JOBS + '/' + job_id, api, task, timeout, observe=observe)
    return result


def discover(host, args, api, task, record, observe=None):
    require_idle(host)
    payload = {'IPAddress': urlsplit(args.update_repository).hostname,
               'ShareType': urlsplit(args.update_repository).scheme.upper(),
               'CatalogFile': args.update_catalog, 'ApplyUpdate': 'False', 'RebootNeeded': False}
    # Optional version-selection parameters are unsupported by older iDRACs.
    # Discovery only builds the applicable update list; it installs nothing.
    if urlsplit(args.update_repository).path.strip('/'):
        payload['ShareName'] = urlsplit(args.update_repository).path.strip('/')
    record(host, status='checking-catalog')
    try:
        response = host.request('POST', action(host, 'InstallFromRepository'), json=payload)
        path = local_path(response.headers.get('Location'), host)
        record(host, scan_job=path)
        wait_job(host, path, api, task, args.update_timeout, observe=observe)
        data = host.request('POST', action(host, 'GetRepoBasedUpdateList'), json={}).json()
    except api.httpx.HTTPStatusError as exc:
        if no_updates(exc, api):
            return []
        raise
    return parse_packages(data.get('PackageList'))


def show_packages(host, packages, api):
    table = api.Table('Device', 'Installed', 'Available', 'Reboot type', expand=True)
    for package in packages:
        installed = package.get('ComponentInstalledVersion', [])
        if isinstance(installed, list):
            installed = ', '.join(dict.fromkeys(installed))
        table.add_row(api.Text(package.get('DisplayName', package['PackagePath'])), api.Text(str(installed)),
                      api.Text(package['PackageVersion']), api.Text(package.get('RebootType', 'Unknown')))
    api.CONSOLE.print(api.Panel(table, title=api.Text(host.name), border_style='blue'))


def discover_updates(hosts, args, api, credentials):
    with UpdateStop(api, hosts) as stop:
        try:
            _discover_updates(hosts, args, api, credentials, stop)
        except _StopRequested:
            pass
        finally:
            if stop.requested.is_set() and not stop.force_requested.is_set():
                args.interrupted = True
                api.CONSOLE.print('Update session stopped. No unfinished flash was powered off; '
                                  'inspect the state file if a job could not be verified complete.', markup=False)


def _discover_updates(hosts, args, api, credentials, stop):
    """One package at a time across the fleet; re-scan after each round."""
    available = [host for host in hosts if host.session_path and not host.blocked]
    if args.update_state.exists():
        previous = json.loads(args.update_state.read_text())
        unresolved = [host.name for host in available
                      if previous.get('hosts', {}).get(host.name, {}).get('restore_power_needed')
                      or previous.get('hosts', {}).get(host.name, {}).get('status') in
                      ('submission-about-to-send', 'waiting-for-firmware-job', 'power-action-about-to-send',
                       'applying-firmware', 'stopped-uncertain')]
        if unresolved:
            table = api.Table('Host', 'Previous status', 'Original power', 'Power restoration owed', expand=True)
            for name in unresolved:
                entry = previous['hosts'][name]
                table.add_row(name, str(entry.get('status', 'Unknown')),
                              str(entry.get('original_power', 'Unknown')),
                              'Yes' if entry.get('restore_power_needed') else 'No')
            api.CONSOLE.print(api.Panel(table, title='Unresolved previous update state'))
            api.CONSOLE.print('This is saved recovery state, not proof that firmware is still running. '
                              'Continuing checks the live job queues and uses current power states; '
                              'previous recovery obligations are kept in a backup.', markup=False)
            if stop.ask('Type CONTINUE to check again; Enter=exit', default='', show_default=False).strip().upper() != 'CONTINUE':
                return
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = args.update_state.with_name(args.update_state.stem + '.previous-' + stamp + args.update_state.suffix)
            api.save(backup, previous)
            api.CONSOLE.print('Previous recovery state preserved: ' + str(backup), markup=False)
    state = {'repository': args.update_repository, 'catalog': args.update_catalog, 'hosts': {}}
    lock = api.Lock()
    finished = set()

    def record(host, **fields):
        with lock:
            entry = state['hosts'].setdefault(host.name, {})
            entry.update(fields)
            if 'status' in fields:
                entry.setdefault('events', []).append(dict(fields))
            api.save(args.update_state, state)
            if 'status' in fields:
                api.PROGRESS.update(tasks[host], busy=True)
            if fields.get('status') in ('up-to-date', 'stopped-no-progress', 'all-remaining-updates-failed',
                                        'power-restoration-failed', 'stopped-uncertain', 'recheck-failed', 'discovery-failed',
                                        'existing-jobs-skipped', 'stopped-by-user'):
                api.PROGRESS.update(tasks[host], description=f'{host.name}: {fields["status"]}',
                                    busy=False,
                                    **({'completed': 100} if fields['status'] == 'up-to-date' else {}))
                if host not in finished:
                    finished.add(host)
                    api.PROGRESS.advance(api.OVERALL_TASK)

    tasks = {host: api.PROGRESS.add_task(f'{host.name}: checking existing job queue', total=100, unit='percent', busy=True) for host in available}
    api.PROGRESS.update(api.OVERALL_TASK, visible=True, description='Hosts finished updating', total=len(available), completed=0)

    def renew(host):
        user, password = credentials[host]
        deadline = api.time.monotonic() + args.unavailable_grace
        while True:
            try:
                host.login(user, password)
                return
            except api.httpx.HTTPError:
                if api.time.monotonic() >= deadline:
                    raise
                api.time.sleep(5)

    for host in available:
        host.read_retry_seconds = args.unavailable_grace
        host.renew_session = lambda host=host: renew(host)
    meter = None
    stopped = api.Event()
    last_start = None
    failed_packages = {host: set() for host in available}
    successful_packages = {host: set() for host in available}
    observations = {host: {} for host in available}
    diagnostic_path = args.update_state.with_suffix('.jobs.jsonl')
    diagnostics_failed = api.Event()

    def observe(host, path, data):
        if stop.force_requested.is_set():
            raise ValueError('Job polling cancelled on exit')
        token = getattr(host.client, 'headers', {}).get('X-Auth-Token')
        data = redact(data, [credentials.get(host, (None, None))[1], token])
        stamp = datetime.now(timezone.utc).isoformat()
        with lock:
            if observations[host].get(path) != data and not diagnostics_failed.is_set():
                try:
                    api.append_log(diagnostic_path, {'event': 'job', 'observed_at': stamp, 'host': host.name,
                                                    'job': path, 'response': data})
                except OSError:
                    diagnostics_failed.set()
                    api.CONSOLE.print('Job diagnostic log unavailable; snapshots remain in the state file.', markup=False)
            observations[host][path] = data
            snapshots = dict(observations[host])
        record(host, job_snapshots=snapshots, jobs_last_checked_utc=stamp)

    def scan(host):
        api.PROGRESS.update(tasks[host], description=f'{host.name}: checking repository updates', busy=True)
        packages = discover(host, args, api, tasks[host], record, observe)
        record(host, packages=packages)
        api.PROGRESS.update(tasks[host], description=f'{host.name}: {len(packages)} updates found; discovery complete', busy=False)
        return packages

    def start(host):
        nonlocal last_start
        if meter and args.power_throttle:
            meter.wait_headroom(stopped)
        elif last_start is not None:
            api.PROGRESS.update(api.OPERATION_TASK, description='Next host start countdown (seconds)', total=60, completed=0)
            while api.time.monotonic() < last_start + 60:
                api.PROGRESS.update(api.OPERATION_TASK, completed=min(60, int(api.time.monotonic() - last_start)))
                api.time.sleep(1)
        current = host.request('GET', api.SYSTEM_PATH).json().get('PowerState')
        if current not in ('On', 'Off'):
            raise ValueError(f'Unstable power state {current}; no power command sent')
        record(host, status='power-action-about-to-send', restore_power_needed=True)
        api.power_action(host, 'On' if current == 'Off' else 'GracefulRestart')
        last_start = api.time.monotonic()
        record(host, status='applying-firmware')
        if meter:
            with meter.lock:
                meter.samples.clear()
                meter.not_before = last_start

    try:
        if not available:
            args.failed = True
            api.CONSOLE.print('No authenticated hosts available for update discovery.')
            return
        queues = api.parallel(available, 'Checking existing jobs', read_jobs)
        ready = [host for host, jobs in queues.items() if all(job_state(job) in TERMINAL for job in jobs.values())]
        for host, jobs in queues.items():
            for path, data in jobs.items():
                observe(host, path, data)
            api.PROGRESS.update(tasks[host], description=f'{host.name}: ' +
                                ('queued for repository discovery' if host in ready else 'existing jobs need review'))
        results = api.parallel(ready, 'Checking repository updates', scan)
        for host in ready:
            if host in results and not results[host]:
                record(host, status='up-to-date')
        for host, jobs in queues.items():
            if host in ready:
                continue
            if stop.requested.is_set():
                return
            try:
                if any(job_state(job) not in TERMINAL for job in jobs.values()) and not args.read_only and not args.no_power_monitor and meter is None:
                    meter = api.PowerMonitor(available, args)
                    meter.start()
                if offer_existing_jobs(host, jobs, args, api, tasks[host], record, start, stop, observe):
                    ready.append(host)
                    results.update(api.parallel([host], 'Checking repository updates', scan))
                else:
                    record(host, status='existing-jobs-skipped')
            except (api.httpx.HTTPError, ValueError, KeyError) as exc:
                args.failed = True
                record(host, status='stopped-uncertain', error=api.error_detail(exc))
                api.CONSOLE.print(api.Panel(api.Text(api.error_detail(exc)), title=api.Text(host.name), border_style='red'))
        if stop.requested.is_set():
            return
        active = [host for host in available if host in results and results[host]]
        for host in available:
            if host in results and results[host]:
                show_packages(host, results[host], api)
            elif host in results:
                record(host, status='up-to-date')
            elif host in ready or host not in queues:
                record(host, status='discovery-failed')
        if len(results) != len(ready) or len(queues) != len(available):
            args.failed = True
        if not active:
            api.CONSOLE.print('No hosts ready for update discovery.' if not ready else
                              'No applicable updates found.' if len(results) == len(ready) else 'Update discovery failed; inspect errors.')
            return
        if args.read_only:
            api.CONSOLE.print('Read-only: repository checked; no firmware installed or power actions sent.')
            return
        api.CONSOLE.print('Updates run one package at a time across the fleet. Required host starts use power throttling; '
                          'iDRAC updates may reset sessions. Originally Off hosts return Off after each host-reboot update.')
        if stop.ask('Type UPDATE to install all applicable updates and perform required restarts until no updates remain or no progress is possible', default='') != 'UPDATE':
            return
        originals = api.parallel(active, 'Reading starting power states',
                                 lambda host: host.request('GET', api.SYSTEM_PATH).json().get('PowerState'))
        if len(originals) != len(active) or any(value not in ('On', 'Off') for value in originals.values()):
            args.failed = True
            api.CONSOLE.print('Cannot establish starting power states; no firmware installed.')
            return
        for host, original in originals.items():
            record(host, original_power=original)
        if meter is None and not args.no_power_monitor:
            meter = api.PowerMonitor(available, args)
            meter.start()
        round_number = 0
        while active and not stop.requested.is_set():
            round_number += 1
            api.CONSOLE.print(f'Firmware update round {round_number}')
            next_hosts = []
            for host in active:
                if stop.requested.is_set():
                    break
                packages = results[host]
                repeated = any(package_id(package) in successful_packages[host] for package in packages)
                if repeated:
                    args.failed = True
                    api.CONSOLE.print(f'{host.name}: a completed package is still offered; it will not be installed again.', markup=False)
                candidates = [package for package in packages if package_id(package) not in failed_packages[host]
                              and package_id(package) not in successful_packages[host]]
                if not candidates:
                    args.failed = True
                    record(host, status='stopped-no-progress' if repeated else 'all-remaining-updates-failed')
                    api.CONSOLE.print(f'{host.name}: no remaining package can make progress; stopped.', markup=False)
                    continue
                package = candidates[0]
                try:
                    require_idle(host)
                    reboot_type = package.get('RebootType', '').upper()
                    if reboot_type not in ('HOST', 'IDRAC', 'NONE'):
                        raise ValueError(f'Unknown package reboot type {reboot_type!r}; inspect before installing')
                    uri = package_url(args.update_repository, package)
                    service = host.request('GET', '/redfish/v1/UpdateService').json()
                    update = service.get('Actions', {}).get('#UpdateService.SimpleUpdate', {})
                    if not update.get('target'):
                        raise ValueError('SimpleUpdate is not advertised by this iDRAC')
                    record(host, status='submission-about-to-send', current_package=package, round=round_number)
                    if stop.requested.is_set():
                        record(host, status='stopped-by-user')
                        break
                    try:
                        response = host.request('POST', update['target'], json={
                            'ImageURI': uri, 'TransferProtocol': urlsplit(uri).scheme.upper(),
                            '@Redfish.OperationApplyTime': 'OnReset' if reboot_type == 'HOST' else 'Immediate'})
                    except api.httpx.HTTPStatusError as exc:
                        if exc.response.status_code in (400, 422):
                            raise UpdateFailure(api.error_detail(exc)) from exc
                        raise
                    job = local_path(response.headers.get('Location'), host)
                    record(host, job=job, status='waiting-for-firmware-job')
                    wait_firmware_job(host, job, api, tasks[host], args.update_timeout,
                             on_scheduled=(lambda host=host: start(host)) if reboot_type == 'HOST' else None,
                             observe=observe)
                    if state['hosts'][host.name].get('restore_power_needed'):
                        api.restore_power(host, originals[host])
                        record(host, restore_power_needed=False)
                    successful_packages[host].add(package_id(package))
                    # A successful prerequisite can make a previously failed
                    # package installable. Retry failures only after progress.
                    failed_packages[host].clear()
                    record(host, status='package-completed')
                    next_hosts.append(host)
                except UpdateFailure as exc:
                    failed_packages[host].add(package_id(package))
                    args.failed = True
                    record(host, status='package-failed', error=str(exc))
                    api.CONSOLE.print(api.Panel(api.Text(str(exc)), title=api.Text(host.name), border_style='red'))
                    # A terminal failure permits other packages, after restoring
                    # original power and checking the queue again.
                    try:
                        if state['hosts'][host.name].get('restore_power_needed'):
                            api.restore_power(host, originals[host])
                            record(host, restore_power_needed=False)
                        next_hosts.append(host)
                    except (api.httpx.HTTPError, ValueError, KeyError) as restore_error:
                        record(host, status='power-restoration-failed', error=api.error_detail(restore_error))
                except (api.httpx.HTTPError, ValueError, KeyError) as exc:
                    args.failed = True
                    record(host, status='stopped-uncertain', error=api.error_detail(exc))
                    api.CONSOLE.print(api.Panel(api.Text(api.error_detail(exc)), title=api.Text(host.name), border_style='red'))
                    # Never repeat an ambiguous POST or power action, or power
                    # off a host while a possibly-running flash is unresolved.
            if stop.requested.is_set():
                break
            results = api.parallel(next_hosts, 'Rechecking repository after updates', scan) if next_hosts else {}
            active = []
            for host in next_hosts:
                if host not in results:
                    args.failed = True
                    record(host, status='recheck-failed')
                elif results[host]:
                    show_packages(host, results[host], api)
                    active.append(host)
                else:
                    record(host, status='up-to-date')
                    api.CONSOLE.print(f'{host.name}: zero applicable updates remaining.', markup=False)
        if stop.requested.is_set():
            state['stop_requested'] = True
            for host in active:
                entry = state['hosts'].get(host.name, {})
                if not entry.get('restore_power_needed') and entry.get('status') not in (
                        'stopped-uncertain', 'power-restoration-failed'):
                    record(host, status='stopped-by-user')
        api.CONSOLE.print('Update loop finished. See ' + str(args.update_state) + ' for per-host results.', markup=False)
    finally:
        if meter:
            meter.stop()
        for host in available:
            host.read_retry_seconds = 0
            host.renew_session = None


class FirmwareUpdater:
    """Run a firmware session against authenticated DellServer instances.

    The injected runtime supplies UI, power actions and persistence so offline
    tests exercise the same workflow without contacting hardware.
    """
    def __init__(self, servers, options, credentials, runtime=None):
        self.servers = servers
        self.options = options
        self.runtime = runtime or SimpleNamespace(
            httpx=httpx, time=time, Event=Event, Lock=Lock,
            CONSOLE=ui.CONSOLE, PROGRESS=ui.PROGRESS, ask=ui.ask, parallel=ui.parallel,
            OVERALL_TASK=ui.OVERALL_TASK, OPERATION_TASK=ui.OPERATION_TASK,
            Panel=Panel, Table=Table, Text=Text, error_detail=ui.error_detail,
            SYSTEM_PATH=SYSTEM_PATH, job_progress=job_progress, save=save, append_log=append_log,
            PowerMonitor=PowerMonitor, power_action=power_action, restore_power=restore_power)
        self.credentials = credentials

    def run(self):
        discover_updates(self.servers, self.options, self.runtime, self.credentials)
