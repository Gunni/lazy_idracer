# Usage and recovery

Run `lazy-idracer --inventory inventory.toml` or provide `--hosts` explicitly.
Passwords are prompted after argument validation. TLS verification is on by
default; use `--ca` for a private CA or explicitly `--insecure` to disable it.

## Configuration prompts

Each prompt shows the resource path, attribute, registry help, raw active and
pending values, and any restrictions. Registry display labels can map to raw
values on write; mappings are printed explicitly. Writes happen on Enter and
are checked by reading active and pending settings back.

- Enter: skip.
- `#1`, `#2`, etc.: choose an observed value; bare numbers are literal values.
- `s`: ask for an attribute prefix to skip within this resource.
- `S`: skip the entire resource.
- `i`: enter individual values for hosts, with current values as defaults.
- `u`: undo the last successful operation, subject to concurrency checks.
- `d` or `D`: prompt to clear this attribute.
- `p`: return to the previous attribute.
- `k`: skip attributes with pending changes.
- `c`: cancel pending changes for this attribute.
- `q`: quit and log out.

Quote a string as JSON if it collides with a command, such as `"d"`.
`--all` includes equal values. `--read-only` prevents configuration writes.
`--search ATTRIBUTE` finds matching attribute names and prompts for them.
`--setting` prompts for a resource and field repeatedly until quit, or accepts
both directly:

```bash
lazy-idracer --inventory inventory.toml --setting /redfish/v1/Systems/System.Embedded.1/Bios/Settings BankXor
```

`--start-from` prompts after login for a section and optional attribute. The
section prompt accepts a copied `RESOURCE :: ATTRIBUTE` line. Earlier fields
are skipped. Resource IDs shown here are examples from Dell systems; discovery
coverage and writability depend on the actual hardware and firmware.

## Applying pending configuration

`--discover-pending-reboot` reads pending differences and displays per-host
from/to tables before offering application. Changes are scheduled as needed;
restarts are requested only when required by the workflow. Original On/Off
power states are recorded and restored after completion is confirmed.

`--power-state` displays current states and offers a fleet power action.
Startup throttling uses fresh telemetry and headroom, with a default 2000 W
budget, 500 W startup reserve, and 15-second settling period. Chassis limits
may inform the effective budget. Readings may exclude other circuit loads.
Use `--no-power-throttle` for fixed 60-second startup spacing.

## Firmware

`--discover-updates` requires a repository URL from TOML or
`--update-repository`. The catalog defaults to `Catalog.xml`. Discovery builds
an applicable update list without installing it. Installation requires typing
`UPDATE`. Packages are submitted serially, and discovery repeats until there
are no updates or no package can make progress.

Existing jobs are inspected before submission. Completed records do not block
updates. Unscheduled downloaded packages are displayed and skipped; the tool
will not silently delete or schedule them. An accepted update returned through
TaskService is also checked in the corresponding Dell OEM queue before power
restoration and rescanning. Temporary iDRAC unavailability is retried within
`--unavailable-grace`; jobs have a bounded `--update-timeout`.

Originally Off hosts can briefly reach normal boot or PXE after firmware
completion. Graceful shutdown is attempted, then ForceOff if the host stays
On for 120 seconds. This happens only after monitored firmware jobs finish.

First Ctrl+C stops new submissions and drains accepted updates before restoring
power. Second Ctrl+C stops waiting, logs out and exits, leaving active updates
alone. Unresolved recovery state remains recorded.

## Saved state and diagnostics

Snapshots, selections, reboot state, update state, and diagnostics are local
runtime files, not source files. Several paths can be set via CLI options;
otherwise they are created in the current working directory. Use a separate
working directory per fleet. Do not run two modifying sessions against the
same fleet/state files concurrently.

A previous unresolved update state is shown in a table. Enter exits; typing
`CONTINUE` saves a timestamped backup and checks the live queues. A fresh run
uses current power states; it does not inherit old power-restoration obligations.

Post-login diagnostics append to `redfish-diagnostics.jsonl`, or the update
state filename with a `.jobs.jsonl` suffix. Request metadata and changing job
snapshots are recorded. Authentication requests, headers and request bodies
are excluded; known passwords/tokens and credential fields are redacted.
Logs and snapshots contain infrastructure details and may contain other
sensitive configuration values. Sanitize them before sharing. Files are
created with owner-only POSIX permissions; Windows ACL behavior differs.

Use `--verbose` for login timings and detailed feedback. `--login-debug` adds
transport phase timing without credentials. `-4` forces IPv4 when necessary.
