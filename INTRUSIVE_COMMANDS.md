# Curveball Tools — Intrusive Commands Audit

This document exists to answer one question, on demand, for anyone who asks
(a NOC manager, a security review board, a future maintainer): **does any
script in this suite send a command to a device that changes its
configuration or state?**

Ship this file in every revision, alongside the CHANGELOG. Update it in the
same pass as any change that adds a new device command, a new tool, or a new
call to a device client's `.run()` (or equivalent) method.

## Methodology

Audited 2026-09-11 by tracing every call site that sends a command over an
open device session (`.run(...)` on an `NPT1250Client` / `NPT1800Client` /
`MUX4216Client` / `GSXClient` instance, and every `_capture(...)` /
`sec(...)` wrapper that calls `.run()` internally) across the full codebase.
For each one, the actual command string was read at its source — not
assumed from a function or variable name — including cases built dynamically
via `.format()`, to catch anything that might only reveal itself with real
input.

**Verdict: every command any tool in this suite actually executes against a
device is read-only** — a `show`/`SHOW` query, a `ping`, or a session-only
display setting (`terminal length 0`, `| no-more` paging) that affects only
the current CLI session, never saved/persisted device configuration.

Two tools are exceptions **in what they print, not what they run**: they
generate the literal text of intrusive commands as part of a copy/paste
runbook for a NOC engineer to execute and reverse by hand. Neither tool ever
passes that generated text back into a `.run()` call or any other execution
path — this was traced and confirmed line by line, not assumed from the
tool's stated intent. Both are documented in full below.

---

## Scripts with NO intrusive commands (read-only only)

Every command sent is `show`/`SHOW`/`ping`/session-display-only. No `set`,
`delete`, `configure`, `commit`, `clear`, `shutdown`, `reload`, `erase`, or
counter-reset command is ever executed by any of these.

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

**Note on `mux4216_client.py`**: this file's `query_batch()` reads MUX
running-config with `show run | include ...` / `show run | section ...` —
these are **reads** of the running configuration (Cisco IOS-style `show
running-config`, filtered), not writes to it. No `configure terminal` is
ever entered by this file.

**Note on `gsx_client.py`**: only ever sends whatever `SHOW T1 ...` command
string it's given by a caller (`gsx_perfmon.py`, the GSX hop inside
`autotester_standalone.py`/`mux_loop_script.py`/`ds1_gsx_test.py`'s
precheck sections). None of those callers ever hand it the destructive
`CONFIGURE T1 ... PERFMON RESET COUNTERS ALL` command — see below.

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
| `CONFIGURE T1 <t1-id> PERFMON RESET COUNTERS ALL` | GSX counter-reset command #6 from the GSX command reference — printed as a REFERENCE step in the generated script text (clearing PM counters before a test so results reflect only the test window) — **never sent live**; the tool's own live GSX pull only ever issues `SHOW T1` commands (see `gsx_client.py` note above) |

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
