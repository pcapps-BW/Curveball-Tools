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


_CMD_ERR_RE = re.compile(r'syntax error|unknown command|invalid',
                         re.IGNORECASE)


def _run_prefer_nomore(c, cmd):
    """
    Run `cmd | no-more`, but fall back to bare `cmd` if the device rejects
    the `| no-more`. Returns (output, cmd_that_worked).

    This exists because the transceiver command is a live disagreement:
    tsandberg (2026-11-02) asked for `| no-more` so the output is not
    segmented; a prior code comment claimed the command "does not support
    | no-more". Rather than guess and hardcode one or the other without a
    device to test on, this tries the pipe, detects a `syntax error` /
    `unknown command` response, and retries without it -- so it is correct
    whichever is true, and the CMD line in the report shows which form the
    device actually accepted.
    """
    out = c.run(cmd + " | no-more")
    if _CMD_ERR_RE.search(out or ""):
        bare = c.run(cmd)
        return bare, cmd
    return out, cmd + " | no-more"


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
    try:
        # try/finally added 2026-10-01: the body below is long, and any
        # exception in it used to return without closing the session.
        # Harmless for a one-shot CLI run (process exits, socket drops) but
        # NOT in the long-lived Flask app, where the transport and its
        # reader thread would survive until the device timed the session out.
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
            # tsandberg asked for `| no-more` here (output was segmented);
            # a prior comment claimed the command does not support it.
            # _run_prefer_nomore tries the pipe and falls back to bare if the
            # device rejects it, so it works either way and the CMD line shows
            # which form was used. Applies to both data-card ports (ts2/1,
            # ts5/1) -- fiber_slots covers both.
            base = "show chassis transceiver information slot {} port1".format(slot)
            print("  collecting: Transceiver -- {}".format(slot))
            tx_out, tx_cmd = _run_prefer_nomore(c, base)
            _section(lines, "Transceiver -- {}".format(slot), tx_cmd, tx_out)
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
        # `detail` adds per-session uptime, which plain `show bfd session`
        # omits (tsandberg, 2026-11-02).
        sec("BFD Sessions", "show bfd session detail")

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
        # DS1 Descriptions removed 2026-11-02 (tsandberg): the suite does not
        # provision the `description` field -- circuit identity lives in
        # `nms-user-label` instead -- so this section was always empty.
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
                           "  To enable: 2 set commands + commit confirm -- see "
                           "the CES PMs DISABLED MOP in the autotester report "
                           "(pm-monitor-enable AND pm-collection-enable)")
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

    finally:
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
