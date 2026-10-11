#!/usr/bin/env python3
"""Interactively compare Dell Redfish attribute sets and normalize selected values."""
import argparse
from contextlib import ExitStack
import getpass
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit
import httpx
from lazy_idracer import DellServer, Configurer, FirmwareUpdater
from lazy_idracer import ui
from lazy_idracer.ui import CONSOLE, PROGRESS, ask, parallel, logout_hosts
from lazy_idracer.constants import DELL
from lazy_idracer.inventory import load_inventory
from lazy_idracer.power import change_power_state
from lazy_idracer.diagnostics import DiagnosticLog

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--inventory', type=Path, help='TOML inventory; CLI options override its defaults')
    parser.add_argument('--hosts', nargs='+', help='Explicit DNS names or IP addresses')
    parser.add_argument('--user', help='Login username (default root)')
    parser.add_argument('--password-per-host', action='store_true', help='Prompt separately for each host')
    parser.add_argument('--read-only', action='store_true', help='Compare/select/save without PATCH requests')
    parser.add_argument('--all', action='store_true', help='Prompt for matching attributes too')
    parser.add_argument('--filter', default='', help='Substring in resource/attribute name')
    parser.add_argument('--search', metavar='ATTRIBUTE', help='Search attribute names across resources and prompt for matches, including equal values and read-only attempts')
    parser.add_argument('--start-from', nargs='*', metavar='RESOURCE [ATTRIBUTE]',
                        help='Prompt for category and optional field after login, or supply RESOURCE [ATTRIBUTE]; skip earlier prompts')
    parser.add_argument('--power-state', action='store_true', help='Show fleet power states and offer On/Off; stagger or throttle power-ons')
    parser.add_argument('--setting', nargs='*', metavar='RESOURCE ATTRIBUTE',
                        help='Prompt for resource/attribute when used alone, or supply both: --setting /redfish/v1/Systems/System.Embedded.1/Bios/Settings BankXor')
    parser.add_argument('--ca', help='Trusted CA PEM file')
    parser.add_argument('--insecure', action='store_true', help='Explicitly disable certificate verification')
    parser.add_argument('-4', '--ipv4', action='store_true', help='Use IPv4 connections while retaining DNS names and TLS verification')
    parser.add_argument('--login-debug', action='store_true', help='Show login transport phase timings without credentials/tokens')
    parser.add_argument('--verbose', action='store_true', help='Show login timings and negotiated HTTP version')
    parser.add_argument('--skip-pending', action='store_true', help='Skip attributes with a pending change on any selected host')
    parser.add_argument('--snapshot', type=Path, default=Path('redfish-snapshot.json'))
    parser.add_argument('--discover-pending-reboot', action='store_true', help='Discover pending settings, preview them and offer to schedule/reboot affected hosts')
    parser.add_argument('--discover-updates', action='store_true', help='Check repository, then offer a serial firmware-update/recheck loop with required restarts')
    parser.add_argument('--update-repository', help='Firmware repository HTTP/HTTPS base URL; required for updates')
    parser.add_argument('--update-catalog', default='Catalog.xml', help='Repository catalog filename (default Catalog.xml)')
    parser.add_argument('--update-timeout', type=int, default=7200, help='Seconds to wait per repository/firmware job (default 7200)')
    parser.add_argument('--update-state', type=Path, default=Path('redfish-update-state.json'), help='Save firmware discovery and job results')
    parser.add_argument('--reboot-state', type=Path, default=Path('redfish-reboot-state.json'), help='Save starting power states and apply/reboot results')
    parser.add_argument('--job-timeout', type=int, default=1800, help='Seconds to wait per host for configuration completion (default 1800)')
    parser.add_argument('--unavailable-grace', type=int, default=300, help='Retry temporarily unavailable iDRAC reads during reboot/apply for this many seconds (default 300)')
    parser.add_argument('--no-power-monitor', action='store_true', help='Disable live host wattage display')
    throttle = parser.add_mutually_exclusive_group()
    throttle.add_argument('--power-throttle', dest='power_throttle', action='store_true', default=True,
                          help='Gate starts on live power headroom (default; 2000 W budget)')
    throttle.add_argument('--no-power-throttle', dest='power_throttle', action='store_false',
                          help='Use fixed 60-second startup staggering instead of power headroom')
    parser.add_argument('--power-budget', type=int, default=2000, help='Reported host power budget in watts (default 2000; not a circuit safety guarantee)')
    parser.add_argument('--power-reserve', type=int, default=500, help='Reserve startup headroom in watts (default 500; uncalibrated)')
    parser.add_argument('--power-settle', type=int, default=15, help='Seconds of fresh readings below startup threshold (default 15)')
    parser.add_argument('--power-wait-timeout', type=int, default=1800, help='Seconds to wait for startup headroom before stopping new starts')
    args = parser.parse_args(argv)
    try:
        inventory = load_inventory(args.inventory) if args.inventory else {}
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.hosts = args.hosts or inventory.get('hosts')
    args.user = args.user or inventory.get('user', 'root')
    args.ca = args.ca or inventory.get('ca')
    args.update_repository = args.update_repository or inventory.get('update_repository')
    if not args.hosts:
        parser.error('Provide --inventory FILE or --hosts HOST [HOST ...]')
    if args.discover_updates:
        if not args.update_repository:
            parser.error('Firmware updates require --update-repository or update_repository in the inventory')
        if (args.setting is not None or args.start_from is not None or args.search is not None or args.filter
                or args.power_state or args.discover_pending_reboot or args.all or args.skip_pending):
            parser.error('--discover-updates cannot be combined with comparison, power-state or pending-apply modes')
        repository = urlsplit(args.update_repository)
        if (repository.scheme not in ('http', 'https') or not repository.hostname or repository.username
                or repository.password or repository.query or repository.fragment or repository.port is not None):
            parser.error('--update-repository requires an HTTP/HTTPS URL without credentials or an explicit port')
        if not args.update_catalog or '/' in args.update_catalog or '\\' in args.update_catalog:
            parser.error('--update-catalog must be a filename within the repository')
        if args.update_timeout <= 0:
            parser.error('--update-timeout must be positive')
    args.start_field = ''
    args.prompt_start_field = args.start_from == []
    if args.start_from is not None:
        if len(args.start_from) > 2:
            parser.error('Use --start-from alone, RESOURCE, or RESOURCE ATTRIBUTE')
        if len(args.start_from) == 2:
            args.start_field = args.start_from[1].strip()
        args.start_from = args.start_from[0] if args.start_from else ''
    if args.start_from is not None and (args.setting is not None or args.power_state or args.discover_pending_reboot):
        parser.error('--start-from is for normal comparison/search, not setting, power-state or pending-apply modes')
    if args.search is not None:
        args.search = args.search.strip()
        if not args.search:
            parser.error('--search requires a nonempty attribute name or substring')
        if args.setting is not None or args.power_state or args.discover_pending_reboot or args.filter:
            parser.error('--search cannot be combined with setting, power-state, pending-apply modes or --filter')
        args.all = True
    if args.setting is not None:
        if args.power_state or args.discover_pending_reboot or args.filter:
            parser.error('--setting cannot be combined with power-state, pending-apply modes or --filter')
        if len(args.setting) not in (0, 2):
            parser.error('Use --setting alone for prompts, or --setting RESOURCE ATTRIBUTE')
    if args.power_state and (args.setting is not None or args.discover_pending_reboot):
        parser.error('--power-state cannot be combined with setting or pending-apply modes')
    if args.setting:
        resource, attribute = args.setting
        parsed = urlsplit(resource)
        if not resource.startswith('/redfish/') or parsed.netloc or parsed.query or parsed.fragment or not attribute:
            parser.error('--setting requires a local /redfish/ resource path and an attribute name')
        args.setting[0] = resource.rstrip('/')
        if args.setting[0].endswith('/Settings'):
            args.setting[0] = args.setting[0][:-len('/Settings')]
        args.all = True
    if args.power_throttle and args.no_power_monitor:
        parser.error('Power throttling is enabled by default; --no-power-monitor requires --no-power-throttle')
    if not 0 < args.power_reserve < args.power_budget or args.power_settle < 5 or args.power_wait_timeout <= 0:
        parser.error('Power reserve must be positive and below budget; settle must be >=5 seconds; wait timeout must be positive')
    if args.job_timeout <= 0:
        parser.error('--job-timeout must be positive')
    if args.unavailable_grace < 0:
        parser.error('--unavailable-grace must not be negative')
    if args.ca and args.insecure:
        parser.error('--ca and --insecure are mutually exclusive')
    if len(set(args.hosts)) != len(args.hosts):
        parser.error('Duplicate hosts')
    for name in args.hosts:
        parsed = urlsplit('https://' + name)
        if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            parser.error('Hosts must be DNS names or IP addresses (bracket IPv6), optionally with a port')
    return args


def main():
    args = parse_args()
    args.failed = False
    ui.OVERALL_TASK = PROGRESS.add_task('Overall resources discovered', total=0, visible=False)
    ui.OPERATION_TASK = PROGRESS.add_task('Preparing', total=len(args.hosts))
    password = None if args.password_per_host else getpass.getpass(f'iDRAC password for {args.user}: ')
    verify = False if args.insecure else ssl.create_default_context(cafile=args.ca)
    with ExitStack() as stack:
        PROGRESS.start()
        stack.callback(PROGRESS.stop)
        hosts = []
        clients = stack.enter_context(ExitStack())
        stack.callback(logout_hosts, hosts)
        passwords = {}
        for name in args.hosts:
            transport = httpx.HTTPTransport(verify=verify, http2=True,
                trust_env=False, local_address='0.0.0.0' if args.ipv4 else None,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2, keepalive_expiry=600))
            client = clients.enter_context(httpx.Client(base_url=f'https://{name}',
                transport=transport, trust_env=False, follow_redirects=False, timeout=30))
            host = DellServer(name, client)
            host.login_debug = args.login_debug
            host.verbose = args.verbose or args.login_debug
            hosts.append(host)
            passwords[host] = getpass.getpass(f'{name} password for {args.user}: ') if args.password_per_host else password
        parallel(hosts, 'Logging in', lambda host: host.login(args.user, passwords[host]))
        authenticated = [host for host in hosts if host.session_path and not host.blocked]
        if authenticated:
            log_path = args.update_state.with_suffix('.jobs.jsonl') if args.discover_updates else Path('redfish-diagnostics.jsonl')
            diagnostics = DiagnosticLog(log_path, passwords.values())
            stack.callback(diagnostics.clear)
            for host in authenticated:
                host.diagnostics = diagnostics
            CONSOLE.print(f'Diagnostics: {log_path}', markup=False)
        if not args.discover_updates:
            passwords.clear()
        if args.start_from is not None:
            while True:
                resource = args.start_from or ask('Start category (q=quit)',
                    default=DELL + 'iDRAC.Embedded.1')
                resource = resource.strip().rstrip('/')
                if resource == 'q':
                    return 0
                if '::' in resource:
                    resource, field = (part.strip() for part in resource.split('::', 1))
                    args.start_field = field
                    resource = resource.rstrip('/')
                parsed = urlsplit(resource)
                if resource.startswith('/redfish/') and not parsed.netloc and not parsed.query and not parsed.fragment:
                    args.start_from = resource
                    break
                CONSOLE.print('Enter a local /redfish/ resource path, for example ' + DELL + 'iDRAC.Embedded.1', markup=False)
                args.start_from = ''
            if args.prompt_start_field and not args.start_field:
                field = ask('Optional start field (example: NTPConfigGroup.1.NTP2); blank=first field, q=quit',
                            default='', show_default=False).strip()
                if field == 'q':
                    return 0
                args.start_field = field
        if args.discover_updates:
            credentials = {host: (args.user, passwords[host]) for host in hosts}
            try:
                FirmwareUpdater(hosts, args, credentials).run()
            finally:
                credentials.clear()
                passwords.clear()
        elif args.power_state:
            change_power_state(hosts, args)
        else:
            Configurer(hosts, args).run()
    return 130 if getattr(args, 'interrupted', False) else 1 if args.failed or any(h.errors for h in hosts) else 0



def entrypoint():
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nStopped; previously saved selections and completed writes are retained.')
        sys.exit(130)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    entrypoint()
