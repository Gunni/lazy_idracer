"""Shared Rich console, progress and concurrent read feedback."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Prompt
from rich.progress import Progress, ProgressColumn, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

CONSOLE = Console()
class CountsColumn(ProgressColumn):
    def render(self, task):
        if task.fields.get('unit') == 'watts':
            return Text(f'{int(task.completed)}/{int(task.total)} W')
        if task.fields.get('unit') == 'percent':
            return Text(f'{int(task.completed)}%')
        if task.total is None:
            return Text('—')
        return Text(f'{int(task.completed)}/{int(task.total)}')


class ActivitySpinnerColumn(SpinnerColumn):
    """Job percent can reach 100 before the host workflow finishes."""
    def render(self, task):
        if 'busy' in task.fields:
            return self.spinner.render(task.get_time()) if task.fields['busy'] else self.finished_text
        return super().render(task)


class FleetProgress(Progress):
    """Keep fleet summaries above host rows regardless of creation order."""
    def get_renderables(self):
        def order(task):
            if task.id in (OVERALL_TASK, OPERATION_TASK):
                return 0, task.id
            if task.fields.get('unit') == 'watts':
                return 1, task.id
            return 2, task.id
        yield self.make_tasks_table(sorted(self.tasks, key=order))


PROGRESS = FleetProgress(ActivitySpinnerColumn(), TextColumn('{task.description}'), BarColumn(),
                    CountsColumn(), TimeElapsedColumn(), console=CONSOLE, expand=True, transient=True)
OVERALL_TASK = None
OPERATION_TASK = None

def error_detail(exc):
    if not isinstance(exc, httpx.HTTPStatusError):
        return str(exc)
    response = exc.response
    lines = [f'HTTP {response.status_code}: {response.request.method} {response.request.url.path}']
    try:
        body = response.json()
    except ValueError:
        return '\n'.join(lines + ['Controller returned no JSON error details.'])
    error = body.get('error', body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        return '\n'.join(lines)
    for field in ('code', 'message'):
        if error.get(field):
            lines.append(str(error[field]))
    details = error.get('@Message.ExtendedInfo', [])
    if isinstance(body, dict) and error is not body:
        details = details + body.get('@Message.ExtendedInfo', [])
    for entry in details:
        if isinstance(entry, dict):
            for field in ('MessageId', 'Message', 'Resolution', 'RelatedProperties', 'MessageArgs'):
                if entry.get(field):
                    label = 'Message parameters (for the message above)' if field == 'MessageArgs' else field
                    lines.append(f'{label}: {entry[field]}')
    return '\n'.join(lines)


def ask(message, **kwargs):
    # Live refresh and terminal input compete for the cursor. Stop rendering
    # while accepting input, then resume the same progress tasks.
    PROGRESS.stop()
    try:
        return Prompt.ask(message, console=CONSOLE, **kwargs)
    finally:
        PROGRESS.start()


def parallel(hosts, description, operation):
    results = {}
    if not hosts:
        return results
    PROGRESS.update(OPERATION_TASK, description=description, completed=0, total=len(hosts))
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = {pool.submit(operation, host): host for host in hosts}
        for future in as_completed(futures):
            host = futures[future]
            try:
                results[host] = future.result()
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                detail = error_detail(exc)
                host.errors[description] = detail
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                    host.blocked = True
                CONSOLE.print(Panel(Text(detail), title=Text(host.name), border_style='red'))
            PROGRESS.advance(OPERATION_TASK)
    return results


def logout_hosts(hosts):
    sessions = [host for host in hosts if host.session_path]
    for task in PROGRESS.tasks:
        PROGRESS.update(task.id, visible=False)
    if not sessions:
        return
    task = PROGRESS.add_task('Logging out', total=len(sessions))
    pool = ThreadPoolExecutor(max_workers=len(sessions))
    try:
        futures = {pool.submit(host.logout): host for host in sessions}
        for future in as_completed(futures):
            host = futures[future]
            try:
                if not future.result():
                    CONSOLE.print(f'{host.name}: logout unsuccessful; session will expire automatically.', markup=False)
            except Exception as exc:
                CONSOLE.print(f'{host.name}: logout unsuccessful: {exc}', markup=False)
            PROGRESS.advance(task)
    except KeyboardInterrupt:
        CONSOLE.print('Logout interrupted; remaining sessions will expire automatically.')
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
