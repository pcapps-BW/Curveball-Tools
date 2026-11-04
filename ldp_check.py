#!/usr/bin/env python3
"""
ldp_check.py -- shared, always-emit LDP status for the circuit tools.

WHY A SHARED HELPER

LDP was checked inconsistently: the forward autotester showed 1250 LDP session
uptime, IXC Check showed adjacency + an outage count, OSPF Events didn't surface
LDP at all, and the reverse autotester never read it. This module gives every
tool ONE way to report LDP, rendered identically -- the same move already made
for loops (loop_check) and MUX xconnects.

ALWAYS SHOW (pcapps, 2026-11-03)

Like the loop lines, an LDP status line is emitted every run even when healthy,
so the absence of a warning means the check ran -- not that it was skipped. A
clean session reads `[ OK ] LDP <peer> Operational -- up <uptime>`; a bad one
reads `[ !! ]`.

TWO DEVICE READS, BOTH CONFIRMED ELSEWHERE IN THE SUITE

  * `show ldp session | no-more`        -- peer / state / uptime. Uptime is a
        real device-reported field (no derivation). Parsed by the canonical
        parse_ldp_sessions in ospf_events (reused here, not re-implemented, so
        the two can't drift).
  * `show ne events | grep LDP-Session | no-more`  -- state-change history.
        Used only to annotate the most recent bounce; optional.

The session's own `uptime` already tells you how long it has been up (i.e. when
it last came up); the event log adds the last DOWN/UP transition timestamp when
a tool has it in hand. Device clocks are UTC (DEVICE_CLOCK_IS_UTC elsewhere in
the suite) -- the bounce stamp is shown as the device reported it, tagged UTC,
rather than silently mis-converted.

Importing note: this module imports only `ospf_events` (which pulls in no
module that imports this one), so it is safe for output_builders / interface_check
/ reverse_autotester / ospf_events to import. It must NOT import ospf_stability
(that would cycle through output_builders).

Built by P. Capps.
"""

import re

from ospf_events import parse_ldp_sessions   # canonical session parser (no drift)

LDP_SESSION_CMD = "show ldp session | no-more"
LDP_EVENT_CMD   = "show ne events | grep LDP-Session | no-more"

# A device timestamp inside an event record: 2026-01-06,00:45:43
_TS_RE = re.compile(r'(\d{4}-\d{2}-\d{2},\d{2}:\d{2}:\d{2})')
# The LDP-Session event name (…-Down / …-Up / bare).
_LDP_EVT_NAME_RE = re.compile(r'(LDP-Session[\w-]*)', re.IGNORECASE)


def last_ldp_bounce(event_raw):
    """
    Most recent LDP-Session state change from `show ne events | grep LDP-Session`.

    Returns {"name": 'LDP-Session-Down'|'LDP-Session-Up'|..., "ts": 'YYYY-MM-DD,HH:MM:SS'}
    for the newest event, or None when the text has no parseable LDP-Session event.

    Records are separated by '******' and a timestamp can appear twice in one
    record -- split on the marker and take the FIRST timestamp per record (the
    suite's confirmed event-log parsing rule), never split on newlines.
    """
    if not event_raw:
        return None
    best = None
    for rec in re.split(r'\*{3,}', event_raw):
        if "ldp-session" not in rec.lower():
            continue
        ts = _TS_RE.search(rec)
        if not ts:
            continue
        nm = _LDP_EVT_NAME_RE.search(rec)
        cand = {"name": nm.group(1) if nm else "LDP-Session", "ts": ts.group(1)}
        # Timestamps are YYYY-MM-DD,HH:MM:SS -- lexical order == chronological.
        if best is None or cand["ts"] > best["ts"]:
            best = cand
    return best


def status_lines(session_raw, event_raw=None, peer_filter=None):
    """
    Always-emit LDP status lines, one per session, to drop under an
    `LDP STATUS:` header in a report.

    session_raw : raw `show ldp session` text (may be None/"" if not collected).
    event_raw   : optional raw LDP-Session event text; used only to annotate the
                  most recent bounce.
    peer_filter : optional substring (e.g. a hub loopback IP). When given, only
                  sessions whose peer contains it are shown -- for per-circuit
                  tools that care about one hub peer, not the whole box.

    Returns a list of lines. Never raises.
    """
    if session_raw is None:
        return ["  [ -- ] LDP -- not checked"]

    sessions = parse_ldp_sessions(session_raw)
    if peer_filter:
        sessions = [s for s in sessions if peer_filter in s.get("peer", "")]

    if not sessions:
        # The command ran but nothing parsed: no sessions, or a format we don't
        # recognise. Say so -- a blank would read as "checked, clean".
        return ["  [ -- ] LDP -- no session data"
                + ("" if not peer_filter else " for peer {}".format(peer_filter))]

    bounce = last_ldp_bounce(event_raw)
    suffix = ""
    if bounce:
        suffix = "  (last {} {} UTC)".format(bounce["name"], bounce["ts"])

    lines = []
    for s in sessions:
        peer  = s.get("peer", "?")
        state = s.get("state", "?")
        up    = s.get("uptime", "?")
        if state.lower() == "operational":
            lines.append("  [ OK ] LDP {} Operational -- up {}{}".format(
                peer, up, suffix))
        else:
            lines.append("  [ !! ] LDP {} {} (expected Operational) -- up {}{}".format(
                peer, state, up, suffix))
    return lines


def self_test(verbose=True):
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    # Two sessions, one operational, one not (dual-homed-ish).
    sess = ("10.111.130.255 :0      false    Operational        1d:10h:37m:54s 35   40\n"
            "10.111.131.255 :0      false    Nonexistent        0d:00h:00m:03s 35   40\n")
    lines = status_lines(sess)
    chk("one line per session", len(lines), 2)
    chk("operational -> OK", lines[0].startswith("  [ OK ] LDP 10.111.130.255:0 Operational -- up 1d:10h:37m:54s"), True)
    chk("non-operational -> !!", lines[1].startswith("  [ !! ] LDP 10.111.131.255:0 Nonexistent"), True)

    # peer filter narrows to one.
    chk("peer filter narrows", len(status_lines(sess, peer_filter="10.111.130.255")), 1)

    # not collected vs no data.
    chk("None -> not checked", status_lines(None), ["  [ -- ] LDP -- not checked"])
    chk("empty -> no session data", status_lines("")[0].startswith("  [ -- ] LDP -- no session data"), True)

    # last bounce: newest of several, split on ******, first ts per record.
    evt = ("LDP-Session-Down   2026-01-06,00:45:43   peerRouterID:10.111.132.255   2026-01-06,00:45:43\n"
           "******\n"
           "LDP-Session-Up     2026-01-06,00:52:10   peerRouterID:10.111.132.255\n"
           "******\n"
           "LDP-Session-Down   2025-12-30,11:02:00   peerRouterID:10.111.132.255\n")
    b = last_ldp_bounce(evt)
    chk("newest bounce picked", (b["name"], b["ts"]), ("LDP-Session-Up", "2026-01-06,00:52:10"))
    chk("no bounce in empty", last_ldp_bounce(""), None)
    chk("bounce annotated on status line",
        "(last LDP-Session-Up 2026-01-06,00:52:10 UTC)" in status_lines(sess, event_raw=evt)[0], True)

    print()
    if fails:
        print("FAILED:")
        for f in fails:
            print("  " + f)
        return False
    print("All ldp_check self-tests passed.")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if self_test() else 1)
