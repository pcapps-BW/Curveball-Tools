# Curveball Tools — Intrusive Commands Audit

This document exists to answer one question, on demand, for anyone who asks
(a NOC manager, a security review board, a future maintainer): **does any
script in this suite send a command to a device that changes its
configuration or state?**

Ship this file in every revision, alongside the CHANGELOG. Update it in the
same pass as any change that adds a new device command, a new tool, or a new
call to a device client's `.run()` (or equivalent) method.

## Methodology

**Re-audited 2026-10-01 at v2.35.** The verdict below is unchanged; the module
inventory and the `gsx_client.py` caller list were both incomplete and have
been corrected. Modules added since the first audit —
`reverse_autotester.py`, `circuit_audit.py`, `ixc_stability.py`,
`ds1_util.py`, `chassis_config.py`, `assurance.py` — were traced the same way
and every command each sends was read at its source:

| Module | Commands it sends |
|---|---|
| `reverse_autotester.py` | `show run \| b controller T3`, `show controllers t3 <id> \| inc [Dd]escription`, `show xconnect all \| i CE<x>:`, `show acr group`, `show configuration interfaces <sub> \| display-set \| no-more`. Plus `SHOW T1/T3` via `gsx_client`, and `site_sweep._discover_ds1s()` on the 1250. All reads. |
| `circuit_audit.py` | `show interfaces description \| no-more`, `show ospf neighbor \| no-more`. Reads. |
| `ixc_stability.py` | `show configuration \| display-set \| grep ds1 ...`, `show interfaces physical status slot <n> \| no-more`. Reads. |
| `ds1_util.py`, `chassis_config.py`, `assurance.py` | **No device session at all** — pure parsers and helpers. They take text that another module already retrieved. |

Verify there is no executed write anywhere:

```
grep -n "\.run(\s*\"\(set\|configure\|commit\|delete\|clear\|no \)" *.py
```

That returns nothing. It is a blunt check — it would miss a command built
dynamically — which is why the per-call-site trace below is the real
assurance, not this grep.

Originally audited 2026-09-11 by tracing every call site that sends a command over an
open device session (`.run(...)` on an `NPT1250Client` / `NPT1800Client` /
`MUX4216Client` / `GSXClient` instance, and every `_capture(...)` /
`sec(...)` wrapper that calls `.run()` internally) across the full codebase.
For each one, the actual command string was read at its source — not
assumed from a function or variable name — including cases built dynamically
via `.format()`, to catch anything that might only reveal itself with real
input.

**Verdict, corrected 2026-10-01: there is exactly ONE command this suite can
execute that is not read-only.** It is opt-in, default off, and resets
performance-monitor counters on a GSX T1 — see **EXCEPTION 4**. Everything
else any tool executes is a `show`/`SHOW` query, a `ping`, or a session-only
display setting (`terminal length 0`, `| no-more` paging) that affects only
the current CLI session and never saved device configuration.

> ⚠ **This document previously stated that command was "never sent live".
> That was false**, and had been since `run_gsx_monitor()` gained its
> `clear_first` option. The function's own docstring documented the write
> honestly the whole time — *"This is the one real config-write path in this
> function"* — so the code was right and this audit was wrong. It was found
> on 2026-10-01 by re-reading every `.run()` / `run_command()` call site
> rather than trusting this file's summary.
>
> The grep in Methodology below would never have caught it: the command is
> passed as a **variable** (`g.run_command(clear_cmd)`), so a pattern looking
> for a literal string after `.run(` returns nothing and reads as a pass.
> That is why the call-site trace is the assurance and the grep is only a
> cross-check.

**Three** places are exceptions **in what they print, not what they run**:
they generate the literal text of intrusive commands as part of a copy/paste
runbook for a NOC engineer to execute and reverse by hand. None of them ever
passes that generated text back into a `.run()` call or any other execution
path — this was traced and confirmed line by line, not assumed from the
tool's stated intent.

| # | Where | Prints |
|---|---|---|
| 1 | `ds1_gsx_test.py` | AIS injection / loops toward the GSX, with its back-out |
| 2 | `mux_loop_script.py` | MUX CEM loop MOP, with the restore step |
| 3 | `output_builders.build_ces_enable_mop()` | Enabling CES PMs on a 1250 |

**A fourth place EXECUTES one** — `ds1_gsx_test.run_gsx_monitor()`, opt-in. See
**EXCEPTION 4**. It is listed separately because it is a different kind of
thing: the three above are text a human runs, the fourth is a command the tool
sends.

All four are documented in full below. **If you add a fifth, add it to a table
here and give it its own section** — a reader counting section headings has to
arrive at the same number as this summary. That had already drifted once:
the text said "three places" while only two numbered sections existed.

---

### EXCEPTION 3 — `output_builders.build_ces_enable_mop()`

Added 2026-10-01. When a DS1 reports `CES PMs not enabled`, the diagnostic
summary appends a MOP for enabling them. The function returns a STRING and has
exactly one caller (`build_diagnostic_summary`), which appends it to the report
text; there is no code path from it to a device. Verify with:

```
grep -n "build_ces_enable_mop" *.py
```

Two call sites only: the definition and the one append. The commands it
prints are:

```
configure
set interfaces ds1-<ds1> unit 0 pm-monitor-enable
set interfaces ds1-<ds1> unit 0 pm-collection-enable
commit confirm 5
<verify connectivity is ok>
commit
```

`commit confirm 5` auto-reverts after five minutes unless the bare `commit`
follows, and the `<verify connectivity is ok>` line is a deliberate human gate
between them. Both are load-bearing: do not "tidy" the block into six
paste-in-one-go lines.

---

## EXCEPTION 4 — `ds1_gsx_test.run_gsx_monitor()` EXECUTES a counter reset

**This is the only non-read-only command any tool in this suite sends.**

```python
# ds1_gsx_test.py, run_gsx_monitor()
if clear_first:
    clear_cmd = "CONFIGURE T1 {} PERFMON RESET COUNTERS ALL".format(t1_id)
    out = g.run_command(clear_cmd)
```

| | |
|---|---|
| **What it does** | Zeroes the performance-monitor counters on one GSX T1, so a test window's results are not polluted by whatever had already accumulated. |
| **When** | Only when `params["clear_first"]` is truthy. **Default off.** |
| **How it is reached** | The web form's `clear_first` checkbox (`templates/ds1_gsx_test.html`) → `app.py` → `run_gsx_monitor()`. Unticked, the function is `SHOW T1 ... STATISTICS` polling only. |
| **Blast radius** | The counters on that one T1. It does **not** affect traffic, service, or saved configuration, and nothing is committed. |
| **Reversible?** | **No.** Counters cleared are gone; there is no "unreset". The historical PM data for that T1 is destroyed. |
| **Failure handling** | A failed clear is reported and the monitor continues, with an explicit warning that the PMs below still include pre-existing counts. It does not silently proceed as though the clear worked. |

### Why this is a genuine exception and not a loophole

It is a `CONFIGURE` verb on a production voice gateway. Judged by what it
*does*, it is the mildest possible write — no service impact, no config
persistence. Judged by what it *destroys*, it is irreversible: a circuit's
accumulated PM history is evidence, and someone investigating an intermittent
fault may want the counters this clears.

**Anyone building an automated front end must treat `clear_first` as a
destructive option.** It must not be defaulted on, and it must not be set
without the operator having chosen it for that run. An unattended poller that
sets it on every pass would quietly erase PM history across the fleet.

### If the read-only guarantee must be absolute

Then `clear_first` has to go — or move to the printed-runbook pattern of
Exceptions 1–3, where STEP 2 of the generated test script already carries the
same command for manual execution. That is a product decision, not an audit
one. **What is not acceptable is the state this document was in until
2026-10-01: the option shipped, wired to a checkbox, while the audit said it
was never sent.**

---

## Scripts with NO intrusive commands (read-only only)

Every command sent is `show`/`SHOW`/`ping`/session-display-only. No `set`,
`delete`, `configure`, `commit`, `clear`, `shutdown`, `reload`, `erase`, or
counter-reset command is ever executed by any of these.

**`ds1_gsx_test.py` is listed in this table with a caveat, not without one** —
its `run_gsx_monitor()` has the opt-in write covered by EXCEPTION 4. Every
other code path in it, and every other module below, holds to the sentence
above.

| Script | Ran from | What it does |
|---|---|---|
| `autotester_standalone.py` | orchestrator | Full T1 route trace w/ PMs across 1250→1800→MUX→GSX |
| `npt1250_discovery.py` | 1250 | Exhaustive read-only device dump ("show tech") |
| `ds1_pms.py` | 1250 | Per-DS1 PM check |
| `fiber_util.py` | 1250 | GE fiber uplink utilization (bps PMs) |
| `health_check.py` | 1250 | High-level 1250 health snapshot |
| `site_sweep.py` | 1250 | Auto-discovers and checks every DS1 on a 1250 in one pass |
| `ospf_events.py` | 1250 | OSPF/LDP event-log + fiber light-level check |
| `interface_check.py` (IXC Check) | 1800 | NNI sub-interface config/OSPF/LDP/ping check |
| `nni_check.py` | 1800 (library, called by IXC Check) | Blast-radius / port-level OSPF check + traffic-passing check |
| `ospf_stability.py` | 1800 (standalone monitor) | Fleet-wide OSPF/LDP adjacency stability + drop-history tracking |
| `gsx_perfmon.py` | GSX | Deep GSX T1 perfmon pull (STATUS/STATISTICS/PERFMON, all 96 15-min intervals) |
| `morning_check.py` | orchestrator | Batch-runs the above tools against open JIRA tickets; posts results |
| `npt1250_client.py` | shared library | Transport + command set for all 1250 pulls above |
| `npt1800_client.py` | shared library | Transport + command set for all 1800 pulls above |
| `mux4216_client.py` | shared library | Transport + command set for all MUX pulls above — see note below |
| `gsx_client.py` | shared library | plink-based GSX transport — see note below |
| `device_base.py` | shared library | SSH/paramiko plumbing only; builds no commands itself |
| `reverse_autotester.py` | orchestrator | GSX → MUX → 1800 → 1250 reverse trace. Sends device commands directly (see Methodology) and, since v2.35, also calls `autotester_standalone.run()` — so it inherits that tool's command set too, which is itself read-only |
| `circuit_audit.py` | 1800 (CLI-only) | Configured-sub-interface audit across the four hubs |
| `ixc_stability.py` | 1800 | **BETA — not for production use**, per its own `TOOL_INFO`. Read-only regardless |
| `ds1_gsx_test.py` | 1250/GSX | **See EXCEPTIONS 1 and 4.** It *prints* intrusive runbook text, and `run_gsx_monitor()` *executes* a PM counter reset when the opt-in `clear_first` is set. All its other device calls are read-only |
| `mux_loop_script.py` | MUX | **See EXCEPTION 2** — same: read-only calls, intrusive text printed |
| `output_builders.py` | no device session | Report rendering. **See EXCEPTION 3** — the only module outside the two Exception tools that emits intrusive text |
| `ds1_util.py`, `chassis_config.py`, `assurance.py` | no device session | Pure parsers/helpers. Receive text another module retrieved; open no session and build no commands |
| `hub_registry.py`, `netbox_client.py`, `jira_client.py`, `secure_config.py`, `platform_paths.py` | no device session | Lookups, API clients, credential store, output paths. No device commands |

**Note on `mux4216_client.py`**: this file's `query_batch()` reads MUX
running-config with `show run | include ...` / `show run | section ...` —
these are **reads** of the running configuration (Cisco IOS-style `show
running-config`, filtered), not writes to it. No `configure terminal` is
ever entered by this file.

**Note on `gsx_client.py`**: only ever sends whatever `SHOW ...` command
string it's given by a caller. The callers, complete as of v2.35:
`gsx_perfmon.py`, the GSX hop inside `autotester_standalone.py`,
`mux_loop_script.py` and `ds1_gsx_test.py`'s precheck sections, and —
**added 2026-10-01** — `reverse_autotester.py`, which starts *from* a GSX T1
and so was the first caller to drive this client as its entry point rather
than its last hop.

**Added 2026-11-02:** `loop_check.LOOP_CONFIG_CMD` = `show configuration | display-set | match "maintenance loopback" | no-more`, run by the autotester's 1250 hop and `ds1_pms` to detect a configured TDM loopback. A **read**. The GSX and MUX loop detectors parse output the suite already pulls (`SHOW T1 ... STATUS`, `show connection name`), no new command.

**Added 2026-11-02:** `gsx_client.pull_isup_circuit()` sends
`SHOW ISUP CIRCUIT SHELF 1 SLOT <s> PORT <p> DS1 <d> status` and
`show trunk group <name> admin` — both **reads**, called from the forward
autotester, `reverse_autotester` and `gsx_perfmon` GSX hops for information
only (mgmt/Barlow). The first prompts
`continue? - Confirm (y/n):`, answered `y` automatically; the confirm is
about the command's runtime, not about changing state.

**One of those callers DOES hand it a destructive command.**
`ds1_gsx_test.run_gsx_monitor()` sends
`CONFIGURE T1 ... PERFMON RESET COUNTERS ALL` when its opt-in `clear_first`
option is set — see **EXCEPTION 4**. Every other caller, and that same
function with `clear_first` off, issues `SHOW` only.

`gsx_client.py` itself is a transport: it sends whatever string it is given
and neither validates nor restricts the verb. **It is not a safety boundary**
— the guarantee lives in its callers, which is precisely why each one has to
be traced individually rather than inferred from this client.

Two additions to this client in v2.27 are worth noting here because they look
like they might touch state and do not: `node_from_prompt()` reads the node
name out of the **login banner already in the buffer**, and
`node_from_output()` reads the `Node:` header off command output **already
retrieved**. Neither sends anything. The client's `hostname` attribute is
populated from the banner, not from a query.

---

## EXCEPTION 1 — `ds1_gsx_test.py` (DS1 Test Builder)

**What it does**: builds a copy/paste-ready test script for running AIS
injection or maintenance loopback tests toward the GSX, with live prechecks,
TEST ON commands, monitoring commands, TEST OFF / back-out commands, and
postchecks. Every chassis token (slot/ces-card/port/sts-1/vt-15) is extracted
from the device's own config render and stamped VERIFIED or UNVERIFIED — the
tool never guesses a token.

**Intrusive commands generated (as text only — see verdict above):**

| Command pattern | Why |
|---|---|
| `set chassis slot <ts> ces-card <card> port ds1/<n> ds1-path force-ais-psn-bound` / `force-defect-tdm-bound ais` | Injects AIS toward the GSX or toward the LEC — the actual test action the NOC engineer runs to verify fault isolation/alarm propagation on a DS1 |
| `delete chassis slot <ts> ces-card <card> port ds1/<n> ds1-path ...` | Reverses the AIS injection above — the TEST OFF / back-out step, generated alongside every TEST ON so a test is never left half-applied |
| `set chassis slot ... maintenance loopback <direction>` | Applies a maintenance loopback for continuity/BER-style testing |
| `set chassis slot ... maintenance loopback release` | Releases the loopback above — again generated alongside the ON command |
| `CONFIGURE T1 <t1-id> PERFMON RESET COUNTERS ALL` | GSX counter-reset command #6 from the GSX command reference. Printed as STEP 2 of the generated script text (clearing PM counters before a test so results reflect only the test window) — **and ALSO executed for real** by `run_gsx_monitor()` when the opt-in `clear_first` option is set. **See EXCEPTION 4.** This row previously read "never sent live", which was false. |

Everything in this list exists in the *generated output file* the tool
writes for a human — never in a call the tool makes itself. The module's own
docstring states this directly: *"NOTHING config-touching is ever executed
by this tool — reads only. The generated commands are for the user to run
manually."* Confirmed by tracing every `build_1250_commands()` /
`build_1800_commands()` call site: their output only ever gets appended to
a report-text list (`s.append(...)` / `s += [...]`), never passed to `.run()`.

**Also worth flagging**: the module docstring notes the 1800 AIS commands are
`UNVERIFIED-SYNTAX per P. Capps` — flagged for review before first
production use, independent of the read-only/write question above.

---

## EXCEPTION 2 — `mux_loop_script.py` (MUX Loop Script)

**What it does**: generates a copy/paste runbook for standing up a test loop
on the MUX (looped facing the GSX, or facing the 1800) with live prechecks,
monitoring commands at each stage, and back-out steps to restore production
binding.

**Intrusive commands generated (as text only — see verdict above):**

| Command pattern | Why |
|---|---|
| `configure terminal` / `no <prod_connect>` / `<gsx_loop>` / `commit` / `end` | Removes the production cross-connect and applies a hairpin loop facing the GSX — the actual test-loop turn-up step |
| `clear cem circuit interface cem-acr <acr> <cem_group>` | Clears circuit counters so the engineer observes live status from a clean baseline immediately after the loop is applied |
| (equivalent `configure terminal` / `commit` blocks for the 1800-facing loop scenario and for restoring production) | Same rationale, other direction / the back-out step that restores the original production connection |

Same guarantee as Exception 1: traced every place these strings are built
(`lines.append(...)` / `lines += [...]`) and confirmed none of it is ever
passed to `.run()`. The tool's own live device calls (`_capture()`/`.run()`
call sites in this file) are 100% `show ...` / `terminal length 0` — see the
read-only table above.

---

## Change log for this document

- 2026-09-11 — Initial audit, created at pcapps's request following a
  service-account/automation trust question. Full trace of every `.run()`
  call site across the codebase.
- 2026-10-01 — Re-audited at v2.35. **The verdict CHANGED.** This file had
  claimed nothing intrusive is ever executed; `ds1_gsx_test.run_gsx_monitor()`
  executes `CONFIGURE T1 ... PERFMON RESET COUNTERS ALL` when its opt-in
  `clear_first` option is set, and that option is wired to a live web
  checkbox. Now documented as **EXCEPTION 4**, with the "never sent live" row
  corrected and the `gsx_client.py` caller guarantee rewritten.

  How it was missed, recorded because the method matters more than the fix:
  the previous audit reasoned from the `gsx_client.py` note ("only ever sends
  whatever `SHOW T1 ...` string it's given") outward to its callers, instead
  of reading each caller. `gsx_client.py` is a transport with no verb
  restriction, so it was never a safety boundary. The grep in Methodology
  also could not have caught it — the command is passed as a variable.

  Other corrections, these ones omissions rather than errors:
  - added `reverse_autotester.py`, `circuit_audit.py`, `ixc_stability.py`,
    `ds1_util.py`, `chassis_config.py`, `assurance.py`, `output_builders.py`
    and the no-session helper modules to the inventory. `reverse_autotester.py`
    was the material gap — it drives device sessions and was absent from a
    document whose value is being complete.
  - added `reverse_autotester.py` to the `gsx_client.py` caller list. The
    "none of those callers" guarantee had not covered the newest caller.
  - gave `build_ces_enable_mop()` its own `EXCEPTION 3` heading. The prose
    said "three places" while only two numbered sections existed, so anyone
    counting headings got a different answer from the summary.
  - recorded this entry. The 2026-10-01 `build_ces_enable_mop` addition had
    been made to the body without a change-log line — this file's own update
    rule, violated by the file itself.
