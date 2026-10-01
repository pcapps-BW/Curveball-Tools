#!/usr/bin/env python3
"""
fiber_util.py -- standalone NPT-1250 fiber (GE) utilization check.

Checks the fiber uplink utilization on a 1250 by reading the 24hour physical PM
bps counters for the GE ports (ge-ts2/1 and ge-ts5/1). One, the other, or both
may be present; a port that returns no data is skipped.

Runs standalone (prompts for its own inputs) OR via launcher.py (params dict).
READ-ONLY: 'show' commands only (with session-only '| no-more' paging).

For each present port it reports the six device counters --
  IN/OUT x Maximum/Average/Current Rate(bps) --
raw bps with a human-readable Mbps annotation, e.g. "18620415  (18.62 Mbps)".
No %-utilization is computed (circuit speeds vary).

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

# Fiber (GE) ports on the 1250. Hardcoded per spec: it will be one, the other,
# or both -- never any other slot for now.
FIBER_PORTS = ["ge-ts2/1", "ge-ts5/1"]

# bps counter lines returned by "... pm 24hour <port> | grep bps"
_BPS_RE = re.compile(r'^\s*((?:IN|OUT)\s+\w+\s+Rate\(bps\))\s*:\s*([0-9]+)\s*$')


def _mbps(bps):
    return "{:.2f} Mbps".format(int(bps) / 1_000_000.0)


# Launcher menu metadata (read by launcher.py auto-discovery).
TOOL_INFO = {
    'name': 'Fiber Utilization',
    'ran_from': '1250',
    'desc': 'Reports fiber (GE) uplink utilization on a 1250 -- IN/OUT Max/Avg/Current bps for ge-ts2/1 and ge-ts5/1.',
    'requires': '1250 IP, creds',
    'order': 40,
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
    """Execute the fiber utilization check given a params dict (no prompting)."""
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
    lines.append("NPT-1250 FIBER UTILIZATION")
    lines.append("Host: {}  IP: {}".format(host, ip))
    lines.append("=" * 64)

    found_any = False
    all_rates = {}   # port -> {label: bps} for the JIRA summary
    for port in FIBER_PORTS:
        cmd = "show interfaces physical pm 24hour {} | grep bps".format(port)
        print("  collecting: {}".format(port))
        raw = c.run(cmd + " | no-more")

        # parse the bps counter lines
        rates = []
        for ln in raw.splitlines():
            m = _BPS_RE.match(ln)
            if m:
                rates.append((m.group(1).strip(), m.group(2)))

        lines.append("")
        lines.append("=" * 64)
        lines.append("  -- {} --".format(port))
        lines.append("=" * 64)
        lines.append("  CMD: {}".format(cmd))

        if not rates:
            lines.append("    (no data -- port not present or no bps counters)")
            print("    {} -> not present / no data".format(port))
            continue

        found_any = True
        # normalize whitespace in labels so summary lookups are exact
        all_rates[port] = {" ".join(label.split()): bps for label, bps in rates}
        for label, bps in rates:
            # label e.g. "IN Maximum Rate(bps)"
            lines.append("    {:<32} {:>12}  ({})".format(label, bps, _mbps(bps)))

    if not found_any:
        lines.append("")
        lines.append("  NOTE: No fiber bps data on either {} -- check the ports.".format(
            " or ".join(FIBER_PORTS)))

    c.close()

    # Echo the full report to the console (in addition to the .txt).
    print("")
    print("\n".join(lines))

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("fiber_util_{}_{}.txt".format(ts, host))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = "Fiber utilization -- {}, run {}.".format(
            host, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Findings summary -- per-port current/max Mbps, visible on the ticket.
        sum_lines = []
        for port in FIBER_PORTS:
            r = all_rates.get(port)
            if not r:
                sum_lines.append("{}  (no data -- port not present)".format(port))
                continue
            def _g(d, key):
                v = d.get(key)
                return _mbps(v) if v else "?"
            sum_lines.append("{}  IN cur {} / max {}  |  OUT cur {} / max {}".format(
                port,
                _g(r, "IN Current Rate(bps)"), _g(r, "IN Maximum Rate(bps)"),
                _g(r, "OUT Current Rate(bps)"), _g(r, "OUT Maximum Rate(bps)")))
        ok, msg = post_report(
            jira_ticket, intro, "Fiber Utilization -- {}".format(host), "\n".join(lines),
            attach_path=out_path, summary_text="\n".join(sum_lines))
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open: {}".format(e))

    return out_path


def main():
    print("=" * 64)
    print("  NPT-1250 FIBER UTILIZATION (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
