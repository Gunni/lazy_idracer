"""Read connection defaults without stored passwords or TLS bypasses."""
from pathlib import Path
import tomllib


def load_inventory(path):
    path = Path(path)
    with path.open('rb') as stream:
        data = tomllib.load(stream)
    unknown = set(data) - {'hosts', 'user', 'ca', 'update_repository'}
    if unknown:
        raise ValueError('Unknown inventory keys: ' + ', '.join(sorted(unknown)))
    hosts = data.get('hosts')
    if not isinstance(hosts, list) or not hosts or any(not isinstance(host, str) or not host.strip() for host in hosts):
        raise ValueError('Inventory hosts must be a nonempty array of host names')
    for key in ('user', 'ca', 'update_repository'):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise ValueError(f'Inventory {key} must be a nonempty string')
    if 'ca' in data:
        certificate = Path(data['ca']).expanduser()
        if not certificate.is_absolute():
            certificate = path.resolve().parent / certificate
        data['ca'] = str(certificate)
    return data
