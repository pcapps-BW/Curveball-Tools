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
import datetime

from gsx_client import GSXClient, find_plink
from device_base import CredentialError, prompt_ip
from jira_client import post_report, valid_ticket

TOOL_INFO = {
    'name':     'GSX Perfmon (96 Intervals)',
    'ran_from': 'gsx',
    'desc':     'STATUS, STATISTICS, PERFMON DAY/THRESHOLDS, and all 96 '
                'fifteen-minute intervals for a single T1 on the GSX. '
                'For stress-test duration planning.',
    'requires': 'GSX IP, T1 assignment (e.g. T1-1-7-2-2 or 7-2-2), GSX creds',
    'order':    55,
}

INTERVAL_COUNT = 96
SEP  = "=" * 64
SEP2 = "-" * 58


# ── helpers ──────────────────────────────────────────────────────────────────

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

    ts_label = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        SEP,
        "  GSX PERFMON -- ALL {} FIFTEEN-MINUTE INTERVALS".format(INTERVAL_COUNT),
        SEP,
        "  T1    : {}".format(t1_id),
        "  GSX IP: {}".format(ip),
        "  Run   : {}".format(ts_label),
        SEP,
    ]

    # ── Standard commands ─────────────────────────────────────────────────────
    standard = [
        ("STATUS",              "SHOW T1 {} STATUS".format(t1_id)),
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
    print("  collecting: PERFMON intervals 1-{} ...".format(INTERVAL_COUNT))
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

    c.close()
    print("  done -- {} intervals collected.".format(INTERVAL_COUNT))

    # ── Write file ────────────────────────────────────────────────────────────
    ts_file  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id  = t1_id.replace("-", "_")
    out_path = r"C:\Claude\gsx_perfmon_{}_{}.txt".format(safe_id, ts_file)
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
            "GSX perfmon (all {} fifteen-minute intervals) -- {} on {}, run {}.".format(
                INTERVAL_COUNT, t1_id, ip, ts_label))
        # Findings summary -- port state + current-day error counters at a
        # glance (the 96 intervals live in the attachment).
        sum_lines = ["{} on {}".format(t1_id, ip)]
        status_raw = std_raw.get("STATUS", "")
        for key in ("Operational Status", "Alarm Status", "Admin"):
            for ln in status_raw.splitlines():
                if key.lower() in ln.lower() and ln.strip():
                    sum_lines.append(ln.strip())
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

def main():
    print(SEP)
    print("  GSX PERFMON -- 96 FIFTEEN-MINUTE INTERVALS (standalone)")
    print(SEP)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
