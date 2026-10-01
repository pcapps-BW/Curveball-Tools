#!/usr/bin/env python3
"""
ospf_events.py -- OSPF up/down event timestamps + fiber light levels on NPT-1250.

Pulls 'show ne events unreported' and filters in Python for OSPF-related state
change events (down and full/up).  Also checks fiber transceiver light levels
for ts2 and ts5.

Runs standalone (prompts for its own inputs) OR via launcher.py (params dict).
READ-ONLY: 'show' commands only.

Built by P. Capps.
"""

import re
import os
import datetime

import platform_paths

try:
    import paramiko  # noqa: F401
    _DEP_ERROR = None
except ImportError as e:
    _DEP_ERROR = "paramiko not installed (pip install paramiko): {}".format(e)

from npt1250_client import NPT1250Client
from device_base import CredentialError, prompt_ip
from jira_client import post_report, valid_ticket

# Fiber transceiver slots to check (same as health_check / fiber_util).
FIBER_SLOTS = ["ts2", "ts5"]

# Regex patterns for OSPF event filtering.
_RE_OSPF  = re.compile(r'ospf', re.IGNORECASE)
_RE_DOWN  = re.compile(r'\bdown\b', re.IGNORECASE)
_RE_FULL  = re.compile(r'\bfull\b', re.IGNORECASE)

# Timestamp extraction for the JIRA summary. Covers the common formats:
# 2026-08-05 / 05-08-2026 / 05/08/2026 followed (possibly with separators)
# by HH:MM or HH:MM:SS.
_RE_TS = re.compile(
    r'(\d{4}-\d{2}-\d{2}|\d{2}[-/]\d{2}[-/]\d{4})\D{0,3}(\d{1,2}:\d{2}(?::\d{2})?)')

# Cap per-category events in the JIRA summary so a flapping circuit
# doesn't blow the comment size (the attachment always has everything).
_SUMMARY_EVENT_CAP = 40

# Tx/Rx optical power extraction for the JIRA summary. Tolerant of the
# field-name variants across transceiver outputs (Rx-laser-power,
# Rx Power, RX Optical Power, ...).
_RE_RX_PWR = re.compile(r'rx[\s-]*(?:laser[\s-]*|optical[\s-]*)?power[^:]*:\s*(-?\d+\.?\d*)',
                        re.IGNORECASE)
_RE_TX_PWR = re.compile(r'tx[\s-]*(?:laser[\s-]*|optical[\s-]*)?power[^:]*:\s*(-?\d+\.?\d*)',
                        re.IGNORECASE)

# Launcher menu metadata.
TOOL_INFO = {
    'name':     'OSPF Events + Fiber Levels',
    'ran_from': '1250',
    'desc':     'OSPF up/down event timestamps from NE unreported events + fiber transceiver light levels (ts2/ts5).',
    'requires': '1250 IP, creds',
    'order':    45,
}


# ── Parsing helpers ──────────────────────────────────────────────────────────

def _parse_ospf_events(raw):
    """
    Filter NE unreported event lines for OSPF state changes.
    Returns dict with keys 'down', 'full', 'other' (all lists of raw lines).
    """
    down, full, other = [], [], []
    for ln in raw.splitlines():
        s = ln.rstrip()
        if not s or not _RE_OSPF.search(s):
            continue
        if _RE_DOWN.search(s):
            down.append(s)
        elif _RE_FULL.search(s):
            full.append(s)
        else:
            other.append(s)
    return {"down": down, "full": full, "other": other}


def _event_line(raw_line, kind):
    """'Date  Time  DOWN/FULL' when a timestamp parses; else the kind plus
    a trimmed copy of the raw line so no event is ever silently dropped."""
    m = _RE_TS.search(raw_line)
    if m:
        return "{}  {}  {}".format(m.group(1), m.group(2), kind)
    return "{}  {}".format(kind, raw_line.strip()[:64])


def _parse_event_dt(raw_line):
    """
    Parse the timestamp out of one raw OSPF event line (same regex used to
    display it, _RE_TS). Returns a datetime, or None if it doesn't match or
    doesn't parse cleanly -- never raises, since an unfamiliar device date
    format should just be excluded from the 7-day count rather than crash
    the whole report.
    """
    m = _RE_TS.search(raw_line)
    if not m:
        return None
    date_part, time_part = m.group(1), m.group(2)
    time_fmt = "%H:%M:%S" if time_part.count(":") == 2 else "%H:%M"
    if "-" in date_part and len(date_part.split("-")[0]) == 4:
        date_fmt = "%Y-%m-%d"                         # ISO: 2026-08-05
    else:
        sep = "-" if "-" in date_part else "/"
        date_fmt = "%d{0}%m{0}%Y".format(sep)         # DD-MM-YYYY / DD/MM/YYYY
    try:
        return datetime.datetime.strptime(
            "{} {}".format(date_part, time_part), "{} {}".format(date_fmt, time_fmt))
    except ValueError:
        return None


def _format_delta(delta):
    """'2d 04h 17m' style duration string from a timedelta."""
    total_s = int(delta.total_seconds())
    if total_s < 0:
        total_s = 0
    d, rem = divmod(total_s, 86400)
    h, rem = divmod(rem, 3600)
    m, _   = divmod(rem, 60)
    parts = []
    if d:
        parts.append("{}d".format(d))
    if d or h:
        parts.append("{}h".format(h))
    parts.append("{}m".format(m))
    return " ".join(parts)


def compute_ospf_uptime(raw_events, now=None):
    """
    Estimate current OSPF adjacency status/uptime from the NE event log.

    Confirmed live on this platform (2026-08-07): 'show ospf interface
    extensive/detail' has no uptime field at all (only a 'Num State
    Transitions' counter), and 'show ospf neighbor' doesn't exist on this
    NPT-1250 image either -- so uptime has to be derived from the last
    logged 'full' (up) event in 'show ne events unreported', same source
    ospf_events.py already parses for bounce counting.

    Returns a dict:
      status  -- "up" | "down" | "unknown"
      since   -- datetime of the relevant transition, or None
      uptime  -- "Xd Xh Xm" string (only when status == "up"), else None
      detail  -- human-readable caveat/explanation string

    Known limitation, stated explicitly rather than silently guessed: the NE
    event log is a short rolling buffer that can fill from unrelated
    high-volume events in well under an hour (confirmed live 2026-08-07). If
    the adjacency has been up longer than the log currently retains, the
    last 'full' event has already rolled off and this returns "unknown"
    instead of estimating -- it will never claim an uptime it can't see
    evidence for in the log.
    """
    now = now or datetime.datetime.now()
    ospf = _parse_ospf_events(raw_events)

    def _latest(events):
        best = None
        for ln in events:
            dt = _parse_event_dt(ln)
            if dt and (best is None or dt > best):
                best = dt
        return best

    last_full = _latest(ospf["full"])
    last_down = _latest(ospf["down"])

    if last_down and (not last_full or last_down > last_full):
        return {
            "status": "down",
            "since":  last_down,
            "uptime": None,
            "detail": "last logged DOWN at {} -- currently down or "
                      "flapping".format(last_down.strftime("%Y-%m-%d %H:%M:%S")),
        }

    if last_full:
        return {
            "status": "up",
            "since":  last_full,
            "uptime": _format_delta(now - last_full),
            "detail": "since last logged FULL event at {} (event-log-derived "
                      "-- see note if this circuit has been stable longer "
                      "than the log currently retains)".format(
                          last_full.strftime("%Y-%m-%d %H:%M:%S")),
        }

    return {
        "status": "unknown",
        "since":  None,
        "uptime": None,
        "detail": "no OSPF full/down event in the currently logged window -- "
                  "device has no direct uptime field on this platform and "
                  "the event log doesn't go back far enough to derive one "
                  "right now",
    }


# Parses 'show ldp session' rows, e.g.:
#   10.111.130.255 :0      false    Operational        1d:10h:37m:54s 35   40
# Peer identifier is "<ip> :<space-id>"; columns after that are Targeted,
# State, Uptime, Timeout, KA. Uptime here is a real device-reported field
# (unlike OSPF) -- no derivation needed, just parse and pass it through.
_RE_LDP_SESSION = re.compile(
    r'^(\d{1,3}(?:\.\d{1,3}){3})\s*:\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s*$',
    re.MULTILINE)


def parse_ldp_sessions(raw):
    """
    Parse 'show ldp session' output into a list of dicts:
      {peer, targeted, state, uptime}
    Returns [] if nothing matches (no sessions, or unfamiliar output format --
    never raises, mirrors the rest of this module's parse-defensively rule).
    """
    sessions = []
    for m in _RE_LDP_SESSION.finditer(raw or ""):
        peer, space_id, targeted, state, uptime, _timeout, _ka = m.groups()
        sessions.append({
            "peer":     "{}:{}".format(peer, space_id),
            "targeted": targeted,
            "state":    state,
            "uptime":   uptime,
        })
    return sessions


def count_recent_down_events(down_events, days=7, now=None):
    """
    How many 'down' events have a parseable timestamp within the last
    `days` days. NOT the same as len(down_events) -- 'show ne events
    unreported' is whatever the device has buffered as unreported, which
    can span more or less than a week depending on the device, not a
    calendar window. Events with no parseable timestamp are excluded from
    the count (still shown in full in the raw report -- just can't be
    time-bounded).
    """
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=days)
    count = 0
    for ln in down_events:
        dt = _parse_event_dt(ln)
        if dt and dt >= cutoff:
            count += 1
    return count


def build_ospf_outage_events(ospf):
    """
    Convert this module's own down/full event lists (from _parse_ospf_events,
    sourced from 'show ne events unreported' on the 1250) into the
    {"dt", "kind", "formatted"} shape output_builders.pair_ospf_outages
    expects -- using this module's own timestamp parser (_parse_event_dt), so
    outage pairing for this tool stays sourced entirely from the 1250's own
    log, with no additional device call and no need to touch the 1800.

    Events with no parseable timestamp are skipped here (same rule
    count_recent_down_events uses) -- they still appear in the raw
    DOWN/FULL sections of the report, just can't be time-ordered for
    pairing.
    """
    events = []
    for raw_line in ospf.get("down", []):
        dt = _parse_event_dt(raw_line)
        if dt:
            events.append({"dt": dt, "kind": "down",
                           "formatted": _event_line(raw_line, "DOWN")})
    for raw_line in ospf.get("full", []):
        dt = _parse_event_dt(raw_line)
        if dt:
            events.append({"dt": dt, "kind": "up",
                           "formatted": _event_line(raw_line, "FULL")})
    return events


def build_ospf_summary(ospf, light_lines=None):
    """
    Findings summary for the JIRA comment (always visible on the ticket):
    per-event 'Date  Time  DOWN/FULL' lines plus a one-line count header,
    capped at _SUMMARY_EVENT_CAP per category (full detail in attachment).
    """
    out = ["OSPF events: {} down / {} full".format(
        len(ospf["down"]), len(ospf["full"]))]
    out.append("OSPF bounces in the last 7 days: {}".format(
        count_recent_down_events(ospf["down"])))
    for kind, key in (("DOWN", "down"), ("FULL", "full")):
        events = ospf[key]
        for ln in events[:_SUMMARY_EVENT_CAP]:
            out.append("  {}".format(_event_line(ln, kind)))
        if len(events) > _SUMMARY_EVENT_CAP:
            out.append("  ... +{} more {} events (see attachment)".format(
                len(events) - _SUMMARY_EVENT_CAP, kind))
    if ospf.get("other"):
        out.append("  ({} other OSPF events without a down/full keyword -- "
                   "see attachment)".format(len(ospf["other"])))
    if light_lines:
        out.append("")
        out.append("Fiber light levels:")
        for ln in light_lines:
            out.append("  {}".format(ln))
    return "\n".join(out)


def _parse_light_levels(raw):
    """
    Extract light-level lines from transceiver output.
    Matches anything containing power, laser, tx, rx, or dBm keywords.
    Falls back to full output if nothing matches.
    """
    levels = []
    for ln in raw.splitlines():
        s = ln.rstrip()
        if re.search(r'(power|laser|tx\b|rx\b|dbm|level)', s, re.IGNORECASE):
            levels.append(s)
    return levels


# ── Launcher interface ───────────────────────────────────────────────────────

def collect_inputs(session=None):
    """Prompt for all inputs; return params dict (or None to abort)."""
    import getpass
    session = session if session is not None else {}

    ip = prompt_ip("1250 IP", default=session.get("ip_1250"))
    if not ip:
        return None

    jira_ticket = None
    if input("Add to JIRA? (y/n) [n]: ").strip().lower() in ("y", "yes"):
        while True:
            jira_ticket = input("JIRA number (NOE-XXXX): ").strip().upper()
            if valid_ticket(jira_ticket):
                break
            if not jira_ticket:
                print("  (blank -- skipping JIRA)")
                jira_ticket = None
                break
            print("  Invalid format. Expected NOE-XXXX (e.g. NOE-1234).")

    open_when_done = input("Open results file when done? (y/n) [y]: ").strip().lower()
    open_when_done = open_when_done in ("", "y", "yes")

    user = session.get("infra_user")
    pw   = session.get("infra_pass")
    if not (user and pw):
        user = input("Username: ").strip()
        pw   = getpass.getpass("Password: ")
    if not (user and pw):
        print("Missing credentials.")
        return None

    return {
        "ip_1250":       ip,
        "username":      user,
        "password":      pw,
        "jira_ticket":   jira_ticket,
        "open_when_done": open_when_done,
    }


def run(params):
    """Execute the tool given a params dict (no prompting)."""
    if _DEP_ERROR:
        print("[!] {} -- tool unavailable.".format(_DEP_ERROR))
        return

    ip             = params["ip_1250"]
    user           = params["username"]
    pw             = params["password"]
    jira_ticket    = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", True)

    c = NPT1250Client(ip, user, pw)
    print("\n[connecting] {}".format(ip))
    try:
        c.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return None
    host = c.hostname
    print("  host: {}".format(host))

    lines = []
    lines.append("OSPF EVENTS + FIBER LIGHT LEVELS")
    lines.append("Host: {}  IP: {}".format(host, ip))
    lines.append("Run:  {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("=" * 64)

    # ── OSPF Events ──────────────────────────────────────────────────────────
    print("  collecting: NE unreported events (filtering for OSPF)")
    raw_events = c.run("show ne events unreported | no-more")
    ospf = _parse_ospf_events(raw_events)

    # Printed here (not just inside build_ospf_summary, which only runs when
    # THIS tool posts to JIRA directly) so a caller running this as a
    # sub-step -- morning_check.py, which always passes jira_ticket=None and
    # just reads the raw report text back -- also gets the 7-day count
    # without having to re-derive it from scratch.
    lines += [
        "",
        "OSPF bounces in the last 7 days: {}".format(
            count_recent_down_events(ospf["down"])),
    ]

    # Down/Up-paired outage summary -- deferred import to avoid a circular
    # import (output_builders imports compute_ospf_uptime/parse_ldp_sessions
    # from this module at its own module-load time).
    from output_builders import pair_ospf_outages, format_ospf_outages
    outages = pair_ospf_outages(build_ospf_outage_events(ospf))
    lines.append("")
    lines.append(format_ospf_outages(outages))

    lines += [
        "",
        "=" * 64,
        "  OSPF DOWN EVENTS",
        "  CMD: show ne events unreported  [python filter: ospf + down]",
        "=" * 64,
    ]
    if ospf["down"]:
        for ln in ospf["down"]:
            lines.append("  {}".format(ln))
    else:
        lines.append("  (none found)")

    lines += [
        "",
        "=" * 64,
        "  OSPF FULL / UP EVENTS",
        "  CMD: show ne events unreported  [python filter: ospf + full]",
        "=" * 64,
    ]
    if ospf["full"]:
        for ln in ospf["full"]:
            lines.append("  {}".format(ln))
    else:
        lines.append("  (none found)")

    if ospf["other"]:
        lines += [
            "",
            "=" * 64,
            "  OTHER OSPF EVENTS (no down/full keyword)",
            "=" * 64,
        ]
        for ln in ospf["other"]:
            lines.append("  {}".format(ln))

    # ── Fiber Light Levels ───────────────────────────────────────────────────
    summary_lights = []   # compact per-slot lines for the JIRA summary
    for slot in FIBER_SLOTS:
        cmd = "show chassis transceiver information slot {} port1 | no-more".format(slot)
        print("  collecting: transceiver {} port1".format(slot))
        raw_xcvr = c.run(cmd)

        lines += [
            "",
            "=" * 64,
            "  FIBER LIGHT LEVELS -- {} port1".format(slot.upper()),
            "  CMD: show chassis transceiver information slot {} port1".format(slot),
            "=" * 64,
        ]

        levels = _parse_light_levels(raw_xcvr)
        if levels:
            for ln in levels:
                lines.append("  {}".format(ln))
            # "ts2  tx -6.4 dBm | rx -6.0 dBm" for the JIRA summary.
            mtx = _RE_TX_PWR.search(raw_xcvr)
            mrx = _RE_RX_PWR.search(raw_xcvr)
            if mtx or mrx:
                summary_lights.append("{}  tx {} dBm | rx {} dBm".format(
                    slot,
                    mtx.group(1) if mtx else "?",
                    mrx.group(1) if mrx else "?"))
            else:
                # Couldn't parse values -- echo the first dBm line raw.
                dbm = [l.strip() for l in levels if "dbm" in l.lower()]
                if dbm:
                    summary_lights.append("{}  {}".format(slot, dbm[0]))
        else:
            # Power-keyword parse found nothing -- dump full output so we don't lose data.
            body = [ln.rstrip() for ln in raw_xcvr.splitlines() if ln.rstrip()]
            if body:
                for ln in body:
                    lines.append("  {}".format(ln))
            else:
                lines.append("  (no data -- slot not present or no transceiver)")

    c.close()

    # Echo to console.
    print("")
    print("\n".join(lines))

    # Write output file.
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("ospf_events_{}_{}.txt".format(ts, host))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    # Optional JIRA post.
    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = "OSPF events + fiber levels -- {} ({}), run {}.".format(
            host, ip, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        ok, msg = post_report(
            jira_ticket, intro,
            "OSPF Events + Fiber Levels -- {}".format(host),
            "\n".join(lines),
            attach_path=out_path,
            summary_text=format_ospf_outages(outages) + "\n\n" +
                         build_ospf_summary(ospf, summary_lights))
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open: {}".format(e))

    return out_path


# ── Standalone entry point ───────────────────────────────────────────────────

def main():
    print("=" * 64)
    print("  OSPF EVENTS + FIBER LIGHT LEVELS (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
