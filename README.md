# Curveball Tools

A NOC tooling suite for Bandwidth's TDM/Ethernet transport network. Traces and
diagnoses circuits across four platforms, in both directions, and reports what
it could **not** prove as loudly as what it could.

Built by P. Capps.

```
NPT-1250 (access)  ->  NPT-1800 (hub)  ->  Cisco NCS 4216 (MUX)  ->  GSX9000
   DS1 / EVC             OC12 + xe- NNI      SONET / ACR / CEM        T1-1-s-p-c
```

Above the hub, each site's **BCR** (`bcr01.<site>`, Cisco IOS-XR) peers BGP with
the 1800 toward the core — checked separately by the BCR to 1800 Check tool.

**Current version is in `app.py` as `APP_VERSION`.** This file does not restate
it — a version number in prose is a version number that goes stale.

---

## Running it

```
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000**. Every tool has a form; the browser polls
`/api/job/<id>` for a live progress log while the run happens in a background
thread.

`Set Curveball Tools Icon.ps1` creates `Start Curveball Tools.lnk` in the
tools folder, carrying the suite icon, for you to move into the Start Menu
and pin. Two `.bat` files exist and do different things:
`Start Curveball Tools.bat` launches the web app,
`Curveball Tools.bat` launches the CLI menu.

Each tool also runs standalone from a terminal, prompting for its own inputs:

```
python autotester_standalone.py
python site_sweep.py
python interface_check.py
```

`python launcher.py` presents them as a CLI menu.

### Verifying it works

Seven self-tests run off-box in seconds, need no credentials and touch no
devices. **`../../RUNBOOK.md` is the owner of verification** — it lists them
and the live checks that follow.

---

## The tools

The home page builds itself from each module's `TOOL_INFO`, so that page is
always the accurate list. Roughly, in the order they appear:

| Tool | What it does |
|---|---|
| Morning Check | Batch-runs the Autotester plus fiber/OSPF/LDP checks across every open ticket assigned to you; posts to JIRA and Slack |
| Site Sweep | Fast triage — every DS1 on a 1250 at once, no route trace |
| OSPF & LDP Stability | Fleet-wide adjacency monitor, xlsx output, persistent state |
| IXC Stability | **BETA — do not use in production** (its own `TOOL_INFO` says so) |
| Card Alarms | Fleet-wide NPT-1250 equipment-alarm sweep — card/fan/power/MCP/temperature, grouped by market, new-vs-known tracking, category filter, per-type tally, Slack on new |
| BCR to 1800 Check | Per-hub BGP session (BCR ↔ 1800) uptime/state/prefixes + the 1800-side physical link, across the four US hubs. First IOS-XR tool |
| **Autotester** | Full end-to-end T1 trace, 1250 → GSX, PMs at every hop. The core tool |
| **Reverse Autotester** | The same trace backwards, GSX → 1250, for when a GSX alarm is all you have. Shares the Autotester page as a second card |
| NPT-1250 Discovery | Full `show tech`-style benchmark capture |
| DS1 PMs | Standalone DS1 performance-monitor pull |
| Fiber Utilization | Optical levels and utilisation |
| OSPF, LDP & Fiber Levels | Event log, LDP session state, plus levels in one pass |
| 1250 Health Check | Chassis, alarms, uptime |
| GSX Perfmon | 96-interval PM pull, with a 24h-clean skip |
| IXC Check | Site + interface check against NetBox, with a VERDICT block |
| MUX CEM Loop Script | Resolves ACR/CEM and prints the loop MOP as copy/paste text |
| DS1 Test Builder | AIS / loops toward the GSX, printed as a runbook with its own back-out |
| SoR Collect | Records one 1250's circuits into the inventory **cache** and reports anything that moved since last time. Not an authority — the device always wins |

`circuit_audit.py` conforms to the tool contract but is **CLI-only** — it has
no `order` key and no `app.py` wiring, so it never renders a card.

---

## Safety

**This network carries live production traffic.**

Almost every device command the suite *executes* is read-only (`show` / `SHOW`
/ `ping`) or session-only (`terminal length 0`). Nothing is ever committed to a
device configuration.

**One exception, opt-in and default off:** the DS1 Test Builder's GSX monitor
can send `CONFIGURE T1 ... PERFMON RESET COUNTERS ALL` to zero the PM counters
on one T1 before a test window, if you tick `clear_first`. No service impact,
but **irreversible** — that T1's accumulated PM history is destroyed. Do not
default it on in any automated caller.

Three further places print intrusive commands as **copy/paste runbook text for
a human to execute and reverse by hand** — loops, AIS injection, and enabling
CES PMs. Each is generated with its own back-out step so a test is never left
half-applied.

**`INTRUSIVE_COMMANDS.md` is the owner of that audit.** Read it before telling
anyone this suite is safe — and note it was itself wrong about the exception
above until 2026-10-01, which is the reason it now documents how each claim
was verified rather than just asserting the verdict.

Credentials are prompted at runtime and held only in the params dict passed to
the background thread — never written to disk, never put in a session or
cookie. Secrets go through `secure_config.py` to the Windows Credential
Manager. The one exception is `NETBOX_TOKEN`, which is still plaintext in
`autotester_config.txt` and not yet in `SECURE_KEYS`.

---

## Architecture

The **docstrings are the primary documentation.** They record why a thing is
the way it is, the date a device output format was confirmed live, and which
trap was being avoided. Read them before changing a parser.

| Layer | Modules |
|---|---|
| Web app | `app.py`, `templates/`, `static/` |
| CLI | `launcher.py`, plus each tool's own `collect_inputs()` |
| Device clients | `device_base.py` and `npt1250_client.py`, `npt1800_client.py`, `mux4216_client.py`, `gsx_client.py`, `bcr_client.py` (IOS-XR, the BCR) |
| Shared lookups | `hub_registry.py` (hub identity), `netbox_client.py`, `ds1_util.py` (DS1 parsing), `chassis_config.py` (1800 config readers), `nni_check.py` |
| Shared checks | `loop_check.py` (loop detection), `ldp_check.py` (LDP session status) — both always-emit status lines reused across the trace tools |
| Reporting | `output_builders.py` — all rendering and the per-hop health checks |
| Integrations | `jira_client.py`, `secure_config.py` |
| Infrastructure | `platform_paths.py` (all output paths), `assurance.py` (failing-loudly machinery), `env_check.py` (dependency banners) |
| Inventory cache | `sor_store.py` (versioned JSON, change history), `sor_collect.py` (one-site collector) |

**Tool contract:** `TOOL_INFO` dict, `collect_inputs(session=None)`, and
`run(params) -> out_path`. `app.py` never calls `collect_inputs()` — it would
hang with no terminal attached — so each Flask route builds the same params
dict and calls `run()` directly. See `CURVEBALL_TOOL_SPEC.md` for the contract
and `ADDING_A_TOOL.md` for wiring a new tool in.

---

## Design principles

These are not aspirations; they are reactions to specific bugs that shipped
wrong data while looking completely plausible.

- **Two independent sources for any fact, compared.** The cheapest and
  highest-value check here — it needs no external oracle and has caught nearly
  every bug found. Device hostname vs NetBox, configured vs live state,
  forward route vs reverse route.
- **Never guess when a lookup is ambiguous.** Print `NONE FOUND` or
  `refusing to pick`. A plausible wrong answer is worse than no answer: an
  unanchored substring match once reported a neighbouring circuit's node, slot
  and T1, all of which looked entirely reasonable.
- **Never render "clean" when the command failed.** Only claim clean if raw
  output actually came back. Absence of evidence gets printed, not skipped.
- **Cumulative counters are not current state.** A healthy mature circuit has
  large non-zero lifetime counters. Verdicts use the device's own
  current-state fields.
- **Say where a number came from.** Every reported value names its source, so
  "the MUX description says X but the device calls itself Y" is expressible.

---

## Documentation

| File | Owns |
|---|---|
| `../../CONTEXT_PRIMER.md` | Version, module and tool counts; what's in flight; open items |
| `../../RUNBOOK.md` | How to verify the suite, off-box and live |
| `ADDING_A_TOOL.md` | Adding a tool, wiring it to the web app, and cutting a release |
| `CURVEBALL_TOOL_SPEC.md` | The tool contract in full |
| `INTRUSIVE_COMMANDS.md` | The read-only audit and its verification greps |
| `INSTALL.md` | First-time setup, including the credential store |
| `WEB_SETUP.md` | How the Flask layer works |
| `Curveball Tools Overview.md` | Non-technical summary |
| `!Revisions/CHANGELOG.md` | **The canonical changelog.** The root `CHANGELOG.md` is a stub pointer, and the stub is what ships inside the release zips |
