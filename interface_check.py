#!/usr/bin/env python3
"""
interface_check.py -- standalone NPT-1800 NNI interface / reachability check.

Standalone port of the SecureCRT interface_check.py. Connects to an NPT-1800
(paramiko, via the shared NPT1800Client) and verifies an NNI sub-interface:
description, config, protocols, OSPF/LDP adjacency, and ping reachability
(destination = interface IP with last octet - 1).

Instead of an IP, the user picks the 1800 by 3-letter site (LAX/ATL/JFK/DFW);
the site maps to the hub IP below.

Runs standalone (prompts for its own inputs) OR via launcher.py (which passes a
params dict to run()).  READ-ONLY: 'show' commands + ping reachability only.

Built by P. Capps.
"""

import re
import os
import datetime

try:
    import paramiko  # noqa: F401
    _DEP_ERROR = None
except ImportError as e:
    _DEP_ERROR = "paramiko not installed (pip install paramiko): {}".format(e)

from npt1800_client import NPT1800Client
from device_base import CredentialError, prompt_ip
from jira_client import post_report, valid_ticket

# Definitive site -> 1800 hub IP map (provided by P. Capps).
SITE_TO_1800 = {
    "ATL": "10.111.130.254",
    "LAX": "10.111.131.254",
    "JFK": "10.111.130.255",
    "DFW": "10.111.131.255",
}


# Launcher menu metadata (read by launcher.py auto-discovery).
TOOL_INFO = {
    'name': 'IXC Check',
    'ran_from': '1800',
    'desc': 'Checks an IXC circuit from the 1800 to the 1250: config, OSPF/LDP, and ping reachability.',
    'requires': '1800 site (ATL/LAX/JFK/DFW), interface, creds',
    'order': 60,
}


def collect_inputs(session=None):
    """
    Prompt for this tool's inputs. Returns a params dict (or None to abort).
    `session` is an optional dict of remembered values across launcher tools:
    device-identifying values are offered as editable defaults; credentials are
    reused silently if already present.
    """
    import getpass
    session = session if session is not None else {}

    # Site (device-identifying -> confirmable, not silent)
    default_site = session.get("site")
    prompt = "1800 site [ATL/LAX/JFK/DFW]"
    if default_site:
        prompt += " [{}]".format(default_site)
    site = input(prompt + ": ").strip().upper()
    if not site and default_site:
        site = default_site
    if site not in SITE_TO_1800:
        print("Invalid site '{}'. Valid: {}".format(site, ", ".join(SITE_TO_1800)))
        return None
    hub_ip = SITE_TO_1800[site]
    print("  {} -> {}".format(site, hub_ip))

    # Interface (device-identifying -> confirmable default)
    default_iface = session.get("interface")
    prompt = "Interface (e.g. xe-ts13/3.624)"
    if default_iface:
        prompt += " [{}]".format(default_iface)
    iface = input(prompt + ": ").strip()
    if not iface and default_iface:
        iface = default_iface
    if not iface or "." not in iface:
        print("Invalid interface -- must include a dot for the VLAN.")
        return None

    # JIRA
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

    # Credentials (reused silently within a launcher session if present)
    user = session.get("infra_user")
    pw   = session.get("infra_pass")
    if not (user and pw):
        user = input("Username: ").strip()
        pw   = getpass.getpass("Password: ")
    if not (user and pw):
        print("Missing credentials.")
        return None

    return {
        "site": site,
        "interface": iface,
        "username": user,
        "password": pw,
        "jira_ticket": jira_ticket,
        "open_when_done": open_when_done,
    }


def run(params):
    """Execute the interface check given a params dict (no prompting)."""
    if _DEP_ERROR:
        print("[!] {} -- tool unavailable.".format(_DEP_ERROR))
        return

    site  = params["site"].upper()
    iface = params["interface"]
    user  = params["username"]
    pw    = params["password"]
    jira_ticket    = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", True)

    if site not in SITE_TO_1800:
        print("[!] invalid site '{}'.".format(site))
        return None
    hub_ip = SITE_TO_1800[site]
    if "." not in iface:
        print("[!] invalid interface '{}' (needs a .VLAN).".format(iface))
        return None
    nni  = iface.split(".")[0]
    vlan = iface.split(".")[1]

    c = NPT1800Client(hub_ip, user, pw)
    print("\n[connecting] {} ({})".format(site, hub_ip))
    try:
        c.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return None
    print("  host: {}  loc: {}".format(c.hostname, c.loc_code))

    lines = []
    lines.append("NPT-1800 INTERFACE CHECK")
    lines.append("Site: {} ({})   Host: {}".format(site, hub_ip, c.hostname))
    lines.append("Interface: {}".format(iface))
    lines.append("=" * 64)

    commands = [
        ("Interface Description",
         "show interfaces description | grep {}".format(iface)),
        ("Interface Config",
         "show configuration interfaces {} unit {} | display-set".format(nni, vlan)),
        ("Protocols (VLAN {})".format(vlan),
         "show configuration protocols | display-set | grep {} | except tunnel".format(vlan)),
        ("OSPF Neighbor",
         "show ospf neighbor | grep {}".format(vlan)),
        ("LDP Adjacency",
         "show ldp adjacency | grep {}".format(vlan)),
    ]

    captured = {}
    for title, cmd in commands:
        print("  collecting: {}".format(title))
        out = c.run(cmd, 30)
        captured[cmd] = out
        lines.append("")
        lines.append("  >> {}".format(title))
        lines.append("  CMD: {}".format(cmd))
        for ln in out.splitlines():
            clean = ln.rstrip()
            if clean:
                lines.append("    {}".format(clean))

    ping_results = {}   # title -> "4/4 (0% loss)" style string, for the summary
    cfg = captured.get(
        "show configuration interfaces {} unit {} | display-set".format(nni, vlan), "")
    m = re.search(r"set family inet address (\d+\.\d+\.\d+\.\d+)/", cfg)
    if m:
        ip = m.group(1)
        octets = ip.split(".")
        octets[-1] = str(int(octets[-1]) - 1)
        dest = ".".join(octets)
        for title, cmd in [
            ("Ping (4)", "ping {} count 4".format(dest)),
            ("Ping (4, 8000B DF)",
             "ping {} count 4 size 8000 do-not-fragment".format(dest)),
        ]:
            print("  collecting: {}".format(title))
            out = c.run(cmd, 30)
            pm = re.search(
                r'(\d+)\s+packets transmitted,\s*(\d+)\s+packets received,'
                r'\s*(\d+)%\s*packet loss', out)
            if pm:
                ping_results[title] = "{}/{} received ({}% loss)".format(
                    pm.group(2), pm.group(1), pm.group(3))
            else:
                ping_results[title] = "no ping statistics in output"
            lines.append("")
            lines.append("  >> {}".format(title))
            lines.append("  CMD: {}".format(cmd))
            for ln in out.splitlines():
                clean = ln.rstrip()
                if clean:
                    lines.append("    {}".format(clean))
    else:
        lines.append("")
        lines.append("  NOTE: No IP address found in interface config -- skipping ping.")

    c.close()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = r"C:\Claude\interface_check_{}_{}.txt".format(ts, c.hostname)
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = "Interface check -- {} on {} ({}), run {}.".format(
            iface, c.hostname, site,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Findings summary -- config/OSPF/LDP/ping at a glance.
        def _found(title_cmd_key, label):
            raw = ""
            for cmd, out in captured.items():
                if title_cmd_key in cmd:
                    raw = out
                    break
            hit = next((l.strip() for l in raw.splitlines() if l.strip()), "")
            return "{}: {}".format(label, hit if hit else "NONE FOUND")
        sum_lines = [
            "Interface {} on {} ({})".format(iface, c.hostname, site),
            _found("show ospf neighbor", "OSPF neighbor"),
            _found("show ldp adjacency", "LDP adjacency"),
        ]
        if ping_results:
            for title, res in ping_results.items():
                sum_lines.append("{}: {}".format(title, res))
        else:
            sum_lines.append("Ping: skipped -- no IP found in interface config")
        ok, msg = post_report(
            jira_ticket, intro,
            "Interface Check -- {}".format(iface), "\n".join(lines),
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
    print("  NPT-1800 INTERFACE CHECK (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
