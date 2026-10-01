# Autotester (Standalone) — Installation & Setup Guide

**T1 Route Tracer with Performance Monitors**

This tool traces a T1 circuit end to end across all four hops — NPT-1250 → NPT-1800 → Cisco NCS 4216 MUX → GSX9000 — collects performance monitors at each hop, and produces a diagnostic report. It can optionally post the report to a JIRA ticket.

This guide covers everything needed to run it on a new machine.

---

## 1. What you received

A folder (e.g. `Consolidation`) containing these files. **Keep them all together in one folder** — the scripts import each other by filename.

| File | Purpose |
|------|---------|
| `launcher.py` | **The program you run.** Menu front door for all the tools. |
| `autotester_standalone.py` | Autotester tool — traces a T1 end to end across all four hops. |
| `npt1250_discovery.py` | NPT-1250 discovery dump tool. |
| `ds1_pms.py` | DS1 PMs tool. |
| `fiber_util.py` | Fiber (GE) utilization tool. |
| `health_check.py` | 1250 high-level health check tool. |
| `interface_check.py` | IXC / interface check tool (runs from the 1800). |
| `device_base.py` | Shared SSH machinery (base class for the device clients). |
| `npt1250_client.py` | NPT-1250 client. |
| `npt1800_client.py` | NPT-1800 client. |
| `mux4216_client.py` | Cisco NCS 4216 MUX client (includes Netbox path reconciliation). |
| `gsx_client.py` | GSX9000 client (uses plink). |
| `netbox_client.py` | Netbox lookups (OC→SONET, GSX management IPs). |
| `output_builders.py` | Builds the Autotester report. |
| `jira_client.py` | Posts results to JIRA. |
| `gsx_hostkey_test.py` | *Optional* standalone test for the GSX host-key handling. Not needed for normal use. |
| `requirements.txt` | Python package list for setup. |
| `ADDING_A_TOOL.md` | How to add a new tool to the launcher (for contributors). |

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
From inside the tool folder, install the two required packages:
```
pip install -r requirements.txt
```
(That installs `paramiko` and `requests`. If `pip` isn't found, use `python -m pip install -r requirements.txt`.)

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
`set_password()` / `clear_password()`, or edit the entry directly in Windows
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

### Quickest way — double-click
Double-click **`Curveball Tools.bat`** in the tool folder. It opens a terminal window, changes to the right directory, and launches the menu automatically. No need to navigate there manually.

### From a terminal
From inside the tool folder:
```
python launcher.py
```

### Pinning to the taskbar (optional, one-time setup)
1. Right-click **`Create Curveball Shortcut.ps1`** → **Run with PowerShell**
   (Double-clicking opens Notepad — that is a Windows default for `.ps1` files, not a bug.)
2. A **Curveball Tools** shortcut appears on your Desktop.
3. Right-click that shortcut → **Pin to taskbar**.

One click on the taskbar icon opens a terminal and launches the menu.

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
- [ ] Launch: double-click `Curveball Tools.bat` — or `python launcher.py`
- [ ] *(Optional)* Right-click `Create Curveball Shortcut.ps1` → Run with PowerShell → Pin to taskbar

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
- Paths (`C:\Claude\`) are currently fixed. A future version is planned with a graphical front end and configurable paths.
