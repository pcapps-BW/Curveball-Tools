# Curveball Tools — Changelog

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
