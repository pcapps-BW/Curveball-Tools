# Curveball Tools — Installation & Setup Guide

**T1 Route Tracer with Performance Monitors**

This tool traces a T1 circuit end to end across all four hops — NPT-1250 → NPT-1800 → Cisco NCS 4216 MUX → GSX9000 — collects performance monitors at each hop, and produces a diagnostic report. It can optionally post the report to a JIRA ticket.

This guide covers everything needed to run it on a new machine.

---

## 1. What you received

A folder containing the whole suite. **Keep it together** — the modules import
each other by filename, so moving or renaming individual files breaks them.

| What | Purpose |
|------|---------|
| `app.py` | **The web app — the normal way to run the suite.** `python app.py`, then http://127.0.0.1:5000 |
| `templates/`, `static/` | The web app's pages and its JS/CSS. |
| `launcher.py`, `Curveball Tools.bat` | CLI menu front door, for terminal use. |
| the tool modules | One per tool (`autotester_standalone.py`, `reverse_autotester.py`, `site_sweep.py`, …). The home page builds itself from these, so that page is always the accurate list. |
| the device clients | `device_base.py` plus `npt1250_client.py`, `npt1800_client.py`, `mux4216_client.py`, `gsx_client.py`. |
| shared lookups & reporting | `hub_registry.py`, `netbox_client.py`, `ds1_util.py`, `chassis_config.py`, `nni_check.py`, `output_builders.py`. |
| integrations & infrastructure | `jira_client.py`, `secure_config.py`, `platform_paths.py`, `assurance.py`. |
| `requirements.txt` | Python package list for setup. |
| `*.md` | Documentation — see `README.md` §Documentation for which file owns what. |

This deliberately does not enumerate every module: the list was wrong for
about two months, missing roughly fifteen files. `README.md` has the
architecture table, and the code is the authority.

---

## 2. Prerequisites

The machine that runs this needs four things installed **once**:

### 2.1 Python 3
Install Python 3 (3.9 or newer; developed on 3.14). During the Windows installer, **check "Add Python to PATH."**

Verify from a PowerShell/Command Prompt:
```
python --version
```

### 2.2 Python packages
From inside the tool folder, install the required packages:
```
pip install -r requirements.txt
```
(That installs `paramiko`, `requests`, `flask` and `keyring`. If `pip` isn't found, use `python -m pip install -r requirements.txt`.)

### 2.3 PuTTY (for the GSX hop)
The GSX hop uses **plink**, which ships with PuTTY. Install PuTTY from the official site. A standard install puts `plink.exe` in `C:\Program Files\PuTTY\`, which the tool finds automatically.

Verify:
```
plink -V
```
If that isn't recognized but PuTTY is installed, the tool still checks the standard Program Files locations. If PuTTY is missing entirely, the tool runs but **skips the GSX hop**.

### 2.4 The `C:\Claude\` folder
The tool reads its config from — and writes its output and host-key store to — `C:\Claude\`. Create it if it doesn't exist:
```
mkdir C:\Claude
```

---

## 3. Configuration file (required)

Create a plain-text file at:
```
C:\Claude\autotester_config.txt
```

with the following contents (one `key=value` per line):

```
NETBOX_URL=https://netbox.bandwidth.com
NETBOX_TOKEN=<your Netbox API token>
JIRA_EMAIL=<your Atlassian email>
JIRA_TOKEN=<your Atlassian API token>
```

Notes:
- **Each user supplies their own tokens.** Do not share a config file containing tokens — treat these like passwords.
- `NETBOX_URL` / `NETBOX_TOKEN` are required for the MUX path reconciliation and the GSX management-IP lookup. Without them the MUX/GSX hops cannot resolve the path.
- `JIRA_EMAIL` / `JIRA_TOKEN` are only needed if you use the "Add to JIRA" option. If you never post to JIRA, they can be omitted.
- Lines beginning with `#` are treated as comments.

**Getting a Netbox API token:** Netbox → your profile → API Tokens.
**Getting an Atlassian API token:** Atlassian account settings → Security → API tokens.

### `JIRA_TOKEN` disappears from this file — that is expected

On first load, `secure_config.py` moves any sensitive key it finds in plaintext
into the OS credential store (**Windows Credential Manager**) and **deletes the
plaintext line from `autotester_config.txt`**.

Keys handled this way:

| Key | Stored where |
|---|---|
| `INFRA_PASS` | Credential Manager |
| `GSX_PASS` | Credential Manager |
| `JIRA_TOKEN` | Credential Manager |

So after your first run, the `JIRA_TOKEN=` line is gone from the file. **Nothing
is broken and you do not need to re-add it** — the token is in Credential
Manager under the app name `Curveball`. Re-adding it to the file just migrates
it again.

To change or clear a stored token, use `secure_config.py`'s
`set_password()` / `delete_password()`, or edit the entry directly in Windows
Credential Manager (Control Panel → Credential Manager → Windows Credentials).

**`NETBOX_TOKEN` is not migrated.** It stays in `autotester_config.txt` as
plaintext. Treat the file as sensitive and do not share or commit it.

### On Linux, there may be no credential store

`keyring` only works on Linux when a Secret Service (GNOME Keyring) or KWallet
daemon is actually running. A headless server or VM usually has neither. In that
case `secure_config.py` falls back to a permissions-locked (`chmod 600`) local
JSON file — **still plaintext**.

This is acceptable for a lab or demo box. It is **not** an appropriate
credential posture for a shared or production deployment; that needs a real
secrets backend. The tool prints a warning when it takes this path — do not
ignore it.

On Windows this never applies: Credential Manager is always available.

---

## 4. Running the tools

### The web app — the normal way
From inside the tool folder:
```
python app.py
```
Then open **http://127.0.0.1:5000**. The home page lists every tool; each has
a form, and the browser shows a live progress log while the run happens.

This is how the suite is normally used. It binds to localhost only — see
`WEB_SETUP.md` §6 before putting it on the network.

Or double-click **`Start Curveball Tools.bat`**, which installs requirements if
needed and starts the web app.

### The CLI menu
```
python launcher.py
```
Or double-click **`Curveball Tools.bat`**. Note the two `.bat` files do
different things: `Start Curveball Tools.bat` → the **web app**,
`Curveball Tools.bat` → the **CLI menu**.

Each tool also runs on its own (`python autotester_standalone.py`), prompting
for its own inputs.

### Checking the install worked
Seven self-tests run off-box, need no credentials and touch no devices:
```
python ds1_util.py
python chassis_config.py
python assurance.py
python gsx_client.py --self-test
python gsx_perfmon.py --self-test
python circuit_audit.py --self-test
python reverse_autotester.py --self-test
```
Each prints one `ok` line per check and ends with a pass line — the exact
wording varies by module (`All reverse_autotester self-tests passed.`,
`All parser self-tests passed.`, and `assurance.py` prints a worked example
instead). **What matters is that no module prints `FAILED` / `FAILURES`.**
If one does, stop — do not go near a device. `../../RUNBOOK.md` covers
verification in full.

### A pinnable shortcut (optional, one-time setup)
1. Right-click **`Set Curveball Tools Icon.ps1`** → **Run with PowerShell**
   (Double-clicking opens Notepad — that is a Windows default for `.ps1` files, not a bug.)
2. **`Start Curveball Tools.lnk` appears in this same folder** — not on your
   Desktop. The icon has to live on a `.lnk` file, which is why the script
   creates one rather than pinning the `.bat` directly.
3. Copy or move that `.lnk` into your Start Menu Programs folder, then pin it.

The shortcut targets `Start Curveball Tools.bat`, so it launches the **web
app** — not the CLI menu. The script's own header comments document a Windows
quirk where the pinned item loses its target; the fix is in there.

The menu is built automatically from the tool files present in the folder, so new tools can be dropped in without changing the launcher (see `ADDING_A_TOOL.md`).

Pick a tool from the menu. The menu shows, for each, the device it runs from, what it
does, and the inputs it needs. You'll then be prompted for that tool's inputs,
whether to post to JIRA (and the `NOE-XXXX` ticket number), whether to auto-open
the result, and your credentials.

**Within one launcher session**, credentials are entered once and reused for
later tools; device-identifying values (1250 IP, site, interface, DS1 list) are
remembered and offered as an editable default (shown so you always confirm which
device you're hitting).

Each tool can also be run on its own if preferred — it will prompt for its own
inputs:
```
python autotester_standalone.py
python npt1250_discovery.py
python ds1_pms.py
python fiber_util.py
python health_check.py
python interface_check.py
```

The Autotester walks all four hops (1250→1800→MUX→GSX). Live progress prints to
the screen; the finished report is written to a file and echoed to the console.

---

## 5. Output

Each tool writes a timestamped report to `C:\Claude\`, e.g.:
```
C:\Claude\Autotester_standalone_<timestamp>_<1250hostname>.txt
```

The report contains, in order:
- **Run Summary** — one line per DS1: pass, key findings, and a suggested fault direction.
- **Diagnostic Summary** — a top-to-bottom view of the route (GSX → MUX → 1800 → 1250) with `[ OK ]` / `[ !! ]` indicators per hop.
- **Full T1 Route** — the complete hop-by-hop path and assignment reference.
- **Shared Infrastructure Stats** and **Per-DS1 Performance Monitors** — the detailed collected data.

If you chose to post to JIRA, the Run Summary + Diagnostic Summary and the per-DS1 PMs are added to the ticket as a comment (in two collapsible panels).

---

## 6. GSX host keys (automatic — informational only)

The first time the tool connects to a given GSX, it records that device's SSH host-key fingerprint in:
```
C:\Claude\gsx_hostkeys.json
```
On later runs it verifies the GSX still presents the same key. If a GSX ever presents a **different** key, the tool refuses to connect and prints a warning — this protects against connecting to the wrong device. If a GSX is legitimately replaced, remove that device's entry from the JSON file and re-run.

No action is needed from you; this happens automatically.

---

## 7. Quick setup checklist

- [ ] Python 3 installed (and on PATH)
- [ ] `pip install -r requirements.txt`
- [ ] PuTTY installed (for the GSX hop)
- [ ] `C:\Claude\` folder exists
- [ ] `C:\Claude\autotester_config.txt` created with your own tokens
- [ ] All tool files kept together in one folder
- [ ] Launch the web app: `python app.py`, then open http://127.0.0.1:5000
- [ ] *(or the CLI menu)* double-click `Curveball Tools.bat` — or `python launcher.py`
- [ ] Run the seven self-tests (section 4) — they need no credentials
- [ ] *(Optional)* Right-click `Set Curveball Tools Icon.ps1` → Run with PowerShell → Pin to taskbar

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `paramiko not installed` / `requests not installed` | Run `pip install -r requirements.txt`. |
| GSX hop is skipped ("plink not found") | PuTTY isn't installed or `plink.exe` isn't in a standard location. Install PuTTY. |
| MUX hop can't resolve the path / Netbox errors | Check `NETBOX_URL` and `NETBOX_TOKEN` in the config file. |
| JIRA post fails with an HTTP error | Check `JIRA_EMAIL` / `JIRA_TOKEN`, and confirm the ticket number exists and you have access. |
| Write fails / no output file | Make sure `C:\Claude\` exists and is writable. |
| A single hop fails mid-run | The tool logs the error, skips that hop, and still writes the report with whatever it collected. Re-run if it was transient (e.g. a dropped connection). |

---

## 9. Notes & limitations

- **Read-only.** The tool only issues show/status/PM commands and session-only paging settings (e.g. `terminal length 0`). It makes no configuration changes to any device.
- **Peerless circuits** (customer-facing, ts21/ts24 on the 1800) correctly stop at the 1800 — there is no Bandwidth MUX or GSX hop for them.
- Runs take a few minutes; the 1800 MUSE lookup in particular is slow by nature.
- Output paths resolve through `platform_paths.py`, so the suite runs on Linux as well as Windows. New tools must use it rather than hardcoding a path.
- The web front end exists (`app.py`); it runs on Flask's development server bound to localhost. See `WEB_SETUP.md` §6 for what to change before it leaves your machine.
