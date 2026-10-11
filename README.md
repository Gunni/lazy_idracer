# lazy_idracer

An interactive CLI for configuring and maintaining a fleet of Dell iDRAC servers.
Compare active and pending attributes, select values across hosts, manage power,
and install firmware from a repository with Rich progress and read-back checks.

This is an alpha release for homelabs. It has been exercised on a Dell fleet;
support across other models and iDRAC firmware versions is not yet established.
It uses Dell OEM Redfish resources and is not a generic Redfish client.

## Install

Requires Python 3.11 or newer. From this repository:

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install .
lazy-idracer --help
```

The package is prepared for distribution but has not been published to PyPI.
`python -m lazy_idracer` is equivalent to the installed command.

## Inventory and TLS

Copy [examples/inventory.toml](examples/inventory.toml) to a private
`inventory.toml` and replace the example hosts:

```toml
hosts = ["idrac-01.example.net", "idrac-02.example.net"]
user = "administrator"
ca = "certs/idrac-ca.pem"  # optional private CA bundle
update_repository = "https://firmware.example.net/"  # optional until updating
```

Inventory keys are `hosts`, `user`, `ca`, and `update_repository`.
Command-line values override inventory defaults. Relative CA paths resolve
beside the inventory file. Passwords are prompted securely and cannot be stored
in the inventory. Inventory files cannot disable certificate verification.

HTTPS certificate and hostname verification are enabled by default. For a
private CA use `ca` or `--ca FILE`. To explicitly disable verification for an
invocation, supply `--insecure`. It cannot be combined with a configured CA.
There is no built-in host list or firmware repository address.

## Use

Start by comparing without writing:

```bash
lazy-idracer --inventory inventory.toml --read-only
lazy-idracer --inventory inventory.toml --all
lazy-idracer --inventory inventory.toml --setting
lazy-idracer --inventory inventory.toml --search PSRedPolicy
lazy-idracer --inventory inventory.toml --power-state
lazy-idracer --inventory inventory.toml --discover-pending-reboot
lazy-idracer --inventory inventory.toml --discover-updates
```

For a direct connection:

```bash
lazy-idracer --hosts idrac-01.example.net --user administrator --read-only
```

Entered configuration values apply immediately unless `--read-only` is set.
Writes are read back; pending values are distinguished from active values.
Firmware installation and configuration application request confirmation.
Each host has a persistent HTTPS session. HTTP/2 is used where supported, with
HTTP/1.1 fallback. `-4` forces IPv4 when the network has broken IPv6 routing.

Startup power throttling is enabled by default with a 2000 W reported-host
budget. Configure it for your setup with `--power-budget`; readings may exclude
shared chassis overhead and other circuit loads. It is not electrical protection.
`--no-power-throttle` uses fixed startup staggering instead.

During firmware updates, the first Ctrl+C stops new submissions and waits for
accepted updates before restoring initial power states. A second Ctrl+C logs
out and exits, leaving updates running and saving recovery information.

Snapshots, recovery records, and post-login diagnostics are written to the
current working directory. Logs exclude authentication requests, request
headers, and request bodies; passwords and tokens are redacted. These files
still contain infrastructure details: inspect and sanitize them before sharing.
Runtime files and private inventories are excluded from Git.

See [the usage guide](docs/usage.md) for prompt commands and recovery behavior.

## Develop and build

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests
python -m build
python -m twine check dist/*
```

Tests run offline. They cover registry mappings, pending writes, read-back,
job transitions, queue delays, retries, interrupts, and power restoration.
They do not establish compatibility with every Dell model. A full server
simulator is not required; add realistic HTTP fixtures when behavior warrants it.

MIT licensed. No affiliation with Dell.
