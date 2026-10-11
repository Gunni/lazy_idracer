"""Normalize Dell job and Redfish task responses for all apply workflows."""
from dataclasses import dataclass, field
import time
from urllib.parse import urlsplit

TERMINAL = frozenset({'Completed', 'Failed', 'CompletedWithErrors', 'Exception', 'Cancelled'})
FAILED = TERMINAL - {'Completed'}


def normalize_job(job):
    if not isinstance(job, dict):
        raise ValueError('Expected a job JSON object')
    oem = job.get('Oem', {})
    oem = {} if oem is None else oem
    if not isinstance(oem, dict):
        raise ValueError('Expected Dell job metadata to be a JSON object')
    dell = oem.get('Dell', {})
    dell = {} if dell is None else dell
    if not isinstance(dell, dict):
        raise ValueError('Expected Dell job metadata to be a JSON object')
    data = {**job, **dell}
    state = data.get('JobState') or data.get('TaskState')
    if state == 'RebootCompleted':
        state = 'Completed'
    elif state in (None, 'unknown', 'Unknown') and str(data.get('PercentComplete')) == '100':
        if str(data.get('Message', '')).strip().lower().rstrip('.') in ('reboot completed', 'reboot is complete'):
            state = 'Completed'
    data['JobState'] = state
    return data


def job_state(job):
    return normalize_job(job)['JobState']


def job_progress(states):
    states = [normalize_job(job) for job in states]
    percentages = []
    for job in states:
        if job_state(job) == 'Completed':
            percentages.append(100)
        else:
            value = job.get('PercentComplete')
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
                percentages.append(value)
            else:
                return sum(job_state(j) == 'Completed' for j in states), len(states), 'jobs'
    return int(sum(percentages) / len(percentages)) if percentages else 0, 100, 'percent'


class JobFailure(ValueError):
    """All watched jobs are terminal, but at least one failed."""
    def __init__(self, jobs):
        self.jobs = jobs
        super().__init__('; '.join(f'{job.path}: {job.state}: {job.message}' for job in jobs))


@dataclass
class Job:
    """A server-owned job with normalized state and its last read-back."""
    server: object
    path: str
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path.startswith('/redfish/'):
            raise ValueError('Invalid local job path')
        parsed = urlsplit(self.path)
        if parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('Invalid local job path')
        self.data = normalize_job(self.data)

    def refresh(self, reader=None):
        raw = reader(self.server, self.path) if reader else self.server.request('GET', self.path).json()
        self.data = normalize_job(raw)
        return self.data

    @property
    def state(self):
        return self.data['JobState']

    @property
    def terminal(self):
        return self.state in TERMINAL

    @property
    def failed(self):
        return self.state in FAILED or self.state == 'Completed' and self.data.get(
            'TaskStatus', self.data.get('JobStatus')) == 'Critical'

    @property
    def ready(self):
        return self.state == 'Scheduled' or self.state == 'Pending' and 'successfully scheduled' in str(
            self.data.get('Message', '')).lower()

    @property
    def message(self):
        return self.data.get('Message') or str(self.data.get('Messages', ''))

    def wait(self, timeout, **kwargs):
        JobGroup([self]).wait(timeout, **kwargs)
        return self.data


class JobGroup:
    """Poll related jobs together; restore power only after the group is terminal."""
    def __init__(self, jobs):
        self.jobs = list(jobs)

    @property
    def terminal(self):
        return all(job.terminal for job in self.jobs)

    @property
    def failures(self):
        return [job for job in self.jobs if job.failed]

    @property
    def progress(self):
        return job_progress([job.data for job in self.jobs])

    @property
    def state_text(self):
        return ', '.join(dict.fromkeys(str(job.state or 'Unknown') for job in self.jobs))

    def wait(self, timeout, *, clock=time, interval=5, reader=None, on_progress=None,
             on_ready=None, ready_predicate=None, until_states=None, timeout_message=None):
        deadline = clock.monotonic() + timeout
        started = False
        while clock.monotonic() < deadline:
            for job in self.jobs:
                job.refresh(reader)
            if on_progress:
                on_progress(self)
            if self.terminal or until_states and all(job.state in until_states for job in self.jobs):
                if self.failures:
                    raise JobFailure(self.failures)
                return [job.data for job in self.jobs]
            if on_ready and not started:
                ready = ready_predicate(self) if ready_predicate else (
                    any(job.ready for job in self.jobs) and
                    not any(job.state in ('Running', 'Starting') for job in self.jobs))
                if ready:
                    on_ready()
                    started = True
            clock.sleep(min(interval, max(0, deadline - clock.monotonic())))
        raise ValueError(timeout_message or f'Jobs did not complete within {timeout}s')
