# Curveball Tools — Systems Meeting Prep

Fact-based answers to what a systems/infra team will likely ask before hosting
this app in prod. Every item below is sourced from the actual code as it
ships — nothing here is a guess. Ship this file alongside `INTRUSIVE_COMMANDS.md`
and the CHANGELOG.

## What it is

A Flask web app (`app.py`, single process, Werkzeug dev server) plus a set of
standalone Python CLI scripts. It's a NOC diagnostic tool: it reads status
from network devices (NPT-1250, NPT-1800, Cisco NCS 4216 MUX, GSX 9000) over
SSH and writes text reports, optionally posting them to JIRA/Slack.

## Network access it needs

Outbound only, no inbound access required except the web UI itself:

- **SSH (TCP/22)** to NE devices on the management network — 1250s, 1800s
  (hub sites ATL/JFK/DFW/LAX/LAB), MUX 4216s, GSX 9000s. Uses `paramiko`; the
  GSX leg uses `plink.exe`/plink binary specifically (see `gsx_client.py`).
- **HTTPS (443)** to `bandwidth-jira.atlassian.net` — JIRA REST API v3, for
  posting reports/attachments/comments (`jira_client.py`).
- **HTTPS (443)** to whatever NetBox instance is configured (`NETBOX_URL` in
  config) — read-only IPAM lookups (`netbox_client.py`).
- **HTTPS (443)** to a Slack incoming-webhook URL, if configured — one-way
  outbound POST only, no Slack API token or scopes needed.
- **HTTPS (443)** to GitHub (`git@github.com:pcapps-BW/Curveball-Tools.git`)
  — only used by `deploy.yml`'s Ansible git-clone task, not by the app at
  runtime.

Inbound: the Flask app binds `127.0.0.1:5000` only (`app.py` line ~846) — it
is not reachable directly from the network. `deploy.yml` puts nginx in front
of it on port 80, proxying to localhost:5000. So the only inbound requirement
is HTTP/80 (or 443 if TLS is added) to whatever host it's deployed on, from
whoever needs the web UI.

## Credential handling

- **Device creds (1250/1800/MUX/GSX)**: entered manually per session by the
  operator (username/password prompt), not stored by default. Not a service
  account currently — this is an open design question for the NOC manager,
  not yet decided.
- **JIRA/NetBox/Slack config values** (`JIRA_TOKEN`, `NETBOX_URL`,
  `NETBOX_TOKEN`, `SLACK_WEBHOOK`, etc.): stored via `secure_config.py`.
  Preference order is real OS keyring (Windows Credential Manager on
  Windows; Secret Service/KWallet on Linux) — probed with a real
  set/get/delete round-trip before being trusted, not just checked for
  presence. If no working keyring backend exists (true today on the headless
  Linux automation host), it falls back automatically to a local JSON file
  at `~/.claude/.secure_fallback.json`, permission-locked to `chmod 600`
  (best-effort; that lockdown is a no-op on Windows, where Credential
  Manager is used instead so the file-permission model doesn't apply there).
- **No credential value is ever printed to console or written to a log or
  report file** — confirmed by reading every `print()` call site in
  `secure_config.py`; only status messages like "stored in Credential
  Manager" or generic failure messages are printed, never the secret itself.
- **JIRA auth**: pcapps's own personal JIRA token today, entered directly in
  the UI (`app.py`). Whether the NOC manager wants a dedicated JIRA service
  account instead is open and unresolved — worth raising if this becomes a
  shared team tool.

## What it writes to disk / accumulation

All output paths resolve through `platform_paths.py`: `CURVEBALL_HOME` env
var override, else `C:\Claude` (Windows) or `~/.claude` (Linux). On the prod
host this means everything lands under one directory, not scattered across
the filesystem.

- Per-run text reports (`interface_check_*.txt`, `1250_discovery_*.txt`,
  `ospf_stability_report_*.txt`, etc.) — one file per run, named with a
  timestamp + hostname. No automatic cleanup/retention job exists today —
  this is a known gap worth naming proactively rather than waiting to be
  asked.
- Small persistent state files: `ospf_stability_state.json` (drop-history
  memory across runs), `gsx_hostkeys.json` (pinned GSX host keys),
  `.secure_fallback.json` (credential fallback store, chmod 600),
  `autotester_config.txt` (non-secret config values).
- Nothing writes to `/var/log` or syslog; all logging is `print()` to
  stdout, captured by `journalctl` under systemd.

## Device command posture (read-only)

Formally audited in `INTRUSIVE_COMMANDS.md` (ships with every revision,
updated any time a new device command is added). Every command any tool
actually executes against a device is `show`/`SHOW`/`ping`, or a
session-only paging setting (`terminal length 0`, `| no-more`) — nothing
persisted to device config. Two tools (`ds1_gsx_test.py`, `mux_loop_script.py`)
generate the *text* of intrusive commands as copy/paste runbooks for a human
to run by hand; traced line-by-line to confirm neither ever feeds that text
into a `.run()` call itself.

## Deploy mechanism — things to flag before they set it up

`deploy.yml` (Ansible) is the existing deploy path, written for a
single-purpose dedicated box. Worth raising explicitly if the target prod
host is shared with anything else:

- `hosts: all` — scopes to every host in whatever inventory it's run
  against, not just the intended target. Needs a specific host/group name
  in prod inventory.
- `git ... force: yes` — hard-resets the deploy directory to match GitHub
  `main` on every run, discarding any local changes. A hand-edit made
  directly on the box (as happened once on the lab host) will be silently
  wiped the next time this playbook runs. Worth deciding whether this
  playbook is authoritative going forward, or excluded/adjusted for prod.
- `nginx` + `listen 80 default_server` — claims the box's default HTTP
  vhost. Collides if anything else on a shared box also wants port 80 or
  the default server slot.
- `seboolean httpd_can_network_connect ... persistent: yes` — a persistent,
  box-wide SELinux policy change, not scoped to this app.
- Flask itself currently runs with `debug=True` (`app.py`) — fine on a
  personal/lab box, but the Werkzeug debugger should be off before this is
  reachable by anyone but the operator, since it can expose an interactive
  Python console over HTTP on unhandled errors. This needs to change before
  prod, independent of where systems decides to host it.

## Rollback story

Versioned zip releases (`!Revisions/Curveball Tools vX.YZ.zip`), one per
CHANGELOG entry, each self-contained (includes `requirements.txt`,
`deploy.yml`, all source). Rollback = extract the prior version's zip over
the deploy directory and restart the systemd service. A "Deployment Status"
table at the top of `!Revisions/CHANGELOG.md` tracks which version is live
on which host, specifically to prevent version drift across hosts.

## Ownership

Pcapps owns it today; team ownership/training is an eventual goal, not
immediate.

## Open items, not yet decided

- Dedicated vs. shared prod box (drives how much of `deploy.yml`'s box-wide
  behavior actually matters).
- JIRA credential model: personal token vs. team service account.
- Device credential model: manual entry (today) vs. some form of stored
  service-account credential for automation.
- Report file retention/cleanup — none exists today.
- A planned "v2 prod-tight" hardening pass: scope `deploy.yml` away from
  `default_server`/`hosts: all`/the SELinux persistent boolean, turn off
  Flask `debug=True`, and decide the deploy-authority question above.
