#!/usr/bin/env python3
"""
health_check.py -- standalone NPT-1250 high-level health check.

A quick overall look at a 1250: interfaces, fiber transceivers (ts2/ts5),
routing adjacencies, DS1 physical status across the CES slots, and alarms/events.
No parsing or pass/fail logic -- it just runs a fixed set of read-only commands
and dumps the output in the standard sectioned format.

Runs standalone (prompts for its own inputs) OR via launcher.py (params dict).
READ-ONLY: 'show' commands only (with session-only '| no-more' paging).

Built by P. Capps.
"""

import os
import re
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

# Hardcoded per spec (matches the fiber + DS1 PMs tools).
FIBER_SLOTS = ["ts2", "ts5"]           # GE transceiver slots
CES_SLOTS   = ["ts1", "ts3", "ts6", "ts8"]

# Ordered command list. Each entry: (section title, command).
# `| no-more` is appended at run time (session-only paging suppression).
def _build_command_list():
    cmds = [
        ("Software Version", "show version"),
        ("Interfaces", "show interfaces"),
    ]
    for slot in FIBER_SLOTS:
        cmds.append(("Transceiver -- {} port1".format(slot),
                     "show chassis transceiver information slot {} port1".format(slot)))
        cmds.append(("Transceiver Laser -- {} port1".format(slot),
                     "show chassis transceiver information slot {} port1 | grep laser".format(slot)))
    cmds += [
        ("LDP Sessions", "show ldp session"),
        ("OSPF Neighbors", "show ospf neighbor"),
    ]
    for slot in CES_SLOTS:
        cmds.append(("DS1 Physical Status -- {}".format(slot),
                     "show interfaces physical status slot {}".format(slot)))
    cmds += [
        ("NE Alarms", "show ne alarms"),
        ("Chassis Alarms", "show chassis alarms"),
        ("NE Events", "show ne events"),
        ("NE Events Unreported", "show ne events unreported"),
    ]
    return cmds


# Launcher menu metadata (read by launcher.py auto-discovery).
TOOL_INFO = {
    'name': '1250 Health Check',
    'ran_from': '1250',
    'desc': 'High-level 1250 health: version, interfaces, fiber transceivers, LDP/OSPF, DS1 status across CES slots, alarms and events.',
    'requires': '1250 IP, creds',
    'order': 50,
}


def collect_inputs(session=None):
    """Prompt for inputs; return params dict (or None to abort)."""
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
        "ip_1250": ip,
        "username": user,
        "password": pw,
        "jira_ticket": jira_ticket,
        "open_when_done": open_when_done,
    }


def run(params):
    """Execute the health check given a params dict (no prompting)."""
    if _DEP_ERROR:
        print("[!] {} -- tool unavailable.".format(_DEP_ERROR))
        return

    ip = params["ip_1250"]
    user = params["username"]
    pw   = params["password"]
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
    lines.append("NPT-1250 HEALTH CHECK")
    lines.append("Host: {}  IP: {}".format(host, ip))
    lines.append("=" * 64)

    # Sections too verbose for the console -- written to the .txt/JIRA only.
    # (NE event logs can be hundreds of lines; user reads those in the file.)
    CONSOLE_SUPPRESS = {"NE Events", "NE Events Unreported"}
    console_lines = list(lines)   # console gets a copy sans the suppressed blocks

    raw_by_title = {}   # section title -> raw output, for the JIRA summary
    for title, cmd in _build_command_list():
        print("  collecting: {}".format(title))
        raw = c.run(cmd + " | no-more")
        raw_by_title[title] = raw
        block = ["", "=" * 64, "  {}".format(title), "  CMD: {}".format(cmd), "=" * 64]
        body = [ln.rstrip() for ln in raw.splitlines() if ln.rstrip()]
        if body:
            block.extend("  {}".format(ln) for ln in body)
        else:
            block.append("  (no output)")
        lines.extend(block)
        if title in CONSOLE_SUPPRESS:
            # console: show the header + a pointer, not the full dump
            console_lines.extend(["", "=" * 64, "  {}".format(title),
                                  "  CMD: {}".format(cmd), "=" * 64,
                                  "  (omitted from console -- see .txt / JIRA)"])
        else:
            console_lines.extend(block)

    c.close()

    # Echo the report to the console (verbose event logs suppressed above).
    print("")
    print("\n".join(console_lines))

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("health_check_{}_{}.txt".format(ts, host))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = "NPT-1250 health check -- {} ({}), run {}.".format(
            host, ip, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Findings summary -- alarm counts + protocol/DS1 state at a glance.
        def _rows(title):
            return [l.strip() for l in raw_by_title.get(title, "").splitlines()
                    if l.strip()]
        sum_lines = []
        for t in ("NE Alarms", "Chassis Alarms"):
            rows = _rows(t)
            if rows:
                sum_lines.append("{}: {} row(s)".format(t, len(rows)))
                sum_lines.extend("  {}".format(r[:70]) for r in rows[:5])
                if len(rows) > 5:
                    sum_lines.append("  ... +{} more (see attachment)".format(len(rows) - 5))
            else:
                sum_lines.append("{}: none".format(t))
        ospf_rows = _rows("OSPF Neighbors")
        ldp_rows  = _rows("LDP Sessions")
        sum_lines.append("OSPF neighbors: {} line(s) | LDP sessions: {} line(s) "
                         "(details in attachment)".format(len(ospf_rows), len(ldp_rows)))
        down_ds1 = []
        for t, raw in raw_by_title.items():
            if t.startswith("DS1 Physical Status"):
                down_ds1 += [l.strip() for l in raw.splitlines()
                             if re.search(r'\b(LOS|LOF|AIS|DOWN)\b', l)]
        if down_ds1:
            sum_lines.append("DS1s not clean: {}".format(len(down_ds1)))
            sum_lines.extend("  {}".format(d[:70]) for d in down_ds1[:8])
            if len(down_ds1) > 8:
                sum_lines.append("  ... +{} more (see attachment)".format(len(down_ds1) - 8))
        else:
            sum_lines.append("DS1s not clean: none flagged (LOS/LOF/AIS/DOWN)")
        ok, msg = post_report(
            jira_ticket, intro, "NPT-1250 Health Check -- {}".format(host),
            "\n".join(lines), attach_path=out_path,
            summary_text="\n".join(sum_lines))
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open: {}".format(e))

    return out_path


def main():
    print("=" * 64)
    print("  NPT-1250 HEALTH CHECK (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
