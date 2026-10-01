#!/usr/bin/env python3
"""
gsx_perfmon.py -- GSX T1 deep perfmon: STATUS, STATISTICS, DAY/THRESHOLDS,
and all 96 fifteen-minute PERFMON intervals.

Useful for stress-test planning: shows how often and when errors occur so
that test durations reflect real failure patterns rather than guesses.

Runs standalone (prompts for its own inputs) OR via launcher.py (params dict).
READ-ONLY: 'SHOW' commands only.

Built by P. Capps.
"""

import os
import re
import sys
import datetime

import platform_paths
from gsx_client import GSXClient, find_plink
from device_base import CredentialError, prompt_ip
from jira_client import post_report, valid_ticket

TOOL_INFO = {
    'name':     'GSX Perfmon (96 Intervals)',
    'ran_from': 'gsx',
    'desc':     'T3 and T1 STATUS, STATISTICS, PERFMON DAY/THRESHOLDS, and all 96 '
                'fifteen-minute intervals for a single T1 (and its parent T3) on '
                'the GSX. For stress-test duration planning.',
    'requires': 'GSX IP, T1 assignment (e.g. T1-1-7-2-2 or 7-2-2), GSX creds',
    'order':    55,
}

# ── 24-hour clean check: skip the 96 interval pulls ──────────────────────────
#
# pcapps 2026-10-01: if the 24-hour bin is clean there is no point pulling 96
# intervals of zeros -- 96 round trips per facility.
#
# FORMAT CONFIRMED LIVE on ATLGSX02, T1-1-7-1-16, 2026-10-01. Counters are
# spelled out in full, one per line, with the error count in the first column
# and three TCA flags after it:
#
#     Near End Path Parameters          Errors    TCA   TCA   TCA
#     Code Violations:                  8         FALSE FALSE FALSE
#     Unavailable Seconds:              0         FALSE FALSE FALSE
#     Failure Count:                    0         -     -     -
#
# and Line-parameter rows can be "N/A" rather than a number:
#
#     Code Violations:                  N/A       N/A   N/A   N/A
#
# The first version of this check looked for abbreviations (CV / ES / SES /
# UAS). None of those appear anywhere in the real output, so it matched zero
# counters and the skip could never have fired -- it would have sat in the
# "could not determine" branch forever. Dead code that looks like a feature.
# Hence the explicit full-text names below, and the self-test against this
# captured sample.
#
# STILL FAILS TOWARD WORK: the skip happens only when >= MIN_COUNTERS numeric
# counters are found AND every one is zero. N/A rows are ignored (not read as
# zero -- a parameter the device does not measure is not evidence of health).
# Unparsed output, a command error, or all-N/A pulls every interval as before
# and says why.

_PM_COUNTER_NAMES = (
    "Code Violations",
    "Errored Seconds",
    "Severely Errored Seconds",
    "Severely Errored Frame Seconds",
    "SEF/AIS Seconds",
    "Controlled Slip Seconds",
    "Unavailable Seconds",
    "Failure Count",
)
# Anchored to start-of-line so "Elapsed Time: 4574" and the Node/Date header
# cannot be mistaken for a counter.
_PM_LINE_RE = re.compile(
    r'^\s*(' + "|".join(re.escape(n) for n in _PM_COUNTER_NAMES) +
    r')\s*:\s*(N/A|\d+)\b', re.I | re.M)
MIN_COUNTERS = 3        # fewer than this and we have not really read the bin


def day_bin_is_clean(raw):
    """
    (is_clean, reason) for a PERFMON CURRENT DAY output.

    True only when >= MIN_COUNTERS numeric counters were found and all are
    zero. Every other case returns False with a reason, including output this
    function does not understand.

    Covers all four sections (Near/Far End, Line/Path) -- a far-end fault is
    still a fault, and the ATLGSX02 sample is dirty only on the far end.
    """
    if not raw or not raw.strip():
        return False, "no CURRENT DAY output"
    if "[command error" in raw.lower():
        return False, "CURRENT DAY command errored"

    found = _PM_LINE_RE.findall(raw)
    if not found:
        return False, ("CURRENT DAY output not recognised -- no PM counters "
                       "parsed; pulling all intervals")

    numeric = [(n, v) for n, v in found if v.upper() != "N/A"]
    if len(numeric) < MIN_COUNTERS:
        return False, ("only {} numeric counter(s) ({} were N/A); pulling all "
                       "intervals".format(len(numeric),
                                          len(found) - len(numeric)))

    nonzero = [(n, v) for n, v in numeric if int(v) != 0]
    if nonzero:
        return False, "{} non-zero of {} ({})".format(
            len(nonzero), len(numeric),
            ", ".join("{}={}".format(n, v) for n, v in nonzero[:4]))
    return True, "all {} counters zero".format(len(numeric))


INTERVAL_COUNT    = 96
T3_INTERVAL_COUNT = 96
SEP  = "=" * 64
SEP2 = "-" * 58


# ── helpers ──────────────────────────────────────────────────────────────────

def _t3_id_from_t1(t1_id):
    """
    Derive the parent T3 ID from a canonical T1 ID.
    'T1-1-7-2-2' -> 'T3-1-7-2'  (drop the trailing T1 index, swap prefix)
    Returns None if the T1 ID doesn't match the expected pattern.
    """
    m = re.match(r'^T1-1-(\d+-\d+)-\d+$', t1_id)
    return "T3-1-{}".format(m.group(1)) if m else None


def _parse_t1_id(raw):
    """
    Accept 'T1-1-7-2-2' or '7-2-2' (slot-port-t1) and return the canonical
    'T1-1-7-2-2'.  Returns None if the string doesn't match either pattern.
    Case-insensitive (user may type lowercase t1-...).
    """
    raw = raw.strip().upper()
    if re.match(r'^T1-1-\d+-\d+-\d+$', raw):
        return raw
    if re.match(r'^\d+-\d+-\d+$', raw):
        return "T1-1-{}".format(raw)
    return None


# ── Launcher interface ────────────────────────────────────────────────────────

def collect_inputs(session=None):
    """Prompt for all inputs; return params dict (or None to abort)."""
    import getpass
    session = session if session is not None else {}

    ip = prompt_ip("GSX IP", default=session.get("gsx_ip"))
    if not ip:
        return None

    while True:
        raw = input("T1 assignment (e.g. T1-1-7-2-2 or 7-2-2): ").strip()
        t1_id = _parse_t1_id(raw)
        if t1_id:
            break
        print("  Invalid format. Expected T1-1-{slot}-{port}-{t1} or {slot}-{port}-{t1}.")

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

    # GSX creds: reuse session values if available, else prompt.
    gsx_user = session.get("gsx_user")
    gsx_pass = session.get("gsx_pass")
    if not (gsx_user and gsx_pass):
        gsx_user = input("GSX username: ").strip()
        gsx_pass = getpass.getpass("GSX password: ")
    if not (gsx_user and gsx_pass):
        print("Missing credentials.")
        return None

    return {
        "gsx_ip":        ip,
        "t1_id":         t1_id,
        "gsx_user":      gsx_user,   # launcher harvests -> session["gsx_user"]
        "gsx_pass":      gsx_pass,   # launcher harvests -> session["gsx_pass"]
        "jira_ticket":   jira_ticket,
        "open_when_done": open_when_done,
    }


# ── Main run ─────────────────────────────────────────────────────────────────

def run(params):
    """Execute the tool given a params dict (no prompting)."""
    plink_path = find_plink()
    if not plink_path:
        print("[!] plink not found. Install PuTTY or add plink to PATH.")
        return None

    ip             = params["gsx_ip"]
    t1_id          = params["t1_id"]
    gsx_user       = params.get("gsx_user") or params.get("username", "")
    gsx_pass       = params.get("gsx_pass") or params.get("password", "")
    jira_ticket    = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", True)

    c = GSXClient(ip, gsx_user, gsx_pass, plink_path=plink_path)
    print("\n[connecting] {}".format(ip))
    try:
        c.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return None

    try:
        # try/finally added 2026-10-01: the body below is long, and any
        # exception in it used to return without closing the session.
        # Harmless for a one-shot CLI run (process exits, socket drops) but
        # NOT in the long-lived Flask app, where the transport and its
        # reader thread would survive until the device timed the session out.
        ts_label = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        t3_id    = _t3_id_from_t1(t1_id)

        lines = [
            SEP,
            "  GSX PERFMON -- ALL {} FIFTEEN-MINUTE INTERVALS".format(INTERVAL_COUNT),
            SEP,
            "  T3    : {}".format(t3_id or "n/a"),
            "  T1    : {}".format(t1_id),
            "  GSX IP: {}".format(ip),
            "  Run   : {}".format(ts_label),
            SEP,
        ]

        # ── T3 standard commands ──────────────────────────────────────────────────
        t3_raw = {}
        if t3_id:
            t3_standard = [
                ("T3 STATUS",              "SHOW T3 {} STATUS".format(t3_id)),
                ("T3 ADMIN",               "SHOW T3 {} ADMIN".format(t3_id)),
                ("T3 STATISTICS",          "SHOW T3 {} STATISTICS".format(t3_id)),
                ("T3 PERFMON CURRENT DAY", "SHOW T3 {} PERFMON CURRENT DAY".format(t3_id)),
                ("T3 PERFMON THRESHOLDS",  "SHOW T3 {} PERFMON THRESHOLDS".format(t3_id)),
            ]
            for label, cmd in t3_standard:
                print("  collecting: {}".format(label))
                try:
                    out = c.run_command(cmd)
                except Exception as e:
                    out = "[command error: {}]".format(e)
                t3_raw[label] = out
                lines += [
                    "",
                    SEP,
                    "  {}".format(label),
                    "  CMD: {}".format(cmd),
                    SEP,
                ]
                for ln in out.splitlines():
                    lines.append("  {}".format(ln))

            # ── T3 96 fifteen-minute intervals ────────────────────────────────────
            t3_clean, t3_why = day_bin_is_clean(
                t3_raw.get("T3 PERFMON CURRENT DAY", ""))
            if t3_clean:
                print("  T3 24h bin clean -- skipping {} interval pulls ({})"
                      .format(T3_INTERVAL_COUNT, t3_why))
                lines += [
                    "",
                    SEP,
                    "  T3 PERFMON -- 15-MINUTE INTERVALS SKIPPED",
                    "  24-hour bin is clean ({}), so all {} fifteen-minute".format(
                        t3_why, T3_INTERVAL_COUNT),
                    "  intervals are zero by definition and were not pulled.",
                    SEP,
                ]
            else:
                print("  collecting: T3 PERFMON intervals 1-{} ... ({})".format(
                    T3_INTERVAL_COUNT, t3_why))
                lines += [
                    "",
                    SEP,
                    "  T3 PERFMON -- ALL {} FIFTEEN-MINUTE INTERVALS".format(T3_INTERVAL_COUNT),
                    "  (interval 1 = most recent; interval {} = oldest ~24 hours ago)".format(T3_INTERVAL_COUNT),
                    SEP,
                ]
                for n in range(1, T3_INTERVAL_COUNT + 1):
                    if n == 1 or n % 16 == 0:
                        print("    T3 interval {}/{}...".format(n, T3_INTERVAL_COUNT))
                    cmd = "SHOW T3 {} PERFMON INTERVAL {} FIFTEENMIN".format(t3_id, n)
                    try:
                        out = c.run_command(cmd)
                    except Exception as e:
                        out = "[command error: {}]".format(e)
                    lines += [
                        "",
                        SEP2,
                        "  INTERVAL {}  ({} min ago)".format(n, n * 15),
                        "  CMD: {}".format(cmd),
                        SEP2,
                    ]
                    for ln in out.splitlines():
                        lines.append("  {}".format(ln))
                print("  done -- {} T3 intervals collected.".format(T3_INTERVAL_COUNT))

        # ── T1 Standard commands ──────────────────────────────────────────────────
        standard = [
            ("STATUS",              "SHOW T1 {} STATUS".format(t1_id)),
            # ADMIN added 2026-10-01 (pcapps). The T3 block above has had it
            # all along; the T1 block never did. Shows the CONFIGURED state
            # (framing, line coding, loopback, admin up/down) as opposed to
            # the operational state STATUS reports -- the two disagreeing is
            # itself a finding.
            ("ADMIN",               "SHOW T1 {} ADMIN".format(t1_id)),
            ("STATISTICS",          "SHOW T1 {} STATISTICS".format(t1_id)),
            ("PERFMON CURRENT DAY", "SHOW T1 {} PERFMON CURRENT DAY".format(t1_id)),
            ("PERFMON THRESHOLDS",  "SHOW T1 {} PERFMON THRESHOLDS".format(t1_id)),
        ]

        std_raw = {}   # label -> raw output, for the JIRA summary
        for label, cmd in standard:
            print("  collecting: {}".format(label))
            try:
                out = c.run_command(cmd)
            except Exception as e:
                out = "[command error: {}]".format(e)
            std_raw[label] = out
            lines += [
                "",
                SEP,
                "  {}".format(label),
                "  CMD: {}".format(cmd),
                SEP,
            ]
            for ln in out.splitlines():
                lines.append("  {}".format(ln))

        # ── 96 fifteen-minute intervals ───────────────────────────────────────────
        t1_clean, t1_why = day_bin_is_clean(std_raw.get("PERFMON CURRENT DAY", ""))
        if t1_clean:
            print("  T1 24h bin clean -- skipping {} interval pulls ({})".format(
                INTERVAL_COUNT, t1_why))
            lines += [
                "",
                SEP,
                "  T1 PERFMON -- 15-MINUTE INTERVALS SKIPPED",
                "  24-hour bin is clean ({}), so all {} fifteen-minute".format(
                    t1_why, INTERVAL_COUNT),
                "  intervals are zero by definition and were not pulled.",
                SEP,
            ]
        else:
            print("  collecting: PERFMON intervals 1-{} ... ({})".format(
                INTERVAL_COUNT, t1_why))
            lines += [
                "",
                SEP,
                "  PERFMON -- ALL {} FIFTEEN-MINUTE INTERVALS".format(INTERVAL_COUNT),
                "  (interval 1 = most recent; interval {} = oldest ~24 hours ago)".format(INTERVAL_COUNT),
                SEP,
            ]

            for n in range(1, INTERVAL_COUNT + 1):
                if n == 1 or n % 16 == 0:
                    print("    interval {}/{}...".format(n, INTERVAL_COUNT))
                cmd = "SHOW T1 {} PERFMON INTERVAL {} FIFTEENMIN".format(t1_id, n)
                try:
                    out = c.run_command(cmd)
                except Exception as e:
                    out = "[command error: {}]".format(e)
                lines += [
                    "",
                    SEP2,
                    "  INTERVAL {}  ({} min ago)".format(n, n * 15),
                    "  CMD: {}".format(cmd),
                    SEP2,
                ]
                for ln in out.splitlines():
                    lines.append("  {}".format(ln))

    finally:
        c.close()
    if not t1_clean:
        print("  done -- {} intervals collected.".format(INTERVAL_COUNT))

    # ── Write file ────────────────────────────────────────────────────────────
    ts_file  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id  = t1_id.replace("-", "_")
    out_path = platform_paths.path("gsx_perfmon_{}_{}.txt".format(safe_id, ts_file))
    body     = "\n".join(lines)

    try:
        with open(out_path, "w") as f:
            f.write(body)
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    # ── JIRA ─────────────────────────────────────────────────────────────────
    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = (
            "GSX perfmon (all {} fifteen-minute intervals) -- {} / {} on {}, run {}.".format(
                INTERVAL_COUNT, t3_id or "n/a", t1_id, ip, ts_label))
        # Findings summary -- T3 state, T1 port state + current-day error
        # counters at a glance (the 96 intervals live in the attachment).
        sum_lines = ["{} / {} on {}".format(t3_id or "n/a", t1_id, ip)]
        # T3 status block
        t3_status_raw = t3_raw.get("T3 STATUS", "")
        for key in ("Operational Status", "Alarm Status"):
            for ln in t3_status_raw.splitlines():
                if key.lower() in ln.lower() and ln.strip():
                    sum_lines.append("T3 {}: {}".format(key, ln.split(":")[-1].strip()))
                    break
        # T1 status block
        status_raw = std_raw.get("STATUS", "")
        for key in ("Operational Status", "Alarm Status", "Admin"):
            for ln in status_raw.splitlines():
                if key.lower() in ln.lower() and ln.strip():
                    sum_lines.append("T1 {}: {}".format(key, ln.split(":")[-1].strip()))
                    break
        day_raw = std_raw.get("PERFMON CURRENT DAY", "")
        nonzero = []
        for ln in day_raw.splitlines():
            m = re.match(r'\s*([A-Za-z/ ]+ Seconds|Code Violations)\s*:\s*(\d+)', ln)
            if m and int(m.group(2)) > 0:
                nonzero.append("{}: {}".format(m.group(1).strip(), m.group(2)))
        if nonzero:
            sum_lines.append("Current-day errors: " + "; ".join(nonzero[:6]))
        else:
            sum_lines.append("Current-day errors: none")
        ok, msg = post_report(
            jira_ticket, intro,
            "GSX Perfmon {} Intervals -- {}".format(INTERVAL_COUNT, t1_id),
            body, attach_path=out_path, summary_text="\n".join(sum_lines))
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open: {}".format(e))

    return out_path


# ── Standalone entry point ────────────────────────────────────────────────────


# ── Self-test: the clean-check against real captured output ─────────────────
#
# Verbatim `SHOW T1 T1-1-7-1-16 PERFMON CURRENT DAY` from ATLGSX02,
# 2026-10-01. This circuit is DIRTY (far-end UAS 3553), which makes it the
# right fixture: it proves the check does NOT skip on a faulty facility, and
# the zeroed variant proves it DOES skip on a clean one.
_DAY_SAMPLE = """Node: ATLGSX02                                 Date: 2026/10/01 12:09:02  GMT
                                               Zone: GMT

---------------------------------------------------------------------
Shelf: 1  Slot: 7   Port: 1   DS1: 16
---------------------------------------------------------------------
T1 Current Day Performance Monitoring Statistics
---------------------------------------------------------------------
Elapsed Time: 4574
---------------------------------------------------------------------
                                            Minor   Major   Critical
Near End Line Parameters          Errors    TCA     TCA     TCA
---------------------------------------------------------------------
Code Violations:                  N/A       N/A     N/A     N/A
Errored Seconds:                  N/A       N/A     N/A     N/A
Severely Errored Seconds:         N/A       N/A     N/A     N/A
---------------------------------------------------------------------
                                            Minor   Major   Critical
Near End Path Parameters          Errors    TCA     TCA     TCA
---------------------------------------------------------------------
Code Violations:                  8         FALSE   FALSE   FALSE
Errored Seconds:                  9         FALSE   FALSE   FALSE
Severely Errored Seconds:         7         FALSE   FALSE   FALSE
SEF/AIS Seconds:                  7         FALSE   FALSE   FALSE
Controlled Slip Seconds:          2         FALSE   FALSE   FALSE
Unavailable Seconds:              0         FALSE   FALSE   FALSE
Failure Count:                    0         -       -       -
---------------------------------------------------------------------
                                            Minor   Major   Critical
Far End Line Paramaters           Errors    TCA     TCA     TCA
---------------------------------------------------------------------
Errored Seconds:                  N/A       N/A     N/A     N/A
---------------------------------------------------------------------
                                            Minor   Major   Critical
Far End Path Parameters           Errors    TCA     TCA     TCA
---------------------------------------------------------------------
Code Violations:                  5         FALSE   FALSE   FALSE
Errored Seconds:                  10        FALSE   FALSE   FALSE
Severely Errored Seconds:         9         FALSE   FALSE   FALSE
Severely Errored Frame Seconds:   1         FALSE   FALSE   FALSE
Controlled Slip Seconds:          1         FALSE   FALSE   FALSE
Unavailable Seconds:              3553      TRUE    TRUE    TRUE
Failure Count:                    4         -       -       -
"""


def self_test(verbose=True):
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    ok, why = day_bin_is_clean(_DAY_SAMPLE)
    chk("dirty real sample is NOT clean", ok, False)
    chk("names a non-zero counter", "Unavailable Seconds" in why or
        "Code Violations" in why, True)

    # 'Elapsed Time: 4574' must never be read as a PM counter.
    names = [n for n, _ in _PM_LINE_RE.findall(_DAY_SAMPLE)]
    chk("Elapsed Time not a counter",
        any("elapsed" in n.lower() for n in names), False)
    chk("counters parsed from all 4 sections", len(names), 18)

    # N/A rows are ignored, not treated as zero.
    na_only = ("Code Violations:  N/A  N/A\n"
               "Errored Seconds:  N/A  N/A\n"
               "Severely Errored Seconds:  N/A  N/A\n")
    ok, why = day_bin_is_clean(na_only)
    chk("all-N/A is not clean", ok, False)
    chk("  and says why", "N/A" in why, True)

    # Same real format, every numeric counter zeroed -> must skip.
    clean = re.sub(r'(:\s+)(\d+)(\s)',
                   lambda m: m.group(1) + "0" + m.group(3), _DAY_SAMPLE)
    clean = clean.replace("Elapsed Time: 0", "Elapsed Time: 4574")
    ok, why = day_bin_is_clean(clean)
    chk("zeroed real sample IS clean", ok, True)

    chk("empty output", day_bin_is_clean("")[0], False)
    chk("command error", day_bin_is_clean("[command error: x]")[0], False)
    chk("unrecognised output",
        day_bin_is_clean("Node: X\nElapsed Time: 4574\nheading")[0], False)

    if verbose:
        if fails:
            print("\nFAILED:")
            for f in fails:
                print("  !! " + f)
        else:
            print("\nAll gsx_perfmon self-tests passed.")
    return not fails


def main():
    print(SEP)
    print("  GSX PERFMON -- 96 FIFTEEN-MINUTE INTERVALS (standalone)")
    print(SEP)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    main()
