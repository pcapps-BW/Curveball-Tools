# Curveball Tools (Standalone) — NOC circuit toolkit

A set of command-line tools for tracing and checking TDM circuits across the
NPT-1250 / NPT-1800 / Cisco NCS 4216 MUX / GSX 9000 path. They are standalone
Python ports of the existing SecureCRT scripts — they run on their own from a
command prompt and do not require SecureCRT.

A **launcher** presents all the tools in one menu; each tool can also be run
directly on its own.

> **New here? Read this file for the overview, follow `INSTALL.md` for setup,
> then run `python launcher.py`.**
>
> **Want to run the Autotester from a browser instead?** See `WEB_SETUP.md`
> for the web front end (proof of concept, one tool wired up so far).

---

## The tools

| Tool | Runs from | What it does |
|------|-----------|--------------|
| **Autotester** | 1250 | Traces a T1 end to end (1250→1800→MUX→GSX), pulls PMs at each hop, flags faults, and (optionally) posts to JIRA. |
| **NPT-1250 Discovery** | 1250 | Full read-only inventory dump of a 1250: hardware, interfaces, protocols, alarms, DS1 status, PMs. |
| **DS1 PMs** | 1250 | Physical status, alarms, and PMs for specific DS1s on a 1250. |
| **Fiber Utilization** | 1250 | Fiber (GE) uplink utilization — IN/OUT Max/Avg/Current bps for ge-ts2/1 and ge-ts5/1. |
| **1250 Health Check** | 1250 | High-level 1250 health: version, interfaces, fiber transceivers, LDP/OSPF, DS1 status across CES slots, alarms and events. |
| **IXC Check** | 1800 | Checks an IXC circuit from the 1800: config, OSPF/LDP, ping reachability. |
| **OSPF Events + Fiber Levels** | 1250 | OSPF up/down event timestamps from NE unreported events + fiber transceiver light levels (ts2/ts5). |
| **GSX Perfmon (96 Intervals)** | GSX | STATUS, STATISTICS, PERFMON DAY/THRESHOLDS, and all 96 fifteen-minute intervals for a single T1. For stress-test duration planning. |
| **MUX CEM Loop Script** | 1250 | Runs live prechecks on all four hops, then generates a copy/paste-ready MOP: Scenario A (hairpin facing GSX), Scenario B (hairpin facing 1250), both restores, and postcheck commands. |

Each tool writes a timestamped `.txt` report to `C:\Claude\`, echoes it to the
console, and can optionally post it to a JIRA ticket.

### The Autotester path

```
   NPT-1250  ──►  NPT-1800  ──►  Cisco NCS 4216 MUX  ──►  GSX 9000
   (DS1 PMs)     (OC12 + PMs)   (SONET path + T3)        (T1 status + PMs)
```

Each hop hands the next the identifiers it needs, in memory — nothing about the
downstream elements is typed by the user. Customer (Peerless) circuits are
detected automatically and the trace correctly stops at the 1800.

---

## Running it

### Quickest way — double-click
Double-click **`Curveball Tools.bat`** in this folder. It opens a terminal, changes to the right directory, and launches the menu automatically.

To pin it to your Windows taskbar, run **`Create Curveball Shortcut.ps1`** once
(right-click → **Run with PowerShell**), then right-click the shortcut it places on your Desktop → **Pin to taskbar**.

### Via the launcher (from a terminal)
```
python launcher.py
```
Pick a tool from the menu. The launcher shows, for each tool, the device it runs
from, what it does, and what inputs it needs. The menu is built by
**auto-discovery** -- the launcher finds every tool file in the folder at startup, so tools can be added without editing the launcher (see *Adding a tool* below). Within one launcher session:

- **Credentials are entered once and reused** silently for later tools (the
  1250/1800/MUX share one login; the GSX has its own).
- **Device-identifying values** (1250 IP, site, interface, DS1 list) are
  remembered and offered as an **editable default** — always shown and
  confirmed, never silently applied, so a tool never runs against the wrong
  device by accident.

### Standalone
Any tool can be run on its own; it prompts for its own inputs:
```
python autotester_standalone.py
python npt1250_discovery.py
python ds1_pms.py
python fiber_util.py
python health_check.py
python interface_check.py
python ospf_events.py
python gsx_perfmon.py
python mux_loop_script.py
```

---

## Adding a tool

The launcher auto-discovers tools: **drop a tool's `.py` into this folder and it
appears in the menu** on the next launch -- no launcher edit needed. A tool file
just needs a `TOOL_INFO` dict plus `collect_inputs(session)` and `run(params)`
functions. Files without a `TOOL_INFO` (shared modules, helpers) are ignored.

Full contributor instructions -- including the exact `TOOL_INFO` fields and the
prompting/no-prompting contract -- are in **`CURVEBALL_TOOL_SPEC.md`**. Hand that file
to anyone who wants to contribute a tool.

If a tool file can't be loaded, the launcher prints a `[skip] ...` line saying
why and continues; one bad tool never breaks the menu.

---

## Architecture at a glance

Small, single-purpose modules. Each device has its own client; a shared base
class holds the common SSH machinery; the tools use those clients. Every tool
exposes `collect_inputs()` (prompting) and `run(params)` (the work), so the
launcher can collect inputs and drive the tool, while standalone use still works.

| Module | Responsibility |
|--------|----------------|
| `launcher.py` | Menu front door. Lists the tools, collects inputs (with session reuse), calls each tool's `run()`. |
| `autotester_standalone.py` | Autotester tool — runs the four hops, assembles the report. |
| `npt1250_discovery.py` | NPT-1250 discovery tool. |
| `ds1_pms.py` | DS1 PMs tool. |
| `fiber_util.py` | Fiber utilization tool. |
| `health_check.py` | 1250 high-level health check tool. |
| `interface_check.py` | IXC / interface check tool (1800). |
| `ospf_events.py` | OSPF event timestamps + fiber light levels tool. |
| `gsx_perfmon.py` | GSX deep perfmon — all 96 fifteen-minute intervals. |
| `mux_loop_script.py` | MUX CEM hairpin loop script generator. |
| `device_base.py` | Shared SSH machinery (connection, prompt handling, command I/O). |
| `npt1250_client.py` | NPT-1250 client. |
| `npt1800_client.py` | NPT-1800 client. |
| `mux4216_client.py` | Cisco NCS 4216 MUX client (SONET path resolution + Netbox reconciliation). |
| `gsx_client.py` | GSX 9000 client (via PuTTY `plink`). |
| `netbox_client.py` | Netbox lookups — OC→SONET path, GSX management IPs. |
| `output_builders.py` | Builds the Autotester report sections. |
| `jira_client.py` | Posts reports to JIRA. |
| `gsx_hostkey_test.py` | *Optional* standalone test for the GSX host-key handling. |

**Keep all files in one folder** — the modules import each other by filename.
Do not reorganize them into subfolders.

### A few design notes for reviewers

- **Each device type connects differently, by necessity.** The two NPT platforms
  use standard SSH. The Cisco MUX requires two older SSH algorithms to be enabled
  explicitly (the device only offers those). The GSX uses an older SSH key type
  the modern Python SSH library rejects, so the GSX hop shells out to PuTTY's
  `plink`. This is why `gsx_client.py` looks different from the others.

- **GSX host keys are pinned, not blindly accepted.** On first contact the tool
  records the GSX's SSH fingerprint (to `C:\Claude\gsx_hostkeys.json`) and pins
  it on later connections. If a GSX presents a *different* key, the tool refuses
  to connect and warns — guarding against connecting to a wrong/spoofed device.

- **The MUX path is resolved Netbox-first.** Rather than guessing the SONET path,
  the tool asks Netbox for the true working path and reconciles it against the
  MUX's live ACR (protection) state. If the circuit is on its protect path, the
  report flags it (`CONFLICT: circuit on PROTECT ...`) rather than silently
  reporting the wrong path.

- **Diagnostics reflect current state.** Per-hop `[ OK ]`/`[ !! ]` indicators are
  based on *current* performance/alarm data. Cumulative lifetime counters (which
  a healthy circuit can carry from past events) are shown in detail but not
  treated as active faults.

- **Read-only.** The tools issue only show/status/PM commands and session-only
  paging settings (e.g. `terminal length 0`). They make **no configuration
  changes** to any device.

---

## Quick start

Full detail in **`INSTALL.md`**. In brief:

1. Install **Python 3**, then from this folder: `pip install -r requirements.txt`
2. Install **PuTTY** (provides `plink`, used for the GSX hop in the Autotester).
3. Create the folder **`C:\Claude\`**.
4. Copy **`autotester_config.example.txt`** to **`C:\Claude\autotester_config.txt`**
   and fill in your own Netbox / JIRA tokens.
5. Run: `python launcher.py`

---

## Known limitations / for reviewer attention

- **Paths are currently fixed to `C:\Claude\`** (config, host-key store, output).
  Making these configurable is planned. For now that folder must exist.
- **The GSX hop (Autotester) requires PuTTY.** Without it, the Autotester still
  runs but skips the GSX hop; the other tools are unaffected.
- **Autotester runs take a few minutes** — the 1800 MUSE lookup in particular is
  slow by nature (a known area for future optimization).
- **A single hop/command failing** is logged; the run continues and still writes
  a report with whatever was collected.
- A graphical front end and a "skip GSX" option are planned but not yet built.

---

## Files in this package

```
Curveball Tools.bat           <- double-click to launch (easiest)
launcher.py                   <- or: python launcher.py

  Tools:
autotester_standalone.py
npt1250_discovery.py
ds1_pms.py
fiber_util.py
health_check.py
interface_check.py
ospf_events.py
gsx_perfmon.py
mux_loop_script.py

  Shared modules:
device_base.py
npt1250_client.py
npt1800_client.py
mux4216_client.py
gsx_client.py
netbox_client.py
output_builders.py
jira_client.py

  Setup / shortcuts:
Create Curveball Shortcut.ps1 (run once to create a taskbar-pinnable shortcut)
requirements.txt
autotester_config.example.txt (template -> copy to C:\Claude\autotester_config.txt)
README.md                     (this file)
INSTALL.md                    (detailed setup)
CHANGELOG.md                  (version history)
```
