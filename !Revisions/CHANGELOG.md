# Curveball Tools — Changelog

---

## Deployment Status

Tracks which version is actually live on each host -- update this whenever a
zip is deployed (not just built). Prevents the "which copy is stale" drift
that's bitten this project before.

| Host                              | Version | Notes |
|------------------------------------|---------|-------|
| pcapps's local Windows machine     | v2.13   | Working folder -- always current, this is where dev happens |
| noe-util-01a.lab1.bwnet.us (Linux) | v1.96   | Confirmed working (NetBox/JIRA/Slack all verified live 2026-09-11) at `/usr/local/scripts/NOEIP/Curveball`, `curveball.service` (systemd), `CURVEBALL_HOME=/usr/local/scripts/NOEIP/Curveball` set via drop-in override. Hand-edit to `hub_registry.py` (LAB site) has been superseded by the real v1.96 deploy. Needs v1.97+ next -- v1.97 adds INTRUSIVE_COMMANDS.md (no functional change), v1.98 adds OSPF outage pairing, v1.99 splits IXC Check's outage summary into LDP + OSPF, v2.00 adds a 1250-side LDP source to ospf_stability.py, v2.01 adds an LDP Outages Excel tab + updates the tool card, v2.02 renames the tool to "OSPF & LDP Stability", v2.03 adds 24h/72h/7d/30d LDP drop-count columns, v2.04 collapses the LDP Outages sheet to one row per site, v2.05 fixes the outage timestamp format to include the day, v2.06 fixes a real false-"still down" bug on dual-homed sites, v2.07 closes two gaps in that fix, v2.08 fixes a naive-IP-substring-match bug that could cross-contaminate DIFFERENT sites' events + a one-time state migration, v2.09 adds the summary/pie-chart block to the LDP Outages sheet, v2.10 adds an "LDP Stability Alert" Slack message, v2.11 adds historical NNI-port correlation for chronic review + fixes the web UI showing raw binary for Excel-output tools, v2.12 disables the NNI-port correlation flag by default pending real-world confirmation (functional changes -- see below), v2.13 adds T3 status awareness to all GSX tools + fixes silent GSX-side Clean bug in the autotester run summary |

---

## v2.21 — 2026-10-01

### MUX resolution: hardcoded map first, then NetBox, exhausting every address

`LOC_TO_MUX` is a hardcoded four-entry dict. When it is stale or the box has
moved, the run used to stop at "MUX not found" (DFW1-MUX-04 / 10.243.1.17,
2026-10-01) even though NetBox already knew the answer: `oc_to_sonet()` returns
the OC's connected endpoint device, which IS the MUX.

The map stays the first choice -- it is fast and usually right. On failure the
run now falls through to NetBox and tries every management address it holds,
rather than giving up.

**`netbox_client.device_mgmt_ips(name)`** (new) -- candidate addresses for any
device in preference order: `primary_ip4` first, then every other IP assigned.
Generic on purpose; `gsx_mgmt_ips()` stays GSX-specific because it groups by
`MGMT-<shelf>-<slot>-<port>` naming, which a MUX has no equivalent of. Returns
`([], [reason])` on failure, never an empty list posing as "no addresses".

**`_mux_candidates()`** -- builds the ordered list, ONE ENTRY PER ADDRESS.
Where two sources name the same IP differently -- a stale name in the hardcoded
map against NetBox's current one -- the address is dialled once and BOTH names
are accepted, letting the device settle it. The first attempt keyed on
(name, ip) and dialled the same box twice; de-duping on IP alone would have
thrown away the alternative name and then refused a good connection for "wrong
hostname". A name disagreement is logged as `MUX NAME CONFLICT` and
deliberately not resolved in code.

**`_connect_mux()`** -- tries each address in turn and **verifies the
connection rather than trusting it**. `MUX4216Client` inherits
`_capture_hostname()` and its prompt ends in `#`, so the MUX reports its own
name. If the box that answers is not one we were aiming for, the session is
closed and the attempt recorded as refused. A wrong MUX is worse than no MUX:
it would produce a complete, plausible, wrong route — the LAXGSX04 failure at
hop scale. Same authority principle as `hub_registry`: the device gets the
final word on who it is.

Every attempt is recorded even on success, so a first choice that always fails
is visible instead of being silently worked around on every run.

**Where the fallback note goes, and why not the obvious place.** It cannot go
in `mux_errors_all`: `run_summary_line()` only reads that dict when the route
FAILED, so on a successful fallback the note would have been collected and
dropped — reporting into the void, the thing v2.20 had just fixed. It is
prepended to each DS1's `netbox_conflict` instead, an existing per-DS1 channel
`output_builders` already renders.

Verified without a network across six cases: NetBox agreeing on the name
(shared IP collapses to one dial), NetBox naming a different MUX (shared IP
accepts both names, conflict flagged), loc missing from the map (NetBox only),
no NetBox at all (hardcoded only), a wrong-name box refused with the right one
accepted on the next address, and every address dead with all attempts
recorded.

**`LOC_TO_MUX` is deliberately still first and still hardcoded.** It is not
authoritative any more, just preferred — the same posture `hub_registry` takes
with its fallback table, except inverted because here the hardcoded value is
the fast path and NetBox is the safety net.

---

## v2.20 — 2026-10-01

### MUX-stage failures now reach the report, and "Check Netbox" stops misdirecting

A MDLDTXMU542 run reported:

```
TS1/27  --  The test completed with errors  |  Route incomplete: MUX not found  |  Check Netbox
```

Three separate problems behind that one line.

**1. MUX-stage errors never reached the report at all.** `run()` built
`step1_errors_all = dict(step1_errors)` -- 1250-stage errors only. The `errs`
dict returned by `MUX4216Client.query_batch()` was printed via `progress()` and
dropped, and `progress()` is console-only by design (noted at the top of
`run()`). Both pre-`query_batch` `continue` paths -- no `LOC_TO_MUX` entry, and
MUX connect failure -- did the same. So the only persisted trace of any MUX
failure was "MUX not found", with the reason existing nowhere retrievable.
`output_builders` has had a per-DS1 `COLLECTION FAILED:` renderer the whole
time; it simply never received anything from the MUX stage.

Now collected into `mux_errors_all` with the reason stated at the point of
failure, including the MUX name, IP, exception, and the loc key involved.

**2. "Check Netbox" was the wrong advice.** The MUX is not resolved from
NetBox -- it comes from `LOC_TO_MUX`, a hardcoded four-entry dict in
`autotester_standalone.py`. An engineer whose MUX connect had timed out was
sent to look at NetBox, where there is nothing to find and no hint of the
actual cause. `run_summary_line()` now gained an `errors=` parameter and emits,
in order of preference: the recorded reason; else "Check Netbox" but ONLY when
something NetBox-sourced is actually missing (OC or hub IP); else "reason not
recorded -- see the console log".

```
|  MUX DFW1-MUX-04 (10.243.1.17) connect failed: TimeoutError: timed out
   -- address is from the hardcoded LOC_TO_MUX map for loc 'dfw', so verify
   the IP is still correct
```

**3. A regression caught before release, worth recording.** The obvious fix was
to merge `mux_errors_all` into `step1_errors_all`. That would have been wrong:
`build_diagnostic_summary()` treats any DS1 in that dict as a TOTAL collection
failure -- it prints `COLLECTION FAILED` and `continue`s, replacing the entire
per-DS1 diagnostic. Correct for a 1250-stage failure where nothing downstream
was collected; destructive for a MUX-stage one, where the 1250 and 1800 hops
succeeded and their optics, OSPF/LDP state and PM counters are real data. The
MDLDTXMU542 report had all of that, and merging the dicts would have deleted a
working report in order to explain a partial failure.

The two dicts are kept apart: MUX reasons go to `run_summary_line()` only.
Verified both ways -- a 1250-stage error still collapses to
`COLLECTION FAILED`, a MUX-stage error leaves the 34-line diagnostic intact.

**Not caused by v2.17.** Checked first, since v2.17 changed the ACR fallback in
this same area. Everything that changed then lives inside `query_batch()`,
which is never reached on either of these paths -- both `continue` before it.
Corroborated by the report itself: the v2.17 branch keeps `cem_group =
expected_cem`, so it would have shown `CEM 723` for `oc12-ts1/1:7:23`. The
report showed `CEM ?`, i.e. no `mux_results` entry at all.

**Carried forward:** `LOC_TO_MUX` is a hardcoded map with one MUX per hub site,
the same shape as the `HUB_IPS` table that had LAX circuits reported as DFW for
two months before `hub_registry` replaced it. Worth considering whether it
should be NetBox-backed with the same authority order. Not changed here -- a
wrong MUX address is worse than a missing one, so that needs its own
discussion.

---

## v2.19 — 2026-10-01

### interface_check.py: LIVE-TESTED at last, plus a VERDICT block

`interface_check.py` had been shipped and wired into the web UI since v1.86
with unit tests only. Tested live on ces01.jfk1 2026-10-01. **Everything that
was fixed blind at v1.86 works:**

- `| no-more` present on all five commands; `_cmd_failed()` reported nothing
- No SITE MISMATCH; the device confirmed `ces01.jfk1`
- Hub IP resolved from NetBox as `10.111.133.255` — the value corrected in
  v2.14, confirming that fix is live
- `nni_check` (v1.86, never previously exercised against a device) ran: derived
  the parent port, counted sub-interfaces, checked alarms, and pulled real
  traffic — 204829 pps in / 204815 pps out over 2.4 min
- Entering an IP in the interface field is rejected: *"that is an IP address,
  not an interface"*. That input used to parse as `nni='10', vlan='111'` and
  dump ~100 unrelated OSPF neighbours as findings

Independent corroboration worth noting: `nni_check` reported `xe-ts13/3: 17
sub-interface(s)`, and `circuit_audit.py` independently found exactly 17
customer sub-interfaces on that port. Two tools, different commands, same
number.

### The gap the live test exposed

`xe-ts13/3.624` was entered against ces01.jfk1, where that port runs .132
through .978 and has no .624. All five reads correctly returned nothing — and
the report was five `(no output)` blocks with **no conclusion drawn**. That
reads like a broken run, and it is indistinguishable from "the interface exists
but every command came back empty", which is an entirely different problem.

The tool already held both facts needed to settle it: every read empty *with no
command failures*, plus a parent port that is present and healthy. It just
never combined them. A new VERDICT block now does, with three outcomes:

- **Parent healthy, all reads empty, no failures** -> `NOT CONFIGURED on
  <host>`, naming the parent's sub-interface count and suggesting the VLAN may
  be wrong or the hub may be wrong
- **Parent not seen either** -> says the interface may be on a different hub OR
  the port may not exist, and explicitly that there is **not enough information
  to say which**. It does not pick one
- **All reads empty BUT a command failed** -> says absence of data proves
  nothing and to fix the failure before concluding anything. An empty result
  from a broken read is not evidence

No verdict block is emitted when any read returned data, so a normal run is
unchanged.

"Did I typo the VLAN or is the circuit broken" is the first question a NOC
engineer asks. The report now answers it instead of implying it.

---

## v2.18 — 2026-10-01

### CES PMs disabled: a copy/paste MOP, and removal of two half-MOPs

`output_builders.build_ces_enable_mop()` appends one MOP block at the end of
the diagnostic summary listing every DS1 that reported `CES PMs not enabled`.
Sequence supplied verbatim by pcapps:

```
configure
set interfaces ds1-<ds1> unit 0 pm-monitor-enable
set interfaces ds1-<ds1> unit 0 pm-collection-enable
commit confirm 5
<verify connectivity is ok>
commit
```

**Still read-only.** The function returns a string and has exactly two call
sites -- its definition and one append into the report text. There is no path
from it to a device. `INTRUSIVE_COMMANDS.md` updated in the same pass, with the
`grep -n "build_ces_enable_mop" *.py` command that verifies the claim.

Appended ONCE after the findings rather than beside each
`[ -- ] CES PMs disabled` line: that line is produced in three separate places,
and a `set` command sitting among the findings reads as part of the diagnosis,
which makes it likelier to be pasted unread.

`commit confirm 5` auto-reverts after five minutes unless the bare `commit`
follows, and the `<verify connectivity is ok>` line is a deliberate human gate
between them. Both are load-bearing and the docstring says not to tidy the
block into six paste-in-one-go lines.

**Two existing half-MOPs removed.** Found while verifying the audit claim:

```
npt1250_client.py:98       "To enable: set interfaces {} unit 0 pm-collection-enable"
npt1250_discovery.py:251   "To enable: set interfaces {} unit 0 pm-collection-enable"
```

Both printed only `pm-collection-enable` -- half the sequence, missing
`pm-monitor-enable`, with no `configure` and nothing to commit. Anyone
following either one would have got no PMs and no error to explain why. They
now point at the full MOP rather than restating a partial command, which also
keeps one copy of the sequence instead of three.

---

## v2.17 — 2026-10-01

### MUX: ACR/active-path fallback no longer guesses an ACR group

When the CEM group could not be located in any ACR group, `mux4216_client.py`
assigned `acr_groups[0]` -- whichever ACR happened to parse first off
`show acr group` -- and the route block printed that ACR number and its active
SONET path as though they had been determined:

```python
if not cem_group:
    cem_group    = expected_cem
    acr_num      = acr_groups[0]["acr"]      # <- a guess
    active_sonet = acr_groups[0]["active"]   # <- a guess
```

A confident wrong ACR and SONET path is the LAXGSX04 failure in a different
field, and the comment documenting that very lesson sits twenty lines below
this code. The NetBox-mismatch branch above it already does the right thing and
raises rather than guessing.

**Now:** `acr_num` and `active_sonet` are left `None` and the report says
`NONE FOUND` (pcapps' call -- mark it not found, do not fail the whole run).
`cem_group` keeps `expected_cem`, because that is arithmetic from the OC
assignment (STS + DS1), not a guess -- it is the value we were looking for.
A warning naming the CEM and MUX is added to `netbox_conflict`.

`NONE FOUND` rather than `?` deliberately: `?` is used across this report for
merely-absent fields, and this is a stronger statement -- we looked and did not
find it.

**Following the None through the rest of the suite**, which mattered more than
the fix itself:

- `mux_loop_script.py` interpolates both values straight into device commands
  (`show run controller SONET-ACR {}`, `show cem interface CEM {}`). With
  `None` it would have sent `SONET-ACR None` to a production MUX and then
  parsed whatever error came back. It now refuses with an explanatory message
  before connecting further.
- Four render sites used `.get("acr_group", "?")`. A positional default does
  NOT apply when the key exists with value `None`, so those would have printed
  the literal string `None`. Changed to `or "?"` in `output_builders.py` (x2),
  `autotester_standalone.py`, and `mux_loop_script.py` (x2).

Verified by rendering a route block both ways -- resolved shows
`Active Path: SONET 0/7/4 / ACR Group: 53`, undetermined shows
`Active Path: NONE FOUND / ACR Group: NONE FOUND` with the CEM still reported.

### Not a bug: ACR 53 on ATL1-MUX-02

Investigated and cleared. A TAMPFLXA952 run reported `ACR Group: 53` /
`Active Path: SONET 0/7/4`, which looked wrong against a `show acr group`
screenshot showing 0/7/4 under ACR 13. `show acr group | i 0/7/4` on the live
device returns ACR 53 (working 0/7/4, protect 0/12/4, currently active 0/7/4).
The report was correct; the screenshot was illustrative documentation, not that
box's current state. Recorded so nobody re-opens it.

---

## v2.16 — 2026-10-01

### `chassis_config.py` (new): read the far-end description off the 1800 itself

MUX identity has come from NetBox alone, via `netbox_client.oc_to_sonet()`.
That is a single source with no way to be contradicted: it can return a SONET
path, `"__AMBIGUOUS__"`, or `(None, None)` -- but never "NetBox and the device
disagree". The 1800's own chassis config names the far end, which makes a
second, device-sourced opinion available.

Format, from real ces01.atl1 output supplied 2026-10-01:

```
pcapps@ces01.atl1> show configuration | display-set | grep 3/1
set chassis slot ts13 data-card dhxe-4 port xe-ts13/1 description "bcr01.atl1 Te0/0/0/34"
```

`set chassis slot <slot> data-card <card> port <port> description "<text>"`,
where the text is `<far-end device> <far-end interface>`.

**Functions**

- `port_description_cmd(port)` -- builds the narrowed, `| no-more` command
- `parse_chassis_ports(raw)` -- `{port: description}`; ports with no
  description line are absent rather than blank
- `split_far_end(desc)` -- `('bcr01.atl1', 'Te0/0/0/34')`; the interface half
  is optional, and free text that is not name-shaped returns `(None, None)`
  instead of inventing a device
- `far_end_for_port(raw, port)` -- exact port match only. `xe-ts13/1` and
  `xe-ts13/10` are different ports; the unanchored-substring version of this
  mistake is what produced the LAXGSX04 wrong-circuit report
- `compare_with_netbox(device_far_end, netbox_mux)` -- AGREE / DISAGREE /
  DEVICE ONLY / NETBOX ONLY / NEITHER

The comparison ignores case and a trailing domain, because `LAX1-MUX-02` and
`lax1-mux-02.bwnet.us` are the same box written two ways and a false DISAGREE
would train people to ignore the check. **DISAGREE is deliberately not
resolved**: neither source is established as authoritative for this field, and
picking a winner would bury the only fact worth surfacing -- that two records
of one thing have diverged.

**⚠ Deliberately NOT wired into the autotester's per-DS1 path.**
`show configuration | display-set` takes ~90-120s to render a full 1800 config
(already documented in `npt1800_client.py`), and `| grep` does not avoid that --
the device renders everything before filtering. Calling this once per DS1 in a
28-DS1 run would add roughly an hour. It is built for a single targeted lookup
or a deliberate cross-check, and the cost is recorded in the module docstring
so nobody wires it into a loop by accident.

17 self-tests (`python chassis_config.py`), including the verbatim atl1 line,
the ts13/1-vs-ts13/10 collision, and a non-chassis `set interfaces ...
description` line that must not match.

### Release discipline

`ADDING_A_TOOL.md` gained a "Cutting a release" checklist. The fourth step is
new and easy to miss: the three most recent zips are tracked in git by explicit
negation in `.gitignore`, so each release means adding the new version and
dropping the oldest. Gitignore cannot express "keep the newest three". The
checklist ends with the `git check-ignore -v` command that catches the mistake,
because the unescaped-`!` version of this rule silently pushed every zip from
v1.0 to v2.14 earlier today.

---

## v2.15 — 2026-10-01

### GSX: `SHOW ... ADMIN` added to the T1 pulls (and the T3 pull in gsx_client)

`SHOW T3 <id> ADMIN` existed in exactly one place -- the T3 block of
`gsx_perfmon.py`. The T1 block never had it, and neither `pull_t1_stats()` nor
`pull_t3_stats()` in `gsx_client.py` (the pair the autotester uses) issued it at
all. Requested by pcapps 2026-10-01.

ADMIN reports the CONFIGURED state -- framing, line coding, loopback,
administrative up/down -- where STATUS reports the operational state. An
admin-down T1 and a faulted T1 look identical in STATUS alone, and the two
disagreeing is itself a finding.

Added directly after STATUS in all three places, matching the existing T3
ordering:

- `gsx_perfmon.py` T1 standard commands -> `("ADMIN", "SHOW T1 {} ADMIN")`
- `gsx_client.pull_t3_stats()` -> `("ADMIN", "SHOW T3 {} ADMIN")`
- `gsx_client.pull_t1_stats()` -> `("ADMIN", "SHOW T1 {} ADMIN")`

**Checked before committing, because this inserts into a list the autotester
verdict logic consumes:**

- Both consumers unpack by destructuring (`for (label, cmd, out) in stats`),
  not by position, so inserting at index 1 shifts nothing.
- `gsx_pm_results` concatenates every command's output into one blob that
  `build_diagnostic_summary()` regex-searches. `Alarm Status:` uses
  `re.search`, and STATUS still runs before ADMIN, so the first match remains
  STATUS's value.
- `_check_gsx_counters()` scans only the regions `_gsx_split_current_perf()`
  carves out for PERFMON CURRENT FIFTEENMIN / CURRENT DAY. ADMIN output falls
  outside both and is never scanned for counters, so it cannot manufacture a
  false non-zero hit.

That last point mattered: items 4 and 10 on the bug list are *already* about
the autotester summary reaching a wrong verdict, so feeding a new input into
that logic without checking it would have been careless.

`SHOW T1 <id> ADMIN` has not been run live. If it turns out not to be valid
syntax it will land as `[command error: ...]` in its own report section --
`run_command()` failures are already caught per-command -- and nothing else
is affected.

---

## v2.14 — 2026-10-01

### DS1 card expansion, GSX SSH failover, and skipping 96 pulls of zeros

Three independent fixes from the 2026-10-01 bug list, plus a new `circuit_audit`
column. Nothing in this release changes what is sent to a device except the
GSX login sequence; all commands remain read-only.

**`ds1_util.py` (new)**

A bare card name now means every DS1 on it: `ts1` -> `ts1/1 .. ts1/28`,
`ts1, ts3, ts5, ts8` -> 112 DS1s. Mixed forms work (`ts1, ts3/4` -> 29) and a
card plus one of its own ports does not duplicate that port. Every DS1 is
returned whether or not it is provisioned — checking all of them is the point,
and an unprovisioned port is a finding rather than something to pre-filter.
The operator gets a note naming what expanded before a one-word input turns
into 28 device logins.

Put in a shared module because five call sites were parsing DS1 input with the
same inline one-liner (`autotester_standalone`, `app.py` x3, `ds1_gsx_test`).
Adding expansion to each would have been five copies of one rule — the
drift-by-accretion shape that had LAX circuits reported as DFW for two months.
All five now call `expand_ds1_list()`. 15 self-tests (`python ds1_util.py`).

**`gsx_client.py` — `connect_first_live()`**

`find_live_ip()` only pings, and a GSX can answer ICMP while refusing SSH.
Observed on ATLGSX02 (192.168.40.5): ping fine, plink died at login with
`FATAL ERROR: Network error: Connection refused`, and because the old path
committed to the first *pingable* address, every DS1 on that card failed with
the same error instead of trying the next management IP.

`connect_first_live()` walks the NetBox mgmt IPs and returns the first that
both pings **and** accepts SSH, handing back the already-open client so the
caller does not log in twice. Failed attempts are closed. Nothing is cached to
disk — trial and error every run, by request, so no address list can go stale.
Each skip is logged, so an address that always refuses SSH stays visible
rather than being silently routed around. `find_live_ip()` is kept, with a
docstring saying it is ping-only, for the callers that just need an address to
print in a runbook.

Wired into `autotester_standalone.py`; the existing `finally: g.close()` is
unchanged.

**`gsx_perfmon.py` — skip the 96 interval pulls when the 24h bin is clean**

If the 24-hour bin is clean there is no value in 96 round trips returning
zeros. Both the T3 and T1 blocks now check `PERFMON CURRENT DAY` first and, if
clean, write a short "intervals skipped" section instead of pulling them.

The first attempt at this was wrong and is worth recording: it looked for
abbreviated counter names (`CV` / `ES` / `SES` / `UAS`). The real output spells
them out in full — `Code Violations:`, `Unavailable Seconds:` — so the pattern
matched zero counters and the skip could never have fired. It would have sat in
the "could not determine" branch forever: dead code that looks like a feature.
Fixed against verbatim output captured from ATLGSX02 T1-1-7-1-16, which is now
the in-tree fixture.

The check fails toward work, deliberately. It skips only when at least three
numeric counters are found and every one is zero. `N/A` rows are ignored rather
than read as zero — a parameter the device does not measure is not evidence of
health. Unparsed output, a command error, or an all-`N/A` bin pulls every
interval exactly as before and prints why. `Elapsed Time: 4574` is anchored out
so it cannot be mistaken for a counter. 10 self-tests
(`python gsx_perfmon.py --self-test`).

**`circuit_audit.py` / `netbox_client.py` — EVC circuit IDs**

`EVC Circuit ID` column added to the AUDIT and CIRCUIT LIST sheets, showing the
NetBox interface description verbatim. No parsing: two formats were already in
the wild (`Zayo: ETYX/389224//ZYO` on WHPLNYWP132, `ZAYO - ETYX/420953//ZYO` on
DIXNILXA364) and every parse rule added its own gap, where anything unmatched
came out blank and read as "no circuit" rather than "could not read".

Picking the right circuit per row is done by identity, not inference, in this
order: the hub's OSPF neighbour address matched against the 1250's NetBox
interface IP (needs no device login, and resolves same-carrier pairs such as
FTMYFLXA939's two Zayo legs); then the 1250's own hub-facing leg via link
addressing; then the carrier named in the description, but only when exactly
one description mentions it. Failing all of those it shows every description,
labelled and flagged, rather than choosing one — a plausible wrong circuit ID
in front of a NOC engineer is the LAXGSX04 failure in a different column.

`netbox_client.device_interface_descriptions()` now also fetches interface IPs
from IPAM, which is what makes the first match possible.

Also: the 1250's own prompt is now authoritative for its hostname
(`pcapps@CLEVOH62320>` -> `CLEVOH62320`), replacing the CLLI+VLAN derivation,
which is demonstrably wrong for some sites. The derivation remains as a
fallback for unreachable devices and those cells are marked unverified. Where
the two disagree the device wins and the disagreement is reported — that list
is the map of where the naming convention breaks.

**Known gaps carried forward**

- Autotester summary reporting `completed successfully` alongside
  `CES Disabled`, and DS3-only PMs — not yet diagnosed, needs full run output.
- `set interfaces ds1-ts3/4 unit 0 pm-monitor-enable` deliberately NOT added to
  any script. It is a configuration command and every tool in this suite is
  read-only; `INTRUSIVE_COMMANDS.md` asserts that. Pending a decision on
  runbook-text versus documentation only.
- Reverse autotester (GSX -> 1250) and the SoR site remain unscoped.

---

## v2.13 — 2026-09-17

### GSX: T3 status awareness + DS1 vs. T3 outage distinction across all tools

**Problem:** The autotester run summary was reporting `GSX-side Clean` even when
the GSX T1 was hard down with `Operational Status: DOWN` / `Alarm Status:
AISFAILURE`. Root cause: `gsx_pm_results` (the flattened GSX T1 data blob) was
being built *after* `run_summary_line` was called, so the summary never had
access to GSX T1 status fields — it could only check MUX-side T3 PM counters,
which were within threshold even though the T1 was dead.

**Second problem:** Even with that fixed, the summary couldn't distinguish between
a DS1-specific outage (`T1-DOWN`) and a T3-level outage that takes all 28 T1s on
a port down together (`T3-DOWN`). These need different ticket responses — one T3
ticket, not 28 DS1 tickets.

**Changes:**

`gsx_client.py`
- Added `get_t3_status(slot, port)` — pulls `SHOW T3 T3-1-{slot}-{port} STATUS`
  from the GSX; used by the autotester to check parent T3 health per DS1.
- Added `pull_t3_stats(slot, port)` — full T3 command set (STATUS, ADMIN,
  STATISTICS, PERFMON CURRENT DAY, PERFMON THRESHOLDS, 96 fifteen-minute
  intervals) for use by `gsx_perfmon.py`.

`gsx_perfmon.py`
- Now collects T3 data (STATUS, ADMIN, STATISTICS, PERFMON CURRENT DAY,
  PERFMON THRESHOLDS, 96 intervals) in addition to the existing T1 data. T3
  ID is derived automatically from the T1 ID the user already entered
  (`T1-1-7-2-2` → `T3-1-7-2`); no extra input needed.
- Report header now shows both T3 and T1 IDs.
- JIRA summary now includes T3 Operational Status and Alarm Status, prefixed
  `T3 ...` / `T1 ...` for clarity (e.g. `T1 Operational Status: DOWN-DS1ALARM`
  instead of the raw unprefixed line).

`autotester_standalone.py`
- Moved `gsx_pm_results` construction to before the run summary block (was after
  it -- the root cause of the silent-clean bug).
- GSX collection loop now also calls `get_t3_status()` per DS1 and stores results
  in `gsx_t3_results`.
- Both `run_summary_line` and `build_diagnostic_summary` now receive
  `gsx_t3_results`.

`output_builders.py` — `run_summary_line`
- GSX-side verdict now checks in priority order:
  1. T3 `Operational Status: DOWN` → `Dirty (T3-DOWN)` (root cause; all T1s
     on that port affected by same failure — one ticket)
  2. T1 `Operational Status: DOWN` → `Dirty (T1-DOWN)` (DS1-specific)
  3. T1 alarm status (non-NONE) → `Dirty ({alarm})` e.g. `Dirty (AISFAILURE)`
  4. MUX-side T3 PM counters → `Dirty ({counter})`
  5. All clear → `Clean`

`output_builders.py` — `build_diagnostic_summary`
- Added `gsx_t3_results` parameter.
- GSX section now shows T3 DOWN with a note ("root cause; all T1s on this port
  affected") and labels the T1 DOWN line as "caused by T3 above" when the T3 is
  also down, so the tech immediately knows the scope of the failure.

`ds1_gsx_test.py`
- Precheck section now runs `SHOW T3 STATUS` before the T1 commands, giving
  the tech T3 health context before starting the test.

`mux_loop_script.py`
- Both the live and commands-only precheck paths now include `SHOW T3 STATUS`
  with an explanatory note. T3 ID derived from the GSX slot/port already in scope.

---

## v2.12 — 2026-09-14

### LDP port correlation: disabled by default pending confirmation against a real NNI outage

pcapps reviewed v2.11's "Any Port-Correlated Drop?" flags and asked how a
"YES" was actually reached, since the column doesn't show which sibling
site each flag paired with. After explaining the mechanism (cross-site
timing correlation, not a real port-down log -- see v2.11's notes), pcapps's
call: "keep the tooling written somewhere but disable it until we get
another NNI outage and I can be certain. false flagging is the worst."

Added `LDP_PORT_CORR_ENABLED = False` as a single kill switch in
`ospf_stability.py`. All the v2.11 correlation code is untouched and still
runs its own internal logic correctly (verified again this version) --
this just gates whether `run()` ever calls it:

- While disabled: `correlate_ldp_port_events()` is never invoked, so no
  outage is ever marked `port_correlated`, and the text report's
  "HISTORICAL PORT-CORRELATED LDP DROPS" section never appears.
- The Excel column header reads "Any Port-Correlated Drop? (disabled)" and
  every row reads "n/a (disabled)" -- deliberately NOT "no", so a disabled
  check can never be misread as "confirmed not correlated" (worse than no
  flag at all, per pcapps's concern about false flagging).
- Legend sheet and the tool's own description both call out that this is
  built but held off pending real-world confirmation.

To re-enable: flip `LDP_PORT_CORR_ENABLED` to `True` once a real NNI outage
happens and pcapps can confirm the flagged sites actually match what went
down. No other code changes needed.

Verified: with the flag off, a synthetic 2-site same-port/close-in-time
fixture (the same one v2.11 confirmed WOULD correlate) produces no
"[PORT EVENT?]" text, no rollup section, and "n/a (disabled)" on every
Excel row.

---

## v2.11 — 2026-09-14

### LDP Outages: flag drops that correlate in time with a sibling site on the same physical NNI port (chronic-ticket review)

pcapps asked: for chronic/problem review, is there a way to check the 1800
logs for the NNI dropping in the same timeframe as the sub-int/EVC, so
repeat review time isn't wasted re-solving what's actually one upstream
fault. Checked the hubs directly -- confirmed there's no protocol running
between the 1800 and the IXC across the NNI itself, and no example of a
port-level Down/Up line anywhere in the event log. So there is no device log
of the physical port's own history to read.

Instead of a port log, this correlates ACROSS sites: every site's own LDP
outage history is already trustworthy (`pair_ospf_outages_by_peer`, hardened
in v2.06-v2.08), and every site's physical port is already known (the same
`hub_state[hub]["interface"]` data `port_rollup()` uses for the live
blast-radius check). If two DIFFERENT sites sharing a physical port both
dropped within 5 minutes of each other (pcapps's call -- tight window,
minimize coincidental false positives), that's treated as one correlated
port event rather than two unrelated circuit faults.

Added `output_builders.pair_ospf_outages_by_peer()`: now tags each returned
outage with `"peer"` (the hub loopback IP its group was keyed on) -- a
one-line, backward-compatible addition, since every event in a peer group
already shares that value.

Added to `ospf_stability.py`:
- `correlate_ldp_port_events()` -- buckets every site's LDP outages by
  (hub, physical port) using the outage's `peer` field, clusters them by
  time, and marks `outage["port_correlated"] = True` on any cluster
  spanning 2+ distinct sites within the 5-minute window. Dual-homed sites
  whose outage can't be attributed to one leg (missing/legacy peer tag) are
  left out of correlation rather than guessed onto either port.
- `_format_ldp_outages_with_flags()` -- same rendering as the shared
  `format_ospf_outages()`, plus a `[PORT EVENT?]` suffix on flagged Down
  lines. Kept local since `interface_check.py`/`ospf_events.py` don't need
  port correlation.
- Text report: new "HISTORICAL PORT-CORRELATED LDP DROPS" section listing
  every correlated cluster (hub, port, sites, timestamps).
- LDP Outages Excel sheet: new "Any Port-Correlated Drop?" column
  (YES/no), amber-filled when YES and the site isn't currently down (red
  still wins for an ongoing outage). Legend sheet documents both the new
  column and that the LDP sheet's Red/Amber now mean something different
  from the main sheet's.

LIMITATION, documented in the code and the legend: this is inferred from
timing, not a direct port-down event -- a "no" on a dual-homed site whose
outage couldn't be attributed to one leg means unproven, not clean.

Verified with a synthetic 4-site fixture: two sites sharing a port 2 minutes
apart correctly flagged as correlated; a third site on the same port but 40
minutes later, and a fourth site entirely on a different port, both
correctly left unflagged.

### Web UI: fixed the report panel showing raw binary for Excel-output tools

pcapps flagged (screenshot): the OSPF & LDP Stability page's report panel
showed unreadable garbage text with a "Download .txt" link, for a tool that
actually outputs an .xlsx workbook. Root cause: `app.py`'s generic
`_run_job()` opened every tool's out_path with `open(path, "r")` regardless
of file type -- for a binary .xlsx this decodes the raw zip/XML bytes as
text and both displays and downloads-as garbage-labeled content.

Fixed for any tool whose out_path is one of `.xlsx/.xls/.docx/.pdf/.zip`:
`_run_job()` no longer reads the binary file as text -- it reuses the
readable text already streamed to the live log via `progress()`/`print()`
during the run (piped through the existing `_JobStdoutTee`), so the report
panel shows the same readable report a user watched scroll by live. Added
`download_ext` to the job-status API response; `job.js` now sets the
download link's label to the real extension (`Download .xlsx` for OSPF &
LDP Stability, `Download .txt` for every other tool) instead of a hardcoded,
sometimes-wrong "Download .txt".

Scoped to Excel-output pages only, per pcapps -- every other tool's out_path
is still a plain .txt report, read and displayed exactly as before.

---

## v2.10 — 2026-09-14

### Slack: added a separate "LDP Stability Alert" alongside the existing OSPF alert

pcapps asked: "on the Slack output, include a 'LDP Stability Alert -- xxxx-xx-xx
xx:xx' section." The existing Slack alert only fired on new OSPF drop events
(`new_events_by_rid`, populated by `merge_drop_events`'s "added" count). LDP's
event-map merge in `_poll_hub` / `_sweep_1250` had no equivalent "was this
genuinely new this run" tracking -- it just merged into `ldp_event_map` /
`ldp_event_map_1250` unconditionally.

Added the same fresh-event detection LDP needs: both `_poll_hub` and
`_sweep_1250` now compute `set(new_events) - set(prior_map)` BEFORE merging,
and record any fresh Down (not Up -- a recovery isn't alert-worthy) into a new
`new_ldp_events_by_rid` dict, threaded through `_run_sweep` and the hub-poll
thread launch in `run()` the same way `new_events_by_rid` already is.

`_aggregate_results` now accepts `new_ldp_events_by_rid` and exposes
`new_ldp_down_events` on each result row (site-level, so identical across a
dual-homed site's per-leg rows -- same pattern as `ldp_outages`).

Added `_build_ldp_slack_alert(new_ldp_drop_results, stamp)`, mirroring
`_build_slack_alert`'s format but headed `"*LDP Stability Alert* -- {stamp}"`.
`run()`'s Slack block now dedups `new_ldp_down_events` by router-id (a
dual-homed site's two leg-rows carry the same site-level list -- counting
both would double the reported drop count), and sends the LDP alert as its
own Slack message, separate from the OSPF alert, only when there's something
new to report. Each alert (OSPF, LDP) is now announced independently in the
progress log too, instead of one combined "no new drops" line.

Verified with a synthetic 3-row result set (a dual-homed site with 1 new LDP
drop duplicated across its 2 hub-leg rows, plus an unrelated site with no new
LDP drops): the dedup step correctly produced exactly one alert line for the
dual-homed site reading "1 new LDP drop(s)", not two, and the unrelated site
did not appear.

---

## v2.09 — 2026-09-14

### LDP Outages sheet: added the same Summary + pie-chart block the main sheet has

pcapps confirmed v2.08's fix resolved the dual-homed/cross-site bugs, then
asked for the main "OSPF Stability — Summary" section (description, run
stats, and four Stable/Issues pie charts for 24h/72h/7d/30d) to be added to
the "LDP Outages" sheet too.

Added a matching "LDP Session Outages — Summary" block: description of how
LDP outages are sourced (both the 1800 and the 1250) and why LDP is tracked
as the better outage indicator, a run-stats line (sites measured / sites
with any LDP outage / currently down), and the same four pie charts using
`ldp_drop_24h/72h/7d/30d` instead of the OSPF drop counts. "Stable" here
means zero LDP drops in that window -- computed over ALL measurable sites
(not just ones with a recorded outage), same population rule the main
sheet's own chart data already uses, deduped by router-id since `results`
carries one row per adjacency leg.

Verified against a 3-site mix (one always-stable, one with a drop 2 hours
ago, one with a drop 10 days ago): Stable/Issues counts came out exactly as
expected in all four windows, and all four charts render.

---

## v2.08 — 2026-09-14

### Bugfix: naive IP-substring matching could mix in a DIFFERENT site's events entirely

pcapps found this one too, from direct device experience: "is it because the
1800 gets multiple hits when filtering by the .5? `show ldp session | grep
.5` gives hits for .5, .50-.59." Correct -- and our Python-side filtering had
the identical bug, one level worse than the v2.06/v2.07 dual-homed fix: not
mixing two sessions of the SAME site, but mixing in a COMPLETELY DIFFERENT
site's events.

Root cause: `check_down_history()` and `check_ospf_outage_events()` filtered
lines with a plain `peer_ip in line` substring test. Filtering for
`10.111.130.5` also matched any line containing `10.111.130.50`-`.59`,
`110.111.130.5`, etc. -- any IP that has the target IP as a numeric prefix or
suffix. This matters most in `ospf_stability.py`'s `_poll_hub`, which filters
a HUB's shared, multi-site event log down to one site by its loopback -- a
false match there attributes a different site entirely's Down/Up event to
the wrong site's stored history.

Fix: new `output_builders._ip_in_line()` -- an exact-token match that
requires the IP not be immediately preceded or followed by another digit,
so `10.111.130.5` no longer matches `.50`-`.59` or a longer address it's
embedded in. Verified against the exact scenario: a log with events for both
`10.111.130.5` and `10.111.130.55` now correctly returns only the `.5`
events when filtering for `.5`.

**State migration**: unlike the missing-"peer"-field issue from v2.07 (which
self-heals per entry because a bad entry is structurally identifiable), a
contaminated `ldp_event_map` entry looks IDENTICAL to a correct one -- same
shape, real timestamp, just attributed to the wrong site. There's no way to
tell after storage which entries came from a different site's log line. Added
a one-time schema migration (`_migrate_state_schema`, tracked via a
`_schema_version` key in the state file) that clears `ldp_event_map` on
every site the first time this version runs, letting it rebuild from the
hub polls using the corrected exact-IP match. LDP full-log retention is
300+ days confirmed on the 1800 side, so nothing is permanently lost.

Confirmed this bug could NOT have affected `ldp_event_map_1250` (the
1250-side source added in v2.00) -- that call passes `peer_ip=None` since
it's already scoped to one device by the SSH session itself, so no IP
filtering happens there at all.

---

## v2.07 — 2026-09-14

### Follow-up fix: v2.06's dual-homed fix had two remaining gaps

pcapps pushed back on v2.06 ("you sure its fixed? both ldp sessions there
have been up for 1m+") rather than taking the synthetic test as sufficient
proof, which was the right call -- there were two more real problems:

1. **Peer-key mismatch between the two data sources.** `_poll_hub` (1800
   side) was tagging events with `hub_name` (a string, e.g. "ces01.atl1"),
   while `_sweep_1250` (1250 side) tags with the hub's own loopback IP
   (parsed from the line's peerRouterID field). Same session, two different
   identifier spaces -- `pair_ospf_outages_by_peer` would never have grouped
   the two sources' views of the SAME session together. Fixed: `_poll_hub`
   now tags with `hub_ip` instead, matching the 1250 side's identifier space.

2. **Legacy state poisoning.** Events already persisted in `state.json`
   from before v2.06 have no `"peer"` key at all (the field didn't exist
   yet). Defaulting a missing peer to `""` -- which is what v2.06 shipped --
   would put ALL untagged legacy events for a site into one shared bucket
   and silently reproduce the exact cross-session bug, just on old data
   instead of new. Fixed: `_ldp_events_from_map` now DROPS any stored event
   with no `"peer"` key entirely, rather than defaulting it. Because the
   stored map is keyed on `"<timestamp>|<kind>"`, a fresh poll that
   re-captures the same still-logged event will overwrite the old untagged
   entry in place with a correctly peer-tagged one -- so this self-heals on
   the next poll, no manual state cleanup needed. LDP full-log retention is
   300+ days confirmed on the 1800 side, so the underlying event data isn't
   lost, just temporarily excluded from pairing until re-collected.

Verified end-to-end with the exact reported scenario reconstructed as a
legacy (untagged) state: 0 events survive the drop, then after simulating
one fresh re-poll with correct peer tags, both sessions pair correctly and
"Currently Down" reads "no" -- matching the real device's confirmed
Operational state on both LDP sessions.

Practical note for pcapps: after this version is deployed and a fresh poll
runs, expect the previously-affected site's outage count to momentarily
show fewer/zero outages (the untagged legacy history was dropped), then
repopulate correctly-tagged on the next poll.

---

## v2.06 — 2026-09-14

### Bugfix: phantom "still down" outages on dual-homed sites (both LDP and OSPF)

Found live by pcapps: PGHKNYSH133 showed "Currently Down: YES" with a Down
timestamp from the day before, while the real device (`show ldp session`)
showed BOTH of its LDP sessions Operational for over a month.

Root cause: `pair_ospf_outages()` tracks only ONE "currently open outage"
slot. `PGHKNYSH133` is dual-homed -- it runs two independent LDP sessions
(to two different hub router-ids) -- and the events for both sessions were
being merged into a single flat timeline before pairing. When session A goes
down, then session B goes down before A recovers, the single open-outage
slot gets reassigned to B -- A's outage is orphaned as "still down" forever,
and A's real recovery event ends up incorrectly closing B's outage instead.
Reproduced exactly with a synthetic two-peer interleaved sequence (see the
new `pair_ospf_outages_by_peer` docstring) and confirmed the fix resolves it.

This affected any dual-homed site (a substantial fraction of the fleet, per
this codebase's own existing comments on dual-homed sites) on BOTH the LDP
pipeline and, it turns out, the OSPF adjacency pipeline for the same
underlying reason (a dual-homed 1250 peers OSPF with two different hub
router-ids too).

Fix: new `output_builders.pair_ospf_outages_by_peer()` groups events by their
session identity (the OTHER end's router-id) BEFORE pairing, then merges the
resulting per-session outage lists back into one chronological list.
`check_ospf_outage_events()` now returns a "peer" field per event, and
`ospf_stability.py`'s stored event maps (`ldp_event_map`, `ldp_event_map_1250`,
`ospf_transitions`) now persist that field too. Care was needed on which
field actually distinguishes the two sessions: on the 1800 hub's own log
(`_poll_hub`), the peerRouterID field is the SITE's loopback -- constant
across both of the site's hubs -- so the hub's own identity (`hub_name`) is
used instead; on the 1250's own log (`_sweep_1250`), peerRouterID correctly
is the hub's loopback and differs between sessions, so the parsed value is
used directly. `interface_check.py`'s LDP/OSPF calls were also switched to
the new grouped function for consistency (no behavior change there -- that
call site is already scoped to one session by construction).

No new device commands, no new device calls -- same data, correctly paired.

---

## v2.05 — 2026-09-14

### Bugfix: outage timestamps were missing the day entirely

`output_builders.format_down_event()` was rendering `MM/YY` (month/year) --
the day was never in the display at all, despite the function's own
docstring claiming `MM/DD/YY`. This produced strings like `09/26 - ...`,
which reads as "September 2026" and made a run of same-day events look like
they spanned into next year at a glance (pcapps: "you can pull logs from the
future???"). Fixed to `MM/DD/YY`, e.g. `09/07/26 - 1301Z, 0901EDT`.

This is the single shared timestamp formatter behind every outage summary
built since v1.98, so the fix applies everywhere at once: `interface_check.py`
(LDP + OSPF Outages), `ospf_stability.py` (LDP SESSION OUTAGE HISTORY, text
report + Excel), and `ospf_events.py`'s own outage block.

---

## v2.04 — 2026-09-14

### ospf_stability.py: LDP Outages sheet is now one row per site

Per pcapps: one row per outage meant the same site repeated across many
rows, which broke sorting. Redesigned the "LDP Outages" Excel sheet to one
row per 1250, same "summarize, don't enumerate" choice the main sheet
already makes with its own single "Last Drop" column instead of a full drop
list.

New columns: Hostname | Router-ID | Hub 1800 | **Currently Down** | **Total
Outages** | Site 24h/72h/7d/30d | Last Down Timestamp | Uptime Before Last
Drop | Last Up Timestamp | Last Outage Duration. Rows are sorted
currently-down sites first, then most total outages, then hostname -- same
"surface problems first" ordering the main sheet uses -- and a currently-down
site's row is highlighted red.

The full, unabridged per-outage list (every Down/Up pair, not just the most
recent) is unchanged and still lives in the text report's "LDP SESSION
OUTAGE HISTORY" section -- this sheet is the sortable summary, that section
is the complete record.

---

## v2.03 — 2026-09-14

### ospf_stability.py: LDP drop counts, same 24h/72h/7d/30d windows as OSPF

New `count_ldp_drops_since()`, parallel to the existing `count_drops_since()`
used for OSPF -- counts LDP Down events (from a `pair_ospf_outages()` list,
using each outage's own `down_dt`) within a rolling window, converting the
UTC device timestamp to local first (same `_device_dt_to_local` conversion
already used everywhere else in this tool) so a bounce right at a window
boundary isn't miscounted by the UTC/Eastern offset. Windows are nested by
construction, same as the OSPF counts.

Site-level (not per-outage), computed once in `_aggregate_results` alongside
`ldp_outages` and carried as `ldp_drop_24h` / `_72h` / `_7d` / `_30d` on every
row.

Surfaced in two places:
- Text report's "LDP SESSION OUTAGE HISTORY" per-site line now reads
  `... -- N outage(s)  |  24h: X  72h: Y  7d: Z  30d: W`.
- Excel "LDP Outages" sheet gained four new columns -- "Site 24h" / "Site
  72h" / "Site 7d" / "Site 30d" -- positioned right after "Hub 1800", same
  naming convention as the main sheet's own OSPF drop columns. Repeated on
  every outage row for a site, same pattern the main sheet already uses.

---

## v2.02 — 2026-09-14

### ospf_stability.py: renamed to "OSPF & LDP Stability"

Quick follow-up to v2.01's card-text update: the tool's NAME (not just its
description) still read "OSPF Stability" everywhere it's surfaced, which
undersold what it now does. Changed `TOOL_INFO['name']` (drives the home
page card title) and the tool's own page (`templates/ospf_stability.html`
`<title>` and `<h1>`) to "OSPF & LDP Stability". Cosmetic only -- no
behavior change.

---

## v2.01 — 2026-09-14

### ospf_stability.py: LDP Outages now has its own Excel tab; tool card updated

Two follow-ups from the LDP outage work in v1.98-v2.00, both prompted by
pcapps noticing the Excel export and the tool's own web page still only
described OSPF:

- **New "LDP Outages" worksheet** in the Excel export (`_build_xlsx`), one
  row per outage (Down + matching Up/Full side by side, so it's directly
  sortable/filterable by duration or date) -- the same `pair_ospf_outages()`
  data the text report's "LDP SESSION OUTAGE HISTORY" section already
  renders, merged from both the 1800 and the 1250. Rows for an outage still
  open (no clearing Up yet) are highlighted red, same convention as the main
  sheet's "session currently down" row color. Deduplicated by router-id for
  the same reason the main sheet and the text report both are (a dual-homed
  site would otherwise show its outage list twice).
- **Tool card / description updated** in two places that had gone stale
  after the LDP work: `ospf_stability.py`'s `TOOL_INFO['desc']` (used
  wherever the launcher/home page lists this tool) and the `<div
  class="sub">` blurb on `templates/ospf_stability.html` (the page shown in
  the screenshot pcapps flagged) -- both now mention LDP session tracking
  and the paired outage format, not just OSPF adjacency.

---

## v2.00 — 2026-09-14

### ospf_stability.py: LDP Outage History now sourced from BOTH ends of the session

Per pcapps: LDP is a better indicator of an actual IXC outage than OSPF
adjacency state, since traffic can keep routing on an intact LDP session
through an OSPF reconvergence -- and both the 1800 and the 1250 run LDP, so
both sides' event logs are worth checking.

Previously, `ospf_stability.py`'s "LDP SESSION OUTAGE HISTORY" section was
sourced only from the hub's (1800) own event log. Confirmed live
(2026-09-14, FARGNDBC636) that a 1250 logs its own LDP-Session-Down/Up
events too, in the exact same format already parsed for the 1800 side
(`YYYY-MM-DD,HH:MM:SS` timestamps, same `LDP-Session-Down`/`LDP-Session-Up`
event names) -- so the same command (`LDP_EVENT_CMD`) and the same parser
(`check_ospf_outage_events`, now accepting `peer_ip=None` to skip the IP
filter when the log is already scoped to one site) now also run directly
against each 1250 during the existing sweep, no new device-query pattern
needed. Confirmed the 1250's clock is UTC (same as the 1800), so the
existing UTC-in/Eastern-out timestamp formatting applies unchanged.

The LDP fetch on the 1250 is best-effort, same as the existing hub-side LDP
fetch: a failure there does not take down the OSPF sweep. Events from both
sources are merged (deduplicated by timestamp+kind) before pairing, so
either side's log can cover a gap in the other's retention -- same
"coverage grows over time" philosophy `merge_drop_events` already uses for
OSPF drops.

State file note: `ospf_stability.py`'s persisted state gains one more key
per site (`ldp_event_map_1250`), alongside the `ldp_event_map` (1800-side)
and `ospf_transitions` keys added in v1.98.

Also fixed in v1.99 (same day, prior to this entry): IXC Check's merged
outage summary was split into separate "LDP Outages" and "OSPF Outages"
blocks -- see that entry below for detail.

---

## v1.99 — 2026-09-14

### IXC Check: split the merged outage summary into separate LDP and OSPF summaries

`interface_check.py` was pulling one event list from the 1800 (`show ne
events | grep <loopback>`) and bucketing every Down/Up into a single "OSPF
Outages" block -- which silently mixed in LDP-Session events too, just
unlabeled as such.

Reasoning for the split (per pcapps): LDP session state and OSPF adjacency
state are two different protocols riding the same link, and traffic can
keep routing on an intact LDP session even while OSPF reconverges -- so an
OSPF-only view can overstate an outage that never actually affected
traffic. LDP is the better proxy for an actual IXC outage.

No new device call -- same already-fetched event text, now split
client-side in `output_builders.check_ospf_outage_events()` via a new
`protocol` argument ("ldp" keeps lines containing the confirmed literal
`LDP-Session` event-name substring; "ospf" keeps everything else -- a
by-elimination match, documented in the function's docstring as not a
confirmed OSPF-specific event name, since no OSPF-specific keyword has been
seen in real 1800 event-log output). `interface_check.py` now prints both
"LDP Outages: N" and "OSPF Outages: N" blocks, and both are included in the
JIRA findings summary.

**Not yet done, blocked on real device output**: pcapps also asked for a
1250-side LDP check (`ospf_stability.py` already sources OSPF adjacency
drops authoritatively from each 1250's own event log -- doing the same for
LDP would be more authoritative than the current 1800-side-only source).
Holding off on writing that parser until a real `show ne events [unreported]
| grep LDP` sample from an actual 1250 is available, per this codebase's
standing rule against guessing device output formats.

---

## v1.98 — 2026-09-14

### New: OSPF Outages summary -- Down/Up paired, with uptime and downtime durations

Every place this suite reports an OSPF/LDP drop count now also reports a
paired outage history in one standard format:

    OSPF Outages: <n>
    1) Down    -> <timestamp> -> Uptime before drop: <duration>
    2) Up/Full -> <timestamp> -> Downtime before up: <duration>
    3) Down    -> <timestamp> -> Uptime before drop: <duration>
    4) Up/Full -> <timestamp> -> Downtime before up: <duration>

Previously, the "OSPF Drops" line in `interface_check.py` and the "LDP
SESSION-DOWN HISTORY" section in `ospf_stability.py` only ever listed Down
timestamps -- the matching Up/Full recovery was never captured, so there
was no way to see how long an outage actually lasted or how long the
circuit had been stable before it dropped.

New shared helpers in `output_builders.py`: `pair_ospf_outages()` (pairs
chronological Down/Up events into outages, computing both durations) and
`format_ospf_outages()` (renders the standard block above). A Down with no
earlier Up in the log window shows "unknown (no prior clear in log)"
instead of guessing; a trailing Down with no clearing Up yet shows "still
down (not yet cleared)".

Updated to use it:
- **`interface_check.py`** (IXC Check) -- the 1800-side device command
  dropped its `| grep Down` filter (now pulls the peer's full event
  history, not down-only) so the Up/Full side is available to pair against.
- **`ospf_stability.py`**, LDP pipeline -- `LDP_EVENT_CMD` broadened from
  `grep LDP-Session-Down` to `grep LDP-Session` for the same reason. Section
  renamed "LDP SESSION-DOWN HISTORY" -> "LDP SESSION OUTAGE HISTORY", and
  now deduplicated by router-id (a dual-homed site's identical history no
  longer prints once per hub leg).
- **`ospf_stability.py`**, OSPF adjacency pipeline (the fleet's
  authoritative drop-count source, read straight from each 1250's own event
  log) -- added `parse_ne_events_ospf_transitions()`, a sibling to the
  existing `parse_ne_events_ospf_down()` that also keeps the Full/recovery
  side. No additional device call: reuses the same `DEVICE_EVENT_CMD`
  output already fetched for the drop-count columns. New "OSPF ADJACENCY
  OUTAGE HISTORY" report section, site-level (same per-leg attribution
  limitation the drop counts already have, documented inline).
- **`ospf_events.py`** (standalone, 1250-side) -- paired its own
  already-collected Down/Full event lists into the same outage format,
  sourced entirely from the 1250, no 1800 involved. Added to both the report
  and the JIRA summary.

State file note: `ospf_stability.py`'s persisted state gains two new keys
per site (`ldp_event_map`, `ospf_transitions`); the old `ldp_down_hits` /
`ldp_down_hits_map` keys are no longer written (left alone, unused, in any
state file that already has them from a prior version).

No new device commands introduced -- existing greps were broadened to stop
excluding the Up/Full side of the same read-only `show ne events` command
already in use. `INTRUSIVE_COMMANDS.md` unaffected.

---

## v1.97 — 2026-09-11

### New: INTRUSIVE_COMMANDS.md -- documented, will ship in every revision

Prompted by a direct question worth having a durable answer to: does any
tool in this suite send a command that changes a device's configuration?
Audited every device-command call site across the whole codebase (~70 call
sites) by reading each command string at its source, not assuming from
function/variable names. Verdict, and full per-script breakdown, now lives
in `INTRUSIVE_COMMANDS.md`.

Short version: every command any tool actually EXECUTES against a device is
read-only (`show`/`SHOW`/`ping`/session-display-only). Two tools --
`ds1_gsx_test.py` (DS1 Test Builder) and `mux_loop_script.py` (MUX Loop
Script) -- generate the literal TEXT of intrusive commands (AIS/loopback
set-delete pairs, MUX `configure terminal`/`commit`/`clear cem circuit`) as
part of a copy/paste runbook for a NOC engineer to run and reverse by hand;
traced every one of those builder functions and confirmed the generated text
only ever lands in a report string, never back into a `.run()` call.

No functional code changed in this version -- documentation only. Ship this
file going forward and keep it current whenever a new tool or device command
is added (noted at the top of the file itself as a standing instruction).

---

## v1.96 — 2026-09-11

### Added LAB as a fifth 1800 hub site

`noe-util-01a` testing needed a lab 1800 (`ces01.lab3`, `10.255.41.255`) that
isn't one of the four production hubs NetBox hub discovery returns. Added as
`LAB` in `hub_registry.py`'s `VERIFIED_HUBS` fallback table (it will only
ever resolve from there, never from NetBox, since NetBox doesn't track it)
and added `lab` to `_SITE_RE` so both authorities that matter -- the device's
own hostname once connected, and NetBox's own hostname parsing -- recognize
it, not just the hardcoded table.

While in there, consolidated a second copy of the same site-detection regex
that `npt1800_client.py` had been maintaining independently (`_capture_hostname()`
had its own literal `(atl|jfk|dfw|lax)` pattern for deriving `loc_code`) --
now calls `hub_registry.site_from_hostname()` instead, so a future new site
only needs updating in one place, per this module's own stated purpose.

Updated the IXC Check web form's site dropdown (was a static, hardcoded
`<option>` list -- the actual functional gap for the web UI specifically,
since the backend validation already flows through `hub_registry` and would
have accepted `LAB` either way) plus the cosmetic display strings elsewhere
that listed the four sites.

Note: this exact change was hand-applied directly on `noe-util-01a` before
this version existed, to unblock same-day testing. This version supersedes
that hand-edit with the same change, source-controlled -- deploy v1.96 there
to replace it.

---

## v1.95 — 2026-09-11

### Repackage fix -- v1.94.zip had a stray artifact in it, do not distribute it

While testing the v1.94 portability fix (forcing `os.name = 'nt'` in a
throwaway interpreter session to confirm Windows behavior was unchanged --
see v1.94 entry below), `platform_paths.py`'s eager `os.makedirs(BASE_DIR,
exist_ok=True)` ran against that forced value on a real POSIX filesystem,
which doesn't treat `\` or `:` as path separators -- so it created a literal
directory named `C:\Claude` inside the working folder instead of a Windows
path. `zip -r .` then swept it into `v1.94.zip`. Harmless in practice (`:`
and `\` aren't legal Windows filename characters, so it likely couldn't have
extracted cleanly on a real Windows host, and it contained no credentials --
just two old leftover test-run text files), but not something to ship.

Fixed by excluding it from the zip build and rebuilding clean; verified with
`unzip -l` that no `C:*` entries remain. **v1.94.zip should be discarded, not
extracted, if it was already copied anywhere** -- use v1.95 instead. No code
changes beyond the version bump; this is a packaging-only fix.

---

## v1.94 — 2026-09-11

### Cross-platform portability -- the suite now runs on Linux, not just Windows

First real run on noe-util-01a.lab1.bwnet.us (the Linux lab VM standing in
for the eventual SNOW-automation host) failed immediately: NetBox showed
"failed" and a 1250 Health Check produced `[JIRA] FAILED: Missing JIRA
credentials`. Root cause was the same everywhere -- every credential/state
path in this codebase was a hardcoded literal Windows path
(`C:\Claude\autotester_config.txt`, plus every tool's `.txt` report output,
`ospf_stability_state.json`, `gsx_hostkeys.json`), scattered across 18 files.
On Linux that path simply doesn't exist, so every config read came back
empty. This was already a known backlog item ("configurable paths / remove
hardcoded C:\Claude") that had never been load-bearing until now.

**New `platform_paths.py`** -- single source of truth for where the suite
reads/writes. Resolution order: `CURVEBALL_HOME` env var override, else
`C:\Claude` on Windows (unchanged -- pcapps's own machine needs zero
migration), else `~/.claude` on Linux. Every one of the 18 files now builds
its paths through `platform_paths.path(...)` / `platform_paths.BASE_DIR`
instead of a literal string. Directory is created eagerly at import time
(`exist_ok=True`, never raises).

**`secure_config.py` -- keyring on Linux.** `JIRA_TOKEN`/`INFRA_PASS`/
`GSX_PASS` went through `keyring` (Windows Credential Manager) with no
fallback -- on a headless Linux VM with no desktop session, there's no
Secret Service/KWallet daemon for keyring to talk to, so every secure key
was silently forgotten between runs (re-prompting every time, never actually
persisting). Fixed: `_keyring_backend_usable()` does a real set/get/delete
round-trip probe (not just "is keyring installed") once per process; if that
fails, falls back to a local JSON file (`~/.claude/.secure_fallback.json`,
chmod 600 best-effort). Still plaintext -- explicitly a lab/demo posture,
not a long-term one, and the code says so at the point it kicks in. Windows
never touches this path; Credential Manager is always usable there.

**`gsx_client.py` -- local OS `ping` was Windows-only.** `ping_host()` (used
to find the live GSX mgmt card before SSHing in) hardcoded `-n`/`-w` flags,
which don't exist on Linux `ping` (`-c`/`-W`, different units). Fixed with an
`os.name` branch; success detection now trusts the process exit code first
(portable) with the old string-matching kept only as a fallback.
`HOSTKEY_STORE` also moved off the hardcoded path. `find_plink()` was
already cross-platform-safe (tries bare `plink` via PATH first) -- no change
needed there, though whether a Linux GSX path exists at all (no `plink` on
Linux, and paramiko can't do the GSX's DSA-512 host key either) is still an
open question, parked for live testing on the lab box.

Compiled and round-trip tested (fake `CURVEBALL_HOME`, Windows-path logic
forced via `os.name` override, keyring fallback file exercised end-to-end
including permission bits) before packaging. All 18 files plus the new
`platform_paths.py` compile clean.

---

## v1.93 — 2026-09-09

### NNI/blast-radius: real traffic-passing check, and a terminology fix

Live report showed `[NNI] NNI xe-ts16/3.548 is UP (OSPF Full)`. Two separate
problems raised:
  1. Wording conflated a sub-interface's OSPF adjacency (protocol-layer up)
     with "the NNI is up" -- a sub-interface being OSPF Full says nothing
     about whether traffic is actually passing. Also inconsistent with this
     codebase's own convention elsewhere (see `autotester_standalone.py`),
     which reserves "NNI" for the physical port and calls the sub-interface
     the EVC/circuit.
  2. No check ever confirmed traffic was actually moving -- only OSPF
     adjacency state.

Fixed both in `nni_check.py`:

Wording -- `check_nni()`'s headlines for the "up" and "circuit-specific"
verdicts now say "Sub-interface X is OSPF Full on physical NNI Y" instead of
"NNI X is UP", and explicitly note this is protocol state only, not a
traffic confirmation.

Traffic check -- new, entirely separate section, queried against the
PHYSICAL port (never the sub-interface): `show interfaces physical pm 15min
<parent> | no-more`. Real output confirmed live 2026-09-09 (ces01.dfw1
xe-ts16/3) already reports device-computed Average/Current pps and bps over
the monitored window, so no need to poll twice and compute a delta
ourselves. New `parse_interface_pm_15min()` parses it; new
`assess_nni_traffic()` classifies AVERAGE pps (not Current -- a single
sample can catch a momentary lull or burst, Average reflects the whole
window) into:
  - **no-traffic** -- flat zero pps both directions
  - **keepalive** -- both directions <= 5 pps (user's rough call: "if its
    something like 5pps just mark it as keepalive") -- protocol hellos only,
    not real data
  - **asymmetric** -- one direction real traffic, other <= 5 pps -- flags
    possible one-way loss rather than being silently called "fine"
  - **passing** -- both directions above the keepalive floor
  - **unknown** -- command failed or output didn't parse; never guessed

Reported as its own "TRAFFIC:" block in `render_block()`, never blended into
the OSPF headline, and pushed into `assurance` findings independently
(`to_findings()`) so a Full-but-keepalive-only or Full-but-asymmetric circuit
surfaces as a real finding instead of reading clean.

`check_nni()` gained an optional `pm_raw` param (same pattern as
`alarms_raw`/`iface_raw`) so callers/tests can supply pre-fetched output.
`interface_check.py` needs no change -- it already calls `check_nni()` with
just `c`/`iface`/`hub_name`, so the new PM query and section come along
automatically.

---

## v1.92 — 2026-09-09

### IXC Check: fixed a real bug hiding the new OSPF/LDP drop check, then reworked its format

Live run surfaced "LDP Session-Down history: skipped -- no IP found in
interface config" on an interface that genuinely had one. Root cause: the
interface-config command was stored in the `captured` dict keyed by the full
command string INCLUDING its trailing `| no-more`, but the later lookup for
that config blob was built without it -- the keys never matched, `cfg` was
always empty, and BOTH ping and the v1.91 drop check silently skipped on
every single run, not just this one. Fixed the lookup key to match exactly
what was stored.

Also reworked the check itself per direction:
  - Broadened the match from LDP-Session-Down only to LDP-Session-Down OR
    OSPF-adjacency-Down for the peer, matching the original two-stage grep
    (`grep <peer-ip> | grep Down`) rather than the narrower LDP-only filter
    v1.91 shipped with. New shared function `check_down_history()` in
    `output_builders.py` replaces `check_ldp_down_history()` (kept as a
    back-compat alias) -- word-boundary match on "Down" so "shutdown" text
    doesn't false-positive.
  - New timestamp format: `MM/YY - HHMMZ, HHMMEST` (or EDT), e.g.
    `09/26 - 1307Z, 0907EDT`. Device timestamp is UTC (confirmed for the
    1250 earlier this session; 1800 assumed the same NE-platform
    convention). Eastern offset is DST-aware via a small hand-rolled US-DST
    calculator (`_us_eastern_is_dst()`) rather than `zoneinfo`/`pytz` --
    deliberately avoided a dependency on the `tzdata` package, which Windows
    Python does not ship with by default and this tool has never otherwise
    required.
  - Moved to the bottom of the interface's own report block, single line:
    `OSPF Drops: <count>, <timestamp>, <timestamp>, ...` -- both in the
    detail section and the JIRA findings summary.

Applied the same `check_down_history()` upgrade to `ospf_stability.py`'s LDP
section (dedup/sort now keyed on the UTC timestamp, not the formatted
display string, since `MM/YY` text sorts wrong across a year boundary).
Deliberately did NOT broaden `ospf_stability.py`'s hub-side grep to a bare
`Down` filter the way `interface_check.py`'s was broadened: that tool already
tracks OSPF adjacency drops separately and more authoritatively from each
1250's own event log (`drop_events` / the existing 24h-72h-7d-30d columns),
and a bare `Down` grep against a hub's full 300+-day log with no per-site
filter would be a much larger transfer than this tool's existing
per-event-type greps, for data it already has from a better source.

---

## v1.91 — 2026-09-09

### IXC-side (1800) tools: LDP session-down bounce history

Question raised from live device output: "for fiber/ixc circuit checks, are
we running `show ne events | grep <peer-loopback> | grep Down` in all the
tools?" Answer was no -- reviewed every tool that does IXC-side (1800) OSPF/
LDP checks and pushed the same check to each one that was missing it.

New shared helpers in `output_builders.py`:
  - `parse_subint_ip()` -- pulls a sub-interface's own IP out of
    `show configuration interfaces ... | display-set` output.
  - `derive_1250_subint_ip()` -- 1800 sub-int IP -> 1250 sub-int IP (4th
    octet - 1; the 1800 side is always odd by design, so this always lands
    on an even octet -- confirmed, not assumed).
  - `derive_1250_loopback()` -- 1250 sub-int IP -> 1250 loopback IP. First
    two octets carried over UNCHANGED (never hardcoded), third forced to
    `130`, fourth = sub-int's own 4th octet / 2. Refuses to guess and
    returns `None` if that octet isn't evenly divisible by 2, rather than
    producing a wrong IP silently.
  - `check_ldp_down_history()` -- parses `LDP-Session-Down` events for a
    specific peer loopback out of a `show ne events` blob into a list of
    timestamps.

Deliberately reads the FULL `show ne events` log, not `| unreported` --
confirmed live that `unreported` gets crowded out by unrelated churn within
roughly an hour to a day, while the full log held matching entries 300+
days old.

Applied to:
  - `interface_check.py` (IXC Check) -- derives the peer's loopback from the
    sub-interface config already being pulled, runs the grep, and reports
    hit count + timestamps in both the detail output and findings summary.
    Skips cleanly (not guessing) when the sub-int has no IP or the derived
    octet isn't evenly divisible.
  - `ospf_stability.py` -- simpler case, already keyed by loopback IP (=
    router-ID) with no derivation needed. Pulls `show ne events | grep
    LDP-Session-Down` once per hub poll (covers every site on that hub in
    one call), matches each site's hits client-side, and reports them in a
    new "LDP SESSION-DOWN HISTORY" section. Kept as its own field --
    deliberately NOT merged into the existing OSPF `drop_events` / drop-count
    columns, since that pipeline is sourced from a different device (the
    1250's own log) with its own timestamp format; conflating the two would
    risk corrupting an already-relied-on counting/provenance model. Merged
    (not overwritten) across hub polls so a dual-homed site's two legs, each
    logged independently by its own hub, don't clobber each other.

`nni_check.py` deliberately excluded -- it's built around whole-port
blast-radius across many siblings' OSPF states, not a single peer's LDP
history, so this check doesn't have a natural home there.

---

## v1.90 — 2026-09-03

### NNI/blast-radius: disambiguate "not in OSPF neighbour table" with a direct probe
Question: does 'show ospf interface <iface>' (Nbr column) give a more
direct read on OSPF status than inferring from an empty neighbour-table
lookup? Confirmed live: yes -- Nbr is a direct adjacency-count signal, and
the command explicitly confirms whether OSPF is even configured on that
interface (rules out "typo'd/wrong interface" and "OSPF not configured
here" as causes, which a bare neighbour-table miss can't distinguish).

Kept the existing `show ospf neighbor` design as the primary check --
that's a deliberate one-call-gets-the-whole-port-plus-siblings design the
blast-radius feature depends on, not something to replace. Added
`show ospf interface <iface>` as a single targeted fallback probe, fired
ONLY when the primary lookup doesn't find the target at all:
  - Nbr == 0 confirmed -> treated as a confirmed DOWN, fed into the normal
    down-handling verdict branches (V_CIRCUIT/V_MULTI/V_PORT_EVENT) so
    blast-radius awareness still applies, instead of a generic "unknown."
  - Interface not listed by the probe either -> distinct headline: OSPF
    likely isn't configured on this NNI at all (not necessarily a fault).
  - Interface listed with Nbr > 0 despite being absent from the neighbour
    table -> flagged as a genuine inconsistency between the two commands,
    not silently trusted either way.
  - Probe unavailable (no device / probe call failed) -> original hedge
    message, unchanged.

Bug caught in testing: the confirmed-down target was never added to
`siblings` (only populated from the neighbour-table scan that didn't find
it), so `total_on_port`/`down_on_port` undercounted it by one and the "N
other sub-interface(s)" headline went negative. Fixed by inserting the
target into `siblings` and recomputing port counts when the probe confirms
Nbr 0.

Tested: reproduced the real report's exact shape (44 Full siblings, target
absent from `show ospf neighbor`, probe confirms Nbr 0) -- now correctly
reports 45 total / 1 down, verdict `circuit-specific`, instead of `unknown`
/ "cannot confirm." Also verified the not-found and inconsistent-Nbr
branches, and confirmed the no-probe-available path is byte-identical to
the prior hedge message.

---

## v1.89 — 2026-09-03

### Job poller: stop silently polling a dead job forever
Observed live: the Flask dev server's file-watcher reloaded mid-run (a code
change saved while an Autotester job was active), which wipes the in-memory
`JOBS` dict on restart. The browser kept polling the now-unknown job id
every 2s -- server correctly answered 404 with `{"error": "Unknown job
id."}`, but that body has no `status` field, so `pollStatus()` in
`static/job.js` (only handles `status === running|done|error`) fell through
every branch silently. Result: infinite dead polling, UI frozen on stale
"Running..." with zero indication anything went wrong.

Fixed: `pollStatus()` now checks `!res.ok` before touching `data.status`,
stops the poll timer, and shows "Lost this job -- the server likely
restarted while it was running (...). Please re-run." This isn't
reload-specific -- any real crash/restart mid-run would hit the same dead
end, so this covers that case too, not just live-dev edits.

Verified: `node --check` on the modified file.

---

## v1.88 — 2026-09-02

### IXC Check ping-skip bug: regex never matched real config, not a missing IP
Reported: NNI sub-interface config genuinely has an IP (visible in the same
report's own Interface Config dump), but the tool printed "interface config
read OK but contains no 'family inet address' -- skipping ping."

Root cause confirmed: `re.search(r"set family inet address (...)"...)`
required "set" and "family inet address" to be contiguous. Real Junos
display-set output is `set interfaces xe-ts16/2 unit 330 family inet
address 10.111.130.11/30` -- the interface name and unit number sit
between "set" and "family", so this pattern could never match real output,
in either place it existed (`interface_check.py`, `npt1800_client.py`'s
standalone test harness). The endpoint-derivation logic right below it
(local IP, last octet -1 -- confirmed correct, matches the documented
convention) was fine and simply never ran because the match never fired.

Fixed both: dropped the "set" prefix requirement --
`re.search(r"family inet address (...)"...)`. Verified against the exact
line shape from the live report (`...unit 330 family inet address
10.111.130.11/30`) -- now matches and correctly derives the 10.111.130.10
ping target.

---

## v1.87 — 2026-09-02

### MUX-side SONET PM check (facing the 1800) -- confirmed gap, now covered
Direction: does the tool check MUX PMs facing the 1800? Answer was no --
confirmed a real gap, not silent under-reporting. The MUX block checked
facility alarms, CEM connection state, and working/protect conflict, but
the only SONET PM error-counter check anywhere (`OC12_COUNTERS`) was
sourced entirely from the **1800**'s own `show interfaces physical pm`, not
the MUX's side of that same OC12/SONET-ACR path.

- `mux4216_client.py`: `query_batch()` now pulls `show controllers sonet
  <active_sonet> | no-more` once `active_sonet` is resolved (cached per
  unique interface within a run -- DS1s sharing a SONET path don't repeat
  the call), stashed as `mux_results[ds1]["sonet_pm"]`. `| no-more`
  required even though this client already sends `terminal length 0`
  post-login -- this specific command's output is long enough to still
  need it (confirmed live on jfk1-mux-02).
- `output_builders.py`: new `check_mux_sonet_health()` reads the device's
  own current-state summary (`Active Defects` / `Detected Alarms` /
  `Asserted/Active Alarms`), NOT the SECTION/LINE LOS/LOF/AIS/RDI/REI
  counts above them -- those are cumulative since-boot/since-clear counts,
  not current state, and would false-flag a healthy circuit that had one
  historical blip. Current 15-min interval's CV/ES/SES/SEFS shown as
  context, not flagged (a partial in-progress bin reading 0 doesn't mean
  much either way).
- Wired into the MUX block of `build_diagnostic_summary()`'s full-route
  branch, between the facility-alarm/Netbox-conflict lines and CEM
  connection state (matches the existing read order).

Tested: fed `check_mux_sonet_health()` the user's live jfk1-mux-02 output
(clean) and a synthetic dirty variant (`Detected Alarms: LRDI`) --
correctly clean on the real sample, correctly flags on the dirty one.

### Diagnostic Summary: GSX and MUX sections now show IP
Direction: 1250 and 1800 already show their IP in this report; GSX and MUX
didn't. Both already had the IP sitting in already-collected data
(`mux.get("mux_ip")`, `mux.get("gsx_ip")` -- the latter already stashed by
`autotester_standalone.py`'s GSX-hop resolution) -- no new collection
needed, just added to the existing name lines:
`GSX  --  <node>  (<ip>)  (current 15 min / 24 hr)` and
`MUX  --  <name>  (<ip>)`.

---

## v1.86 — 2026-09-01

### NNI / blast-radius check — "one ticket, not fifty"

**The problem.** When circuits fail, the NOC works the queue ticket by ticket and
opens an IXC ticket per EVC. If the NNI carrying those circuits is down, every
one of those tickets is the same outage reported N times — effort wasted, and the
actual fault (one port) buried under its symptoms (many circuits). Any tool
reporting circuit status should look ONE LEVEL UP: a circuit-down report is only
actionable if its NNI is up.

**Two levels of blast radius.** An 1800 NNI is a sub-interface — `xe-ts16/2.111`
is one 1250 site, and every EVC at that site rides it. Its physical parent
`xe-ts16/2` carries many such sub-interfaces, i.e. many sites. So:

```
sub-interface down  ->  every circuit at that one site
physical port down  ->  every circuit at every site on that port   <- one ticket
```

**Built from proven commands only.** Deliberately no new device syntax, because
guessing at output format is what produced the LAXGSX04 / 11-2-28 wrong-circuit
report:

- `show ospf neighbor | no-more` — one call lists every 1250 on the hub with its
  interface and adjacency state, parsed by the already-verified
  `parse_ospf_neighbors()`. That single call yields the target NNI's state **and**
  every sibling on the same physical port — the blast radius, free.
- `show ne alarms | match "<parent>" | no-more` — row presence means alarm (the
  convention already used in this codebase). Rows are **counted, never
  column-parsed**, so no format is assumed.

Anything not obtainable from those two is reported as `unknown` rather than guessed.

**New `nni_check.py`** — `check_nni()` returns one of five verdicts:

| Verdict | Meaning |
|---|---|
| `nni-up` | NNI Full — a circuit fault here is genuine |
| `circuit-specific` | this NNI down, its siblings Full — ticket it |
| `multiple-circuits` | ≥2 down but not a majority — look for a common cause first |
| `port-event` | ≥2 down **and** ≥50% of the port — **ONE ticket, not per EVC** |
| `unknown` | not in the neighbour table; state not confirmable from OSPF alone |

Thresholds are explicit constants (`PORT_EVENT_MIN_DOWN=2`,
`PORT_EVENT_MIN_SHARE=0.5`). Integrates with `assurance.py`, categorising a
port-level event as `partial-application` — reporting one circuit as faulty while
its whole port is down is a true statement that gives a false picture.
`check_nni()` never raises for device or parse reasons.

**OSPF Stability: port rollup at zero cost.** It already polls
`show ospf neighbor detail` on every hub, so every interface and state is
in hand — `port_rollup()` groups by physical port with **no extra device calls**.
A new `PORT-LEVEL EVENTS` section sits high in the report footer (*"read these
BEFORE ticketing circuits"*), listing each affected port, its down/total share,
and the affected sites. Port events also print in the live progress log. Ports are
keyed per-hub, so the same port name on two hubs never merges.

**Interface Check:** now runs the full NNI/blast-radius block (one extra command)
and renders it in the report.

**Scope note — TDM tools not yet covered.** `autotester_standalone`,
`site_sweep`, `health_check` and `ds1_gsx_test` trace *TDM* circuits whose 1800
hop is an **OC12** toward the MUX, not an Ethernet `xe-` NNI. That needs a
different check against different commands, and it is not attempted here rather
than pretending this one applies.

**Verified:** 46 checks on `nni_check` (every verdict path, alarm counting,
threshold boundaries, and that bad input — an IP, a physical-only interface,
empty/garbage output, a dead SSH session — is reported and never raised) plus 21
on the OSPF port rollup.

---

## v1.85 — 2026-09-01

### OSPF Stability: one row per ADJACENCY, not per site (+ Interface column)

The report was one row per site, which forced two lossy aggregations on the ~68
dual-homed sites: **state = worst leg** and **uptime = minimum leg**. A site with
a healthy 9-day leg and a leg that came up 30 seconds ago rendered as a single
row reading `Init / 30s` — the healthy leg was invisible. And the **interface**,
the actual circuit identifier an engineer acts on, was parsed and then discarded.

Now one row per adjacency. State, uptime and interface are per-leg and **exact**.
A dual-homed site appears twice with its legs on adjacent rows (sort is
hostname, then hub).

**16 columns** (was 14): `Hostname · Router-ID · Hub 1800 · Interface · Legs ·
State · Uptime · Site 24h · Site 72h · Site 7d · Site 30d · Last Drop ·
Count Trust · Oldest Evt (d) · Source · Last Poll`.

**New `Legs` column — a diagnostic the drop counts cannot give.** Comparing the
two legs' *uptimes* answers whether one event took both adjacencies:

- `legs together` — uptimes agree within `LEG_SYNC_TOLERANCE_S` (120 s), so both
  came up in the same event, so both went **down** in the same event. Points at
  the site's transport, not either hub. Observed on DLTHMNME624, where 3 of 4
  drops hit both hubs in the same second.
- `legs differ` — independent, so hub- or path-specific.
- `single-homed` / `unknown` (a leg's uptime didn't parse).

Derived purely from hub-reported uptime, which is exact. Nothing inferred.

**Drop counts stay SITE-scoped, and the headers now say so** (`Site 24h` etc.).
The device event log identifies its peer by router-id, and the hub poll never
learns each hub's own router-id, so attributing an event to one leg would need an
inference this tool has no evidence for. Rather than guess, the columns are
labelled and the `Legs` column carries the per-leg signal instead.

**`Count Trust` is now sharper.** It previously compared *minimum uptime across
legs* against site-aggregated drops — a mushy comparison on dual-homed sites. Now
each leg's own uptime is checked, so a young leg on an otherwise-stable site is
caught. **Expect the fleet SUSPECT count to differ from a per-site baseline;
this is the check getting more sensitive, not a regression.**

**Also:** progress log and report header now distinguish adjacency rows from
unique sites, and the run reports how many dual-homed sites show
`legs together`. Excel summary block and charts moved right (data now spans
A–P, spacer Q, summary R–Y).

**Verified:** 33 checks. Two real bugs caught by the tests during development —
a duplicated `State` header that pushed `Last Poll` off the end, and
mis-mapped centre-alignment columns. A third "failure" turned out to be the
test's expectation being wrong and the code right: a 9-day leg on a site whose
log shows no drops in 30 days **is** a genuine contradiction and correctly flags
SUSPECT.

---

## v1.84 — 2026-09-01

### New `assurance.py` — shared machinery for failing loudly on prod gear

Every serious bug found in this suite has had the same shape: it failed
**silently** and produced output that looked structurally valid. None threw an
exception; none looked broken. What caught nearly all of them was not diligence
— it was that two independent numbers had to agree and something compared them.

This module makes that pattern the path of least resistance instead of something
re-invented per tool. Before it: ~120 ad-hoc `[!]` markers, 25 `_conflict`
fields, 9 `WARNING:` strings and 7 `_flag` variables across four files, each with
its own convention.

**Nine failure categories** as `CAT_*` constants, so the taxonomy lives in code
rather than a document nobody opens: `never-armed`, `self-retiring`,
`reporting-into-void`, `partial-application`, `fails-as-success`,
`structurally-blind`, `never-proven`, `drift-by-accretion`, `lost-tooling`.
Eight of the nine were hit for real in this suite during 2026-08/09.

**Seven mechanisms:**

| Mechanism | Counters | Real bug it would have caught |
|---|---|---|
| `contradiction()` | fails-as-success, partial, blind, drift | paginated read: uptime said 6 h, log said 0 drops in 24 h |
| `assert_fresh()` | self-retiring | stale hub map rendering identically to a fresh one |
| `Coverage` | partial-application | *"read 1 page of 47"* instead of silence |
| `suppression()` | never-armed | a permanent mute is indistinguishable from a broken check |
| `record_delivery()` | reporting-into-void | Slack armed in the UI, silently no-op with no webhook |
| `require_artifact()` | lost-tooling | v1.81 in the UI with no zip on disk |
| `self_test()` | never-armed, never-proven | proves the ALARM fires, not just that the check passes |

`contradiction()` is the highest-value one and the cheapest: unlike a delivery
receipt or a clock it needs **no external oracle**, only two paths to the same
answer.

**Provenance** tags every reported value — `measured` / `cached` / `proven-zero`
/ `inferred` / `fallback` / `unreachable` — and `untrusted_share()` reports what
fraction is not directly observed. A number with no stated source is how
fails-as-success hides.

`footer()` renders one standard block (coverage, provenance, delivery, findings,
verdict), replacing the four hand-rolled versions in the suite.

**Deliberately not adopted anywhere yet.** Retrofitting OSPF Stability would mean
re-validating two days of hard-won verification against a refactor for cosmetic
gain. Adoption is for new tools, and opportunistic when a tool is already being
touched. Stdlib only — an assurance layer that breaks on a missing dependency is
its own failure mode.

**Verified:** 46 checks. Every mechanism is tested to both stay silent on good
input *and* fire on bad — including the module's own `self_test()` being caught
out by a check that never fires, a check that false-alarms, and a check that
raises. `python assurance.py` prints a worked example seeded with the five real
defects from this suite.

---

## v1.83 — 2026-09-01

### Consolidated hub identity onto NetBox — new `hub_registry.py`

Removes the *cause* of the v1.82 LAX/DFW mislabel rather than the instance. The
same fact — which 1800 hub is at which management IP and what it's called — was
hardcoded in **two** places:

```
npt1800_client.HUB_IPS         IP   -> {name, ip}
interface_check.SITE_TO_1800   SITE -> IP
```

On 2026-07-08 the mapping was verified live and `SITE_TO_1800` was corrected.
`HUB_IPS` was not. Two copies of one fact, one updated — LAX reported as DFW for
~2 months. Anything fixed in one copy can rot in the other, so the second copy
is now gone.

**New `hub_registry.py` — authority order:**

1. **The device itself.** Once connected, the hub's own hostname (`ces01.lax1`)
   states where it is. Nothing can drift from that, so it always wins —
   `name_from_hostname()`. Note `npt1800_client._capture_hostname()` was already
   deriving `loc_code` this way; the authoritative answer was on the box the
   whole time, just not used for the hub *name*.
2. **NetBox.** Needed for site → IP, since the IP must be known before we can
   connect and ask. OSPF Stability already discovers hubs this way and has never
   been wrong about hub identity.
3. **`VERIFIED_HUBS` in `hub_registry.py`** — fallback only, so a NetBox outage
   can't take these tools offline. Used **with a visible warning**, never silently.

**Drift detection.** When NetBox and the fallback disagree, both values are
reported and NetBox wins — the disagreement is never resolved quietly. This is
the check that would have caught the original bug:

```
[!] DRIFT for LAX: NetBox says 10.111.140.99 but hub_registry fallback
    says 10.111.131.254 -- one is stale, NetBox is authoritative
```

Sites present in the fallback but missing from NetBox are also reported, and the
fallback fills the gap rather than dropping the site.

**Changes:**
- `npt1800_client.HUB_IPS` literal is now **empty**, populated at import from
  `hub_registry.build_hub_ips()`. Kept as a name only for backward compatibility.
- `query_batch()` resolves the hub name hostname-first, then registry, then the
  legacy dict — and its error message no longer says "not in HUB_IPS map".
- `interface_check.SITE_TO_1800` is built from `hub_registry.build_site_to_ip()`;
  `run()` resolves live via `site_to_ip()` and prints the provenance.
- `interface_check` now **cross-checks the requested site against the site the
  device reports**, emitting `[!] SITE MISMATCH` when they differ — the same
  two-source principle as OSPF Stability's `Count Trust`.
- `npt1800_client.main()`'s hardcoded `[10.111.131.254]` prompt default now comes
  from the registry and lists all known hubs. Its interface parsing also picked
  up the `rsplit(".", 1)` and `/` checks from v1.82's `interface_check` fix.
- Dropped dead `HUB_IPS` imports from `autotester_standalone.py` and
  `mux_loop_script.py` (imported, never used).
- Legacy `132.x` / `135.x` IPs live in `hub_registry.LEGACY_IPS`, resolve with
  source `"legacy"` and an "unverified" name, so they can't be quoted as fact.

**Verified:** 27 checks across five scenarios — NetBox down (fallback + warning),
NetBox agrees (no warnings), NetBox disagrees (drift reported, NetBox wins),
NetBox partial (missing sites reported, fallback fills), and device hostname
correct even when the map is deliberately drifted. Plus a codebase scan
confirming no hub-IP mapping literals remain outside `hub_registry.py`.

---

## v1.82 — 2026-09-01

### FIXED: LAX circuits reported as DFW for ~2 months (HUB_IPS mislabel)

`npt1800_client.py` `HUB_IPS` mapped `10.111.131.254` to `"DFW-1800"`. That box
is **LAX** (`ces01.lax1`). Every tool reading `HUB_IPS` for a hub *name*
inherited the error — the IP used for connection was correct, only the label
was wrong, so nothing failed loudly.

Surfaced by a **Morning Check** FULL T1 ROUTE for `SLKCUTMA660 ts1/25`, showing
`HOP 2 Hub: DFW-1800 (10.111.131.254)` feeding `HOP 3 MUX: LAX1-MUX-02`. The MUX
was right; the hub label was not.

This was already diagnosed on **2026-07-08** (TODO.md: *"Definitive SITE -> 1800
IP map ... clears the old HUB_IPS 'DFW-1800' mislabel"*) and corrected in
`interface_check.py`'s `SITE_TO_1800`, but **the fix was never applied to
`HUB_IPS` itself**. Two maps of the same fact, one updated.

Verified four hubs are now:
```
10.111.130.254  ATL-1800      10.111.131.254  LAX-1800   <-- was DFW-1800
10.111.130.255  JFK-1800      10.111.131.255  DFW-1800
```
The unverified `132.x` / `135.x` entries are absent from the definitive map and
are not returned by NetBox hub discovery (which finds exactly the four above).
Retained so existing lookups don't start returning `None`, but renamed to
`"... (unverified)"` so they can't be quoted as fact. **NetBox is authoritative;
these hardcoded maps drift.**

### FIXED: wrong GSX endpoint selected — node, slot AND T1 all from another circuit

**This is the real cause of the bad HOP 4 data.** Reported on
`SLKCUTMA660 ts1/25` (ACR 16, CEM 112): tool emitted `LAXGSX04 / 11-2-28`,
truth is `LAXGSX01 / 12-2-12`. Node, slot and T1 all wrong — port 2 matched only
by coincidence.

Since node and slot come from `p[0]`/`p[1]` of the T3 controller *description*,
both being wrong meant the tool had read **a different circuit's controller** —
i.e. `gsx_ce_iface` itself was wrong. Three compounding bugs in the xconnect
parse (`mux4216_client.py`):

1. **The inner `break` exited only the inner loop.** The outer `for line` loop
   continued, so each later matching line **overwrote** the earlier result —
   **the last match won, not the first.** Never intended.
2. **`if "CE{acr}:{cem}" in line` is an unanchored substring.** `CE16:1120`
   contains `CE16:112`, so a neighbouring circuit's line matched. The device-side
   `| i {acr}:{cem}` has the same flaw, so several lines came back to iterate.
3. Together: multiple lines matched and the last one's endpoint
   (`CE8/2/10:128`) was used. `128 % 100 = 28` — exactly the wrong T1 reported.
   The true CEM id is `112`, and `112 % 100 = 12` — the correct T1.

Now:
- CE token matched with an **anchored** regex (`CE{acr}:{cem}(?!\d)`), so
  neighbouring circuits can't match
- **all distinct candidate endpoints collected**, first-match-wins deterministically
- a single candidate is used silently; **more than one raises a
  `[!] GSX ENDPOINT AMBIGUOUS` banner** listing every candidate plus the raw
  xconnect output, warning that node/slot/port/T1 may *all* belong to another circuit
- on ambiguity, prefers the endpoint whose CEM id equals the ACR-side CEM group
  (held true in the one case verified against the GSX) but still flags it —
  one confirmed case is not a rule
- `RuntimeError` on no match now includes the raw output

Verified by replaying the reported case: old logic reproduces `CE8/2/10:128` →
T1 28; new logic selects the `:112` endpoint → T1 12. Anchoring rejects both
`CE16:1120` and `CE116:112`.

**VERIFIED LIVE 2026-09-01** — same circuit re-run after the fix, every field now
matches the GSX:

| Field | Before | After | Truth |
|---|---|---|---|
| Hub | DFW-1800 | LAX-1800 | ✓ |
| GSX CE | CE8/2/10:128 | CE8/0/19:112 | ✓ |
| Node | LAXGSX04 | LAXGSX01 | ✓ |
| GSX IP | 192.168.45.20 | 192.168.45.3 | ✓ |
| Assignment | 11-2-28 | 12-2-12 | ✓ |
| T1 Command | T1-1-11-2-28 | T1-1-12-2-12 | ✓ |

No ambiguity banner raised — after anchoring exactly one candidate matched, so the
correct endpoint was selected outright. The real CE interface is `8/0/19`; the
`:112` CEM id was predicted correctly, the interface portion was not (the
pre-fix test fixture guessed `12/2/12`).

Note: the "two T1 sources disagree" symptom was itself caused by this
wrong-endpoint bug. With the correct endpoint, `112 % 100 = 12` and the
controller description's `-12` agree, so `T1 SOURCE CONFLICT` stays silent. The
cross-check is retained as a guard, but the open question of which source wins is
resolved — they agree when the endpoint is right.

### Also FIXED: GSX T1 had two sources, one silently discarded

`mux4216_client.py` parsed the T3 controller description as
`NODE-SLOT-PORT-T1` (e.g. `LAXGSX04-11-2-14`), took node/slot/port from
`p[0..2]`, then **discarded `p[3]` — the T1** — and derived T1 from
`int(gsx_cem_id) % 100` instead.

When the two sources disagree the report showed only the CEM-derived value with
no indication of ambiguity. In the reported run `CE8/2/10:128` gave
`128 % 100 = 28`, printing `Assignment: 11-2-28` and
`T1 Command: SHOW T1 T1-1-11-2-28` — a command that may point at the wrong
circuit.

Both sources are now parsed and compared:
- new `gsx_t1_desc` (from the description) and `gsx_t1_conflict` in `mux_results`
- `build_route_block()` prints a `[!] T1 SOURCE CONFLICT` banner under the T1
  command when they disagree, quoting both values
- when they agree it prints `(T1 confirmed by controller description: ...)`

The emitted value is unchanged (still CEM-derived) pending a live run to
establish which source is authoritative — deliberately not guessing, since
picking wrong would swap one silent error for another.

### Added `| no-more` to 6 remaining Ribbon commands (audit)

Full audit of every `.run("show ...")` in the suite, prompted by this bug class
appearing twice in two days (`ospf_stability.py` v1.81, `interface_check.py`
below). 44 show-style calls found; 27 already correct.

Fixed (Ribbon/NPT — needs per-command `| no-more`):
- `ds1_gsx_test.py` L313, L387 — `show configuration | display-set | match ...`
  (**real risk**: config output paginates)
- `autotester_standalone.py` L207, L209 — transceiver info / TCA
- `site_sweep.py` L126, L129 — transceiver info / TCA

**Not** changed: the 11 hits in `mux4216_client.py`. That is a Cisco NCS 4216
which disables paging session-wide with `terminal length 0` post-login (L76);
`| no-more` is not valid Cisco syntax and would break those commands.

Re-audit: **0 Ribbon `show` commands remain without `| no-more`.**

### FIXED: interface_check.py — four bugs

Triggered by a report run against `ces01.jfk1` with interface `10.111.135.100`.

1. **An IP was accepted as an interface name.** Validation was
   `if "." not in iface`, which an IP satisfies. `split(".")` then yielded
   `nni="10"`, `vlan="111"` (the first two octets), producing
   `show configuration interfaces 10 unit 111` and a device syntax-error dump.
   → new `validate_iface()` rejects IPs (naming the correct format), plus
   missing `/`, missing `.vlan`, non-numeric VLAN. Split is now
   `rsplit(".", 1)`.
2. **No `| no-more` on any of the 5 commands.** Once a `---More---` prompt
   appeared, the session fed the *next* command's characters to the **pager**
   instead of the CLI — origin of `Can't open help file`, `No previous regular
   expression`, stray `[Press 'h' for instructions.]`, and a trailing
   `> 111 / syntax error`. Worse, **OSPF neighbour output ran past the pager
   into the LDP section**, so the LDP heading sat above OSPF data.
3. **`grep <vlan>` matched almost everything.** On 10.111.x.x, `111` hits the
   second octet of nearly every address plus uptimes like `11m:51s` — one
   interface check returned ~100 neighbours. Filters now use the full interface
   name (measured: bare `111` matched 3/3 sample lines, `xe-ts16/2.111` → 1/3).
4. **Misleading closing note.** *"No IP address found in interface config --
   skipping ping"* read as a clean negative when the config command had
   **errored**. New `_cmd_failed()` marks sections `[!] FAILED`, adds a failure
   summary block, and distinguishes "config read OK, no inet address" from
   "config command never ran".

**Not yet run against a live device** — unit-tested only (23 checks).

### Note on versioning
`v1.81` (OSPF Stability tool + `| no-more` fix) was set in `app.py` but never
zipped to `!Revisions`, so the tool page showed a version with no artifact.
`v1.82` supersedes it and should be zipped.

---

## v1.80 — 2026-08-17

### T1 assignment field: added a visible format example
Confirmed `T1-1-9-2-8` (from a live screenshot) is a valid T1 assignment --
matches `T1-1-{slot}-{port}-{DS1}`. The placeholder text already showed an
example ("T1-1-7-2-2 or 7-2-2"), but placeholders disappear the moment a
value is typed, so there was nowhere to check the format once you're
actually using the field. Added a persistent `.hint` line under the field
(same pattern already used elsewhere on these forms, e.g. the "Open results
file..." hint) spelling out the format plus an example, in all three
templates where this field appears:
  - `gsx_perfmon.html` (the tool in the screenshot)
  - `ds1_gsx_test.html` (GSX Monitor's T1 assignment field)
  - `mux_loop.html` (GSX T1 field)

No backend validation changes -- `T1-1-9-2-8` already passed the existing
regex-based acceptance in `gsx_perfmon.py`/`app.py`.

---

## v1.79 — 2026-08-17

### Run Summary verdict wording: "PASS/FAIL" -> plain-language sentences
Direction: "TSX/Y -- PASS/FAIL" isn't intuitive to anyone outside the team
reading a JIRA comment. Replaced everywhere `run_summary_line()`
(output_builders.py) emits it:
  - `PASS`                    -> `The test completed successfully`
  - `PASS (Peerless -- tsNN)` -> `The test completed successfully (Peerless -- tsNN)`
  - `FAIL`                    -> `The test completed with errors`

`app.py`'s `_scan_run_summary()` (parses this same line to build the
Autotester rollup badge -- checked/flagged/verdict) updated in lockstep:
`_RUN_SUMMARY_LINE_RE` now matches the full phrase instead of the bare
PASS|FAIL token, and the flagged-DS1 check compares against "The test
completed with errors" instead of "FAIL". Underlying rollup semantics
unchanged (still flags on route-incomplete OR any Dirty hop) -- wording
only.

Not touched (out of scope -- internal/dev-facing, not a report readers see):
`gsx_hostkey_test.py`'s `[PASS]`/`[FAIL]` labels (SSH host-key pinning test
tool output) and `mux4216_client.py`'s internal "marking FAIL" exception
message.

Tested: fed `_scan_run_summary()` a synthetic Run Summary block with one
success line, one route-incomplete line, and one Dirty-but-"completed
successfully" line -- correctly flags both non-clean DS1s, verdict "issues".

### TODO.md: logged two open items for later
- NNI/EVC resolution doesn't apply to Peerless/T1-SAToP-over-OC12 circuits
  (confirmed live 2026-08-17) -- no local Ethernet NNI exists for that
  circuit type at all. Decision needed: N/A-style message vs. a NetBox
  lookup.
- Morning Check TS1/2 (10.111.130.73) CES RxPsnUnderrunEvents finding --
  parked for pcapps to re-check (static vs. climbing count).

---

## v1.78 — 2026-08-07

### GSX PM counters: show both 15-min and 24-hr values, not a collapsed max
Caught by direction: the GSX device-name line has always read "(current 15
min / 24 hr)", but `_check_gsx_counters()` only ever reported ONE number per
counter. Root cause: `_gsx_current_perf_region()` matched the generic
substring "PERFMON CURRENT", which is present in both the FIFTEENMIN and
DAY section's own command echoes -- so both windows got merged into a
single contiguous region with no boundary between them, and the max value
across both was reported as if it were one number.

Replaced with `_gsx_split_current_perf()`, which keys off each section's
own distinct echo ("...FIFTEENMIN" vs "...DAY") to keep the two windows
separate, plus `_gsx_scan_counters()` run against each independently.
`_check_gsx_counters()` now returns both values side by side:
`Code Violations: 4039 / 14224  (15min / 24hr)` instead of a single
ambiguous `Code Violations: 4039`.

Tested: synthetic GSX PM blob with distinct 15min/24hr/THRESHOLDS sections
-- confirmed both windows report correctly and the THRESHOLDS config-limit
table is still correctly excluded (not misread as an error count).

---

## v1.77 — 2026-08-07

### Custom taskbar/Start icon for the launcher shortcut
Added `pixel_tyler.ico` (converted from the user's `pixel tyler.png`, all
standard sizes 16-256px baked in) and `Set Curveball Tools Icon.ps1` -- a
one-time PowerShell helper that (re)generates "Start Curveball Tools.lnk"
in this same folder, pointing at the existing `Start Curveball Tools.bat`
with its icon set to `pixel_tyler.ico`.

Why a separate script instead of just editing the .bat: Windows can't apply
a custom icon directly to a `.bat` file, and won't offer "Pin to
taskbar"/"Pin to Start" on one either -- the icon (and the pinning) has to
live on a `.lnk` shortcut instead. Run the script once (right-click -> Run
with PowerShell), then pin/move the `.lnk` it creates instead of the raw
`.bat`.

No app.py/output logic touched this version -- packaging/UX only.

---

## v1.76 — 2026-08-07

### 1800 side-label reorder + redundant identifier line removed
Direction: the report is meant to read like the vertical circuit-route
diagram it visually resembles (GSX / MUX / 1800 / 1250, each a device-name
"anchor" flanked by PM checks above/below), not like a flat list -- readers'
eyes should land on each device name line first, then look up/down to see
which PMs face which side. The 1800's `-- network-facing side (...) --` /
`-- MUX-facing side (...) --` labels now sit directly above the `1800 --
{name} ({ip})` line instead of above the OC PM/alarm lines, in both the
Peerless and full-route branches of `build_diagnostic_summary()`. The
standalone `oc-ts20/1:13:18`-style identifier line (which duplicated the
same value already shown in the side-label's parens) is removed.

Before:
```
        -- network-facing side (oc-ts20/1:13:18) --
  [ OK ] OC PMs clean
  [ OK ] OC alarms clean
         oc-ts20/1:13:18
         1800  --  ATL-1800  (10.111.130.254)
         -- 1250-facing side (NNI/EVC) --
```
After:
```
  [ OK ] OC PMs clean
  [ OK ] OC alarms clean
         -- network-facing side (oc-ts20/1:13:18) --
         1800  --  ATL-1800  (10.111.130.254)
         -- 1250-facing side (NNI/EVC) --
```

Tested: rendered `build_diagnostic_summary()` directly with dummy Peerless
and full-route inputs, confirmed output matches this layout exactly in both
branches.

---

## v1.75 — 2026-08-07

### OSPF + LDP uptime added to Autotester and Site Sweep (general 1250 circuit health)
New request: show OSPF and LDP adjacency uptime alongside the existing
fiber/EVC health checks, since both tools already do a general circuit
health check on the 1250 and this was a real gap.

Confirmed live on the actual device before writing any code (no guessing
this time, after the earlier AIS/event-log troubleshooting burned time on
wrong command syntax): this NPT-1250 image has **no direct uptime field**
for OSPF (`show ospf interface extensive/detail` only has a "Num State
Transitions" counter; there's no `show ospf neighbor` on this image at all).
LDP is different -- `show ldp session` reports a real uptime field directly
(`Uptime  1d:10h:37m:54s`).

- `ospf_events.py`: added `compute_ospf_uptime(raw_events, now=None)` --
  derives OSPF adjacency status/uptime from the last logged FULL/DOWN event
  in `show ne events unreported` (same source this module already parses
  for the 7-day bounce count). Returns status up/down/unknown + a human
  "Xd Xh Xm" uptime string. Explicitly returns "unknown" (never guesses) if
  no full/down event is in the currently logged window -- this event log is
  a short rolling buffer that can fill from unrelated high-volume events in
  well under an hour (confirmed live 2026-08-07), so an adjacency that's
  been stable longer than the log retains will correctly show "unknown,"
  not a wrong number.
- `ospf_events.py`: added `parse_ldp_sessions(raw)` -- parses `show ldp
  session` rows (peer, targeted, state, uptime). LDP's uptime is a real
  device field, no derivation needed.
- `output_builders.py`: added `check_ospf_ldp_health(ospf_events_raw,
  ldp_session_raw)` -- shared render helper (same pattern as
  `check_fiber_health`/`check_evc_health`), returns (flags, info) for a
  `[ !! ]`/`[ OK ]` block. Threaded two new optional params into
  `build_diagnostic_summary()` (`ospf_events_raw`, `ldp_session_raw`),
  rendered right after the EVC block in both the Peerless and full-route
  branches -- per-1250, shared across all DS1s in a run, same as fiber/EVC.
- `autotester_standalone.py`: HOP 1 (1250) now also runs `show ne events
  unreported` and `show ldp session` alongside the existing fiber/EVC
  collection, and passes both into `build_diagnostic_summary()`.
- `site_sweep.py`: added `_collect_ospf_ldp()` (mirrors `_collect_fiber()`),
  a new "OSPF / LDP HEALTH" report section next to the existing "GE FIBER
  UPLINK HEALTH" section, and an OSPF/LDP line in the compact JIRA/Slack
  summary.

Scope note: this is display-only (uptime shown in the diagnostic block and
summary), matching what was asked. It does NOT feed into `_ds1_review()`'s
root-cause verdict logic or the fault-direction one-liner in
`run_summary_line()` -- that would mean touching the root-cause matrix
`_ais_verdict()` already flagged for the user's own review, kept out of
scope here to control cost.

Tested: `compute_ospf_uptime()` and `parse_ldp_sessions()` against the
user's actual pasted `show ldp session` output and synthetic full/down
event lines (up-3-days case, and a down-after-full case) -- both produce
correct status/uptime/flags. `check_ospf_ldp_health()` wiring verified
directly. Full module compile-check passed on all four changed files.

---

## v1.74 — 2026-08-07

### OC12 alarms: show the actual alarm text, not just a count
Per direction: "(1 present)" told the reader nothing without logging into
the device to look it up. `oc12_alarms` was already capturing the full
matched device line(s) (filtered on "R-DS1AIS-P" / "AIS-V" -- this check
is specifically AIS-family alarms, not a general alarm catch-all), just
never displaying them. Both the Peerless and full-route branches of
`build_diagnostic_summary()` now print the actual line(s):
`[ !! ] OC12 alarm(s): R-DS1AIS-P : 1 (AIS present on facility)` instead
of `[ !! ] OC12 alarms (1 present)`. Also updated `_ds1_review()`'s order-3
citation (the "1800 network-facing/OC path" REVIEW text) to cite the same
real alarm line via `_cite_flagged_line()`, same pattern as the order-2
NNI fix and the generalized secondary-hop fix -- so the word summary and
the diagnostic block now agree and both show the actual alarm, not a tag
or a count.

Tested directly: alarm-present diagnostic output now shows the real
device line, and the REVIEW text (both as primary root cause and as a
secondary/non-root citation) now embeds that same real text.
NOTE: `output_builders.py` changed -- restart the server.

---

## v1.73 — 2026-08-07

### Diagnostic Summary: 1800 identifier repositioned to match existing convention
The "1800 -- {name} ({ip})" identifier line was sitting at the very top of
the 1800 section (both Peerless and full-route branches), ahead of both
the MUX/network-facing and 1250-facing sub-sections. Per direction, moved
it to sit BETWEEN the two sub-sections instead -- which also matches a
convention this same function already uses elsewhere (the 1250 section's
own "1250 -- {ip}" identifier sits between its CES and TDM readings, not
above all of them). New read order: network-facing header -> OC12/OC
status -> OC identifier -> "1800 -- name (ip)" -> 1250-facing header ->
NNI/EVC status. Applied to both the Peerless and full-route branches.
Verified against the exact reported shape (Peerless -- ts21, ATL-1800,
oc12-ts21/1:7:21, NNI xe-ts16/2) -- output now matches the requested
layout exactly.
NOTE: `output_builders.py` changed -- restart the server.

---

## v1.72 — 2026-08-07

### Fiber utilization: real numbers instead of "data collected"
Per direction: `_fiber_sentence()` now shows actual per-port IN/OUT max
and average bps (as Mbps), pulled directly from the Fiber Utilization
tool's own report text, instead of the generic "data collected -- see
attachment" placeholder from v1.64:

    Fiber utilization:
    ge-ts2/1: IN max 18.62 Mbps / avg 6.21 Mbps -- OUT max 9.00 Mbps / avg 3.00 Mbps
    ge-ts5/1: IN max 1.23 Mbps / avg 0.65 Mbps -- OUT max 1.00 Mbps / avg 0.11 Mbps

Shows both IN and OUT rather than collapsing to one number -- dropping
either could hide a real saturation issue on an asymmetric circuit. Still
no pass/fail verdict (that tool has no threshold to judge against; circuit
speeds vary). `build_paragraph()`'s fiber/OSPF join changed from a space
to a newline since the fiber sentence is now multi-line -- was running
the OSPF sentence onto the end of the last port line.

Tested against a synthetic report matching fiber_util.py's exact output
format (both ports, both directions) -- numbers extract and format
correctly, combined paragraph reads cleanly.
NOTE: `morning_check.py` changed -- restart the server.

---

## v1.71 — 2026-08-06

### Generalized the light-level citation fix to every secondary dirty hop
`_ds1_review()`'s "Other dirty segments (...) sit downstream" trailer only
ever named secondary/non-root hops -- no actual value -- the same class of
"real fault, silently under-detailed" issue as the light-level bug (v1.70),
just for T3/OC/GSX/etc. counters too, not only light levels. Added
`_cite_flagged_line()` and used it for every secondary dirty hop: now
cites the actual "[ !! ] ..." line when one can be found for that hop's
tag (falls back to name+tag, the old behavior, only if no match is
found). Tested against CES-root-cause runs with a secondary NNI light
flag, a secondary T3 flag, and a secondary GSX flag (no tag at all,
matched by hop name instead) -- all now cite the real line.

Reviewed (no bug found, no change needed): `build_jira_summary()`'s
VERDICT-lifting logic, which detects a verdict block by its 9-space
indent. Confirmed the call site that emits AIS verdicts is always
followed by either a blank line or a differently-indented line before any
other 9-space-indented content could reappear, so it can't currently
mis-capture unrelated lines into a verdict. Noted as a structural
assumption worth remembering if that call site's surrounding lines ever
change shape.

Not changed, flagged for the user's own domain review: `_ais_verdict()`'s
4-branch root-cause matrix (LOS / incoming AIS / mid-segment dirty /
everything-clean-so-1800-is-inserting-it) -- this is network domain
judgment, not something to unilaterally rewrite from here.

Full 21/22 sanity sweep re-run clean (the 1 non-match is an outdated
assertion in the throwaway test script from before v1.70, not a
regression).
NOTE: `output_builders.py` changed -- restart the server.

---

## v1.70 — 2026-08-06

### Real bug: light-level fault dropped from the summary when it wasn't the root cause

Same NOE-4644/ge-ts5/1 circuit, second live occurrence: the -34.0 dBm
reading was still out of spec, but CES (RxPsnUAS) was now the primary
root-cause hop instead of the NNI/light-level one. v1.69's fix made
`_light_level_blurb()` go silent whenever ANY dBm flag existed, to avoid
contradicting a flagged reading with a false "fine" claim -- but that
over-corrected: `_ds1_review()`'s REVIEW text only cites the reading for
whichever hop it's actually talking about (CES, this run), so the light
level fault vanished from the paragraph entirely rather than being
flagged separately. Silence wasn't a lie, but it was still a miss -- a
real, still-active fault needs to surface every run it's true, not only
on runs where it also happens to be the root cause.

Fixed: `_light_level_blurb()` no longer goes silent when something's
flagged. It now always states light-level status explicitly -- "Light
levels OUT OF SPEC: [ !! ] NNI ge-ts5/1 Rx laser -34.0 dBm (< -10.0)."
when anything's flagged (independent of, and sometimes redundant with,
the REVIEW text -- redundancy here is fine, silence was the actual bug),
or the existing "look fine" confirmation citing every distinct reading
when genuinely all clean.

Tested directly against both NOE-4644 shapes: CES-as-root-cause with the
light level still bad (now correctly surfaces "OUT OF SPEC"), and the
fully-clean case (still confirms correctly, citing both readings). Full
21-check sanity sweep from the same pass re-run clean.
NOTE: `output_builders.py` changed -- restart the server.

---

## v1.69 — 2026-08-06

### Real bug: light-level blurb cited the wrong (clean) reading, missed a flagged -34.0 dBm

Found live on NOE-4644: the "Light levels look fine (NNI Rx -3.2 / Tx
-2.4 dBm...)" blurb sat right above a scrolled-down "[ !! ] NNI ge-ts5/1
Rx laser -34.0 dBm (< -10.0)" flag it never mentioned. Not hallucinated --
the -3.2/-2.4 reading is real -- but a single DS1's diagnostic text can
carry TWO independent optical readings that both get labeled "NNI":

1. The 1800's own NNI/EVC physical PM (`check_nni_health`, printed as
   "NNI optics  Rx x / Tx y dBm").
2. The 1250's own GE fiber uplink laser (`check_fiber_health`, printed as
   "[ !! ] NNI <port> Rx laser x dBm (< floor)").

Both `_light_level_blurb()` and the order-2 REVIEW citation (v1.63) only
ever checked/cited the FIRST ("NNI optics") reading, so a genuinely clean
1800-side number could get cited as proof "levels are fine" while a
completely separate, flagged-dirty 1250-side reading sat elsewhere in the
same block, unmentioned and uncontradicted.

Fixed both:
- `_light_level_blurb()` now first checks whether ANY "[ !! ] ... dBm"
  line exists anywhere in that DS1's diagnostic text -- if so, it stays
  silent instead of risking a contradictory "fine" claim. Only when
  nothing at all is flagged does it cite a confirmation, and now cites
  EVERY distinct clean reading found (both checks), not just one.
- `_ds1_review()`'s order-2 citation (the "1800 1250-facing side dirty"
  REVIEW text) now searches for whichever line actually carries the
  "[ !! ] ... dBm" flag and cites THAT, instead of assuming it's always
  the 1800-side "NNI optics" reading.

Tested directly against the exact NOE-4644 shape: confirmed the blurb
now stays silent when the ge-ts5/1 flag is present (rather than falsely
claiming "fine"), confirmed it correctly cites both readings when a
synthetic all-clean case has two, and confirmed the REVIEW text now
cites the actual flagged -34.0 dBm line instead of the unrelated clean
-3.2/-2.4 one.
NOTE: `output_builders.py` changed -- restart the server.

---

## v1.68 — 2026-08-06

### Home page: Morning Check card moved next to Autotester, description clarified
Per direction: the home page's tool cards are sorted by each module's
`TOOL_INFO['order']`. Morning Check's `order` changed from 80 (last card)
to 5 (first card) -- one below Autotester's 10 -- so it now sits first,
directly beside Autotester's card in the grid. Description reworded to
lead with "Batch Autotester:" and explicitly name that it runs the same
Autotester circuit trace (plus Fiber Utilization + OSPF Events) per
ticket, matching the "Morning Check = batch Autotester, verified same
function call, not a re-implementation" point confirmed earlier today.
NOTE: `morning_check.py` changed -- restart the server.

---

## v1.67 — 2026-08-06

### Standalone Autotester now also posts to Slack
Closes the gap flagged during a decision-confirmation pass: only Morning
Check posted to Slack; standalone Autotester (run directly, not through a
Morning Check batch) had no Slack wiring at all. Added, with the same
Findings Summary + Diagnostic Summary & Full T1 Route content Morning
Check posts:

- New optional Slack webhook field on the Autotester page (web) and a new
  prompt in its CLI `collect_inputs()`, both persisted via
  `secure_config.SLACK_WEBHOOK` (same saved value Morning Check already
  uses/updates -- one webhook, shared across tools).
- `autotester_standalone.run()` posts to Slack independently of whether a
  JIRA ticket was given (a webhook is a separate concern from a ticket,
  same as Morning Check) -- includes the ticket link when one was given.
- `post_slack()` moved from `morning_check.py` into `jira_client.py` as a
  shared utility (it was already fully generic -- no Morning-Check-specific
  logic) so Autotester doesn't need a cross-tool import to reuse it. Also
  added a shared `SLACK_TEXT_LIMIT` (32000) with truncation as a backstop,
  on top of Morning Check's own pre-truncation.

Verified: `jira_client.post_slack()` against a mocked webhook (missing
webhook, normal post, oversized-text truncation), and confirmed both
`autotester_standalone` and `morning_check` import and use the shared
function correctly.
NOTE: `app.py`, `jira_client.py`, `morning_check.py`,
`autotester_standalone.py`, and `templates/autotester.html` changed --
restart the server.

---

## v1.66 — 2026-08-06

### Slack now also gets Diagnostic Summary & Full T1 Route per ticket
Per direction: Slack should carry both JIRA expands' content -- "Findings
Summary" (paragraph, unchanged) AND "Diagnostic Summary & Full T1 Route"
(condensed per-DS1 route/diagnostic) -- enough to sanity-check a finding is
in the right ballpark without opening JIRA. `build_slack_digest()` now
appends `digest_info["route_diag"]` per ticket in a Slack code block
(```...```) so the OK/!! alignment stays readable, right after that
ticket's paragraph. Still no raw threshold bullet list -- that's the one
thing v1.61 explicitly kept out of Slack.

Added a defensive size cap (`_SLACK_TEXT_LIMIT = 32000`): the Incoming
Webhook posts a single "text" blob, not Block Kit, so a long multi-ticket
batch with a full diagnostic block per ticket could get large enough for
Slack to mangle or reject it silently. If the assembled digest goes over
the cap, it's truncated with a note pointing at JIRA / the batch `.txt`
for the rest, instead of risking a garbled or dropped post.

Tested: two-ticket digest (one full-suite with paragraph + route_diag, one
no-trigger ticket with neither) renders the expected structure, and a
synthetic 40-ticket oversized digest truncates cleanly under the cap.
NOTE: `morning_check.py` changed -- restart the server.

---

## v1.65 — 2026-08-06

### Full reading-flow parity: standalone Autotester's JIRA post + the batch .txt

**Standalone Autotester now posts the same 2-expand shape as Morning
Check.** Was still using the older `post_comment()`/`_build_adf()` 3-panel
shape (Findings Summary expand + "Test Results -- Run Summary &
Diagnostic" expand + a separate "Per-DS1 Performance Monitors" expand).
Switched its own JIRA post to `post_report()` -- the same general-purpose
function Morning Check already uses -- so the comment now reads: intro
line, "Findings Summary" expand (unchanged), "Diagnostic Summary & Full T1
Route" expand (condensed per-DS1 route + diagnostic detail only -- no
per-DS1 PM dump inline anymore), full raw report (including the PMs)
attached as `.txt`. `post_comment()`/`_build_adf()` are left in place
unchanged for `mux_loop_script.py`, which still uses them -- untouched by
this change.

**Batch summary .txt gets its own "Findings Summary" section, same
flow.** `_write_batch_summary()` (the consolidated `MorningCheck_Batch_*.txt`
the web UI report panel and Download link point to) was still built from
the old `status_line` / `detail_lines` fields -- it never picked up the
paragraph work from v1.61, so the on-disk batch file didn't match what
JIRA/Slack were now showing. Added a "FINDINGS SUMMARY" section at the top
listing every ticket's paragraph (identical text to its JIRA comment /
Slack message), followed by the existing "PER-TICKET STATUS" quick-scan
index unchanged below it. Same three-tier reading flow everywhere now:
Findings Summary (10-second read) -> Diagnostic Summary & Full T1 Route
(next level of detail) -> full raw text (attachment / per-ticket .txt),
consistent across the JIRA comment, Slack, and the local batch file.

Tested: `_build_report_adf()` structure directly (confirms exactly 2
expand panels, correctly titled and ordered, no stray 3rd PM panel), and
`_write_batch_summary()` against a mixed batch (one full-suite ticket with
a paragraph, one no-trigger ticket without one) -- confirmed the fallback
to `status_line` for entries with no paragraph.
NOTE: `autotester_standalone.py`, `jira_client.py` (import only), and
`morning_check.py` changed -- restart the server.

---

## v1.64 — 2026-08-06

### Findings Summary: added 7-day OSPF bounce count + light-level "OK" blurb

**OSPF bounces in the last 7 days.** `ospf_events.py`'s "unreported events"
pull from the device isn't a calendar window -- it's whatever the device
has buffered, which can span more or less than a week. Added
`count_recent_down_events()`, which parses each down-event's own timestamp
(already extracted for display) and counts only those inside a real 7-day
window; printed into the raw report as "OSPF bounces in the last 7 days: N"
so morning_check.py's `_ospf_sentence()` can read the same number back
instead of re-deriving a rough count from the whole report text. Also
folded into `build_ospf_summary()` for the standalone tool's own JIRA post.
Caught + fixed a bug in my own date-format builder while testing (an extra
leading separator broke DD-MM-YYYY/DD/MM/YYYY parsing entirely) before
shipping -- both timestamp formats verified against synthetic 7/17-day-old
events.

**Light levels "OK" confirmation.** Per direction: any light level worse
than -10 dBm should be flagged (already true -- `FIBER_RX_DBM_FLOOR` has
been -10.0 since the DS1 diagnostic summary work). Added
`_light_level_blurb()` (output_builders.py): when a DS1's NNI reading is
present and AT or BETTER than that same floor, the paragraph now says so
explicitly ("Light levels look fine (NNI Rx -6.4 / Tx -2.6 dBm, both at or
better than the -10.0 dBm floor)"), reusing the exact constant that trips
the `[ !! ]` flag so the two can never disagree. When the reading is
already below the floor, no separate blurb is added -- that case is
already covered by the REVIEW text (v1.63), which cites the same reading.

**Correction: the old "Fiber levels are clean" line was never actually
meaningful.** While building the above, found that `fiber_util.py` (the
"Fiber Utilization" tool Morning Check runs) only reports GE port bps
THROUGHPUT -- it has no pass/fail threshold at all (circuit speeds vary,
so no %-utilization is computed to judge against) and never checks optical
dBm levels. `_fiber_sentence()`'s "Fiber levels are clean" wording
(introduced in v1.61) was therefore never actually verifying anything --
it would say "clean" every single run regardless of the actual data,
since the marker it scanned for never appears in that tool's output. Real
light-level health comes from the NNI reading above (per-DS1, via
Autotester). Corrected the wording to state what was actually collected
(utilization data, see attachment) instead of a health verdict with no
basis.

Tested directly: 7-day counter against synthetic in-window/out-of-window/
unparseable events (ISO and DD-MM-YYYY), light-level blurb against clean/
dirty/no-data NNI readings, and the full `build_jira_summary()` /
`_ospf_sentence()` / `_fiber_sentence()` output end to end.
NOTE: `ospf_events.py`, `output_builders.py`, and `morning_check.py`
changed -- restart the server.

---

## v1.63 — 2026-08-06

### Two clarity bugs found on today's TS1/7 (NOE-4746 re-run) live output

**1) REVIEW text told the reader to go pull light levels the tool had
already pulled and printed.** The order-2 (1800 1250-facing/NNI dirty)
REVIEW message always said "Check fiber optics levels and NNI errors on
that span" -- but the exact NNI Rx/Tx dBm reading that TRIPPED the flag is
already printed a few lines above it in the same Diagnostic Summary
("NNI optics  Rx -10.8 / Tx -2.6 dBm"). Read as the tool contradicting
itself: showing the level, then telling you to go check the level.
Fixed: `_ds1_review()` (output_builders.py) now pulls that exact reading
back out of the diagnostic text via regex and cites it directly in the
recommendation, instead of issuing a generic "go check" instruction. Also
folded in the LEC-territory question raised live: the recommendation now
explicitly notes that if the 1250's own reading is clean, the fault may sit
further out in the LEC's fiber path than this NNI check can see from our
side, and engaging the LEC is reasonable in that case.

**2) "1800 clean" and "1800 dirty" looked contradictory in the same block.**
The 1800 hop has two independent interfaces -- the MUX-facing OC/SONET side
and the 1250-facing NNI/EVC side -- and `build_diagnostic_summary()` printed
both back-to-back with only a single "1800 -- <hub> (<ip>)" label sitting
between them, which read as labeling only the second (NNI) section. Someone
skimming top-to-bottom could see "[ OK ] OC PMs clean" / "[ OK ] OC alarms
clean" immediately followed by "[ !! ] NNI ... Rx power" and reasonably ask
"is the 1800 clean or not?" -- confirmed live 2026-08-06 on TS1/7 (Peerless
-- ts20): OC side was genuinely clean, NNI side was genuinely dirty, not a
data bug, just an unlabeled/ambiguous block. Fixed: the hub identifier line
now sits ABOVE both sub-sections as a real header, with each sub-section
explicitly labeled "-- MUX-facing side (...) --" / "-- network-facing side
(...) --" and "-- 1250-facing side (NNI/EVC) --". Applied to both the
normal full-route branch and the Peerless branch.

Both fixes tested directly against the exact TS1/7 data from the live
screenshot (Peerless -- ts20, NNI Rx -10.8 dBm, OC clean) -- confirmed the
new REVIEW text cites the reading instead of demanding it be re-pulled, and
the diagnostic block now reads unambiguously as two separate interfaces.
NOTE: `output_builders.py` changed -- restart the server.

---

## v1.62 — 2026-08-06

### Autotester's own JIRA summary is now a real collapsed expand
Standalone Autotester's per-DS1 summary block (`jira_client._build_adf`) was
always-visible plain text by original design ("the glance-read Phil wants
on the ticket"). Per updated direction, changed to a real collapsed
"Findings Summary" expand -- same treatment Morning Check's equivalent
panel already gets -- so the ticket body stays short until clicked open.
Same change applied to the oversized-report stub-comment fallback
(`_fallback_attach`) for consistency. Verified both ADF paths produce an
actual `"type": "expand"` block (not an unwrapped codeBlock) via direct
structure checks.
NOTE: `jira_client.py` changed -- restart the server.

---

## v1.61 — 2026-08-06

### Fix: JIRA_TOKEN migration silently broke every tool except Morning Check
`secure_config.py` (added for Morning Check) migrates `JIRA_TOKEN` out of
`autotester_config.txt` into Windows Credential Manager the first time it
loads config, deleting the plaintext line once migrated. `jira_client.py`'s
`load_jira_config()` -- used by every OTHER tool's JIRA posting (Autotester,
Fiber, OSPF, Discovery, Health, GSX Monitor, DS1 Test Builder) -- only ever
read the plaintext file and never checked Credential Manager, so all of them
started failing with "Missing JIRA credentials" the moment that migration
ran. Confirmed live: Autotester popup "[JIRA] FAILED: Missing JIRA
credentials (JIRA_EMAIL / JIRA_TOKEN)" right after a Morning Check run.
Fixed: `load_jira_config()` now falls back to Credential Manager
(`secure_config.get_password("JIRA_TOKEN")`) when the plaintext file has no
token. Tested with a fake Credential Manager fallback -- token recovers
correctly when the plaintext line is absent.

### Morning Check -- human-readable output (replaces the raw findings wall)
JIRA and Slack were both showing raw threshold-violation lines pulled
straight from each tool's output (e.g. "NNI xe-ts13/4 Rx power -10.8 dBm (<
-10.0)") -- not useful at a glance. Per-user direction (budget-conscious:
templated Python, NOT a live LLM call -- no new API key/dependency/cost):

- New JIRA layout, 2 expands instead of 1:
  1. **"Findings Summary"** -- human-readable paragraph: the per-DS1
     hop-by-hop root-cause review Autotester already computes internally
     (`output_builders.build_jira_summary` -- reused as-is, it already reads
     as plain-English prose, e.g. "REVIEW: The 1250 has taken TDM errors...
     engage the LEC if the counters keep climbing.") plus one plain sentence
     each for Fiber and OSPF (`morning_check._fiber_sentence` /
     `_ospf_sentence`). This is now ALSO the entirety of what Slack posts
     per ticket -- no more raw bullet dump there either.
  2. **"Diagnostic Summary & Full T1 Route"** -- condensed per-DS1 route +
     diagnostic blocks only (not the shared infra stats / raw PM dumps).
- The full raw combined report (everything: run summary, route blocks,
  diagnostic summaries, shared infra stats, per-DS1 PMs) is now ONLY in the
  attached `.txt` -- no longer duplicated inline. Side benefit: the inline
  comment is now far under the JIRA size limit that was previously
  triggering the "too large, fell back to attachment" path on multi-DS1
  tickets.
- `autotester_standalone.run()` gained an opt-in `params["_extra_out"]`
  dict -- when a caller passes one in, `run()` populates it with the
  already-computed `jira_summary` (per-DS1 prose) and `route_diag`
  (condensed route+diagnostic text) instead of making the caller re-parse
  them back out of the raw report text. Purely additive: every existing
  caller (web Autotester route, its own CLI) doesn't set this key, so
  `run()`'s return value (`out_path`) is completely unchanged for them.
- `morning_check.run_full_suite()` / `run_lite_suite()` now return
  `(report_text, out_path, digest_info)` -- dropped the old `summary_text`
  slot (folded into `digest_info["paragraph"]` /
  `digest_info["route_diag"]` instead, since both suite-runners write to
  the same dict already).
- Tested with stubbed Autotester/Fiber/OSPF sub-tools: paragraph assembly,
  route/diagnostic extraction, Slack digest (paragraph-only, no bullets),
  and the JIRA ADF's two-expand structure all verified directly.
NOTE: `app.py`, `morning_check.py`, `autotester_standalone.py`, and
`jira_client.py` all changed -- restart the server.

---

## v1.60 — 2026-08-06

### Morning Check -- dedupe DS1s parsed from the trigger line
Live run showed a couple tickets with the same DS1 listed twice in the
parsed trigger (NOE-4607: ts1/1, ts1/5, ts1/20, ts1/28 each appeared
twice; NOE-4746: ts1/7 twice) -- real description text mentioning a DS1
more than once (full list + a "recheck these" callout, in one case).
Harmless but wasteful -- would've run the Autotester against the same
DS1 twice per ticket. `parse_trigger()` now dedupes via
`dict.fromkeys()` (keeps first-seen order) after extracting tokens.
Tested against the exact duplicate patterns from the live run, plus a
regression check that the fused-token fix from v1.59 still holds.
NOTE: `morning_check.py` changed -- restart the server.

---

## v1.59 — 2026-08-06

### Two real bugs found on the first full live Morning Check run
Auth was finally sorted (v1.58) and the run processed all 7 tickets --
surfaced two separate, real bugs along the way.

**1) DS1 trigger parsing fused a DS1 with adjoining note text.**
NOE-4746's description line was `1250: 10.111.131.76 | ts1/7Telamon
Engaged @ 8/5 - 0915AM` -- no separator between the DS1 and the note.
The old parser split on commas/ampersands/whitespace then accepted
anything `startswith("ts")`, so "ts1/7Telamon" (fused, no space) passed
right through as a second "DS1" and got fed to the Autotester, which
correctly failed hop 1 with "No MUSE assignment found for ts1/7telamon".
Fixed: `parse_trigger()` now uses `re.finditer(r'ts\d+/\d+')` to PULL
well-formed DS1 tokens out of the free text directly (same shape
`_ds1_parts()` uses elsewhere in the suite), instead of splitting first
and loosely filtering. "ts1/7Telamon Engaged" now correctly extracts
just `ts1/7` and drops the rest. Tested: fused-token case, clean
multi-DS1 case, no-DS1 (lite) case, and garbage-only DS1 field (falls
back to lite) -- all pass.

**2) JIRA attachment fallback failed with "Missing JIRA credentials"
even with valid credentials.** NOE-4746's combined report (Autotester +
Fiber + OSPF) was too large for an inline comment, correctly triggering
`_fallback_attach()` -- which posted the stub comment fine (using `cred`
directly) but then called `attach_file(ticket, file_path)` with NO
email/token, so `attach_file()` fell through to `load_jira_config()`
(the plaintext-config-only loader) instead of reusing the credentials
already sitting right there in `cred`. Since Morning Check's JIRA_TOKEN
lives in keyring (secure_config.py) rather than the plaintext config
file `load_jira_config()` reads, it found nothing and errored -- even
though the exact same `cred` had just successfully authenticated the
stub comment two lines earlier. Fixed: `_fallback_attach()` now decodes
its own `cred` (base64 "email:token") and passes both through to
`attach_file()` explicitly. This is in the SHARED jira_client.py, so it
fixes the same latent bug for every other tool on the site too, not
just Morning Check -- it just never got exercised before because no
other tool's reports were routinely large enough to hit this fallback
path. Tested with a mocked attach_file() confirming the real
email/token now arrive correctly.
- NOTE: `morning_check.py`, `jira_client.py` changed -- restart the
  server.

---

## v1.58 — 2026-08-06

### Morning Check -- root cause found: token was actually invalid (401), not an account mismatch
v1.57's `/myself` check delivered the real answer: `HTTP 401 -- Client
must be authenticated to access this resource`. The token/email pair is
flat-out rejected. The ticket search still reporting a clean HTTP 200
with 0 issues the whole time was never really a "success" -- confirmed
against Atlassian's own support KB ("JQL search REST API returns 200 OK
without API token"): with bad/missing auth, the search endpoint silently
FALLS BACK TO ANONYMOUS ACCESS and returns a normal-shaped empty result
instead of erroring. So every prior "0 tickets" result across v1.54-v1.57
was this same 401 wearing a disguise.

- `get_my_open_tickets()` now hard-stops the moment `/myself` confirms a
  401/403 -- raises a new `JiraAuthError` instead of continuing on to run
  the search (which would've just silently repeated the anonymous-fallback
  masquerade). `run()` catches it and writes a batch summary that says
  "JIRA AUTHENTICATION FAILED" with the actual reason and the fix
  (regenerate the token at id.atlassian.com -> Security -> API tokens),
  instead of the generic "no tickets found" message.
- Added a second, independent check as defense in depth: Jira's response
  carries an `x-ausername` header naming who the request actually ran as.
  If it ever comes back `anonymous` even after `/myself` looked fine,
  that's caught too.
- Transient failures on `/myself` (network blip, timeout -- NOT a
  401/403) do NOT hard-stop; only a confirmed auth rejection does, so a
  flaky connection can't be mistaken for bad credentials.
- Tested all three paths: confirmed 401 (search never even gets called),
  anonymous-fallback-despite-clean-/myself, and the normal healthy path
  (unaffected, ticket found and processed normally).
- NOTE: `morning_check.py` changed -- restart the server. This is a Jira
  API token problem on your end, not a bug in the tool -- regenerate the
  token and re-enter it once the server's back up.

---

## v1.57 — 2026-08-06

### Morning Check -- surface WHO Jira thinks is authenticated
v1.56's diagnostic came back "currentUser() is not matching any NOE
tickets at all" -- confirming the email/token pair authenticates as A
real Jira identity (HTTP 200), just not necessarily the "Phillip Capps"
one. A clean 200 on the ticket search only proves the token is valid --
it never proves whose account it is.

- Added `_whoami()` -- calls `GET /rest/api/3/myself` (Jira's standard
  "who am I" endpoint) and logs the actual authenticated displayName,
  emailAddress, and accountId, every run, right before the ticket query.
- If that name isn't literally "Phillip Capps", the JIRA email/API token
  entered into the form belongs to a different Atlassian account than
  the one assigned on those NOE tickets -- likely cause: token was
  generated under a different Atlassian ID/SSO email than
  pcapps@bandwidth.com. Re-generate the API token while logged into
  id.atlassian.com AS that exact account, or use whatever email IS
  bound to the token in the "JIRA email" field.
- Degrades gracefully if /myself itself fails (bad token, network) --
  logs the failure but still lets the main query attempt run.
- Tested both the "wrong account" case and the "/myself call itself
  fails" case with mocked responses.
- NOTE: `morning_check.py` changed -- restart the server and re-run;
  the very first JIRA log line will now say exactly who's authenticated.

---

## v1.56 — 2026-08-06

### Morning Check -- ground-truth diagnostic for the still-empty ticket query
v1.55's fixes (new endpoint, `currentUser()`, confirmed "In Progress" is
the right status name in the UI) still came back with 0 tickets on the
next live run. Rather than guess a fourth time, added a diagnostic
fallback that queries the API directly for ground truth instead of
inferring from the UI:

- When the main query (assignee + status filter) matches 0 issues, it now
  automatically re-runs a broader query -- assignee only, no status
  filter -- and logs the result:
  - If THAT also finds 0, the log says so explicitly and points at the
    account/permissions behind the JIRA token as the likely cause, not
    the status filter.
  - If it finds issues, it prints each one's real `status.name` straight
    from the API response, so an exact-string mismatch against
    `"In Progress"` (extra space, different workflow name, case Jira
    itself doesn't normalize the way the board UI displays it, etc.)
    is visible directly instead of guessed at.
- Tested both branches with mocked responses.
- NOTE: `morning_check.py` changed -- restart the server and re-run;
  the log will show which of the two cases this actually is.

---

## v1.55 — 2026-08-06

### Morning Check -- fixed silent zero-match ticket query
Live run after the v1.54 endpoint fix returned HTTP 200 (no error) but 0
tickets, even though the assignee has a real open NOE ticket. Two fixes:

- JQL switched from `assignee = "<email>"` to `assignee = currentUser()`.
  Jira Cloud's "User Email Visibility" privacy setting can make the
  email-string form silently resolve to zero matches (valid 200, empty
  result, nothing that looks like an error) if the API can't map that
  email to an account. Since the JIRA token used already belongs to that
  exact account, `currentUser()` can't have this failure mode --
  `assignee_email` is now display/logging only, no longer part of the
  actual query.
- Added logging for the two things needed to debug this class of issue
  without guessing: the exact JQL sent, and an explicit
  "succeeded but matched 0 issues" line when that happens (previously
  looked identical in the log to nothing happening at all).
- Still unconfirmed: whether the hardcoded `status = "In Progress"`
  filter matches this project's actual workflow status name. If tickets
  still come back empty after this fix, check the ticket's Status field
  value against that string exactly.
- NOTE: `morning_check.py` changed -- restart the server.

---

## v1.54 — 2026-08-06

### Morning Check -- fixed JIRA ticket search (Atlassian removed the old endpoint)
First live demo run hit: `[JIRA] Query returned HTTP 410 -- "The requested
API has been removed. Please migrate to the /rest/api/3/search/jql API."`
Confirmed via Atlassian's own docs/community posts -- `/rest/api/3/search`
is gone, fully replaced by `/rest/api/3/search/jql`.

- `get_my_open_tickets()` now POSTs to `/rest/api/3/search/jql` with a JSON
  body (`{"jql": ..., "maxResults": 50, "fields": [...]}`) instead of
  GETting the old endpoint with query params.
- The new endpoint also dropped `startAt`/`total` in favor of
  `nextPageToken`-based pagination -- added a loop (capped at 20 pages /
  1000 issues) instead of assuming everything fits on one page.
- Tested with mocked responses: single page, multi-page via
  `nextPageToken`, and the exact HTTP 410 case from the live run (returns
  an empty list + logs the error, doesn't crash the job).
- NOTE: `morning_check.py` changed -- restart the server.

---

## v1.53 — 2026-08-06

### Morning Check -- save JIRA token/email + Slack webhook (opt-in exception)
Per P. Capps: retyping the JIRA API token and Slack webhook URL every run
was annoying enough to fix. Morning Check now persists four fields --
`JIRA_ASSIGNEE_EMAIL`, `JIRA_EMAIL`, `SLACK_WEBHOOK` (plaintext in
`autotester_config.txt`) and `JIRA_TOKEN` (Windows Credential Manager via
`secure_config.py`/keyring) -- the same split the CLI path already used,
just now reachable from the web form too.

- Type a value once and it's saved; leave any of those four fields blank
  on a later run and it falls back to the saved one. Infra/GSX device
  passwords are explicitly NOT part of this -- they stay session-only,
  same as every other tool on the site.
- This is a deliberate, scoped exception to `app.py`'s stated
  "credentials are POSTed fresh every run, never written to disk" rule --
  documented inline at the route so it reads as an intentional choice,
  not a silent policy drift.
- Added `secure_config.load_config_silent()` -- same as `load_config()`
  but never calls `getpass()` for missing secure keys. `load_config()`'s
  interactive prompt would have hung the Flask request (and the whole
  request thread, not just a terminal) the first time JIRA_TOKEN wasn't
  set yet. The web route uses the silent version exclusively.
- The GET page now prefills assignee email / JIRA email / Slack webhook
  from what's saved (JIRA token is never echoed back into the page,
  even though it's stored -- only used server-side).
- Tested end-to-end with a stubbed config store: save-on-first-submit,
  prefill-on-reload, fallback-when-blank, and confirmed device passwords
  never touch the persistence layer.
- Also added a TODO entry (Known Issues / Watch List) to rotate the
  Slack webhook that got pasted into a chat session on 2026-08-06.
- NOTE: `app.py`, `secure_config.py`, `templates/morning_check.html`
  changed -- restart the server.

---

## v1.52 — 2026-08-06

### Morning Check -- wired up + review fixes
A separate chat drafted a new tool (`morning_check.py` + `secure_config.py`,
a Windows-Credential-Manager credential store) that scans open NOE tickets
assigned to you, runs Autotester/Fiber/OSPF per a trigger line in each
ticket's description, posts results to JIRA, and sends a Slack digest.
Files landed in the folder already; this release wires it up and fixes
what a review turned up:

- **Template was in the wrong place.** `morning_check.html` was sitting in
  the project root instead of `templates/`, where Flask's loader actually
  looks -- `render_template("morning_check.html")` would have thrown
  `TemplateNotFound` and the page would 500 on first load. Moved the
  correct copy into `templates/`; the stray root copy is preserved (not
  deleted) at `legacy/morning_check.html.retired`.
- **Applied the app.py wiring** from the staged `app_morning_check_additions.txt`:
  import, `TOOL_URLS`/`_TOOL_MODULES` entries, the `/tools/morning-check`
  and `/api/morning-check/run` routes. Reachable from the home page now.
- **"Open results files when done" did nothing.** `morning_check.run()`
  collected the checkbox but never read it -- every sub-tool call hardcoded
  `open_when_done: False`. Wired it up, but scoped to open the ONE
  consolidated batch summary (see below), not every per-ticket/per-tool
  file -- opening 3xN Notepad windows for a multi-ticket run would've been
  worse than doing nothing.
- **Report panel / Download would always be dead.** `run()` never returned
  a path (by design -- it writes one file per ticket, not one combined
  report), so the web UI's "done" panel always showed the generic
  "finished without producing a report file" message and Download 404'd.
  Added `_write_batch_summary()` -- one small consolidated file (ticket,
  mode, status, findings) written at the end of every run, including the
  "no tickets found" and dry-run early-exits (previously those paths
  didn't even reach a return statement). `open_when_done` now opens this
  file specifically.
- **`keyring` was missing from `requirements.txt`.** `secure_config.py`'s
  Windows Credential Manager storage depends on it; without it the module
  degrades gracefully (no crash) but silently re-prompts for every
  password on every run instead of ever actually storing them. Added
  `keyring>=24.0`.
- **Misleading Slack-webhook hint.** The form said the webhook URL is
  "saved to config" -- true only on the CLI path (`collect_inputs()`);
  the new web route never persists it (matches this app's stated
  design: credentials/URLs posted from the browser are never written to
  disk). Corrected the hint instead of adding web-side persistence, to
  keep that existing architecture promise intact.
- Gave `morning_check.py`'s `TOOL_INFO['order']` a value of 80 -- it was
  70, a duplicate of the DS1 Test Builder's, which left home-page card
  ordering between the two non-deterministic.
- Added an `open_when_done` prompt to the CLI path (`collect_inputs()`)
  for parity with the web form.
- NOTE: `app.py`, `morning_check.py`, `requirements.txt`,
  `templates/morning_check.html` changed/added -- restart the server
  (Python + new route) and run `pip install -r requirements.txt` to pick
  up `keyring`.

---

## v1.51 — 2026-08-05

### GSX Monitor -- opt-in "Clear counters first" (its first-ever write command)
- Root cause of "same PMs every run today": the GSX Monitor was always
  read-only by design (matches its "(read-only)" label) -- it only ever
  sent SHOW T1 <id> STATISTICS. Clearing counters was STEP 2 of the
  generated test script, meant to be run manually on the GSX CLI; if that
  manual step wasn't run between today's tests, the STATISTICS counters
  were genuinely cumulative and correctly showing the same numbers. Not a
  bug -- confirmed with P. Capps and added the missing option instead.
- New checkbox on the GSX Monitor form: "Clear counters before
  monitoring." Unchecked by default (stays fully read-only). When
  checked, sends CONFIGURE T1 <id> PERFMON RESET COUNTERS ALL exactly
  once, right after connecting and before the first poll.
- Per P. Capps: this is the ONLY config-write command permitted anywhere
  in this tool for now, and it's hardcoded, not parameterized -- the only
  variable in it is t1_id, which is regex-validated (T1-1-\d+-\d+-\d+)
  before this code path ever sees it. No form field or future param can
  turn this into a different command. Any other write action needs
  separate explicit sign-off before it gets added.
- Heading changed from "GSX Monitor (read-only)" to "GSX Monitor
  (read-only by default)" so the page stays accurate either way.
- NOTE: `ds1_gsx_test.py`, `app.py`, `templates/ds1_gsx_test.html`
  changed -- restart the server AND hard refresh (Ctrl+Shift+R).

---

## v1.50 — 2026-08-05

### DS1 Test Builder -- JIRA comment now separates prechecks too
- Extended the v1.49 split to the JIRA post, not just the web page. The
  comment now has TWO collapsed panels instead of one: "Prechecks (live
  output, already run)" first, then "DS1 Test Script -- <device> <test>
  (<host>)" holding STEP 2 onward -- same split, same order, as the
  on-screen view.
- `ds1_gsx_test.py`: added `_split_precheck_section()`, a Python port of
  job.js's `renderReport` regex (pulls the STEP 1/PRECHECKS block out of
  the assembled script text). Falls back to (None, unchanged text) if the
  markers aren't found, matching the web side's safe fallback.
- `jira_client.py`: `_build_report_adf()` / `post_report()` gained
  optional `precheck_text` / `precheck_title` params -- when given, that
  content gets its own `expand` panel ahead of the main one. Every other
  caller (Autotester, DS1 PMs, interface check, fiber util, health check,
  OSPF events, NPT-1250 discovery) is unaffected -- they don't pass these,
  so they get exactly the one-panel ADF they always did.
- The downloaded .txt file and any size-fallback file attachment are still
  the single complete script (prechecks included) -- only the inline JIRA
  comment content and the on-screen preview split it into two panels.
- Also fixed the JIRA size pre-check to count precheck_text + report_text
  together against the 30k-char inline limit (previously would've only
  measured report_text, undercounting once precheck_text became separate).
- NOTE: `ds1_gsx_test.py`, `jira_client.py` changed -- restart the server
  for this one (Python, not template/static).

---

## v1.49 — 2026-08-05

### DS1 Test Builder -- prechecks split into their own expand box
- On-screen report now pulls the "STEP 1 -- PRECHECKS (already run -- live
  output below)" block out of the main script preview and into its own
  collapsed `<details>` box above it ("Prechecks (live output, already
  run) -- click to expand"). The actual copy/paste script (STEP 2 onward)
  is what's immediately visible, without the live device output pushing
  it down the page.
- Done entirely client-side (job.js) by pattern-matching the STEP 1/STEP 2
  headers already embedded in the report text -- the downloaded .txt file
  and the JIRA-attached script are UNCHANGED (still one complete file with
  prechecks included, for the record). Only the on-screen split view is new.
- Generic and safe for every other tool: if a report's text doesn't contain
  that STEP 1/STEP 2 shape (true for every tool except this one), the box
  stays hidden and the report renders exactly as before.
- NOTE: `job.js`, `style.css`, `templates/_job_panels.html` changed --
  hard refresh (Ctrl+Shift+R).

---

## v1.48 — 2026-08-05

### DS1 Test Builder -- restored left column title
- Side-by-side layout (v1.47) left the build column without a heading,
  since it previously just inherited the page's top-level h1. Added a
  "Build Test Script" heading matching the "GSX Monitor (read-only)"
  heading style on the right, so both columns look intentional.
- NOTE: `templates/ds1_gsx_test.html` changed -- hard refresh (Ctrl+Shift+R).

---

## v1.47 — 2026-08-05

### DS1 Test Builder page -- side-by-side layout, separate logs, Stop removed
- Build Test Script and GSX Monitor now sit in two columns side by side
  (stacks to one column on narrow windows) instead of stacked full-width.
- Each box now gets its OWN status/log and report panel underneath it,
  instead of sharing one. Root cause of the earlier "Stop does nothing"
  report: the Stop button's click handler was bound once to whichever
  form initialized first (the build form), so clicking it while the
  monitor was running acted on the wrong job's id. Splitting the panels
  fixes that class of bug generally, not just for Stop.
- Removed the Stop button entirely per request -- it wasn't reliable
  enough to keep. The underlying `/api/job/<id>/stop` endpoint and
  `stop_event` plumbing are left in place server-side (harmless, unused)
  in case a properly-scoped Stop gets added later.
- `_job_panels.html` now takes an optional `panel_suffix` (set via
  `{% set panel_suffix = 'foo' %}` before including) so a page can embed
  multiple independent status/report pairs; omitted entirely it falls
  back to the original `statusPanel`/`reportPanel` ids, so every other
  single-form tool page is unaffected.
- NOTE: `job.js`, `style.css`, and `templates/_job_panels.html` +
  `templates/ds1_gsx_test.html` changed -- hard refresh (Ctrl+Shift+R).

---

## v1.46 — 2026-08-05

### JIRA-posted alert -- bigger, louder, ticket link
- Confirmed working as of v1.44 (the in-page toast fired correctly) but
  was easy to miss: small, top-right corner, opposite side of the screen
  from the form. Rebuilt as the primary "you can continue now" signal
  since native OS desktop notifications turned out to be silently
  swallowed on this machine (Chrome permission + Windows notification
  settings were both fine -- almost certainly Focus Assist or a
  quiet-notification policy outside our control; not worth chasing
  further since this replacement is more reliable anyway):
  - Full-width banner across the TOP of the page (can't end up "off to
    the side" of the content), bigger bold text, drop-in animation,
    20s display / click-to-dismiss.
  - Auto-detects the ticket number in the JIRA result line and adds an
    "Open NOE-XXXX" link straight to the ticket.
  - Audible two-tone chime (Web Audio API, no external files) -- rising
    tone on success, falling tone on failure -- so it's noticeable even
    if you're not looking at the screen. Unlocked from the form-submit
    click (browser autoplay policy requires a real user gesture) and
    reused when the async JIRA result arrives later.
  - Desktop notification + failure alert() kept as extra fallback layers.
- NOTE: `job.js` + `style.css` changed -- hard refresh (Ctrl+Shift+R).

---

## v1.45 — 2026-08-05

### DS1 Test Builder -- "may be hung" warning was firing on normal runs
- Investigated a run that looked stuck at "[1800] running prechecks ..."
  for 5+ minutes -- it wasn't hung, it finished normally at 6m46s. Root
  cause: `_warn_after()`'s 300s (5 min) floor is tuned for single/dual-
  hop tools, but this tool walks 4 live hops per DS1 (1250 -> 1800 ->
  MUX -> GSX) with several commands each, so a normal single-DS1 run
  routinely runs 6-7 minutes. Bumped this tool's floor to 480s (8 min)
  so the warning banner stops firing on ordinary runs.
- No functional bug found or fixed -- the earlier "something broken"
  read was the warning banner's timing, not a hang.

---

## v1.44 — 2026-08-05

### Web -- JIRA "posted" notification
- When the live log shows the JIRA post result, the browser now notifies
  the user that the script/report is on the ticket and they can continue
  (`job.js`, `style.css`). Three layers:
  - Desktop notification (works even from another tab/window) --
    permission requested on first job submit.
  - In-page toast, top-right, green border on success / red on failure,
    auto-dismisses after 12s or on click.
  - alert() popup as last resort on FAILURE when desktop notifications
    are denied (success stays non-blocking).
- Works for every tool automatically -- it keys off the "[JIRA] Comment
  posted / attached / FAILED" lines all tools already print. Once per
  job.
- NOTE: `job.js` + `style.css` changed -- hard refresh (Ctrl+Shift+R).

---

## v1.43 — 2026-08-05

### GSX Monitor -- Stop button, clean output, deltas; script in JIRA
- **Stop button**: generic stoppable-job plumbing (`threading.Event` per
  job, `POST /api/job/<id>/stop`, `stoppable` flag in status polls). The
  GSX Monitor sleeps in 1s steps so a Stop lands within ~a second; the
  log ends with "[GSX Monitor] STOPPED by user after N poll(s)". A red
  Stop button appears in the status panel only while a stoppable job is
  running. Non-stoppable jobs reject the endpoint (400).
- **Monitor output deduped**: counter lines containing "Alarm" matched
  both the counter regex and the status/alarm keyword scan, printing
  everything twice. Now parsed once with a seen-set.
- **"+N since last poll" deltas**: the counters are cumulative, and the
  test question is "are errors still incrementing while my loop is up?"
  -- so a counter that climbs between polls is tagged
  "<-- +N since last poll". Static counters print plain.
- **Generated script now posts as an expand panel in the JIRA comment**
  (switched the Test Builder to post_report): intro line, visible
  per-circuit summary, collapsed "DS1 Test Script -- <device> <test>"
  panel with the FULL script (previous post_comment path truncated to
  4000 chars under a misleading panel title), file attachment as before.
- Known issue (parked per P. Capps): GSX creds still not auto-carrying
  from the build form to the monitor form on some runs -- workaround is
  typing them in the monitor form.
- NOTE: `job.js` + `style.css` changed -- hard refresh (Ctrl+Shift+R).

---

## v1.42 — 2026-08-05

### Fixes from first live DS1 Test Builder run
- **GSX Monitor creds not forwarded**: the cred-copy submit listener was
  registered AFTER job.js's submit handler (same element = registration
  order), so blank creds were read before the copy ran. Registration
  reordered -- "blank = reuse from above" now actually works.
- **JIRA post produced nothing**: root cause was in `jira_client._build_adf`
  -- the Test Builder posts with an empty PM-panel text, and ADF rejects
  empty text nodes, so Jira 400'd the whole comment. Panels with no
  content are now omitted (guarded in `_build_report_adf` too). Also:
  a checked "Post to JIRA" box with a blank ticket is now a form error
  instead of a silent skip, and the tool logs "[JIRA] skipped -- no
  ticket provided" when applicable.
- **"[K" artifacts (root cause, all tools)**: `device_base`'s reader
  thread pumps 1 byte at a time and ANSI-stripped per byte -- a
  multi-byte sequence like \x1b[K never matched, survived into reports,
  and browsers displayed the bare "[K" after eating the invisible ESC.
  `_clean()` now strips ANSI over the whole assembled output. (Belt &
  suspenders: `mux_loop_script._capture` also strips.) Fixes the same
  artifact previously seen in Autotester PM sections.
- **ROUTE section now lists every device with its IP** per DS1 (1250,
  1800 + OC path, MUX, GSX + T1 assignment).
- **Every command section now uses the STEP 6 format** -- "On <device>
  (<ip>):" header before the commands (STEPs 2, 3, 4, 5, 6; grouped per
  GSX node for multi-DS1 runs).
- **STEP 4 (MONITOR) now includes the clear-counters command** per DS1
  ("clear once the test is up, then poll"), alongside the SHOW commands.

---

## v1.41 — 2026-08-05

### DS1 Test Builder -- GSX Monitor auto-fill
- The user now only ever types the 1250 IP + DS1(s) (+ creds once). The
  build job publishes its resolved GSX targets (node, IP, T1 assignment
  per DS1) mid-run through the job-status feed (`ds1_gsx_test.run()`
  gained an optional `result_sink`; `/api/job/<id>` returns it as
  `extras`), and the page auto-fills the GSX Monitor's IP + assignment
  the moment the route resolves -- no digging through the script text.
- Multiple DS1s render as clickable target chips ("ts1/1 -> T1-1-10-1-15
  @ 10.x.x.x") that fill the monitor form on click.
- GSX credentials carry over from the build form to the monitor form
  automatically when left blank ("blank = reuse from above").
- Plumbing is generic (`job.js` exposes a per-poll `window.onJobData`
  hook; any tool can publish `extras`) -- CLI behavior unchanged
  (`result_sink` defaults to None).
- NOTE: `job.js` changed -- hard refresh (Ctrl+Shift+R) to pick it up.

---

## v1.40 — 2026-08-05

### NEW TOOL: DS1 Test Builder (AIS / Loops to GSX) -- `ds1_gsx_test.py`
- Single-page DS1 test workflow: resolve the full route per DS1 (1250 ->
  1800 -> MUX -> GSX, same chain as the Autotester/MUX Loop tool), run
  live read-only prechecks, then generate a copy/paste test script.
  **Nothing config-touching is ever executed.** Script sections:
  1. ROUTE (per-DS1: MUSE, 1800 OC path, GSX node + T1 assignment)
  2. PRECHECKS (live output embedded: 1250 status/PMs, 1800 OC
     PMs/alarms, GSX STATUS/STATISTICS)
  3. CLEAR GSX COUNTERS (CONFIGURE T1 ... PERFMON RESET COUNTERS ALL
     per DS1 -- per P. Capps, GSX clears only)
  4. TEST ON (AIS or loop commands for the chosen device + direction)
  5. MONITOR (GSX SHOW commands to repeat, or use the web monitor)
  6. TEST OFF / BACK-OUT (always-run section)
  7. POSTCHECKS (commands only)
- **Never-guess token extraction**: the 1250's ces-card type and the
  1800's slot/card/port/sts-1/vt-15 tokens are read from each device's
  own config render (grep "chassis slot <ts> ces-card"), and the 1800's
  sts/vt is cross-checked against the resolved OC string. Every change
  command is stamped [VERIFIED against config: <actual line>] or a loud
  [UNVERIFIED -- ...] with exactly what didn't match. Prod-safe by
  construction.
- Directions supported (per P. Capps' command reference 2026-08-05):
  - 1250 AIS: toward GSX (force-ais-psn-bound) / toward LEC
    (force-defect-tdm-bound ais); turn-down = delete of the same path.
  - 1250 loops: facility (faces LEC) / terminal (faces BW core);
    down = maintenance loopback release.
  - 1800 AIS (**SYNTAX UNVERIFIED -- flagged with a warning banner in
    the UI and the generated script**): toward 1250 (psn-bound) /
    toward MUX/GSX (tdm-bound ais) / disable (both directions).
  - 1800 loops: facility / terminal (orientation notes marked "verify").
- **GSX Monitor** (web page, read-only): polls SHOW T1 <id> STATISTICS
  at a chosen interval (5-300s) for a chosen duration (30s-1hr),
  streaming a timestamped snapshot (status/alarm lines + any non-zero
  counters) into the live log each poll -- watch errors move (or stay
  flat) while a loop/AIS test is up. Fixed duration since jobs can't be
  killed yet.
- Web: /tools/ds1-gsx-test with dynamic direction dropdown per
  device/test combo; JIRA posting includes the visible summary + full
  script attachment. CLI launcher picks the tool up automatically
  (order 70). `job.js` now supports multiple forms per page.
- Verified: token extractors (exact match / mismatch->UNVERIFIED /
  missing->UNVERIFIED), all command builders against the provided
  reference syntax (incl. oc48 variant + delete forms), web validation
  paths, monitor job lifecycle.

---

## v1.39 — 2026-08-05

### All tools -- visible findings summary on every JIRA post
- `post_report()` gained `summary_text` (mirroring `post_comment`):
  an always-visible findings block on the ticket, in BOTH the normal
  inline post and the too-large attachment fallback. No more bare
  "see attached file" comments from any tool.
- Per-tool summaries:
  - **OSPF Events + Fiber Levels**: per-event "Date  Time  DOWN/FULL"
    lines (timestamp parsed from each event; unparseable lines echoed
    raw so nothing is dropped), count header, capped at 40 per category
    with a "+N more" pointer. Fiber light levels as
    "ts2  tx -6.4 dBm | rx -6.0 dBm" per slot.
  - **Fiber Utilization**: per-port "IN cur/max | OUT cur/max" in Mbps.
  - **DS1 PMs**: one line per DS1 -- the device's own physical status
    row + NE alarm count (first alarm quoted).
  - **1250 Health Check**: NE/Chassis alarm counts (first rows quoted),
    OSPF neighbor / LDP session line counts, and any DS1 status rows
    flagged LOS/LOF/AIS/DOWN across all CES slots.
  - **NPT-1250 Discovery**: fiber/CES slot inventory, active MUSE DS1
    count + list, alarm section counts, OSPF/LDP line counts.
  - **GSX Perfmon**: T1 operational/alarm/admin status lines + non-zero
    current-day error counters (or "none").
  - **IXC Check**: OSPF neighbor + LDP adjacency found/none (row
    quoted), ping results parsed to "4/4 received (0% loss)" for both
    the standard and 8000B DF pings.
  - **MUX CEM Loop Script** (both modes): resolved circuit path
    (1250/DS1/MUSE, 1800/OC, MUX ACR/CEM/conn, GSX node/IP) + reminder
    that loop commands are generated, not executed.
- Verified: all 10 files compile; OSPF summary formatting across three
  timestamp formats + no-timestamp fallback; post_report summary
  placement on both the normal and fallback paths.

---

## v1.38 — 2026-08-05

### JIRA summary -- per-DS1 REVIEW line (standardized diagnosis matrix)
- **Every DS1 section in the JIRA summary now ends with a REVIEW line**
  (`output_builders.py`: `_ds1_review()`): a brief paragraph that states
  what was seen, explains WHY the conclusion follows, and names the next
  step -- not just "Engage X". Derived entirely from the standardized
  per-hop Clean/Dirty verdicts, walked bottom-up: faults cascade
  downstream (away from the LEC, toward the GSX), so the lowest dirty
  hop is treated as the root cause. The finite matrix:
  - Route incomplete -> fix Netbox, re-run before concluding.
  - All clean -> no active fault at test time; suspect intermittent /
    outside the tested span. (Notes when CES PMs were disabled.)
  - 1250 TDM LOS -> LEC handoff; everything above is echoing the loss.
  - 1250 TDM errored (no LOS) -> marginal LEC signal; monitor.
  - 1250 CES dirty, rest clean -> packet transport 1250<->1800, not the
    physical circuit; re-run to distinguish historical vs climbing.
  - 1800 1250-side dirty -> NNI/EVC segment (optics/fiber).
  - 1800 MUX-side dirty -> SONET transport 1800<->MUX (Peerless
    variant: toward the Peerless handoff; engage Peerless).
  - MUX 1800-side dirty (non-AIS) -> MUX facility / CEM connection.
  - MUX GSX-side dirty -> T3 span MUX<->GSX.
  - GSX dirty, route clean -> the GSX port itself.
  - Multiple dirty -> root named first; downstream dirt flagged as
    likely knock-on effects.
- DS1_AIS cases keep their existing (more specific) VERDICT paragraph
  and skip the generic REVIEW -- no duplication.
- Verified all 9 matrix branches with synthetic runs.

### Web -- version number in the watermark
- Watermark now reads "Built by P. Capps · vX.XX" on every page
  (`app.py`: APP_VERSION constant + context processor, `base.html`).
  One place to bump per release, kept in sync with this changelog.

---

## v1.37 — 2026-08-05

### JIRA comment -- per-DS1 brief summary, always visible
- **New `build_jira_summary()`** (`output_builders.py`): builds a compact
  per-DS1 block for the JIRA comment -- DS1 assignment as a header, that
  DS1's per-hop Clean/Dirty status on readable lines beneath it, plus any
  DS1_AIS VERDICT paragraph lifted from that DS1's diagnostic summary.
  Repeated per DS1, since each channel can have different symptoms.
- **The summary is always visible on the ticket** (`jira_client.py`,
  `autotester_standalone.py`):
  - Normal-size reports: summary appears as plain text right under the
    intro line, above the existing collapsed panels.
  - Too-large reports (the attachment fallback that fired on the 7-DS1
    run): the stub comment now carries the same visible summary -- no
    more bare "see attached file" with zero at-a-glance info. When the
    Run Summary + Diagnostic block also fits, it rides along in a
    collapsed panel underneath as before.
- Verified with synthetic 3-DS1 run (clean / route-FAIL / DS1_AIS with
  verdict): summary renders per-DS1, normal ADF = paragraph > summary >
  2 expands, fallback stub ADF = paragraph > summary > optional expand.

---

## v1.36 — 2026-08-05

### JIRA posting -- fix indefinite hang on large reports
- Symptom: multi-DS1 Autotester run stuck at "[JIRA] Posting results to
  NOE-XXXX ..." indefinitely. Root cause: requests' timeout= only covers
  waiting for RESPONSE bytes -- it does NOT bound the upload/send phase.
  A proxy/VPN that accepts the connection but stalls consuming a large
  upload (a 7-DS1 report builds a multi-hundred-KB JSON comment) blocks
  requests.post() forever, so the 30s timeout never fired. Small runs =
  small payload = worked; large runs could hang. Three fixes
  (`jira_client.py`):
  1. **Watchdog** (`_bounded()`, 240s ceiling): post_comment/post_report
     now run their network work in a bounded thread. A stall comes back
     as "[JIRA] FAILED: TIMED OUT after 240s ..." with a reminder that
     the report file is already saved locally -- the job finishes
     normally instead of hanging.
  2. **Size pre-check** (COMMENT_TEXT_LIMIT = 30k chars): reports over
     Jira's inline comment limit were guaranteed a CONTENT_LIMIT_EXCEEDED
     rejection anyway -- and that giant JSON upload was exactly the
     payload most likely to trigger the send-phase stall. Now oversized
     reports skip the comment attempt and go straight to the existing
     stub-comment + file-attachment fallback.
  3. **Progress prints** in the fallback path ("report text ~N KB --
     over the inline comment limit...", "posting stub comment ...",
     "uploading attachment X (N KB) ...") -- the JIRA phase is no longer
     silent between "Posting results ..." and the final result, so the
     web live log / console shows exactly which step it's on.
- Applies to all tools (Autotester via post_comment; the others via
  post_report). Verified watchdog timeout, fast-path passthrough, and
  size-bypass routing with synthetic tests.

---

## v1.35 — 2026-08-05

### Autotester -- DS1_AIS cross-hop correlation VERDICT
- **The static "(AIS from LEC/1250 direction ...)" note is replaced with
  an automated troubleshooting verdict** (`output_builders.py`:
  `_ais_verdict()` + `_wrap_note()`). When the MUX reports DS1_AIS, the
  diagnostic summary now correlates it against the same run's upstream
  data and prints a brief paragraph walking the logic and naming the
  next action. Finite symptom matrix, checked in priority order:
  1. 1250 TDM LOS/LOF -> LEC handoff failure (1250 is generating the
     AIS). Engage the LEC.
  2. 1250 receiving AIS -> failure deeper in the LEC network; 1250 is
     passing it through. Engage the LEC.
  3. 1250 TDM clean but middle dirty (CES/EVC/NNI/OC, named in the
     verdict) -> transport fault between 1250 and MUX; work that
     segment first.
  4. 1250 TDM errored (no hard LOS/AIS), path clean -> marginal LEC
     signal; monitor.
  5. Everything clean -> the 1800 is inserting AIS into an unequipped /
     mis-stitched VT (provisioning or mapping gap -- clean PMs are
     EXPECTED here since AIS transports without errors). Verdict names
     the exact MUSE circuit, OC/STS/T1, ACR/CEM to verify, and the MUX
     'show cem circuit' Lbits corroboration check.
- Rationale: AIS is generated at the point of failure and travels
  downstream only, so the AIS origin is fully determined by data the
  Autotester already collects in the same run (1250 TDM state, CES/EVC/
  NNI/OC health). Verified all 5 branches with synthetic inputs.
- `_wrap_note()` wraps verdicts to report width without splitting
  interface names on hyphens.

---

## v1.34 — 2026-08-05

### Autotester -- diagnose GSX node/slot/port unresolved (None-None-None-N)
- Confirmed the "FLAG: WARNING: Netbox not populated -- MUX description
  fallback used..." message only ever fires from the Tier-2 MUX-controller-
  description branch in `mux4216_client.py` -- i.e. it's proof that branch
  DID run and DID succeed (found the ACR via the MUX's SONET-ACR
  description), not the Tier-3 shotgun-guess fallback. That message and
  a Tier-3 "legacy ACR-guess used" message are mutually exclusive, so
  seeing the Tier-2 wording means Tier-2 worked.
- The separate "GSX None-None-None-N" symptom is unrelated to that --
  it comes from a second, independent description lookup (the GSX-side
  T3 controller's description, parsed for a NODE-SLOT-PORT-T1 naming
  convention like `LAXGSX04-11-2-14`) failing to resolve. Root cause is
  almost always that the T3 controller simply has no description
  configured, or one that doesn't match the expected 4-part format --
  a provisioning-documentation gap, same family of issue as an
  unpopulated Netbox entry, just on the GSX side of the MUX instead.
- `mux4216_client.py`: added a `gsx_flag` on the mux result dict --
  fires a specific warning ("no description configured" with the raw
  command output, or "doesn't match expected format" with what was
  found) whenever node/slot/port can't be derived, instead of silently
  leaving them `None` with no trail to follow.
- `autotester_standalone.py`: surfaces `gsx_flag` as its own FLAG line
  in the live progress log (same place `netbox_conflict` already
  showed), and the "GSX {node}-{slot}-{port}-{t1}" progress line now
  prints `?` instead of `None` for any unresolved field. (The polished
  report already did this via `build_route_block`'s `or "?"` -- this
  was only a console/live-log cosmetic gap.)

---

## v1.33 — 2026-08-05

### Web Front End — pass/fail rollup + attribution
- **Overall PASS/FAIL badge on finished reports** (`app.py`, `job.js`,
  `_job_panels.html`, `style.css`) -- scans the Run Summary section
  (currently only the Autotester writes one) for any "Dirty" tag or
  "FAIL" line across all DS1s and shows one glance-check badge above the
  report: green "PASS -- N DS1(s) clean" or red "ISSUES DETECTED --
  <DS1s>". No change to the underlying report text or to any tool's
  logic -- the existing per-DS1 PASS/FAIL in the Run Summary only ever
  reflected whether the route resolved, not hop health, so this adds the
  rollup that was missing rather than replacing anything. Tools that
  don't emit a Run Summary show no badge (nothing to roll up yet).
- **Subtle attribution watermark** on every page (`templates/base.html`,
  `style.css`) -- "Built by P. Capps", fixed bottom-right, low opacity,
  inherited automatically by every tool page and the home page.

---

## v1.32 — 2026-08-05

### Web Front End — live step-by-step progress log
- **Status panel now shows a scrolling log while a job runs**, not just an
  elapsed timer. No changes to any tool's actual logic -- every tool
  already `print()`s progress as it works ("[connecting] ...",
  "[GSX] resolving ...", etc.); `app.py` now captures those same lines
  per-job via a small thread-aware stdout tee (`_JobStdoutTee`) and hands
  them back through `/api/job/<id>` as a `log` list. `job.js` appends only
  the new lines each poll and auto-scrolls.
- Concurrent jobs stay correctly separated (each background thread's
  writes are routed to its own job's log via a thread-id map) -- verified
  with two simultaneous runs against different IPs.
- This was the natural next step flagged when the polling architecture
  was first built (v1.29): the background-thread + poll scaffolding
  needed no structural changes, just a place to route the existing print
  output.

---

## v1.31 — 2026-08-04

### Web Front End — all 9 tools + home page
- **Home page** (`templates/home.html`) -- lists every tool as a card
  (name, which device it runs from, description, what it requires),
  pulled live from each module's own `TOOL_INFO` dict and sorted by the
  same `order` the CLI launcher menu uses. `/` now renders this instead
  of going straight to the Autotester form.
- **Remaining 8 tools wired up** (`app.py`, new templates): NPT-1250
  Discovery, DS1 PMs, Fiber Utilization, OSPF Events + Fiber Levels,
  1250 Health Check, GSX Perfmon (96 Intervals), IXC Check, and the MUX
  CEM Loop Script -- each gets its own page/route/`/api/.../run` endpoint,
  following the same pattern as the Autotester: no `collect_inputs()`,
  same background-job + polling architecture, same "never persist
  credentials" rule.
  - Discovery / Fiber / OSPF / Health share one route helper
    (`_simple_1250_route`) since all four take identical inputs (1250 IP
    + infra creds).
  - DS1 PMs re-validates entries server-side with the tool's own
    `_parse_and_validate()` before launching the job, so bad slot/channel
    input comes back as an immediate form error instead of a failed run.
  - GSX Perfmon reuses the tool's own `_parse_t1_id()` for the same reason.
  - IXC Check's site dropdown is checked against `interface_check.SITE_TO_1800`.
  - MUX CEM Loop Script's form branches on 1250+DS1 vs. MUX-direct mode.
    MUX-direct never opens a live GSX session (GSX info there is only
    embedded as "run manually" text), so GSX credentials are optional in
    that mode -- only required in 1250+DS1 mode, matching what the code
    actually uses.
- **`app.py` generalized**: job-runner/polling machinery (`_launch_job`,
  `_run_job`, `/api/job/<id>`, `/download/<id>`) is now tool-agnostic,
  taking a `run_callable` closure per tool instead of being hardcoded to
  the Autotester.
- **`templates/index.html` retired** -- superseded by `base.html` +
  per-tool templates + `home.html`. Left on disk (not referenced by any
  route) since it couldn't be removed in this environment; safe to
  delete manually.
- Verified: `py_compile` across all tool modules + `app.py`, plus a live
  smoke test of every `/tools/*` page (200) and one job run per tool
  shape (bad-input validation returns 400s with the right messages, and
  unreachable-device runs finish as a graceful `error`/no-report status
  rather than crashing the server).

---

## v1.30 — 2026-08-04

### Web Front End
- **"Open results file when done" checkbox** (`templates/index.html`,
  `app.py`) -- exposes the same `open_when_done` behavior the CLI's
  "Open results file when done? (y/n)" prompt has always had. Was
  previously hardcoded to `False` for the web app (auto-opening a file only
  makes sense when the server and browser are the same machine). A caption
  under the checkbox says so directly, and it stays unchecked by default.

---

## v1.29 — 2026-08-04

### New: Web Front End (Proof of Concept)
- **`app.py` + `templates/index.html`** -- a small Flask app that runs the
  Autotester from a browser instead of a terminal. See `WEB_SETUP.md` for
  the full walkthrough. Highlights:
  - `collect_inputs()` is never called from the web layer (it uses
    `input()`/`getpass()`, which would hang with no terminal attached) --
    the web form builds the same `params` dict directly and calls
    `autotester_standalone.run(params)`, the exact same code path as the CLI.
  - Each run happens in a background thread; the browser polls a status
    endpoint every ~2s showing an elapsed timer, and flags a "may be hung"
    warning past a threshold (5 min, scaling up slightly for multi-DS1
    runs) -- without literally blocking an HTTP request for the run's
    duration.
  - Credentials are submitted fresh per run and held only in memory for
    that job -- never persisted.
  - Finished report renders on the page plus a download link for the same
    `.txt` the CLI would have written.
  - `autotester_standalone.run()` now returns the output file path (was:
    returned nothing) so the web layer can retrieve it. No change to CLI
    behavior -- `main()` still ignores the return value.
  - `requirements.txt` gained `flask>=3.0`.
  - Known POC limitations (in-memory job store, no real job-kill, dev
    server only) and the checklist for eventually moving this onto a
    shared network host are documented in `WEB_SETUP.md`.

---

## v1.28 — 2026-08-04

### Report Format
- **Run Summary standardized to Clean/Dirty per hop/side** (`output_builders.py`)
  - Replaced the itemized-counter findings line (raw counter names + values,
    plus a trailing "Suggest: ..." fault-direction phrase) with a fixed,
    standardized format:
    ```
    TS1/7  --  PASS  |  1250: TDM Clean / CES Dirty (RxPsnES)  |  1800: 1250-side Clean / MUX-side Clean  |  MUX: 1800-side Clean / GSX-side Clean
    ```
    - **1250**: Clean/Dirty on TDM vs CES
    - **1800**: Clean/Dirty on 1250-side (NNI + EVC) vs MUX-side (OC PM +
      alarms) -- Peerless circuits label the second side "network-side"
      since there's no Bandwidth MUX beyond a Peerless 1800 hop
    - **MUX**: Clean/Dirty on 1800-side (facility alarms + connection/xconnect
      state) vs GSX-side (T3 controller counters)
    - Dirty tags name the counter/alarm that tripped it (e.g. `(RxPsnES)`,
      `(LP_AIS)`) -- never the raw value. The trailing "Suggest: ..." phrase
      is gone; the per-side Clean/Dirty tags already convey direction.
  - **CEM Pkts Missing / Failure Counts / Generated+Received Lbits/Rbits
    dropped from Dirty determination entirely**, at the Run Summary AND the
    Diagnostic Summary's CEM indicator. These are cumulative-since-last-
    "clear cem circuit" counters (per the CEM Hairpin Loop MOP's own manual
    clear step) -- not a current-state reading. A mature, healthy circuit
    shows large non-zero values for all of them, so treating any non-zero
    value as "Dirty" was flagging nearly every circuit's MUX hop regardless
    of real health (root cause of the "every DS1 shows MUX CEM errors"
    pattern). The raw circuit counters are still visible in full in the MUX
    Stats section (`>> CEM Circuit Health`) for reference -- just no longer
    drive a pass/fail-style verdict. Connection state and xconnect leg status
    (genuine current-state signals) still do.
  - `run_summary_line()` gained two new parameters (`all_t3_stats`,
    `fiber_results`/`evc_results`) to support the 1800/MUX side-splitting --
    call site updated in `autotester_standalone.py`.

---

## v1.27 — 2026-08-04

### Report Layout
- **Autotester: Full Route + Diagnostic Summary now grouped per channel**
  (`autotester_standalone.py`)
  - Previously the report had two separate combined sections: one Diagnostic
    Summary block spanning every DS1, followed by every DS1's Full Route
    block. Now each DS1's Full Route is immediately followed by that same
    DS1's own Diagnostic Summary before moving to the next channel -- easier
    to digest one circuit at a time instead of cross-referencing two lists.
  - `build_diagnostic_summary` is now called once per DS1 (still the same
    function/logic, just scoped to a single-element list per call) instead of
    once for the whole run. Output content is unchanged, only the ordering.
  - The JIRA "Diagnostic Summary" panel is unaffected -- it's built from the
    same per-channel blocks concatenated together.

### New Features
- **GSX IP address in the Full Route block** (`output_builders.py`,
  `autotester_standalone.py`)
  - `HOP 4 -- GSX` now shows the resolved GSX management IP:
    ```
      HOP 4 -- GSX
        Node       : LAXGSX01
        IP         : 192.168.45.3
        Assignment : 11-2-27
        T1 Command : SHOW T1 T1-1-11-2-27
    ```
  - The IP is the live management address the Autotester actually reached
    (resolved via Netbox mgmt IPs + reachability ping, same value used to
    open the GSX session). Shows `?` if no reachable IP was resolved for that
    DS1 (mirrors the existing fallback convention used elsewhere).

---

## v1.26 — 2026-08-04

### Performance
- **1800 MUSE lookup batched per hub instead of per DS1** (`npt1800_client.py`)
  - Root cause of the "any show configuration | ... command is slow" complaint:
    the MUSE ID -> OC lookup (`show configuration | display-set | match
    "{muse_id} "`) issued that call **once per DS1**. The slowness is a
    device-side full-config render (~90-120s), and that render cost is paid
    per CALL, not per DS1 -- so a 4-DS1 run on one hub paid it 4 times
    (~6-8 minutes) for data a single render already contains.
  - New `_muse_lookup_batch()` issues **one render per hub**, matched against
    a regex alternation of every DS1's muse_id needed that run (Junos-style
    `match` supports `|` alternation), then groups the returned lines back to
    each muse_id locally in Python. Same source command, same selectivity
    (still only matches the muse_ids actually needed -- no broader/unscoped
    dump), just consolidated. This mirrors the pattern `npt1250_client.
    query_batch` already uses for `peer-address` (one render, parse many).
  - Net effect: an N-DS1 run on one hub now costs ~90-120s total instead of
    N * 90-120s. No output/behavior change -- verified the OC resolution and
    Peerless-customer detection are correctly scoped per-DS1 even when
    multiple DS1s share one combined render (unit-tested with a 2-DS1 mixed
    Peerless/non-Peerless fixture).
  - The separate OC alarm/PM stat commands (already deduped per unique OC,
    not a full-config render) keep their own smaller timeout, unaffected.

---

## v1.25 — 2026-08-04

### New Tools
- **mux_loop_script.py** — `MUX CEM Loop Script` (launcher order 60) — item 13
  - Generates a copy/paste-ready CEM hairpin loop MOP for a circuit. Does **not**
    execute any config — it connects only to run read-only prechecks, then writes
    a script the engineer pastes manually.
  - **Two input modes:**
    1. **1250 IP + DS1 (default)** — resolves the full route automatically
       (1250 → 1800 → MUX → GSX), the same path the Autotester walks, then fills
       every identifier into the script.
    2. **MUX-direct** — engineer supplies MUX IP / name / ACR / CEM group directly
       (plus optional other-hop info); only the MUX is queried. For when the route
       is already known or Netbox is unavailable.
  - **Prechecks are run live** against every reachable hop and the real output is
    embedded in the script as the baseline. **Postchecks are emitted as commands
    only** (run manually after testing).
  - Generates both scenarios from the MOP, with correct `connect` command syntax
    derived from the resolved ACR / CEM / GSX CE identifiers:
    - **Scenario A** — hairpin facing the **GSX** (`connect ... CEM{gsx} CEM{gsx}`),
      isolates the GSX; includes turn-up, validation, and restore.
    - **Scenario B** — hairpin facing the **1250** (`connect ... CEM-ACR{n} CEM-ACR{n}`),
      isolates the 1250/LEC; includes turn-up, validation, and restore.
  - Connection name (e.g. `CB_OC3_STS02_02`) is discovered automatically via
    `show run | include CEM-ACR{n}` — never typed by hand (new
    `get_connection_name()` in `mux4216_client.py`).
  - Peerless circuits are detected and rejected (no MUX/GSX hop to loop).
  - Saves timestamped `.txt` to `C:\Claude\`, supports JIRA attachment, auto-opens.

---

## v1.24 — 2026-08-04

### New Features
- **Autotester CEM circuit health** (`mux4216_client.py`, `output_builders.py`,
  `autotester_standalone.py`) — item 14
  - The Autotester now checks CEM circuit health at the MUX hop, closing the gap
    between "the SONET path resolves" and "the emulated circuit is actually clean."
  - New `mux4216_client.query_cem_health()` pulls, per DS1:
    - `show cem circuit interface cem-acr {acr} {cem}` — Pkts Missing, Failure
      Counts, Lbits/Rbits (any non-zero = flagged)
    - `show xconnect all | include CE{acr}:{cem}(` — both legs UP
    - `show connection name {conn}` — Current State UP
    - `show acr group {acr} detail cem` — working/protect state
  - New `get_connection_name()` discovers the connection name automatically
    (shared with the loop-script tool).
  - **Diagnostic Summary** MUX block now shows `[ OK ]/[ !! ] CEM circuit`,
    `Connection UP`, and `Xconnect leg(s) DOWN` indicators.
  - **Run Summary** flags `MUX CEM: Pkts Missing=… / Failure Counts=…` with a
    "check MUX" suggestion.
  - **MUX Stats** section gains a `>> CEM Circuit Health` block with the raw
    circuit / xconnect / connection / ACR-detail output per DS1.
  - All additions are read-only; no change to how the route is resolved.

---

## v1.23 — 2026-08-04

### New Files
- **`Curveball Tools.bat`** — double-click launcher (item 7)
  - Opens a terminal window in the correct directory and runs `python launcher.py`
  - No need to navigate manually; works by double-click from anywhere

- **`Create Curveball Shortcut.ps1`** — one-time taskbar shortcut setup (item 7)
  - Right-click → **Run with PowerShell** (do not double-click — that opens Notepad,
    a Windows default for `.ps1` files)
  - Creates `Curveball Tools.lnk` on the Desktop, ready to right-click → Pin to taskbar

### Documentation
- **`README.md`** — updated tools table (added OSPF Events, GSX Perfmon), updated
  Running section and file list to include `.bat` and shortcut script
- **`INSTALL.md`** — added "Quick launch / Taskbar shortcut" section (step 4),
  updated Quick Setup Checklist

---

## v1.22 — 2026-08-04

### Bug Fixes
- **DS1 input normalization** — item 19 (`autotester_standalone.py`, `ds1_pms.py`)
  - DS1 entries are now lowercased on input, so `TS1/7` and `ts1/7` both work
  - `&` is now accepted as a separator in addition to `,` — entries like
    `TS1/23 & TS1/28` are split correctly instead of being treated as one item

---

## v1.21 — 2026-08-04

### Bug Fixes
- **output_builders.py** — MUX DS1_AIS direction annotation (item 17)
  - `[ !! ] MINOR DS1_AIS` in the diagnostic output now appends
    `(AIS from LEC/1250 direction -- signal loss at bottom of route)`
  - DS1_AIS on the MUX T1 path means the 1800 is inserting AIS into the SONET
    payload because the 1250 is not delivering a valid DS1 — the fault is on the
    LEC/customer side, not the IXC side

- **output_builders.py** — GSX PM interval label in diagnostic header (item 18)
  - GSX node header now reads `GSX -- NYCGSX02  (current 15 min / 24 hr)` instead
    of just `GSX -- NYCGSX02`
  - Clarifies that the `[ !! ]` error counts shown are the max across the current
    15-minute interval and current 24-hour interval (whichever is higher), not
    cumulative statistics

---

## v1.20 — 2026-08-04

### New Tools
- **gsx_perfmon.py** — `GSX Perfmon (96 Intervals)` (launcher order 55)
  - Pulls a complete 24-hour picture of a single T1 on the GSX:
    - `SHOW T1 ... STATUS`
    - `SHOW T1 ... STATISTICS`
    - `SHOW T1 ... PERFMON CURRENT DAY`
    - `SHOW T1 ... PERFMON THRESHOLDS`
    - `SHOW T1 ... PERFMON INTERVAL N FIFTEENMIN` for all 96 intervals
      (interval 1 = most recent, interval 96 = oldest, ~24 hours back)
  - T1 assignment accepts full form (`T1-1-7-2-2`) or short form (`7-2-2`)
  - Reuses GSX credentials from session (no re-prompt if already entered this session)
  - Saves timestamped `.txt` to `C:\Claude\`, supports JIRA post with attachment
    fallback, and auto-opens the file
  - Useful for stress-test planning: reveals error frequency and timing so test
    durations are grounded in real failure patterns

---

## v1.19 — 2026-08-04

### New Features
- **MUX controller description fallback** (`mux4216_client.py`) — item 12
  - When Netbox has no OC→SONET mapping for a circuit (e.g. ts3 not yet populated),
    the autotester now tries a second resolution method before falling back to the
    unreliable legacy ACR-guess
  - New method `find_acr_by_description(hub_hostname, slot_port)` scans
    `show run | section controller` on the MUX and looks for a SONET-ACR stanza
    whose description matches `{1800-hostname} {OC-slot/port}`
    (e.g. `ces01.lax1 ts3/1`). Case-insensitive.
  - If matched, uses that ACR number to resolve VTG / T1 / CEM group directly —
    the same path as a successful Netbox lookup, but without Netbox
  - On success: marks `netbox_conflict` with a reminder to populate Netbox
  - Resolution tier order is now:
    1. Netbox `oc_to_sonet` (most accurate, verifies path)
    2. MUX description scan (accurate, works without Netbox)
    3. Legacy ACR-guess (shotgun, last resort)
  - No changes to `autotester_standalone.py` — fallback is entirely inside
    `query_batch`

---

## v1.18 — 2026-08-04

### Bug Fixes
- **Dynamic OC type labeling** — autotester no longer hardcodes "OC12" in output
  - The 1800 regex already captured any OC speed (`oc12`, `oc48`, `oc3`, etc.); only
    the display labels were wrong
  - Added `_oc_label(oc_iface)` helper in `output_builders.py` that extracts the
    speed from the interface identifier (e.g. `oc48-ts20/1:13:1` → `"OC48"`)
  - All user-facing "OC12" label strings replaced in:
    - `output_builders.py` — route blocks, peerless block, full diagnostic block,
      run summary findings and suggestions
    - `npt1800_client.py` — stat section header and "could not find" error message
    - `autotester_standalone.py` — "NPT-1800 OC STATS" section header
    - `mux4216_client.py` — internal `Cannot parse OC interface` error text

---

## v1.17 — 2026-08-04

### Bug Fixes
- **output_builders.py** — CES PM display now distinguishes "disabled" from "clean"
  - When the device returns "switch of this interface object is off" (PMs not enabled), `npt1250_client` already writes `"CES PMs not enabled on this DS1."` into the PM blob
  - `build_diagnostic_summary` now detects that marker and shows `[ -- ] CES PMs disabled` instead of `[ OK ] CES PMs clean`
  - Applies to both the full-route and peerless diagnostic blocks

---

## v1.16 — 2026-08-04

### New Features
- **IP address validation** on all manual IP entry prompts
  - Rejects entries that aren't 4 octets with each octet 0–255 (e.g. `192.168.270.1` or `192.168.150` both fail)
  - Re-prompts in a loop until a valid IP is entered — no more silent TCP failures from a typo
  - Implemented via `prompt_ip()` in `device_base.py`; wired into autotester, health check, fiber util, DS1 PMs, discovery, and OSPF events

### Bug Fixes
- **output_builders.py** — incomplete route now returns FAIL in run summary
  - When Netbox data is missing (1800/OC12 not resolved, hub IP unknown, or MUX not found), `run_summary_line` now returns `FAIL | Route incomplete: ... | Check Netbox` instead of `PASS | All clean`

---

## v1.15 — 2026-08-04

### Bug Fixes
- **Bad credential loop fixed** across all tools
  - Previously: auth failure was silently swallowed, bad creds stayed in the session, every subsequent run re-used them with no way to re-enter without quitting
  - Now: `CredentialError` (new exception in `device_base.py`) is raised on `AuthenticationException` (paramiko) or "Access denied" in plink output (GSX)
  - `launcher.py` catches it in `dispatch()`, prints the rejection message, and clears **only the failing cred pair** (`infra` or `gsx`) from the session — the user is re-prompted on the very next tool run without restarting the launcher
  - Files changed: `device_base.py`, `gsx_client.py`, `launcher.py`, `autotester_standalone.py`, `health_check.py`, `fiber_util.py`, `ds1_pms.py`, `interface_check.py`, `npt1250_discovery.py`, `ospf_events.py`

---

## v1.14 — 2026-08-04

### Bug Fixes
- **output_builders.py** — Run Summary CES reporting now shows the worst error, not the first
  - Previous logic stopped at the first CES counter it found (e.g. `RxPsnOverrunEvents=8`) even when a more severe counter was present (`RxPsnES=1627`)
  - Now scans all CES counters and reports the one with the highest value in the run summary line

---

## v1.13 — 2026-08-04

### Bug Fixes
- **output_builders.py** — CES PM display placement corrected (NOE-4503)
  - CES PMs now appear **above** the `1250 --` identifier line (`[ !! ] CES ...` / `[ OK ] CES PMs clean`)
  - Removed the duplicate CES block that was also appearing below the `ds1-tsX/Y MUSE_XXXX` line
  - TDM PMs remain below the identifier line, unchanged
  - Fix applied to both the full route block and the peerless block

---

## v1.12 — 2026-08-04

### New Tools
- **ospf_events.py** — `OSPF Events + Fiber Levels` (launcher order 45)
  - Pulls `show ne events unreported` and filters in Python for OSPF state changes
  - Separate sections for DOWN events and FULL/UP events; catch-all for other OSPF lines
  - Also checks fiber transceiver light levels on ts2 and ts5 (TX/RX power)
  - Saves timestamped `.txt` to `C:\Claude\`, supports JIRA post and auto-open

### Bug Fixes
- **jira_client.py** — JIRA `CONTENT_LIMIT_EXCEEDED` fallback
  - When a report is too large to post as a comment (HTTP 400), the script now:
    1. Posts a short stub comment noting the attachment
    2. Uploads the `.txt` output file directly as a JIRA attachment
  - Affects all tools: autotester, health check, fiber util, DS1 PMs, interface check, discovery, and the new OSPF events tool

---

## v1.11 — (prior release)

*(No changelog maintained before v1.12 — see git history or `!Revisions/` zip archive for earlier changes.)*
