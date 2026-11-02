#!/usr/bin/env python3
"""
output_builders.py -- Stage B polished output for the standalone Autotester.

Faithful port of the SecureCRT builders:
  - build_route_block        (FULL T1 ROUTE, HOP 1-4 + SUMMARY)
  - build_peerless_block     (PARTIAL T1 ROUTE, ends at 1800)
  - build_diagnostic_summary (per-DS1 vertical route w/ [ OK ]/[ !! ] + X connectors)
  - run_summary_line         (one-liner "test completed successfully/with errors" +
                               standardized Clean/Dirty per hop/side)

The 'X' between hops is intentional (NOT '|' -- a pipe breaks JIRA table rendering).

Built by P. Capps.
"""

import re
import loop_check
import datetime
import textwrap

from ospf_events import compute_ospf_uptime, parse_ldp_sessions

SEP  = "=" * 58
SEP2 = "-" * 58


def _wrap_note(text, indent="         "):
    """Wrap a verdict paragraph to report width with a uniform indent."""
    return textwrap.wrap(text, width=72, initial_indent=indent,
                         subsequent_indent=indent,
                         break_on_hyphens=False, break_long_words=False)


def _ais_verdict(tdm_los, tdm_ais_in, tdm_hits, mid_flags,
                 muse_id, oc12, acr, cem):
    """
    Cross-hop correlation for a DS1_AIS facility alarm on the MUX.

    AIS is generated AT the point of failure and travels downstream only --
    so where the AIS originated is determined by what the upstream hops
    (collected in this same run) look like. Finite symptom matrix, walked
    top-priority-first:

      1. 1250 TDM shows LOS/LOF        -> LEC handoff failure. The 1250
         lost its input and is itself inserting the AIS the MUX sees.
      2. 1250 TDM shows incoming AIS   -> failure deeper in the LEC network;
         the LEC's gear generated it, the 1250 is passing it through.
      3. 1250 TDM clean, middle dirty  -> transport fault between 1250 and
         MUX (CES/EVC/NNI/OC); AIS is a symptom of that segment.
      4. everything clean              -> the 1800 is inserting AIS into
         this VT: nothing stitched to the timeslot (or stitched to a
         different one). Provisioning/mapping gap, not a broken link --
         clean PMs everywhere are EXPECTED in this case, because AIS is a
         valid framed signal that transports without errors.
    """
    if tdm_los:
        return ("VERDICT: The 1250 has lost its incoming signal from the "
                "LEC (LOS) and is inserting AIS downstream -- the MUX alarm "
                "is the echo of that loss. Trouble is at the LEC handoff "
                "(cabling / smartjack / LEC last mile). Engage the LEC.")
    if tdm_ais_in:
        return ("VERDICT: The 1250 is RECEIVING AIS from the LEC -- the "
                "failure is upstream inside the LEC network, before our "
                "handoff. The 1250 is passing it through untouched. Engage "
                "the LEC and reference the incoming AIS at our demarc.")
    if mid_flags:
        return ("VERDICT: The 1250's TDM input is clean, but the transport "
                "path between the 1250 and MUX shows trouble ({}) -- the "
                "AIS is most likely a symptom of that segment. Work the "
                "dirty segment first; the AIS should clear with it."
                .format(", ".join(mid_flags[:4])))
    if tdm_hits:
        return ("VERDICT: The 1250's TDM input shows errors ({}) but no "
                "hard LOS/AIS, and the transport path is clean. Marginal "
                "signal from the LEC direction -- monitor, and engage the "
                "LEC if the counters keep climbing."
                .format(", ".join(h.split(":")[0].strip() for h in tdm_hits[:3])))
    return ("VERDICT: The MUX sees AIS but the 1250's TDM input AND the "
            "entire 1250<->1800 transport path are clean. A clean path "
            "carries AIS without logging a single error, so this pattern "
            "means the AIS is being inserted by the 1800 itself -- almost "
            "always because nothing is stitched into this VT timeslot "
            "(or the circuit is stitched to a different one). Check 1800 "
            "provisioning: verify {} is cross-connected to {} (expected "
            "CEM {} on ACR {}). Corroborate on the MUX with 'show cem "
            "circuit interface cem-acr {} {}' -- actively incrementing "
            "Lbits = far end reporting no attachment circuit."
            .format(muse_id or "the MUSE circuit", oc12 or "?",
                    cem or "?", acr or "?", acr or "?", cem or "?"))


# ---- helpers ----
def _ok_or(flagged, label):
    return "[ !! ] {}".format(label) if flagged else "[ OK ] {}".format(label)


def _check_counters(text, counters):
    """Return ['COUNTER: value', ...] for any non-zero counter found in text."""
    hits = []
    for c in counters:
        m = re.search(r'{}\s*:\s*([1-9]\d*)'.format(re.escape(c)), text)
        if m:
            hits.append("{}: {}".format(c, m.group(1)))
    return hits


def _cd(dirty, tag=None):
    """Clean/Dirty tag for the Run Summary, with an optional short hint of
    which counter/alarm tripped it -- name only, never the raw value.
    Several MUX/CEM counters are cumulative-since-last-"clear" and the exact
    figures aren't meaningful without a baseline (see run_summary_line)."""
    if not dirty:
        return "Clean"
    return "Dirty ({})".format(tag) if tag else "Dirty"


def _oc_label(oc_iface):
    """Derive the OC speed label from an interface identifier.
    'oc48-ts20/1:13:1' -> 'OC48', 'oc12-ts2/1:6:3' -> 'OC12',
    'oc-ts5/1:2:1' -> 'OC', '' or None -> 'OC'.
    """
    m = re.match(r'oc(\d+)-', (oc_iface or '').lower())
    return "OC{}".format(m.group(1)) if m else "OC"


def _oc12_alarms_for(oc12_stat, subchannel):
    """
    AIS-V / R-DS1AIS-P alarm lines on a shared OC controller, scoped to ONE
    circuit's sub-channel.

    The SONET controller output is pulled once per OC facility and shared by
    every DS1 riding it, so collecting every AIS line and attributing it to
    each DS1 made a channel-1 circuit report the alarms for channels 16-19 as
    its own (pcapps, 2026-11-02: looking at oc12-ts21/1:10:1, flagged the
    :10:16 .. :10:19 alarms).

    Each alarm line carries `interface-name=<subchannel>`, so match on that,
    END-ANCHORED: ':10:1' must not match ':10:16' or ':10:10'. Same
    substring-boundary class as the LAXGSX04 wrong-circuit bug.

    If the circuit's sub-channel is unknown, every AIS line is returned rather
    than none -- hiding alarms because we could not scope them would be the
    worse failure.
    """
    ais = [l.strip() for l in (oc12_stat or "").splitlines()
           if "R-DS1AIS-P" in l or "AIS-V" in l]
    if not subchannel:
        return ais
    # interface-name=oc12-ts21/1:10:1 followed by a non-digit (';', space, EOL)
    pat = re.compile(r'interface-name=' + re.escape(subchannel) + r'(?!\d)')
    scoped = [l for l in ais if pat.search(l)]
    # If none matched but the sub-channel carries no explicit :sts:ds1 (a bare
    # OC facility), fall back to all -- better a noisy report than a silent one.
    if not scoped and ":" not in subchannel.split("/")[-1]:
        return ais
    return scoped


def _parse_cd(val):
    """'Clean' / 'Dirty (TAG)' / 'Disabled' -> (dirty_bool, tag_or_state)."""
    val = (val or "").strip()
    if val.startswith("Dirty"):
        m = re.search(r'\(([^)]*)\)', val)
        return True, (m.group(1) if m else "")
    return False, val


def _cite_flagged_line(diag_text, keyword):
    """First '[ !! ]' line in this DS1's diagnostic text mentioning
    `keyword` (case-insensitive) -- best-effort citation of the actual
    flagged detail for a hop, instead of naming the hop with no value."""
    if not keyword:
        return None
    kw = keyword.lower()
    for ln in diag_text.splitlines():
        s = ln.strip()
        if s.startswith("[ !! ]") and kw in s.lower():
            return s
    return None


def _ds1_review(segs, diag_text):
    """
    One-liner review + suggested next step for a DS1, derived from the
    standardized per-hop Clean/Dirty verdicts. Finite symptom matrix,
    walked bottom-up: faults cascade DOWNSTREAM (away from the LEC,
    toward the GSX), so the lowest dirty hop is the root cause and
    anything dirty above it is usually a knock-on effect.

    Returns None when a DS1_AIS VERDICT already covers this DS1 (the
    caller skips the generic review in that case).

    Returns a tuple (review_text, secondary_text).  review_text is the
    main REVIEW: paragraph.  secondary_text is the "Other dirty
    segment(s)..." sentence when multiple hops are dirty, or "" when not.
    The caller renders them as separate paragraphs.
    """
    joined = "  |  ".join(segs)
    if "Route incomplete" in joined:
        reason = next((s for s in segs if "Route incomplete" in s), "Route incomplete")
        return ("REVIEW: {} -- hop health can't be verified end-to-end "
                "until the route resolves. Fix the circuit documentation "
                "(Netbox) and re-run before drawing conclusions.".format(reason), "")

    peerless = "network-side" in joined
    tdm = ces = s1250 = snet = smux = st3 = (False, "")
    for seg in segs[1:]:
        if seg.startswith("NOTE:"):
            continue
        m = re.match(r'1250: TDM (.+?) / CES (.+)$', seg)
        if m:
            tdm, ces = _parse_cd(m.group(1)), _parse_cd(m.group(2))
            continue
        m = re.match(r'1800: 1250-side (.+?) / (?:MUX|network)-side (.+)$', seg)
        if m:
            s1250, snet = _parse_cd(m.group(1)), _parse_cd(m.group(2))
            continue
        m = re.match(r'MUX: 1800-side (.+?) / GSX-side (.+)$', seg)
        if m:
            smux, st3 = _parse_cd(m.group(1)), _parse_cd(m.group(2))

    gsx_dirty = bool(re.search(r'\[ !! \] GSX', diag_text))

    # Route order, bottom (LEC) -> top (GSX). (order, name, dirty, tag)
    hops = [
        (0, "1250 TDM",       tdm[0],  tdm[1]),
        (1, "1250 CES",       ces[0],  ces[1]),
        (2, "1800 1250-side", s1250[0], s1250[1]),
        (3, "1800 {}".format("network-side" if peerless else "MUX-side"),
                              snet[0], snet[1]),
        (4, "MUX 1800-side",  smux[0], smux[1]),
        (5, "MUX GSX-side",   st3[0],  st3[1]),
        (6, "GSX",            gsx_dirty, ""),
    ]
    dirty = [h for h in hops if h[2]]

    if not dirty:
        note = (" (CES PMs are disabled, so the packet side couldn't be "
                "verified.)" if ces[1] == "Disabled" else "")
        return ("REVIEW: Every hop tested clean in both directions -- no "
                "active fault on this route at test time.{} If trouble is "
                "still being reported, suspect an intermittent issue or "
                "something outside the tested span (LEC last mile / "
                "customer equipment) and re-run during the trouble window."
                .format(note), "")

    order, name, _, tag = dirty[0]
    if order == 0:
        if "LOS" in (tag or "").upper():
            body = ("The 1250 has no incoming signal from the LEC ({}) -- "
                    "every hop above it is clean or just echoing that loss "
                    "downstream. The fault is at or beyond the LEC handoff; "
                    "engage the LEC.".format(tag))
        else:
            body = ("The 1250 is taking TDM errors ({}) from the LEC "
                    "direction while the path above it is clean -- marginal "
                    "signal at the handoff. Monitor, and engage the LEC if "
                    "the counters keep climbing.".format(tag))
    elif order == 1:
        body = ("The 1250's packet (CES) side shows {} while the T1 itself "
                "(TDM) and every hop above are clean -- this points at the "
                "packet transport between the 1250 and 1800 (timing/jitter/"
                "buffer), not the physical circuit. These PM counters "
                "accumulate, so re-run and compare before chasing it: "
                "static counts = historical event, climbing counts = "
                "active issue.".format(tag))
    elif order == 2:
        # BUG (found live 2026-08-06, NOE-4644): a DS1's diagnostic text can
        # carry TWO independent optical readings -- the 1800's own NNI/EVC
        # physical PM (check_nni_health, printed as "NNI optics  Rx x / Tx
        # y dBm") and the 1250's own GE fiber uplink laser (check_fiber_
        # health, printed as "[ !! ] NNI <port> Rx laser x dBm (< floor)").
        # side_1250_cd (the tag driving THIS hop) is actually set by the
        # fiber-uplink check, not the 1800's NNI check -- but the original
        # fix here always cited the 1800's "NNI optics" line regardless,
        # which is a genuinely different (and often still-clean) reading.
        # On NOE-4644 that meant citing a clean -3.2/-2.4 dBm reading while
        # the ACTUAL flagged one (ge-ts5/1, -34.0 dBm) sat unmentioned a
        # few lines below -- not a fabricated number, just the wrong one
        # for what tripped this specific flag. Fixed: cite whichever line
        # actually carries the "[ !! ]" dBm flag, whichever check produced
        # it, instead of assuming it's the 1800-side reading.
        flagged = [ln.strip() for ln in diag_text.splitlines()
                   if ln.strip().startswith("[ !! ]") and "dBm" in ln]
        reading = (" (already flagged above -- {})".format(flagged[0])) if flagged else ""
        body = ("The 1800's 1250-facing side is dirty ({}) while the 1250 "
                "below it and everything above are clean -- the trouble is "
                "on the NNI/EVC segment between the 1250 and 1800{}. This "
                "points at physical fiber degradation or a dirty connector "
                "on that span; if the 1250's own reading also comes back "
                "clean, it's possible the fault sits further out in the "
                "LEC's fiber path than this NNI check can see from our "
                "side -- worth engaging the LEC if nothing turns up "
                "locally.".format(tag, reading))
    elif order == 3:
        # tag here is often a generic label ("R-DS1AIS") rather than the
        # actual device alarm text -- cite the real "[ !! ] ... alarm(s):
        # ..." line (now showing the actual alarm names, 2026-08-07) when
        # one can be found, same pattern as the order-2 NNI fix.
        cited = _cite_flagged_line(diag_text, "alarm")
        detail = " ({})".format(cited) if cited else ""
        if peerless:
            body = ("The 1800's network-facing side shows {} while our side "
                    "of the route is clean{} -- the trouble is toward the "
                    "Peerless network handoff. Engage Peerless.".format(tag, detail))
        else:
            body = ("The OC path between the 1800 and MUX shows {} while "
                    "both ends are otherwise clean{} -- SONET transport "
                    "segment. Check the optics/path between the 1800 and "
                    "MUX.".format(tag, detail))
    elif order == 4:
        body = ("The MUX's 1800-facing facility shows {} with a clean path "
                "below it -- check the MUX SONET facility and CEM "
                "connection state on the MUX itself.".format(tag))
    elif order == 5:
        body = ("The T3 between the MUX and GSX shows {} while the entire "
                "route below the MUX is clean -- the fault is isolated to "
                "the MUX<->GSX span. Check the T3 port and cabling.".format(tag))
    else:
        body = ("The GSX side shows trouble while the entire route below "
                "it is clean -- the issue is at the GSX port itself, not "
                "the transport. Check the GSX T1/port state.")

    secondary = ""
    if len(dirty) > 1:
        # Generalizes the light-level fix (v1.70): a secondary/non-root
        # dirty hop used to be named only ("Other dirty segments (1800
        # 1250-side, GSX)...") with no actual value -- the same class of
        # "real fault, silently under-detailed" miss the light-level bug
        # was, just for every OTHER counter type too (T3, OC, GSX, etc.),
        # not only light levels. Now cites the actual flagged line when one
        # can be found for that hop, falling back to name+tag (old
        # behavior) only if no matching line turns up.
        # Returned separately (not appended to body) so the caller can
        # render it as its own paragraph with a blank line before it.
        other_bits = []
        for h in dirty[1:]:
            name, tag = h[1], h[3]
            search_terms = [tag] if tag else []
            if "GSX" in name:
                search_terms.append("GSX")
            cited = None
            for term in search_terms:
                cited = _cite_flagged_line(diag_text, term)
                if cited:
                    break
            other_bits.append(cited if cited else "{} ({})".format(name, tag or "flagged"))
        secondary = ("Other dirty segment(s) also flagged: {} -- likely "
                     "knock-on effects of the root cause above, but worth a "
                     "quick independent look -- clear the root first, then "
                     "re-run.".format("; ".join(other_bits)))
    return "REVIEW: " + body, secondary


def _light_level_blurb(diag_text):
    """
    Always states light-level status explicitly for this DS1 -- either
    flagged out-of-spec readings, or a clean confirmation -- regardless of
    which hop the REVIEW text picked as the PRIMARY root cause this run.
    Keyed off the existing "[ !! ] ... dBm" flags in the diagnostic text --
    never recalculates thresholds independently, so it can never disagree
    with whatever check_fiber_health() or check_nni_health() decided.

    BUG #1 (found live 2026-08-06, NOE-4644, first occurrence): a DS1's
    diagnostic block can carry TWO independent optical readings -- the
    1800's own NNI/EVC physical PM ("NNI optics  Rx x / Tx y dBm") and the
    1250's own GE fiber uplink laser ("[ !! ] NNI <port> Rx laser x dBm
    (< floor)"). The original version only ever checked the FIRST ("NNI
    optics") reading and declared "Light levels look fine" off a genuinely
    clean -3.2/-2.4 dBm reading while a SEPARATE flagged -34.0 dBm reading
    (ge-ts5/1) sat elsewhere in the same block, unmentioned.

    BUG #2 (found live 2026-08-06, NOE-4644, SECOND occurrence -- the first
    fix over-corrected): made this go silent entirely whenever ANY "[ !! ]
    ... dBm" flag existed anywhere in the block, to avoid contradicting a
    flagged reading with a false "fine" claim. But that meant a genuinely
    out-of-spec light level got dropped from the paragraph ENTIRELY the
    moment a DIFFERENT hop (CES, in this case) became the primary root
    cause -- _ds1_review()'s citation only covers the hop it's actually
    talking about, not every other flagged item. Silence isn't a false
    claim, but it's still a miss: "light levels out of spec" needs to
    surface every run it's true, not only on runs where it happens to also
    be the root cause.

    Fixed: never go silent when something's actually flagged. If ANY
    "[ !! ] ... dBm" line exists, state it explicitly as OUT OF SPEC
    (redundant with the REVIEW text on runs where it's also the root
    cause -- that's fine, silence was the actual bug). Only when nothing
    at all is flagged does this confirm clean, citing every distinct
    reading found (both the 1800-side and 1250-side checks).
    """
    bad = [ln.strip() for ln in diag_text.splitlines()
           if ln.strip().startswith("[ !! ]") and "dBm" in ln]
    if bad:
        return "Light levels OUT OF SPEC: {}.".format("; ".join(bad))

    readings = []
    for m in re.finditer(r'NNI optics\s+Rx\s+(-?[\d.]+)\s*/\s*Tx\s+(-?[\d.]+)\s*dBm', diag_text):
        readings.append("1800 NNI/EVC Rx {} / Tx {} dBm".format(m.group(1), m.group(2)))
    for m in re.finditer(r'(\S+)\s+laser\s+Rx\s+(-?[\d.]+)\s*/\s*Tx\s+(-?[\d.]+)\s*dBm', diag_text):
        readings.append("1250 {} Rx {} / Tx {} dBm".format(m.group(1), m.group(2), m.group(3)))
    if not readings:
        return ""
    return "Light levels look fine ({}).".format("; ".join(readings))


def build_jira_summary(ds1_map, run_summary_block, diag_text_parts):
    """
    Per-DS1 brief summary for the JIRA comment (visible without expanding
    anything): DS1 assignment as a header, that DS1's per-hop Clean/Dirty
    one-liner split onto readable lines beneath it, plus any DS1_AIS
    VERDICT paragraph lifted from that DS1's diagnostic summary. Rinse and
    repeat per DS1 -- each can have different symptoms.

    run_summary_block layout: [SEP, title, SEP, "", <one line per DS1>...],
    in ds1_map iteration order. diag_text_parts is parallel to ds1_map too.
    """
    per_ds1_lines = run_summary_block[4:4 + len(ds1_map)]
    out = []
    for idx, ds1 in enumerate(ds1_map):
        line = per_ds1_lines[idx] if idx < len(per_ds1_lines) else ""
        segs = [s.strip() for s in line.split("  |  ") if s.strip()]
        out.append(segs[0] if segs else ds1.upper())
        for seg in segs[1:]:
            out.append("  {}".format(seg))

        # Lift the wrapped VERDICT paragraph(s) out of this DS1's
        # diagnostic summary (9-space-indented block starting "VERDICT:").
        diag = diag_text_parts[idx] if idx < len(diag_text_parts) else ""
        verdicts, current = [], None
        for dl in diag.splitlines():
            if dl.strip().startswith("VERDICT:"):
                if current:
                    verdicts.append(" ".join(current))
                current = [dl.strip()]
            elif current is not None:
                if dl.startswith("         ") and dl.strip():
                    current.append(dl.strip())
                else:
                    verdicts.append(" ".join(current))
                    current = None
        if current:
            verdicts.append(" ".join(current))
        for v in verdicts:
            out.extend(textwrap.wrap(
                v, width=72, initial_indent="  ", subsequent_indent="  ",
                break_on_hyphens=False, break_long_words=False))

        # One-liner review + suggested next step (finite symptom matrix).
        # Skipped when a DS1_AIS VERDICT above already explains this DS1.
        # _ds1_review returns (review_text, secondary_text); secondary is
        # the "Other dirty segment(s)..." sentence when multiple hops are
        # dirty -- rendered as its own paragraph with a blank line before
        # it so it doesn't run on at the end of the REVIEW paragraph.
        if not verdicts:
            review, secondary = _ds1_review(segs, diag)
            if review:
                out.extend(textwrap.wrap(
                    review, width=72, initial_indent="  ",
                    subsequent_indent="  ", break_on_hyphens=False,
                    break_long_words=False))
            if secondary:
                out.append("")
                out.extend(textwrap.wrap(
                    secondary, width=72, initial_indent="",
                    subsequent_indent="  ", break_on_hyphens=False,
                    break_long_words=False))

        blurb = _light_level_blurb(diag)
        if blurb:
            out.append("")
            out.extend(textwrap.wrap(
                blurb, width=72, initial_indent="",
                subsequent_indent="  ", break_on_hyphens=False,
                break_long_words=False))
        out.append("")
    return "\n".join(out).rstrip()


# Marker written by npt1250_client.query_ds1_pms when the device returns
# "switch of this interface object is off" (pm-collection-enable not set).
_CES_DISABLED_RE = re.compile(r'CES PMs not enabled', re.IGNORECASE)


def build_ces_enable_mop(ds1s):
    """
    Copy/paste MOP for enabling CES PMs, for the DS1s that came back disabled.

    NOT EXECUTED BY ANYTHING. Every tool in this suite is read-only and
    INTRUSIVE_COMMANDS.md asserts it; this function only ever returns TEXT.
    It follows the same convention as ds1_gsx_test.py and mux_loop_script.py,
    which generate intrusive MOPs as copy/paste material with explicit
    "not executed" framing.

    Emitted ONCE at the end of a run rather than beside each
    "[ -- ] CES PMs disabled" line, for two reasons: that line is produced in
    three separate places, and a `set` command sitting inside the findings
    reads as part of the diagnosis -- someone skimming is likelier to paste it
    without reading.

    Command sequence supplied by pcapps 2026-10-01, verbatim. Two `set` lines
    are required (pm-monitor-enable AND pm-collection-enable), and the
    `<verify connectivity>` step between `commit confirm 5` and the bare
    `commit` is a HUMAN GATE -- do not remove it to tidy the output. Without
    it the sequence reads as six lines to paste in one go, which defeats the
    point of `commit confirm`.

    Returns "" when nothing is disabled, so the caller can append
    unconditionally.
    """
    ds1s = [d for d in (ds1s or []) if d]
    if not ds1s:
        return ""
    out = [
        "",
        SEP,
        "  CES PMs DISABLED -- MOP TO ENABLE  ({} DS1{})".format(
            len(ds1s), "" if len(ds1s) == 1 else "s"),
        SEP,
        "  These commands are COPY/PASTE REFERENCE ONLY. Nothing below was",
        "  executed by this tool, and nothing in this suite executes it.",
        "",
        "  'commit confirm 5' rolls the change back automatically after five",
        "  minutes unless the bare 'commit' is issued. Verify connectivity in",
        "  between -- that step is the safety net, not a formality.",
        "",
    ]
    for ds1 in ds1s:
        out.append("  --- ds1-{} ---".format(ds1))
        out.append("  configure")
        out.append("  set interfaces ds1-{} unit 0 pm-monitor-enable".format(ds1))
        out.append("  set interfaces ds1-{} unit 0 pm-collection-enable".format(ds1))
        out.append("  commit confirm 5")
        out.append("  <verify connectivity is ok>")
        out.append("  commit")
        out.append("")
    out.append(SEP)
    return "\n".join(out)

CEM_CIRCUIT_COUNTERS = [
    "Pkts Missing", "Failure Counts",
    "Generated Lbits", "Received Lbits",
    "Generated Rbits", "Received Rbits",
]

CES_COUNTERS = [
    "RxPsnLostPkts", "RxPsnOverrunEvents", "RxPsnUnderrunEvents",
    "RxPsnLopsEvents", "RxPsnES", "RxPsnSES", "RxPsnUAS",
    "TxPsnDiscardPkts", "RxPsnOowDiscardPkts",
]
TDM_COUNTERS = ["DS1ES-L", "DS1SES-L", "DS1CV-L", "DS1LOSS-L", "DS1UAS-L"]
OC12_COUNTERS = ["ES-S", "SES-S", "CV-S", "SEFS-S", "ES-L", "SES-L", "CV-L", "UAS-L"]
T3_COUNTERS = [
    "P-bit Coding Violation", "C-bit Coding Violation", "P-bit Err Secs",
    "Unavailable Secs", "Line Errored Secs", "AIS Defect Secs", "LOS Defect Secs",
]

# GSX PERFMON is columnar: "<Counter>:   <errors>   <Minor> <Major> <Critical>"
# e.g. "Unavailable Seconds:              772       TRUE    TRUE    TRUE"
# We check the leading Errors value (first number after the colon).
#
# IMPORTANT: only the "PERFMON CURRENT" sections hold actual error counts.
# The "PERFMON THRESHOLDS" section lists CONFIG LIMITS (e.g. "Line Errored
# Seconds: 518 648 778") using similar names -- those must NOT be read as
# errors, or a healthy circuit gets falsely flagged. We therefore scan only
# the current-performance region and stop at the THRESHOLDS section.
GSX_PM_COUNTERS = [
    "Code Violations", "Errored Seconds", "Severely Errored Seconds",
    "SEF/AIS Seconds", "Controlled Slip Seconds", "Unavailable Seconds",
]

# ---- Fiber (GE uplink) physical-layer health ----
# Physical PM error counters on the 1250 fiber ports (ge-ts2/1, ge-ts5/1).
# Flag any of these that read > 0. The last group (PCS/FEC) reads N/A on 1G
# optics; flag only when numeric and > 0.
FIBER_ERR_COUNTERS = [
    "Rx Error Pkts",
    "Rx CRC Align Error Pkts",
    "Collision Errors",
    "Rx Jabber Pkts",
    "Rx Fragments",
    "Rx Undersize Pkts",
    "Rx Oversize Pkts",
    "PCS-ES", "PCS-SES", "PCS-UAS", "FEC-UCE",
]
# Received optical power floor. Rx weaker (more negative) than this is flagged.
# (TX is shown but NOT flagged for now -- pending a per-transceiver threshold
#  command that will supply real per-optic limits; see TODO.)
FIBER_RX_DBM_FLOOR = -10.0

# ---- NNI resolution + physical-NNI (1800-side) health ----
def _optical_value(text, field):
    """Return float dBm for an optical field (MinRxPower/TxPower), else None."""
    m = re.search(r'^\s*' + re.escape(field) + r'\s*:\s*(-?\d+\.?\d*)',
                  text, re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def resolve_nni_from_description(desc_output):
    """
    Parse 'show interfaces description | grep <prefix>' output from the 1800.
    Returns (logical_iface, physical_iface, status):
      status 'ok'        -> one NNI found; logical e.g. xe-ts13/3.570,
                            physical e.g. xe-ts13/3
      status 'none'      -> no interface line matched
      status 'ambiguous' -> matched >1 distinct physical interface (can't tell
                            which circuit -> caller flags + skips)
    """
    ifaces = []
    for line in (desc_output or "").splitlines():
        m = re.match(r'^\s*([a-z]+-ts\d+/\d+(?:\.\d+)?)\b', line)
        if m:
            ifaces.append(m.group(1))
    ifaces = sorted(set(ifaces))
    if not ifaces:
        return None, None, "none"
    # group by physical base (xe-ts13/3.570 -> xe-ts13/3); one base = one NNI
    bases = {}
    for name in ifaces:
        bases.setdefault(name.split(".")[0], []).append(name)
    if len(bases) > 1:
        return None, None, "ambiguous"
    base = list(bases)[0]
    subs = [n for n in bases[base] if "." in n]
    logical = subs[0] if subs else base
    return logical, base, "ok"


def check_nni_health(physical_pm):
    """
    Physical NNI health (1800 side) from 'show interfaces physical pm'.
    Flags FIBER_ERR_COUNTERS > 0 and MinRxPower < floor. Returns (flags, info).
    Optical fields here are MinRxPower/TxPower (not the transceiver 'laser'
    format used on the 1250). TX shown in info, not flagged.
    """
    flags, info = [], []
    for counter in FIBER_ERR_COUNTERS:
        val = _fiber_counter_value(physical_pm, counter)
        if val is not None and val > 0:
            flags.append("{}: {}".format(counter, val))
    rx = _optical_value(physical_pm, "MinRxPower")
    tx = _optical_value(physical_pm, "TxPower")
    if rx is not None and rx < FIBER_RX_DBM_FLOOR:
        flags.append("Rx power {} dBm (< {})".format(rx, FIBER_RX_DBM_FLOOR))
    if rx is not None or tx is not None:
        info.append("NNI optics  Rx {} / Tx {} dBm".format(
            rx if rx is not None else "?", tx if tx is not None else "?"))
    return flags, info


# ---- Logical EVC / sub-interface health ----
# Logical sub-interface PM (ge-tsX/1.0 on the 1250; xe-tsX/Y.VLAN on the 1800).
# Flag Rx packet discards (> 0), reported as the raw count plus the discard
# ratio: discards / (Rx Pass Pkts + Rx Discard Pkts), as a percent -- so a
# large-looking count can be judged in context (321765 is alarming alone, but
# ~0.1% of traffic is usually not). Packets only; octets are not flagged.
# Tx Discard reads "NA" on these platforms and is ignored.


def check_evc_health(evc_by_iface):
    """
    Shared logical-EVC health check for the DIAGNOSTIC SUMMARY.

    evc_by_iface: {iface_name: <logical pm text>}
    Returns list of "[ !! ]"-worthy flag strings (empty if no Rx discards).
    Each flag: "<iface> Rx Discard Pkts: <n> (<pct>)".
    """
    flags = []
    for iface in sorted(evc_by_iface):
        pm = evc_by_iface[iface] or ""
        discard = _fiber_counter_value(pm, "Rx Discard Pkts")
        if discard is None or discard <= 0:
            continue
        passed = _fiber_counter_value(pm, "Rx Pass Pkts")
        if passed is not None and (passed + discard) > 0:
            ratio = discard / float(passed + discard) * 100.0
            pct = "{:.2f}%".format(ratio) if ratio >= 0.01 else "<0.01%"
        else:
            pct = "ratio n/a"
        flags.append("{} Rx Discard Pkts: {} ({})".format(iface, discard, pct))
    return flags



def _fiber_counter_value(pm_text, counter):
    """Return int value of a fiber PM counter, or None if absent/N/A."""
    # lines look like:  "Rx CRC Align Error Pkts    :  0"
    m = re.search(r'^\s*' + re.escape(counter) + r'\s*:\s*(\S+)',
                  pm_text, re.MULTILINE)
    if not m:
        return None
    tok = m.group(1)
    if tok.upper() == "N/A":
        return None
    try:
        return int(tok)
    except ValueError:
        return None


def _fiber_laser_rx_tx(laser_text):
    """Return (rx_dbm, tx_dbm) floats from '| grep laser' output, None if absent."""
    rx = tx = None
    mrx = re.search(r'Rx-laser-power[^:]*:\s*(-?\d+\.?\d*)', laser_text)
    mtx = re.search(r'Tx-laser-power[^:]*:\s*(-?\d+\.?\d*)', laser_text)
    if mrx:
        try: rx = float(mrx.group(1))
        except ValueError: pass
    if mtx:
        try: tx = float(mtx.group(1))
        except ValueError: pass
    return rx, tx


# ---- 1250 loopback IP derivation from an 1800 sub-interface's own config --
# Confirmed network convention (2026-09-09): the 1800 side of an NNI
# point-to-point subnet is always the ODD address; the 1250 side is always
# (1800's own 4th octet - 1) -- same rule the ping-target derivation already
# uses (interface_check.py) -- which by design always lands on an EVEN
# address. The 1250's own loopback then reuses that SAME first-two-octet
# block (never hardcoded -- every example seen has been 10.111.x.x, but
# that's this network's convention, not something to bake in), a FIXED
# third octet of 130 (unrelated to whatever subnet the NNI itself lives in),
# and a 4th octet of (1250 sub-int 4th octet / 2).

def parse_subint_ip(cfg_text):
    """
    Extract a sub-interface's own IP from 'family inet address' in a
    'show configuration interfaces ... | display-set' blob -- same field the
    ping-target derivation already parses. Returns the IP string, or None.
    """
    m = re.search(r"family inet address (\d+\.\d+\.\d+\.\d+)/", cfg_text or "")
    return m.group(1) if m else None


def derive_1250_subint_ip(hub_subint_ip):
    """1800 sub-int IP -> 1250 sub-int IP (4th octet - 1). Same rule the
    existing ping-target derivation already uses."""
    octets = hub_subint_ip.split(".")
    octets[-1] = str(int(octets[-1]) - 1)
    return ".".join(octets)


def derive_1250_loopback(subint_1250_ip):
    """
    1250 sub-interface IP -> 1250 loopback IP, per confirmed convention
    (2026-09-09): 1st/2nd octets carried over unchanged, 3rd octet forced to
    130, 4th octet = sub-int's own 4th octet / 2 -- guaranteed to divide
    evenly by network design (the 1250 side of an NNI subnet always lands on
    an even address).

    Returns the loopback IP string, or None if the 4th octet ISN'T evenly
    divisible by 2 -- that means the addressing convention was violated
    somewhere upstream (wrong interface, unusual subnet, bad input). Refuses
    to guess rather than silently deriving the wrong site's loopback.
    """
    parts = (subint_1250_ip or "").split(".")
    if len(parts) != 4:
        return None
    try:
        last = int(parts[3])
    except ValueError:
        return None
    if last % 2 != 0:
        return None
    return "{}.{}.130.{}".format(parts[0], parts[1], last // 2)


_RE_DOWN_TS = re.compile(
    r'(\d{4})-(\d{2})-(\d{2}),(\d{2}):(\d{2}):(\d{2})')

# Confirmed live 2026-09-14 (FARGNDBC636): 'show ne events' LDP-Session
# entries embed a peerRouterID field, e.g.
#   {router-id:X.X.X.55,peerRouterID:X.X.X.254,...}
# -- lets a multi-peer log (a dual-homed 1250 running LDP to two different
# hubs) be split back into one stream per peer before pairing.
_RE_PEER_RTRID = re.compile(r'peerRouterID:([\d.]+)')


def _ip_in_line(ip, line):
    """
    Exact-IP-token match within a line of raw device text -- NOT a substring
    test. Confirmed live (2026-09-14, pcapps): 'show ldp session | grep .5'
    on the 1800 hit peers '.5' AND '.50'-'.59' -- the same naive-substring
    over-match this function replaces was silently pulling in a DIFFERENT
    site's events any time one loopback's digits happened to be a prefix of
    another's (e.g. filtering for '10.111.130.5' also matched
    '10.111.130.50'..'59'). That's a worse bug than the same-site
    cross-session mixing pair_ospf_outages_by_peer fixed: it can merge two
    completely unrelated sites' outage histories together.

    Requires the IP to be a genuinely standalone token in the line: not
    immediately preceded or followed by another digit (which would mean it's
    actually the middle/prefix of a longer address).
    """
    for m in re.finditer(re.escape(ip), line):
        start, end = m.start(), m.end()
        if start > 0 and line[start - 1].isdigit():
            continue
        if end < len(line) and line[end].isdigit():
            continue
        return True
    return False


def _us_eastern_is_dst(dt_utc):
    """
    True if dt_utc (a naive UTC datetime) falls in US Eastern daylight time.

    Deliberately NOT using zoneinfo/pytz: this runs on the user's Windows
    workstation, where stdlib zoneinfo has no IANA database unless the
    'tzdata' package happens to be installed -- a dependency this tool
    doesn't otherwise have and shouldn't silently require. US DST rule
    (2007-present) is fixed and simple enough to compute directly: starts
    2nd Sunday of March, ends 1st Sunday of November, both at 02:00 local.
    Approximated here to the day (not the exact 2am transition hour) -- off
    by at most a few hours right at the two transition boundaries per year,
    which is immaterial for a "was this circuit event during EST or EDT"
    label.
    """
    year = dt_utc.year
    # 2nd Sunday of March
    d = datetime.date(year, 3, 1)
    first_sunday = 1 + (6 - d.weekday()) % 7
    dst_start = datetime.date(year, 3, first_sunday + 7)
    # 1st Sunday of November
    d = datetime.date(year, 11, 1)
    first_sunday = 1 + (6 - d.weekday()) % 7
    dst_end = datetime.date(year, 11, first_sunday)
    today = dt_utc.date()
    return dst_start <= today < dst_end


def format_down_event(dt_utc):
    """
    Format a UTC device timestamp as 'MM/DD/YY - HHMMZ, HHMMEST' (or EDT).

    Changed 2026-09-14 from 'MM/YY' to 'MM/DD/YY' (per pcapps, after the
    original format was mistaken for a future date at a glance) -- the day
    was never actually in the display, only month and year, which is exactly
    what made a run of same-month timestamps look suspicious. Confirmed
    2026-09-09 display convention otherwise unchanged.

    Eastern offset is DST-aware (see _us_eastern_is_dst) so the local time and
    its EST/EDT label are actually correct, not just always labelled EST.
    """
    is_dst = _us_eastern_is_dst(dt_utc)
    offset_h = 4 if is_dst else 5
    local = dt_utc - datetime.timedelta(hours=offset_h)
    tz_label = "EDT" if is_dst else "EST"
    return "{:02d}/{:02d}/{:02d} - {}Z, {}{}".format(
        dt_utc.month, dt_utc.day, dt_utc.year % 100,
        dt_utc.strftime("%H%M"), local.strftime("%H%M"), tz_label)


def check_down_history(raw_events, peer_ip):
    """
    LDP-Session-Down AND OSPF-adjacency-Down bounce history for a specific
    peer IP, from the 1800's own 'show ne events | grep <peer-ip> | grep
    Down' (confirmed 2026-09-09 -- 'show ne events', NOT 'show ne events
    unreported', retains history far longer: 300+ days observed live, vs. the
    'unreported' buffer that's been crowded out by unrelated noise in well
    under an hour elsewhere in this tool -- see ospf_events.py/
    compute_ospf_uptime).

    Broad match on 'Down' (word-boundary, case-insensitive) rather than
    'LDP-Session-Down' specifically, matching the original two-stage grep
    (grep <ip>, then grep Down) -- catches OSPF adjacency-down entries for
    this peer too, not LDP sessions alone.

    Returns a list of dicts, most-recent-first (matches device log order):
      {"detected_utc": datetime, "formatted": "MM/DD/YY - HHMMZ, HHMMEST"}
    Caller decides how to render/count/flag -- this just parses, never
    raises.
    """
    if not raw_events or not peer_ip:
        return []
    hits = []
    for line in raw_events.splitlines():
        if not _ip_in_line(peer_ip, line):
            continue
        if not re.search(r'\bDown\b', line, re.IGNORECASE):
            continue
        m = _RE_DOWN_TS.search(line)
        if not m:
            continue
        dt_utc = datetime.datetime(*(int(g) for g in m.groups()))
        hits.append({"detected_utc": dt_utc, "formatted": format_down_event(dt_utc)})
    return hits


# Back-compat alias: earlier name, LDP-only. New callers should use
# check_down_history (broader match, structured return). Kept only until
# every caller is confirmed migrated.
def check_ldp_down_history(raw_events, peer_loopback_ip):
    hits = check_down_history(raw_events, peer_loopback_ip)
    return [h["detected_utc"].strftime("%Y-%m-%d %H:%M:%S") for h in hits]


def check_ospf_outage_events(raw_events, peer_ip, protocol=None):
    """
    Down AND Up/Full events for a specific peer IP, from the 1800's own
    'show ne events' (long-retention log -- see check_down_history for why
    'unreported' is deliberately not used). Same broad word-boundary match
    convention as check_down_history, but keeps the Up/Full side instead of
    discarding it, so the two can be paired into outages (see
    pair_ospf_outages).

    protocol: None (default) -- match any Down/Up/Full event for this peer,
              regardless of which protocol logged it (original behavior;
              this is what mixes LDP-Session and OSPF-adjacency entries
              together).
              "ldp"  -- keep only lines containing 'LDP-Session', the
                        confirmed literal event-name substring this suite
                        already greps for on the 1800 (see
                        ospf_stability.LDP_EVENT_CMD).
              "ospf" -- keep only lines NOT containing 'LDP-Session'. This is
                        a by-elimination match, not a confirmed OSPF-specific
                        event name -- the two-stage grep this function
                        replicates was confirmed (2026-09-09) to only ever
                        match LDP-Session or OSPF-adjacency entries against a
                        given peer IP line, so "not LDP" is a safe stand-in
                        for "OSPF" until an OSPF-specific event-name keyword
                        is confirmed on this platform. Documented here rather
                        than assumed silently.

    peer_ip may be None/"" to skip the IP filter entirely -- for use when
    raw_events is already scoped to a single site's own log (e.g. querying a
    1250 directly for its own LDP session events, rather than a hub's shared
    log covering many peers by IP). IMPORTANT when peer_ip is None: a device
    with more than one session of this type (e.g. a dual-homed 1250 running
    LDP to two different hubs) will have events for BOTH sessions in the same
    raw text -- each returned event carries a "peer" field (parsed from the
    line's own peerRouterID, when present) specifically so the caller can
    group by peer before pairing (see pair_ospf_outages_by_peer) instead of
    pairing one flat stream that mixes two independent sessions together.

    Returns a list of {"dt": datetime, "kind": "down"|"up", "formatted": str,
    "peer": str} dicts, in whatever order they appear in raw_events --
    pair_ospf_outages sorts them, so the caller does not need to pre-sort.
    "peer" is the line's own peerRouterID when parseable, else peer_ip (so a
    caller that already scoped the query to one peer -- passing a real
    peer_ip -- gets a consistent, non-empty peer tag even on a raw-format
    line with no embedded peerRouterID field), else "".
    """
    if not raw_events:
        return []
    out = []
    for line in raw_events.splitlines():
        if peer_ip and not _ip_in_line(peer_ip, line):
            continue
        is_ldp = "LDP-Session" in line
        if protocol == "ldp" and not is_ldp:
            continue
        if protocol == "ospf" and is_ldp:
            continue
        m = _RE_DOWN_TS.search(line)
        if not m:
            continue
        dt_utc = datetime.datetime(*(int(g) for g in m.groups()))
        if re.search(r'\bDown\b', line, re.IGNORECASE):
            kind = "down"
        elif re.search(r'\b(?:Up|Full)\b', line, re.IGNORECASE):
            kind = "up"
        else:
            continue
        peer_m = _RE_PEER_RTRID.search(line)
        peer = peer_m.group(1) if peer_m else (peer_ip or "")
        out.append({"dt": dt_utc, "kind": kind, "formatted": format_down_event(dt_utc),
                   "peer": peer})
    return out


def _fmt_outage_delta(delta):
    """'2d 04h 17m' style duration string from a timedelta. Same style used
    elsewhere in this tool suite (ospf_events._format_delta) -- duplicated
    here (rather than imported) so this module's outage helpers don't
    reach into another module's private helper."""
    total_s = int(delta.total_seconds())
    if total_s < 0:
        total_s = 0
    d, rem = divmod(total_s, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append("{}d".format(d))
    if d or h:
        parts.append("{}h".format(h))
    parts.append("{}m".format(m))
    return " ".join(parts)


def pair_ospf_outages(events):
    """
    Pair chronological down/up(-full) events into outages, computing the
    uptime that preceded each drop and the downtime before each recovery.

    events: list of {"dt": datetime, "kind": "down"|"up", "formatted": str}.
    The "dt" values must be internally consistent (all local, or all UTC --
    doesn't matter which, as long as they're all the same) since deltas are
    computed directly between them; the "formatted" string is purely for
    display, so this stays agnostic of any one source's timestamp
    convention/format -- callers from different logs (1800 long-retention
    event log, 1250 event log, etc.) can each supply their own.

    Pairing rule: process chronologically. A "down" always opens a new
    outage -- even if the previous one never saw a matching "up" in this log
    window (the recovery event may simply have rolled off before the drop
    did; that does not mean the site never recovered -- see the "unknown"/
    "still down" labels below). An "up" closes the MOST RECENT open outage,
    if one exists; an "up" with no open outage is dropped, since it means
    the matching "down" already rolled off the log and there is nothing to
    pair it to.

    Returns a list of dicts, chronological, one per outage:
      {
        "down_formatted":     str,
        "uptime_before":      str,  # duration since the previous recovery,
                                     # or "unknown (no prior clear in log)"
                                     # if there's no earlier "up" to measure
                                     # from
        "up_formatted":       str or None,   # None if still open
        "downtime_before_up": str,  # duration of the outage, or
                                     # "still down (not yet cleared)" if open
      }
    """
    ev = sorted(events, key=lambda e: e["dt"])
    outages = []
    open_outage = None
    last_up_dt = None
    for e in ev:
        if e["kind"] == "down":
            if last_up_dt is not None:
                uptime_before = _fmt_outage_delta(e["dt"] - last_up_dt)
            else:
                uptime_before = "unknown (no prior clear in log)"
            open_outage = {
                "down_dt":            e["dt"],
                "down_formatted":     e["formatted"],
                "uptime_before":      uptime_before,
                "up_formatted":       None,
                "downtime_before_up": "still down (not yet cleared)",
            }
            outages.append(open_outage)
        else:  # "up"
            last_up_dt = e["dt"]
            if open_outage is not None and open_outage["up_formatted"] is None:
                open_outage["up_formatted"] = e["formatted"]
                open_outage["downtime_before_up"] = _fmt_outage_delta(
                    e["dt"] - open_outage["down_dt"])
                open_outage = None
    return outages


def pair_ospf_outages_by_peer(events):
    """
    Group events by their "peer" field (see check_ospf_outage_events) BEFORE
    pairing, then merge the resulting per-peer outage lists back into one
    chronological list.

    WHY THIS EXISTS (bug found live 2026-09-14, PGHKNYSH133): a site can have
    more than one independent session of the same type to track -- e.g. a
    dual-homed 1250 running LDP to two different hubs (confirmed via 'show
    ldp session' showing two Operational peers on one box). pair_ospf_outages
    tracks only ONE "most recently opened, not yet closed" outage at a time.
    Feeding it a flat, unscoped stream that actually contains TWO independent
    sessions' events interleaved can cross-pair one session's Down with the
    OTHER session's Up, or leave a real (already-cleared) outage looking
    permanently open because the single open-outage slot got claimed by the
    wrong session's event. That is exactly what happened on PGHKNYSH133: the
    tool reported an LDP outage still open when the real device showed BOTH
    LDP sessions Operational for over a month.

    Events with no identifiable peer (peer is None/"" -- an older stored
    event from before the "peer" field existed, or a raw line with no
    parseable peerRouterID and no peer_ip to fall back to) are grouped
    together as their own single stream, preserving prior behavior for that
    case rather than silently dropping them.
    """
    groups = {}
    for e in events:
        groups.setdefault(e.get("peer") or "", []).append(e)
    merged = []
    for peer_key, grp in groups.items():
        outs = pair_ospf_outages(grp)
        # Tag each outage with the peer its group was keyed on. Every event
        # in `grp` shares this same peer (that's the whole point of grouping
        # before pairing), so it's a safe, cost-free thing to carry forward --
        # added so a caller can attribute an outage back to which physical/
        # logical session it happened on (e.g. ospf_stability.py correlating
        # LDP outages across sites sharing a physical NNI port, added
        # 2026-09-14). Empty string when the event had no identifiable peer,
        # same as the grouping key above -- callers should treat "" as
        # "unknown", not a real peer.
        for o in outs:
            o["peer"] = peer_key
        merged.extend(outs)
    merged.sort(key=lambda o: o["down_dt"])
    return merged


def format_ospf_outages(outages):
    """
    Render a paired outage list in the standard display format:

      OSPF Outages: <n>
      1) Down    -> <timestamp> -> Uptime before drop: <duration>
      2) Up/Full -> <timestamp> -> Downtime before up: <duration>
      3) Down    -> <timestamp> -> Uptime before drop: <duration>
      4) Up/Full -> <timestamp> -> Downtime before up: <duration>
      ...

    <n> is the number of OUTAGES (not lines) -- each outage renders as two
    numbered lines, and numbering runs sequentially across the whole list.
    """
    lines = ["OSPF Outages: {}".format(len(outages))]
    n = 1
    for o in outages:
        lines.append("{}) Down    -> {} -> Uptime before drop: {}".format(
            n, o["down_formatted"], o["uptime_before"]))
        n += 1
        up_ts = o["up_formatted"] if o["up_formatted"] else "(not yet logged)"
        lines.append("{}) Up/Full -> {} -> Downtime before up: {}".format(
            n, up_ts, o["downtime_before_up"]))
        n += 1
    return "\n".join(lines)


def check_ospf_ldp_health(ospf_events_raw, ldp_session_raw):
    """
    OSPF adjacency uptime (derived from the NE event log -- this platform has
    no direct uptime field, confirmed live 2026-08-07) + LDP session uptime
    (a real device-reported field, no derivation needed). Per-1250, shared
    across all DS1s in the run -- same pattern as check_fiber_health /
    check_evc_health above.

    ospf_events_raw / ldp_session_raw: raw 'show ne events unreported' /
      'show ldp session' text, or None/"" if not collected.

    Returns (flags, info):
      flags -- "[ !! ]"-worthy strings (OSPF down, LDP session not Operational)
      info  -- informational uptime lines for anything healthy
    """
    flags, info = [], []

    if ospf_events_raw:
        ospf = compute_ospf_uptime(ospf_events_raw)
        if ospf["status"] == "up":
            info.append("OSPF up {} ({})".format(ospf["uptime"], ospf["detail"]))
        elif ospf["status"] == "down":
            flags.append("OSPF {}".format(ospf["detail"]))
        else:
            info.append("OSPF uptime unknown -- {}".format(ospf["detail"]))

    if ldp_session_raw:
        sessions = parse_ldp_sessions(ldp_session_raw)
        if not sessions:
            info.append("LDP: no session data parsed")
        for s in sessions:
            line = "LDP {} {} -- up {}".format(s["peer"], s["state"], s["uptime"])
            if s["state"].lower() != "operational":
                flags.append(line)
            else:
                info.append(line)

    return flags, info


def _parse_tca_thresholds(tca_text):
    """
    Parse 'show chassis transceiver tca slot tsX port1' output.
    Columns in the table: warning low | warning high | alarm low | alarm high.
    We use 'warning low' as the flag threshold -- the device's own "pay
    attention" line, one step softer than alarm.

    Returns dict (None for any value not found in the text):
      rx_warn_low  -- Rx Optical Power warning low  (used as Rx flag threshold)
      rx_alarm_low -- Rx Optical Power alarm low    (informational)
      tx_warn_low  -- Tx Optical Power warning low  (informational)

    Example line parsed:
      Rx Optical Power (dBm)  :  -20.0 dBm   -3.0 dBm   -21.0 dBm   -2.0 dBm
    """
    out = {"rx_warn_low": None, "rx_alarm_low": None, "tx_warn_low": None}
    if not tca_text:
        return out
    # Rx: four dBm values after the colon; warn_low is first, alarm_low is third
    rx_m = re.search(
        r'Rx Optical Power[^:]*:\s*(-?\d+\.?\d*)\s*dBm\s+(-?\d+\.?\d*)\s*dBm\s+(-?\d+\.?\d*)',
        tca_text)
    if rx_m:
        try: out["rx_warn_low"]  = float(rx_m.group(1))
        except ValueError: pass
        try: out["rx_alarm_low"] = float(rx_m.group(3))
        except ValueError: pass
    # Tx: warning low is the first dBm value after the colon
    tx_m = re.search(r'Tx Optical Power[^:]*:\s*(-?\d+\.?\d*)\s*dBm', tca_text)
    if tx_m:
        try: out["tx_warn_low"] = float(tx_m.group(1))
        except ValueError: pass
    return out


_MUX_SONET_STATE_FIELDS = ["Active Defects", "Detected Alarms", "Asserted/Active Alarms"]


def check_mux_sonet_health(sonet_pm_raw):
    """
    MUX-side SONET health facing the 1800, from 'show controllers sonet
    <iface> | no-more' on the NCS 4216. Added 2026-09-02 -- confirmed gap:
    the tool checked MUX facility alarms, CEM connection state, and
    working/protect conflict, but never pulled SONET PM/alarm data from the
    MUX's own side of the OC12/SONET-ACR path facing the 1800 (the existing
    OC12_COUNTERS check is sourced entirely from the 1800's side of that
    same path, not the MUX's).

    Uses the device's own current-state summary lines (Active Defects /
    Detected Alarms / Asserted-Active Alarms) rather than the SECTION/LINE
    LOS/LOF/AIS/RDI/REI counts printed above them -- those are cumulative
    since-boot/since-clear event counts, not current state (same trap as a
    "Num State Transitions" counter elsewhere in this tool), and would
    false-flag a healthy circuit that had one blip in the past.

    Returns (flags, info):
      flags -- any of the three current-state fields reading other than "None"
      info  -- the current (in-progress) 15-min interval's CV/ES/SES/SEFS
               counts, if that table was found (context, not flagged --
               a partial in-progress bin reading 0 doesn't mean much either
               way)
    """
    flags, info = [], []
    if not sonet_pm_raw:
        return flags, info

    for field in _MUX_SONET_STATE_FIELDS:
        m = re.search(r'{}:\s*(.+)'.format(re.escape(field)), sonet_pm_raw)
        if m:
            val = m.group(1).strip()
            if val and val.upper() != "NONE":
                flags.append("SONET {}: {}".format(field, val))

    m = re.search(
        r'INTERVAL\s+CV\s+ES\s+SES\s+SEFS\s*\n\s*(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)',
        sonet_pm_raw)
    if m:
        interval, cv, es, ses, sefs = m.groups()
        info.append("SONET current interval ({}): CV {} / ES {} / SES {} / SEFS {}".format(
            interval, cv, es, ses, sefs))

    return flags, info


def check_fiber_health(fiber_by_port):
    """
    Shared fiber/IXC uplink health check for the DIAGNOSTIC SUMMARY.

    fiber_by_port: {port_name: {"pm": <physical pm text>,
                                "laser": <grep laser text>,
                                "tca": <transceiver tca text>}}
       pm may be the concatenation of the 24hour + 15min PM output.
       tca is optional -- from 'show chassis transceiver tca slot tsX port1'.
       When tca is present, the device-reported Rx warning-low threshold is
       used for flagging instead of the hardcoded FIBER_RX_DBM_FLOOR fallback.

    Returns (flags, info):
      flags -- list of "[ !! ]"-worthy strings (error counters >0, Rx laser
               below threshold)
      info  -- list of informational strings (laser Rx/Tx levels per port)
    TX laser is reported in `info` only (not flagged).
    """
    flags, info = [], []
    for port in sorted(fiber_by_port):
        data = fiber_by_port[port] or {}
        pm    = data.get("pm", "") or ""
        laser = data.get("laser", "") or ""
        tca   = data.get("tca", "") or ""

        for counter in FIBER_ERR_COUNTERS:
            val = _fiber_counter_value(pm, counter)
            if val is not None and val > 0:
                flags.append("{} {}: {}".format(port, counter, val))

        rx, tx = _fiber_laser_rx_tx(laser)

        # Use device-reported warning threshold when available; fall back to
        # the hardcoded floor so tools that haven't collected TCA yet still work.
        tca_thresh = _parse_tca_thresholds(tca)
        rx_thresh = (tca_thresh["rx_warn_low"]
                     if tca_thresh["rx_warn_low"] is not None
                     else FIBER_RX_DBM_FLOOR)

        if rx is not None and rx < rx_thresh:
            flags.append("{} Rx laser {} dBm (warn threshold {} dBm)".format(
                port, rx, rx_thresh))
        if rx is not None or tx is not None:
            info.append("{} laser  Rx {} / Tx {} dBm".format(
                port,
                "{}".format(rx) if rx is not None else "?",
                "{}".format(tx) if tx is not None else "?"))
    return flags, info



def _gsx_split_current_perf(text):
    """
    Split the raw GSX PM blob into its 15-min and 24-hr CURRENT PERFMON
    regions separately, excluding the THRESHOLDS config table and the
    STATISTICS block.

    Bug fixed 2026-08-07: the old version matched the generic substring
    "PERFMON CURRENT", which is present in BOTH the FIFTEENMIN and DAY
    command echoes -- so both sections got captured into one combined
    region with no boundary between them, and the caller could only ever
    report a single collapsed max value even though the GSX line explicitly
    says "(current 15 min / 24 hr)", implying both should be shown. This
    now keys off each section's own distinct command echo
    ("...FIFTEENMIN" vs "...DAY") so the two stay separate.

    Returns (fifteen_region, day_region) -- either can be "" if that
    section wasn't found in the text.
    """
    fifteen, day = [], []
    mode = None
    for line in text.splitlines():
        up = line.upper()
        if "PERFMON CURRENT FIFTEENMIN" in up:
            mode = "fifteen"
            continue   # don't include the echoed command line itself
        if "PERFMON CURRENT DAY" in up:
            mode = "day"
            continue
        if "THRESHOLD" in up:
            mode = None
            continue
        if mode == "fifteen":
            fifteen.append(line)
        elif mode == "day":
            day.append(line)
    return "\n".join(fifteen), "\n".join(day)


def _gsx_scan_counters(region):
    """Non-zero GSX_PM_COUNTERS values in one region -- {counter: max_value}.
    ^\\s* anchors to start-of-line so 'Code Violations:' doesn't match
    'Line Code Violations:' / 'Path Code Violations:'. A counter can appear
    twice within one window (Near End / Far End Path); report the max seen."""
    vals = {}
    if not region:
        return vals
    for c in GSX_PM_COUNTERS:
        pat = re.compile(r'^\s*{}\s*:\s+([0-9]+)\b'.format(re.escape(c)), re.MULTILINE)
        for m in pat.finditer(region):
            v = int(m.group(1))
            if v > 0:
                vals[c] = max(vals.get(c, 0), v)
    return vals


def _check_gsx_counters(text):
    """
    Return ['Counter: <15min> / <24hr>', ...] for any GSX PERFMON counter
    that's non-zero in EITHER window -- both values always shown side by
    side (0 if genuinely zero in that window), matching the GSX line's
    "(current 15 min / 24 hr)" label instead of silently collapsing both
    windows into one number (2026-08-07 fix).
    """
    fifteen_region, day_region = _gsx_split_current_perf(text)
    if not fifteen_region and not day_region:
        return []
    v15 = _gsx_scan_counters(fifteen_region)
    v24 = _gsx_scan_counters(day_region)
    hits = []
    for c in GSX_PM_COUNTERS:
        a, b = v15.get(c, 0), v24.get(c, 0)
        if a or b:
            hits.append("{}: {} / {}  (15min / 24hr)".format(c, a, b))
    return hits


# ---- FULL ROUTE BLOCK ----
def build_route_block(ds1, host_1250, loopback_ip, muse_id,
                      hub_name, hub_ip, oc12, mux_data):
    gsx_assign = "{}-{}-{}".format(
        mux_data.get("gsx_slot") or "?",
        mux_data.get("gsx_port") or "?",
        mux_data.get("gsx_t1")   or "?")
    lines = [
        SEP,
        "  FULL T1 ROUTE  --  {}".format(ds1.upper()),
        SEP, "",
        "  HOP 1 -- NPT-1250",
        "    Host       : {}".format(host_1250),
        "    Loopback   : {}".format(loopback_ip),
        "    DS1        : {}".format(ds1),
        "    MUSE       : {}".format(muse_id),
        "", SEP2,
        "  HOP 2 -- NPT-1800 Hub",
        "    Hub        : {}".format(hub_name),
        "    IP         : {}".format(hub_ip),
        "    {:<10} : {}".format(_oc_label(oc12), oc12),
        "", SEP2,
        "  HOP 3 -- Cisco NCS 4216 MUX",
        "    MUX        : {}".format(mux_data.get("mux_name", "?")),
        "    IP         : {}".format(mux_data.get("mux_ip", "?")),
        # ACR / active path are None when the CEM could not be located in any
        # ACR group. Rendered as NONE FOUND rather than "?" or "None": "?" is
        # used all over this report for merely-absent fields, and this is a
        # stronger statement -- we looked and did not find it. Previously a
        # guessed ACR was printed here as though it were determined.
        "    Active Path: {}".format(
            "SONET {}".format(mux_data["active_sonet"])
            if mux_data.get("active_sonet") else "NONE FOUND"),
        "    ACR Group  : {}".format(mux_data.get("acr_group") or "NONE FOUND"),
        "    CEM Group  : {} (VTG {}, T1 {})".format(
            mux_data.get("cem_group", "?"),
            mux_data.get("vtg") or "?",
            mux_data.get("t1")  or "?"),
        "    GSX CE     : CE{}:{}".format(
            mux_data.get("gsx_ce_iface", "?"), mux_data.get("gsx_cem_id", "?")),
        "", SEP2,
        "  HOP 4 -- GSX",
        # The node name has two sources: the MUX port description and the
        # GSX's own login prompt. Say which, so "device-confirmed" is visible
        # as a positive fact rather than only its absence being implied.
        "    Node       : {}{}".format(
            mux_data.get("gsx_node") or "?",
            {"confirmed":   "   (confirmed by the device itself)",
             "unconfirmed": "   (from the MUX description; device did not "
                            "name itself)",
             # Name BOTH. The route block is the reference an engineer copies
             # commands out of, so a disagreement about WHICH BOX must be on
             # the line itself -- not left to the diagnostic summary further
             # down.
             "conflict":    "   <-- MUX description; the device at this IP "
                            "says '{}'".format(
                                mux_data.get("gsx_node_device") or "?"),
             }.get(mux_data.get("gsx_name_verdict"), "")),
        "    IP         : {}{}".format(
            mux_data.get("gsx_ip") or "?",
            "" if mux_data.get("gsx_ip")
            else ("   <-- " + mux_data["gsx_lookup_error"]
                  if mux_data.get("gsx_lookup_error")
                  else "   <-- GSX not reached; the node name above is from "
                       "the MUX description and is UNVERIFIED")),
        "    Assignment : {}".format(gsx_assign),
        "    T1 Command : SHOW T1 T1-1-{}".format(gsx_assign),
    ]
    # ISUP circuit ID (trunk-group name), information only -- requested for
    # mgmt/Barlow, 2026-11-02. Only shown when the GSX hop resolved it; the
    # full SHOW ISUP CIRCUIT / show trunk group output is in the raw section.
    if mux_data.get("gsx_isup_id"):
        lines.append("    ISUP Circuit: {}".format(mux_data["gsx_isup_id"]))
    # The T1 number has two independent sources (live CEM id vs the controller's
    # provisioned description) which can disagree. When they do, say so right
    # under the command rather than printing one silently -- an operator acting on
    # a wrong T1 touches the wrong circuit.
    if mux_data.get("gsx_xc_conflict"):
        # Upstream ambiguity: more than one xconnect endpoint matched, so the
        # NODE and SLOT may belong to a different circuit too -- not just the T1.
        lines += [
            "",
            "    [!] GSX ENDPOINT AMBIGUOUS -- node/slot/port/T1 may all be wrong:",
            "        {}".format(mux_data["gsx_xc_conflict"]),
        ]
    if mux_data.get("gsx_t1_conflict"):
        lines += [
            "",
            "    [!] T1 SOURCE CONFLICT -- assignment above may be wrong:",
            "        {}".format(mux_data["gsx_t1_conflict"]),
        ]
    elif mux_data.get("gsx_t1_desc"):
        lines.append("    (T1 confirmed by controller description: {})".format(
            mux_data.get("gsx_desc") or "?"))
    lines += [
        "", SEP,
        "  SUMMARY",
        "  1250 {}  |  {}  |  {}".format(ds1, muse_id, oc12),
        "  {} ACR {} CEM {}  |  CE{}:{}  |  {} {}".format(
            mux_data.get("mux_name", "?"), mux_data.get("acr_group") or "?",
            mux_data.get("cem_group", "?"),
            mux_data.get("gsx_ce_iface", "?"), mux_data.get("gsx_cem_id", "?"),
            mux_data.get("gsx_node") or "?", gsx_assign),
        SEP,
    ]
    return "\n".join(lines)


# ---- PEERLESS (PARTIAL) ROUTE BLOCK ----
def build_peerless_block(ds1, host_1250, loopback_ip, muse_id,
                         hub_name, hub_ip, oc12, slot_num):
    lines = [
        SEP,
        "  PARTIAL T1 ROUTE  --  {}".format(ds1.upper()),
        "  NOTE: ts{} detected. No Bandwidth MUX or GSX. Peerless circuit.".format(slot_num),
        SEP, "",
        "  HOP 1 -- NPT-1250",
        "    Host       : {}".format(host_1250),
        "    Loopback   : {}".format(loopback_ip),
        "    DS1        : {}".format(ds1),
        "    MUSE       : {}".format(muse_id),
        "", SEP2,
        "  HOP 2 -- NPT-1800 Hub",
        "    Hub        : {}".format(hub_name),
        "    IP         : {}".format(hub_ip),
        "    {:<10} : {}".format(_oc_label(oc12), oc12),
        "", SEP,
        "  Route ends at 1800 -- ts21/ts24 is customer-facing (Peerless).",
        SEP,
    ]
    return "\n".join(lines)


# ---- DIAGNOSTIC SUMMARY (vertical route, OK/!! indicators, X connectors) ----
def mux_xconnect_status(mux):
    """
    Validate the MUX CEM cross-connect is built as expected.

    The MUX joins the ACR/1250 side (CEM-ACR<acr>:<cem>) to the GSX side
    (CE<gsx_ce>:<gsx_id>). All four values are resolved by the MUX hop; the
    xconnect leg state (xconnect_ok / xconnect_raw) and the named-connection
    state (connection_state) are captured by query_cem_health and live in the
    nested `cem_health` dict -- NOT at the top level of mux_results[ds1].
    gsx_xc_conflict is top-level. This adds no device command; it states,
    explicitly, whether the binding that is UP is the one we expect.

    The xconnect leg state is the signal we reliably have (it comes straight
    from 'show xconnect all' during resolution), so the status LEADS with it.
    connection_state depends on a connection NAME having resolved, which it may
    not, so it is a secondary line shown only when actually read.

    Returns a list of report lines (may be empty when the MUX hop produced no
    xconnect data -- e.g. a Peerless circuit or a failed MUX resolve).
    """
    mux    = mux or {}
    acr    = mux.get("acr_group")
    cem    = mux.get("cem_group")
    gsx_ce = mux.get("gsx_ce_iface")
    gsx_id = mux.get("gsx_cem_id")
    # Nothing to validate if the MUX hop did not resolve the binding.
    if not (acr and cem and acr != "?" and cem != "?"):
        return []

    # Xconnect/connection health is nested under cem_health (method A output).
    ch     = mux.get("cem_health") or {}
    xc_ok  = ch.get("xconnect_ok")
    xc_raw = (ch.get("xconnect_raw") or "").strip()
    state  = (ch.get("connection_state") or "?")

    expected = "CEM-ACR{}:{} <-> CE{}:{}".format(
        acr, cem, gsx_ce or "?", gsx_id or "?")

    lines = ["  XCONNECT STATUS:"]
    # LEAD with the xconnect leg state -- the reliably-captured signal.
    if xc_ok is True:
        lines.append("  [ OK ] xconnect up, both legs -- {}".format(expected))
    elif xc_ok is False and xc_raw:
        lines.append("  [ !! ] an xconnect leg is DOWN -- {} -- {}".format(
            xc_raw.splitlines()[0][:80], expected))
    elif xc_ok is False:
        lines.append("  [ !! ] no xconnect for CE{}:{} -- the binding may not "
                     "be built -- {}".format(acr, cem, expected))
    else:
        # xc_ok is None: query_cem_health did not run (e.g. CEM health lookup
        # raised) -- the binding resolved but its leg state was not captured.
        lines.append("  [ -- ] xconnect state not read -- {}".format(expected))
    # Named-connection binding state, only when it was actually read.
    if state and state not in ("?", ""):
        if state.upper() in ("UP", "CONNECTED"):
            lines.append("  [ OK ] connection binding {}".format(state))
        else:
            lines.append("  [ !! ] connection binding {} (expected UP)".format(
                state))
    # A conflict the MUX hop already flagged means the binding we validated
    # might be a different circuit's -- surface it here, where the binding is.
    if mux.get("gsx_xc_conflict"):
        lines.append("  [ !! ] {}".format(mux["gsx_xc_conflict"]))
    return lines


def build_diagnostic_summary(ds1_list, ds1_map, pm_results, oc12_results,
                             hub_results, mux_results, all_oc12_stats,
                             all_t3_stats, peerless, errors, gsx_pm_results=None,
                             fiber_results=None, evc_results=None,
                             ospf_events_raw=None, ldp_session_raw=None,
                             gsx_t3_results=None, loop_1250=None):
    gsx_pm_results  = gsx_pm_results or {}
    loop_1250 = loop_1250 or {}   # ds1 -> list of 1250 maintenance loops
    _ces_disabled = []   # DS1s needing the CES-enable MOP (see end of fn)
    gsx_t3_results  = gsx_t3_results or {}
    fiber_results = fiber_results or {}
    evc_results = evc_results or {}
    # Fiber uplink (NNI physical) + EVC (logical) health are per-1250 (shared by
    # all DS1s in the run); compute once.
    fiber_flags, fiber_info = check_fiber_health(fiber_results) if fiber_results else ([], [])
    evc_flags = check_evc_health(evc_results) if evc_results else []
    # OSPF/LDP uptime -- also per-1250, shared across all DS1s; compute once.
    ospf_ldp_flags, ospf_ldp_info = check_ospf_ldp_health(ospf_events_raw, ldp_session_raw)

    def _emit_1800_nni(hub, out):
        """Render the 1800 NNI/EVC health lines (the 'bottom' of the 1800 block)."""
        nni = (hub or {}).get("nni") or {}
        status = nni.get("status")
        if not status:
            return  # no NNI data collected (older run / not available)
        if status == "ambiguous":
            out.append("  [ !! ] NNI ambiguous ({} matches) -- not checked".format(
                nni.get("prefix", "?")))
            return
        if status in ("none", "error"):
            out.append("  [ .. ] NNI not resolved ({})".format(status))
            return
        nflags, ninfo = check_nni_health(nni.get("phys_pm", ""))
        phys = nni.get("physical", "?")
        if nflags:
            for f in nflags:
                out.append("  [ !! ] NNI {} {}".format(phys, f))
        else:
            out.append("  [ OK ] NNI {} PMs clean".format(phys))
        for i in ninfo:
            out.append("         {}".format(i))
        eflags = check_evc_health({nni.get("logical", "evc"): nni.get("evc_pm", "")})
        if eflags:
            for f in eflags:
                out.append("  [ !! ] EVC {}".format(f))
        else:
            out.append("  [ OK ] EVC {} PMs clean".format(nni.get("logical", "?")))

    lines = []

    for ds1 in ds1_list:
        lines.append(SEP)
        lines.append("  DIAGNOSTIC SUMMARY  --  {}".format(ds1.upper()))
        lines.append(SEP)
        lines.append("")

        if ds1 in errors:
            lines.append("  COLLECTION FAILED: {}".format(errors[ds1]))
            lines.append("")
            continue

        # Loop status goes FIRST -- a looped DS1 reads clean PMs at every hop
        # below, so "everything's clean" must be read in light of the loop.
        # Status is shown even when clean (pcapps 2026-11-02) so absence of a
        # warning is not mistaken for absence of the check. Fiber/GE excluded.
        _mux_for_loop = mux_results.get(ds1, {})
        _l1250 = loop_1250.get(ds1)
        _lmux = _mux_for_loop.get("mux_loop")
        _lgsx = _mux_for_loop.get("gsx_loop")
        lines.append("  LOOP STATUS:")
        lines.append(loop_check.status_line(
            "1250 {}".format(ds1), _l1250,
            ", ".join(x["type"] for x in _l1250) if _l1250 else ""))
        # MUX has TWO interfaces in the path and a loop can sit on either:
        # the T3/CE controller facing the GSX, and the SONET controller
        # facing the 1800. Emit a line for EACH so both are visibly checked
        # -- a single collapsed line read as "only one side was looked at"
        # (pcapps, 2026-11-02).
        for _side, _iface in (
                ("faces GSX",  (_mux_for_loop.get("gsx_ce_iface") or "").strip()),
                ("faces 1800", (_mux_for_loop.get("active_sonet") or "").strip())):
            if not _iface:
                # Side's controller was not resolved -- say so rather than
                # silently drop it, so the count is always both sides.
                lines.append("  [ -- ] MUX {} -- controller not identified"
                             .format(_side))
                continue
            _hits = [l for l in (_lmux or [])
                     if (l.get("controller") or "").split()[-1:] == [_iface]]
            lines.append(loop_check.status_line(
                "MUX {} ({})".format(_iface, _side),
                _hits, "; ".join("{} {}".format(l["controller"] or "?", l["mode"])
                                 for l in _hits)))
        lines.append(loop_check.status_line(
            "GSX T1", bool(_lgsx), _lgsx or ""))
        lines.append("")

        # MUX cross-connect validation -- is the binding that is UP the one we
        # expect? (ACR/CEM side joined to the GSX CE side.)
        _xc = mux_xconnect_status(_mux_for_loop)
        if _xc:
            lines.extend(_xc)
            lines.append("")

        pm   = pm_results.get(ds1, "")
        oc12 = oc12_results.get(ds1, "")
        oc_lbl = _oc_label(oc12)
        hub  = hub_results.get(ds1, {})
        mux  = mux_results.get(ds1, {})
        oc12_base = oc12.split(":")[0] if oc12 else ""
        oc12_key  = "{}|{}".format(hub.get("hub_ip", ""), oc12_base)
        oc12_stat = all_oc12_stats.get(oc12_key, "")
        fa        = mux.get("facility_alarm", "")
        gsx_node  = mux.get("gsx_node", "?")
        gsx_slot  = mux.get("gsx_slot", "?")
        gsx_port  = mux.get("gsx_port", "?")
        gsx_t1    = mux.get("gsx_t1", "?")
        gsx_assign = "{}-{}-{}".format(gsx_slot, gsx_port, gsx_t1)

        # common 1250 TDM/CES detection
        ces_hits = _check_counters(pm, CES_COUNTERS)
        tdm_hits = _check_counters(pm, TDM_COUNTERS)
        phys_los  = bool(re.search(r'ds1/\d+\s+LOS', pm))
        alarm_los = bool(re.search(r'DS1LOS-L', pm) and
                         re.search(r'port-name=ds1-{}'.format(re.escape(ds1)), pm, re.IGNORECASE))
        oc12_errs = _check_counters(oc12_stat, OC12_COUNTERS)
        # Scope alarms to THIS circuit's sub-channel, not every DS1 on the OC.
        oc12_alarms = _oc12_alarms_for(oc12_stat, oc12)

        if ds1 in peerless:
            lines.append("  Peerless circuit (ts{}).".format(peerless[ds1]))
            lines.append("")
            lines.append("         Peerless Network")
            lines.append("")
            lines.append("              X")
            lines.append("")
            # 1800 -- has TWO independent interfaces (MUX/network-facing OC,
            # and 1250-facing NNI/EVC) that can be dirty independently of
            # each other. Label both explicitly -- "OC clean" sitting right
            # above "NNI dirty" with no divider read as contradictory
            # information about "the 1800" (confirmed live 2026-08-06),
            # when really they're two different links off the same box.
            # The "1800 -- {name} ({ip})" identifier sits BETWEEN the two
            # sub-sections (not at the top), and the network-facing side
            # label sits right above it (not above the OC PM/alarm lines) --
            # per direction 2026-08-07: matches the "eyes land on the device
            # name line" reading style used throughout this report.
            for e in oc12_errs:
                lines.append("  [ !! ] {} PM {}".format(oc_lbl, e))
            if not oc12_errs:
                lines.append("  [ OK ] {} PMs clean".format(oc_lbl))
            # Show the actual matched alarm line(s), not just a count --
            # "(1 present)" told the reader nothing without logging into
            # the device to look it up, which defeats the point of this
            # report (2026-08-07 direction).
            if oc12_alarms:
                lines.append("  [ !! ] {} alarm(s): {}".format(
                    oc_lbl, "; ".join(oc12_alarms)))
            else:
                lines.append("  [ OK ] {} alarms clean".format(oc_lbl))
            lines.append("         -- network-facing side ({}) --".format(oc12 or oc_lbl))
            lines.append("         1800  --  {}  ({})".format(
                hub.get("hub_name", "?"), hub.get("hub_ip", "?")))
            lines.append("         -- 1250-facing side (NNI/EVC) --")
            _emit_1800_nni(hub, lines)
            lines.append("")
            lines.append("              X")
            lines.append("")
            # 1250 fiber/IXC uplink (NNI physical + optical health)
            if fiber_flags:
                for f in fiber_flags:
                    lines.append("  [ !! ] NNI {}".format(f))
            else:
                lines.append("  [ OK ] NNI PMs clean")
            for i in fiber_info:
                lines.append("         {}".format(i))
            # 1250 EVC (logical sub-interface) health
            if evc_flags:
                for f in evc_flags:
                    lines.append("  [ !! ] EVC {}".format(f))
            else:
                lines.append("  [ OK ] EVC PMs clean")
            # 1250 OSPF/LDP uptime (site-level, shared across all DS1s in this run)
            if ospf_ldp_flags:
                for f in ospf_ldp_flags:
                    lines.append("  [ !! ] {}".format(f))
            for i in ospf_ldp_info:
                lines.append("  [ OK ] {}".format(i))
            # 1250 -- CES above the identifier line; only dirty TDM below
            if _CES_DISABLED_RE.search(pm):
                lines.append("  [ -- ] CES PMs disabled")
            elif ces_hits:
                for e in ces_hits:
                    lines.append("  [ !! ] CES {}".format(e))
            else:
                lines.append("  [ OK ] CES PMs clean")
            lines.append("         1250  --  {}".format(
                ds1_map.get(ds1, {}).get("loopback_ip", "?")))
            lines.append("         ds1-{}  {}".format(
                ds1, ds1_map.get(ds1, {}).get("muse_id", "?")))
            if phys_los or alarm_los:
                lines.append("  [ !! ] TDM  LOS -- loss of signal from LEC")
            elif tdm_hits:
                for e in tdm_hits:
                    lines.append("  [ !! ] TDM {}".format(e))
            else:
                lines.append("  [ OK ] TDM PMs clean")
            lines.append("")
            continue

        # ---- FULL ROUTE: GSX -> MUX -> 1800 -> 1250 (top-down) ----
        gsx_pm = gsx_pm_results.get(ds1, "")
        # Operational/alarm status = the GSX's "down" indicator (like TDM LOS)
        gsx_down = bool(re.search(r'Operational Status:\s*DOWN', gsx_pm, re.IGNORECASE))
        gsx_alarm_m = re.search(r'Alarm Status:\s*(\S+)', gsx_pm)
        gsx_alarm = gsx_alarm_m.group(1) if gsx_alarm_m else ""
        gsx_hits = _check_gsx_counters(gsx_pm)
        # Check parent T3 status -- if T3 is DOWN, the DS1 is down because of
        # the T3, not a DS1-specific issue. All 28 T1s on this port are affected.
        gsx_t3_raw  = gsx_t3_results.get(ds1, "")
        gsx_t3_down = bool(re.search(r'Operational Status:\s*DOWN', gsx_t3_raw, re.IGNORECASE))
        gsx_t3_alarm_m = re.search(r'Alarm Status:\s*(\S+)', gsx_t3_raw)
        gsx_t3_alarm   = gsx_t3_alarm_m.group(1) if gsx_t3_alarm_m else ""

        # Identity cross-check (v2.28): MUX port description vs the name the
        # GSX gave at login.
        #
        # On a CONFLICT the header is labelled with the DEVICE's own name, not
        # the MUX-derived one -- the counters below came off the box we logged
        # into, so naming them after the box the MUX expected would be an
        # active mislabel, which is the whole fault being reported.
        _name_verdict = mux.get("gsx_name_verdict")
        _name_conflict = mux.get("gsx_name_conflict")
        _hdr_node = (mux.get("gsx_node_device") or gsx_node
                     if _name_verdict == "conflict" else gsx_node)
        lines.append("         GSX  --  {}  ({})  (current 15 min / 24 hr)".format(
            _hdr_node, mux.get("gsx_ip", "?")))
        # Printed above the counters, because it decides whether the counters
        # are even the right device's.
        if _name_conflict:
            lines.append("  [ !! ] {}".format(_name_conflict))
        if gsx_t3_down:
            lines.append("  [ !! ] GSX T3 DOWN{}  <-- root cause; all T1s on this port affected".format(
                " ({})".format(gsx_t3_alarm) if gsx_t3_alarm and gsx_t3_alarm.upper() != "NONE" else ""))
        if gsx_down:
            if gsx_t3_down:
                lines.append("  [ !! ] GSX T1 DOWN{}  (caused by T3 above)".format(
                    " ({})".format(gsx_alarm) if gsx_alarm and gsx_alarm.upper() != "NONE" else ""))
            else:
                lines.append("  [ !! ] GSX DOWN-DS1ALARM{}".format(
                    " ({})".format(gsx_alarm) if gsx_alarm and gsx_alarm.upper() != "NONE" else ""))
        if gsx_hits:
            for e in gsx_hits:
                lines.append("  [ !! ] GSX {}".format(e))
            # State the PARENT T3 whenever the DS1 is erroring.
            #
            # Until 2026-10-01 the T3 was mentioned only when it was DOWN, so a
            # DS1 taking errors behind a T3 that was up produced no T3 line at
            # all -- and the reader had to go and check the DS3 by hand to tell
            # a DS1-specific fault from a T3-level one. That is the same
            # one-ticket-or-28 distinction v2.13 added, which only covered the
            # outage case and not the errored case.
            #
            # Costs no extra command: gsx_t3_raw is already pulled per DS1 for
            # the T3-DOWN check above. It is STATUS, not PM counters, so the
            # wording claims only what it knows -- "no alarm on the parent T3",
            # never "the T3 is clean".
            if not gsx_t3_down:
                _t3_alarmed = bool(gsx_t3_alarm
                                   and gsx_t3_alarm.upper() not in ("NONE",
                                                                    "NOALARM"))
                if _t3_alarmed:
                    lines.append(
                        "  [ !! ] parent T3 {}-{} ALARM {} -- may affect every "
                        "DS1 on this port".format(
                            mux.get("gsx_slot") or "?",
                            mux.get("gsx_port") or "?", gsx_t3_alarm))
                elif gsx_t3_raw.strip():
                    lines.append(
                        "  [ -- ] parent T3 {}-{} is UP with no alarm -- these "
                        "errors look DS1-specific".format(
                            mux.get("gsx_slot") or "?",
                            mux.get("gsx_port") or "?"))
                else:
                    # No T3 status came back. Say so rather than implying the
                    # parent is healthy by omission.
                    lines.append(
                        "  [ -- ] parent T3 status not read -- cannot say "
                        "whether this is DS1-specific or T3-level")
        if not gsx_t3_down and not gsx_down and not gsx_hits:
            lines.append("  [ OK ] GSX PMs clean")
        lines.append("         {}".format(gsx_assign))
        lines.append("")
        lines.append("              X")
        lines.append("")

        # MUX / T3
        t3_iface = mux.get("gsx_ce_iface", "")
        t3_stat  = all_t3_stats.get(t3_iface, "")
        t3_errs  = _check_counters(t3_stat, T3_COUNTERS)
        fa_alarm = ("LP_AIS" in fa) if fa else False

        if t3_errs:
            for e in t3_errs:
                lines.append("  [ !! ] T3 {}".format(e))
        else:
            lines.append("  [ OK ] T3 clean")
        lines.append("         CE{}:{}".format(
            t3_iface, mux.get("gsx_cem_id", "?")))
        lines.append("         MUX  --  {}  ({})".format(
            mux.get("mux_name", "?"), mux.get("mux_ip", "?")))
        lines.append("         {} | SONET {}  ACR {}  CEM {}".format(
            oc12, mux.get("active_sonet") or "?", mux.get("acr_group") or "?",
            mux.get("cem_group", "?")))
        fa_list = mux.get("facility_alarms") or []
        if fa_list:
            # Cross-hop inputs for the DS1_AIS verdict -- all collected in
            # this same run, so the tool can walk the troubleshooting logic
            # itself instead of leaving a static direction hint.
            tdm_ais_in = bool(re.search(r'DS1AIS', pm) and
                              re.search(r'port-name=ds1-{}'.format(re.escape(ds1)),
                                        pm, re.IGNORECASE))
            mid_flags = []
            if ces_hits:
                mid_flags.append("CES {}".format(ces_hits[0].split(":")[0].strip()))
            if evc_flags:
                mid_flags.append("EVC")
            if fiber_flags:
                mid_flags.append("NNI")
            if oc12_errs:
                mid_flags.append("{} {}".format(
                    oc_lbl, oc12_errs[0].split(":")[0].strip()))
            if oc12_alarms:
                mid_flags.append("{} alarms".format(oc_lbl))

            for sev, syslog in fa_list:
                lines.append("  [ !! ] {} {}".format(sev, syslog))
                if "DS1_AIS" in syslog.upper():
                    verdict = _ais_verdict(
                        (phys_los or alarm_los), tdm_ais_in, tdm_hits,
                        mid_flags, ds1_map.get(ds1, {}).get("muse_id"),
                        oc12, mux.get("acr_group"), mux.get("cem_group"))
                    lines.extend(_wrap_note(verdict))
        else:
            lines.append("  [ OK ] No alarms")
        nb_conflict = mux.get("netbox_conflict")
        if nb_conflict:
            lines.append("  [ !! ] {}".format(nb_conflict))

        # MUX-side SONET PM facing the 1800 (added 2026-09-02 -- previously
        # unchecked; see check_mux_sonet_health() for why).
        sonet_flags, sonet_info = check_mux_sonet_health(mux.get("sonet_pm", ""))
        if sonet_flags:
            for f in sonet_flags:
                lines.append("  [ !! ] {}".format(f))
        elif mux.get("sonet_pm"):
            lines.append("  [ OK ] SONET PMs clean")
        for i in sonet_info:
            lines.append("         {}".format(i))

        # CEM circuit health (item 14). Pkts Missing / Failure Counts / Lbits /
        # Rbits are cumulative-since-last-"clear cem circuit" (see the CEM
        # Hairpin Loop MOP), not a current-state reading -- a mature circuit
        # shows large non-zero values regardless of health. They no longer
        # drive an [ OK ]/[ !! ] verdict here; the raw counters are still in
        # the MUX Stats section (>> CEM Circuit Health) for reference.
        cem_h = mux.get("cem_health") or {}
        if cem_h:
            conn_state = cem_h.get("connection_state", "")
            xc_ok = cem_h.get("xconnect_ok", True)
            if conn_state and conn_state.upper() != "UP":
                lines.append("  [ !! ] Connection State: {}".format(conn_state))
            elif conn_state == "UP":
                lines.append("  [ OK ] Connection UP")
            if not xc_ok:
                lines.append("  [ !! ] Xconnect leg(s) DOWN")

        lines.append("")
        lines.append("              X")
        lines.append("")

        # 1800 -- has TWO independent interfaces (MUX-facing OC, and
        # 1250-facing NNI/EVC) that can be dirty independently of each
        # other. Label both explicitly -- see the matching comment in the
        # Peerless branch above for why (confirmed live 2026-08-06: read as
        # contradictory "1800 clean" vs "1800 dirty" without this). The
        # "1800 -- {name} ({ip})" identifier sits BETWEEN the two
        # sub-sections, and the MUX-facing side label sits right above it
        # (not above the OC PM/alarm lines) -- per direction 2026-08-07,
        # matches the Peerless branch's "eyes land on the device name line"
        # reading style.
        if oc12_errs:
            for e in oc12_errs:
                lines.append("  [ !! ] {} PM {}".format(oc_lbl, e))
        else:
            lines.append("  [ OK ] {} PMs clean".format(oc_lbl))
        # Show the actual matched alarm line(s), not just a count (2026-08-07
        # direction -- same fix as the Peerless branch above).
        if oc12_alarms:
            lines.append("  [ !! ] {} alarm(s): {}".format(
                oc_lbl, "; ".join(oc12_alarms)))
        else:
            lines.append("  [ OK ] {} alarms clean".format(oc_lbl))
        lines.append("         -- MUX-facing side ({}) --".format(oc12 or oc_lbl))
        lines.append("         1800  --  {}  ({})".format(
            hub.get("hub_name", "?"), hub.get("hub_ip", "?")))
        lines.append("         -- 1250-facing side (NNI/EVC) --")
        _emit_1800_nni(hub, lines)

        lines.append("")
        lines.append("              X")
        lines.append("")

        # 1250 fiber/IXC uplink (NNI physical + optical health, faces the core)
        if fiber_flags:
            for f in fiber_flags:
                lines.append("  [ !! ] NNI {}".format(f))
        else:
            lines.append("  [ OK ] NNI PMs clean")
        for i in fiber_info:
            lines.append("         {}".format(i))
        # 1250 EVC (logical sub-interface) health
        if evc_flags:
            for f in evc_flags:
                lines.append("  [ !! ] EVC {}".format(f))
        else:
            lines.append("  [ OK ] EVC PMs clean")

        # 1250 OSPF/LDP uptime (site-level, shared across all DS1s in this run)
        if ospf_ldp_flags:
            for f in ospf_ldp_flags:
                lines.append("  [ !! ] {}".format(f))
        for i in ospf_ldp_info:
            lines.append("  [ OK ] {}".format(i))

        # 1250 -- CES above the identifier line; only dirty TDM below
        if _CES_DISABLED_RE.search(pm):
            lines.append("  [ -- ] CES PMs disabled")
            _ces_disabled.append(ds1)
        elif ces_hits:
            for e in ces_hits:
                lines.append("  [ !! ] CES {}".format(e))
        else:
            lines.append("  [ OK ] CES PMs clean")
        lines.append("         1250  --  {}".format(
            ds1_map.get(ds1, {}).get("loopback_ip", "?")))
        lines.append("         ds1-{}  {}".format(
            ds1, ds1_map.get(ds1, {}).get("muse_id", "?")))
        if phys_los or alarm_los:
            lines.append("  [ !! ] TDM  LOS -- loss of signal from LEC")
        elif tdm_hits:
            for e in tdm_hits:
                lines.append("  [ !! ] TDM {}".format(e))
        else:
            lines.append("  [ OK ] TDM PMs clean")
        lines.append("")

    lines.append(SEP)
    # One MOP block for every DS1 that reported CES PMs disabled -- appended
    # once, after the findings, never inline. Returns "" when none are.
    mop = build_ces_enable_mop(_ces_disabled)
    if mop:
        lines.append(mop)
    return "\n".join(lines)


# ---- RUN SUMMARY ONE-LINER (standardized Clean/Dirty per hop/side) ----
def run_summary_line(ds1, ds1_map, pm_results, oc12_results,
                     hub_results, mux_results, all_oc12_stats, all_t3_stats,
                     peerless, fiber_results=None, evc_results=None,
                     gsx_pm_results=None, gsx_t3_results=None,
                     errors=None):
    """
    One-liner per DS1: Clean/Dirty per hop, split by direction where a hop
    has two distinct sides:
      1250: TDM / CES
      1800: 1250-side (NNI+EVC) / MUX-side (OC PM + alarms)
      MUX:  1800-side (facility alarms + connection state) / GSX-side (T3)

    Dirty tags name the counter/alarm that tripped it -- never the raw value.
    CEM Pkts Missing / Failure Counts / Lbits / Rbits are deliberately NOT
    part of this (see build_diagnostic_summary's CEM section for why): they
    are cumulative-since-last-"clear cem circuit" counters, not a current-
    state reading, so treating any non-zero value as "Dirty" would flag
    almost every mature circuit regardless of real health.
    """
    fiber_results = fiber_results or {}
    evc_results = evc_results or {}

    pm       = pm_results.get(ds1, "")
    oc12     = oc12_results.get(ds1, "")
    hub      = hub_results.get(ds1, {})
    mux      = mux_results.get(ds1, {})
    oc12_base = oc12.split(":")[0] if oc12 else ""
    oc12_key  = "{}|{}".format(hub.get("hub_ip", ""), oc12_base)
    oc12_stat = all_oc12_stats.get(oc12_key, "")

    # ---- 1250: TDM / CES ----
    phys_los  = bool(re.search(r'ds1/\d+\s+LOS', pm))
    alarm_los = bool(re.search(r'DS1LOS-L', pm) and
                     re.search(r'port-name=ds1-{}'.format(re.escape(ds1)), pm, re.IGNORECASE))
    tdm_hits = _check_counters(pm, TDM_COUNTERS)
    if phys_los or alarm_los:
        tdm_cd = _cd(True, "DS1LOS")
    elif tdm_hits:
        tdm_cd = _cd(True, tdm_hits[0].split(":")[0].strip())
    else:
        tdm_cd = _cd(False)

    if _CES_DISABLED_RE.search(pm):
        ces_cd = "Disabled"
    else:
        # Worst (highest-value) CES counter drives the tag -- value not shown.
        _ces_max_c, _ces_max_v = None, 0
        for c in CES_COUNTERS:
            m = re.search(r'{}\s*:\s*([1-9]\d*)'.format(re.escape(c)), pm)
            if m:
                val = int(m.group(1))
                if val > _ces_max_v:
                    _ces_max_v, _ces_max_c = val, c
        ces_cd = _cd(bool(_ces_max_c), _ces_max_c)

    line_1250 = "1250: TDM {} / CES {}".format(tdm_cd, ces_cd)

    # ---- 1800: 1250-side (NNI+EVC) / MUX-side (OC PM + alarms) ----
    fiber_flags, _ = check_fiber_health(fiber_results) if fiber_results else ([], [])
    evc_flags = check_evc_health(evc_results) if evc_results else []
    if fiber_flags:
        side_1250_cd = _cd(True, "NNI")
    elif evc_flags:
        side_1250_cd = _cd(True, "EVC")
    else:
        side_1250_cd = _cd(False)

    oc12_pm_errs = _check_counters(
        oc12_stat, ["ES-S", "SES-S", "CV-S", "ES-L", "SES-L", "UAS-L"])
    oc12_alarm_count = sum(1 for line in oc12_stat.splitlines()
                           if "R-DS1AIS-P" in line or "AIS-V" in line)
    if oc12_pm_errs:
        side_net_cd = _cd(True, oc12_pm_errs[0].split(":")[0].strip())
    elif oc12_alarm_count:
        side_net_cd = _cd(True, "R-DS1AIS")
    else:
        side_net_cd = _cd(False)

    if ds1 in peerless:
        # Peerless ends at the 1800 -- the "other side" is the Peerless
        # customer network, not a Bandwidth MUX, so label it accordingly.
        line_1800 = "1800: 1250-side {} / network-side {}".format(
            side_1250_cd, side_net_cd)
        return "  {}  --  The test completed successfully (Peerless -- ts{})  |  {}  |  {}".format(
            ds1.upper(), peerless[ds1], line_1250, line_1800)

    line_1800 = "1800: 1250-side {} / MUX-side {}".format(side_1250_cd, side_net_cd)

    # If key route data is missing (netbox not populated), we can't confirm the
    # circuit is healthy -- report "completed with errors" rather than
    # silently passing.
    # Name the source that is actually missing, and the recorded reason where
    # there is one.
    #
    # This used to end every incomplete route with "| Check Netbox" regardless
    # of cause. The MUX is NOT from NetBox -- it comes from LOC_TO_MUX, a
    # hardcoded dict in autotester_standalone.py -- so an engineer whose MUX
    # connect had simply failed was sent to look at NetBox, where there was
    # nothing to find and no hint of the real cause (2026-10-01).
    missing, netbox_relevant = [], False
    if not oc12:
        missing.append("1800/OC not resolved")
        netbox_relevant = True
    if not hub.get("hub_ip"):
        missing.append("hub IP unknown")
        netbox_relevant = True
    if not mux or not mux.get("mux_name") or mux.get("mux_name") == "?":
        missing.append("MUX not found")
    if missing:
        # The per-DS1 error recorded at the point of failure beats any guess
        # this function could make about why.
        why = (errors or {}).get(ds1, "")
        tail = ("  |  {}".format(why) if why
                else ("  |  Check Netbox" if netbox_relevant
                      else "  |  reason not recorded -- see the console log"))
        return "  {}  --  The test completed with errors  |  Route incomplete: {}{}".format(
            ds1.upper(), ", ".join(missing), tail)

    # ---- MUX: 1800-side (facility alarms + connection state) / GSX-side (T3) ----
    t3_iface   = mux.get("gsx_ce_iface", "")
    t3_stat    = all_t3_stats.get(t3_iface, "")
    t3_errs    = _check_counters(t3_stat, T3_COUNTERS)
    # Check GSX T3 status first -- if the parent T3 is DOWN, all 28 T1s on that
    # port are affected by the same root cause. Flag as T3-DOWN (one ticket, not
    # 28) rather than T1-DOWN. Fall through to T1 operational/alarm status for
    # DS1-specific failures, then T3 PM counters.
    gsx_t3_raw  = (gsx_t3_results or {}).get(ds1, "")
    gsx_t3_down = bool(re.search(r'Operational Status:\s*DOWN', gsx_t3_raw, re.IGNORECASE))
    gsx_pm      = (gsx_pm_results or {}).get(ds1, "")
    gsx_t1_down  = bool(re.search(r'Operational Status:\s*DOWN', gsx_pm, re.IGNORECASE))
    gsx_alarm_m  = re.search(r'Alarm Status:\s*(\S+)', gsx_pm)
    gsx_alarm    = gsx_alarm_m.group(1) if gsx_alarm_m else ""
    gsx_t1_alarm = bool(gsx_alarm and gsx_alarm.upper() not in ("NONE", "NOALARM"))
    if gsx_t3_down:
        side_gsx_cd = _cd(True, "T3-DOWN")     # root cause -- all T1s on port affected
    elif gsx_t1_down:
        side_gsx_cd = _cd(True, "T1-DOWN")     # DS1-specific failure
    elif gsx_t1_alarm:
        side_gsx_cd = _cd(True, gsx_alarm)
    elif t3_errs:
        side_gsx_cd = _cd(True, t3_errs[0].split(":")[0].strip())
    else:
        side_gsx_cd = _cd(False)

    fa_alarms = mux.get("facility_alarms") or []
    cem_h = mux.get("cem_health") or {}
    conn_state = (cem_h.get("connection_state") or "")
    conn_bad = bool(conn_state) and conn_state.upper() not in ("UP", "?")
    xc_bad = cem_h.get("xconnect_ok") is False
    if fa_alarms:
        side_1800m_cd = _cd(True, fa_alarms[0][1])
    elif conn_bad:
        side_1800m_cd = _cd(True, "Connection")
    elif xc_bad:
        side_1800m_cd = _cd(True, "Xconnect")
    else:
        side_1800m_cd = _cd(False)

    line_mux = "MUX: 1800-side {} / GSX-side {}".format(side_1800m_cd, side_gsx_cd)

    parts = [line_1250, line_1800, line_mux]

    nb_conflict = mux.get("netbox_conflict")
    if nb_conflict:
        parts.append("NOTE: {}".format(nb_conflict))

    # Identity cross-check (v2.28). A CONFLICT -- the MUX description and the
    # GSX's own login name disagree -- means the GSX-side counters above were
    # read off a different device than the one this circuit is supposed to
    # land on, so "completed successfully" would be a false pass. An
    # UNCONFIRMED name (device gave none) is a gap, not a contradiction: it
    # gets a note and keeps the verdict.
    name_verdict = mux.get("gsx_name_verdict")
    if name_verdict == "conflict":
        return ("  {}  --  The test completed with errors  |  {}  |  "
                "{}".format(ds1.upper(), "  |  ".join(parts),
                            mux.get("gsx_name_conflict")
                            or "GSX identity conflict"))
    if name_verdict == "unconfirmed":
        parts.append("NOTE: {}".format(
            mux.get("gsx_name_conflict") or "GSX node name unconfirmed"))

    return "  {}  --  The test completed successfully  |  {}".format(
        ds1.upper(), "  |  ".join(parts))
