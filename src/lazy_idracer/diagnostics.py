"""Post-login diagnostics without authentication headers or request bodies."""
from datetime import datetime, timezone
from threading import Lock
import json
import os


def redact(value, secrets=()):
    secrets = [secret for secret in secrets if isinstance(secret, str) and secret]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key).lower().replace('-', '').replace('_', '')
            sensitive = any(word in name for word in ('password', 'passwd', 'secret', 'token',
                                                      'authorization', 'credential', 'privatekey', 'apikey'))
            result[key] = '[REDACTED]' if sensitive else redact(item, secrets)
        return result
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
    return value


class DiagnosticLog:
    def __init__(self, path, secrets=()):
        self.path = path
        self.secrets = list(secrets)
        self.lock = Lock()

    def record(self, entry, secrets=()):
        with self.lock:
            append_log(self.path, {'observed_at': datetime.now(timezone.utc).isoformat(),
                                   **redact(entry, [*self.secrets, *secrets])})

    def clear(self):
        self.secrets.clear()


def append_log(path, entry):
    """Append a diagnostic JSON line without truncating earlier runs."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
                   'w', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(redact(entry), ensure_ascii=False) + '\n')
        stream.flush()
