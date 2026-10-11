"""Atomic persistence of configuration and recovery records."""
import json
import os
from pathlib import Path
import tempfile

def save(path, data):
    # Replace only a complete owner-only file; interrupted writes retain the
    # previous recovery record. A sibling file keeps replacement atomic.
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def snapshot(hosts, args):
    save(args.snapshot, {h.name: {'resources': h.resources, 'errors': h.errors} for h in hosts})

