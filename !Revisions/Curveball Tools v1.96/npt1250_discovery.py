#!/usr/bin/env python3
"""
npt1250_discovery.py -- standalone NPT-1250 discovery dump.

Standalone port of the SecureCRT 1250_discovery.py. Connects to a 1250 over SSH
(paramiko, via the shared NPT1250Client machinery) and writes a full read-only
discovery dump: version, chassis, interfaces, protocols, alarms, DS1 physical
status across populated CES slots, MUSE config, PMs on active DS1s, loopbacks.

Prompts: 1250 IP + credentials.
READ-ONLY: issues only 'show' commands (with session-only '| no-more' paging).

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


def _section(lines, title, cmd, output):
    lines.append("")
    lines.append("=" * 64)
    lines.append("  {}".format(title))
    lines.append("  CMD: {}".format(cmd))
    lines.append("=" * 64)
    for ln in output.splitlines():
        clean = ln.rstrip()
        if clean:
            lines.append(clean)
    lines.append("")


def _get_ces_slots(client):
    """Populated CES card slots (excludes ts2/ts5 which are GE/fiber)."""
    raw = client.run("show chassis hardware | no-more")
    slots = []
    for line in raw.splitlines():
        m = re.search(r'\+\s+(ts\d+)\s+:', line)
        if m and m.group(1) not in ("ts2", "ts5"):
            slots.append(m.group(1))
    return slots


def _get_fiber_slots(client):
    """GE/fiber slots (DHXE cards, typically ts2/ts5)."""
    raw = client.run("show chassis hardware | no-more")
    slots = []
    for line in raw.splitlines():
        m = re.search(r'\+\s+(ts\d+)\s+:.*DHXE', line, re.IGNORECASE)
        if m:
            slots.append(m.group(1))
    return slots


def _get_active_ds1s(client):
    """DS1 interfaces with MUSE tunnels (peer-address present)."""
    raw = client.run(
        'show configuration | display-set | grep ds1 | grep "peer-address" | no-more')
    ds1s = []
    for line in raw.splitlines():
        m = re.search(r'(ds1-ts\d+/\d+)', line)
        if m and m.group(1) not in ds1s:
            ds1s.append(m.group(1))
    return ds1s


# Launcher menu metadata (read by launcher.py auto-discovery).
TOOL_INFO = {
    'name': 'NPT-1250 Discovery ("show tech")',
    'ran_from': '1250',
    'desc': 'Full read-only inventory dump of a 1250: hardware, interfaces, protocols, alarms, DS1 status, and PMs.',
    'requires': '1250 IP, creds',
    'order': 20,
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
    """Execute the discovery dump given a params dict (no prompting)."""
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
    lines.append("NPT-1250 DISCOVERY DUMP")
    lines.append("Host: {}".format(host))
    lines.append("IP:   {}".format(ip))
    lines.append("=" * 64)

    raw_by_title = {}   # section title -> raw output, for the JIRA summary

    def sec(title, cmd, nomore=True):
        full = cmd + (" | no-more" if nomore else "")
        print("  collecting: {}".format(title))
        raw = c.run(full)
        raw_by_title[title] = raw
        _section(lines, title, cmd, raw)

    # Device info
    sec("Software Version", "show version")
    sec("Chassis Hardware", "show chassis hardware")
    sec("Interface Summary", "show interfaces")

    # Fiber interfaces
    fiber_slots = _get_fiber_slots(c)
    if not fiber_slots:
        lines.append("\n  NOTE: No fiber (GE) slots detected.")
    for slot in fiber_slots:
        # transceiver info does not support | no-more
        cmd = "show chassis transceiver information slot {} port1".format(slot)
        print("  collecting: Transceiver -- {}".format(slot))
        _section(lines, "Transceiver -- {}".format(slot), cmd, c.run(cmd))
        sec("Fiber Interface Config -- {}".format(slot),
            "show configuration interfaces ge-{}/1 | display-set".format(slot))
        sec("Fiber Interface Stats -- {}".format(slot),
            "show interfaces ge-{}/1".format(slot))

    # Routing / protocols
    sec("Protocols Config (OSPF/LDP)",
        "show configuration protocols | display-set | except tunnel")
    sec("OSPF Neighbors", "show ospf neighbor")
    sec("OSPF Interfaces", "show ospf interface")
    sec("LDP Sessions", "show ldp session")
    sec("LDP Adjacency", "show ldp adjacency")
    sec("BFD Sessions", "show bfd session")

    # Alarms and events
    sec("Chassis Alarms", "show chassis alarms")
    sec("System Alarms", "show system alarms")
    sec("NE Alarms", "show ne alarms")
    sec("NE Events", "show ne events")
    sec("NE Events Unreported", "show ne events unreported")

    # DS1 physical status -- populated CES slots
    ces_slots = _get_ces_slots(c)
    if not ces_slots:
        lines.append("\n  NOTE: No CES slots detected.")
    for slot in ces_slots:
        sec("DS1 Physical Status -- {}".format(slot),
            "show interfaces physical status slot {}".format(slot))

    # MUSE / active DS1 config
    sec("Active DS1s (MUSE Tunnels)",
        'show configuration | display-set | grep ds1 | grep "peer-address"')
    sec("All Peer Addresses",
        'show configuration | display-set | match "peer-address"')
    sec("NE-ID (VLAN)", 'show configuration | display-set | grep ne-id')
    sec("DS1 Descriptions", 'show configuration | display-set | grep description')
    sec("Customer DS1s (Peerless)",
        'show configuration | display-set | grep Peerless')
    sec("PM Collection Enabled Interfaces",
        'show configuration | display-set | grep pm-collection-enable | match interfaces')
    sec("PM Monitor Enabled Interfaces",
        'show configuration | display-set | grep pm-monitor-enable | match interfaces')

    # PMs -- active DS1s only
    active_ds1s = _get_active_ds1s(c)
    lines.append("")
    lines.append("=" * 64)
    lines.append("  PERFORMANCE MONITORS -- ACTIVE DS1s ({})".format(len(active_ds1s)))
    lines.append("=" * 64)
    if not active_ds1s:
        lines.append("  No active MUSE DS1s found.")
    else:
        for ds1 in active_ds1s:
            print("  collecting: PMs -- {}".format(ds1))
            lines.append("\n  -- {} --".format(ds1))

            cmd = "show interfaces pm 15min {} ces".format(ds1)
            raw = c.run(cmd + " | no-more")
            if "switch of this interface object is off" in raw:
                raw = ("  NOTE: CES PMs not enabled on this DS1.\n"
                       "  To enable: set interfaces {} unit 0 pm-collection-enable".format(ds1))
            _section(lines, "CES PM 15min -- {}".format(ds1), cmd, raw)

            cmd = "show interfaces pm 24hour {} ces".format(ds1)
            raw = c.run(cmd + " | no-more")
            if "switch of this interface object is off" in raw:
                raw = "  NOTE: CES PMs not enabled on this DS1."
            _section(lines, "CES PM 24hr -- {}".format(ds1), cmd, raw)

            sec("Physical PM 24hr -- {}".format(ds1),
                "show interfaces physical pm 24hour {}".format(ds1))

    # Loopbacks
    sec("Loopback Config", 'show configuration | display-set | match "lo0"')
    sec("Active Maintenance Loopbacks",
        'show configuration | display-set | grep "loopback terminal"')

    c.close()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("1250_discovery_{}_{}.txt".format(ts, host))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = "NPT-1250 discovery -- {} ({}), run {}.".format(
            host, ip, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Findings summary -- inventory + alarm state at a glance.
        def _rows(title):
            return [l.strip() for l in raw_by_title.get(title, "").splitlines()
                    if l.strip()]
        sum_lines = [
            "Fiber slots: {}".format(", ".join(fiber_slots) or "none"),
            "CES slots:   {}".format(", ".join(ces_slots) or "none"),
            "Active MUSE DS1s: {}{}".format(
                len(active_ds1s),
                "  ({})".format(", ".join(active_ds1s[:12]) +
                                 (" ..." if len(active_ds1s) > 12 else ""))
                if active_ds1s else ""),
        ]
        for t in ("Chassis Alarms", "System Alarms", "NE Alarms"):
            rows = _rows(t)
            if rows:
                sum_lines.append("{}: {} row(s) -- first: {}".format(
                    t, len(rows), rows[0][:60]))
            else:
                sum_lines.append("{}: none".format(t))
        sum_lines.append("OSPF neighbors: {} line(s) | LDP sessions: {} line(s)".format(
            len(_rows("OSPF Neighbors")), len(_rows("LDP Sessions"))))
        ok, msg = post_report(
            jira_ticket, intro,
            "NPT-1250 Discovery -- {}".format(host), "\n".join(lines),
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
    print("  NPT-1250 DISCOVERY (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
