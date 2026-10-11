# Working on lazy_idracer

- Keep the code simple. Prefer small modules and focused fixes to broad rewrites.
- Source lives in `src/lazy_idracer`; the installed command is `lazy-idracer`.
- Preserve HTTPS certificate and hostname verification by default. Only an
  explicit `--insecure` CLI option may disable it; never inventory configuration.
- Do not introduce private hosts, repository addresses, credentials, logs,
  snapshots, certificates, or recovery records into source or Git.
- Keep tests offline. Do not submit firmware updates, alter configuration, reset
  controllers, or change live server power without explicit user authorization.
- Test changed behavior with meaningful regressions. Use HTTPX MockTransport for
  transport-level tests and fake clocks for polling; build a simulator only if needed.
- Run `python -m unittest discover -s tests`. For packaging changes, build the
  wheel and sdist, inspect their contents, and check installation in a clean venv.
- Never replay ambiguous mutating requests automatically. Preserve unresolved
  job/power recovery information and handle graceful and forced exits separately.
- Inventory TOML must not accept passwords or TLS bypass options. CLI values
  override inventory defaults; resolve relative CA paths beside the inventory.
- Before structural changes, keep a recovery archive outside the repository.
- Use read-only access wherever possible, writing only where required.
- On Linux systems with a user systemd manager, commands expected to take more
  than a few seconds must run using
  `systemd-run --user --collect [--no-block] [--wait] [--pipe] [--pty] -p RuntimeMaxSec=Ns --unit="<agent>-<session>-<task>"`.
  Choose a sensible runtime limit. For noninteractive commands read the last
  three log lines with `journalctl --user -u "<unit>.service" -n 3 --no-pager`.
