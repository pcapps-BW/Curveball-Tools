#!/usr/bin/env python3
"""
mux_loop_script.py -- CEM Hairpin Loop Script Generator (item 13)

Resolves the full T1 route, runs live prechecks on all four hops, then
generates a copy/paste-ready MOP for:
  - Scenario A: hairpin loop facing the GSX (isolates GSX from circuit)
  - Scenario B: hairpin loop facing the 1250 (isolates 1250/LEC from circuit)
  - Both restore procedures
  - Postcheck commands (commands only -- user runs after testing)

Default input mode: 1250 IP + DS1 (auto-resolves the full route).
Alternate mode: MUX-direct (user provides identifiers; only MUX is queried).

Built by P. Capps.
"""

import sys
import getpass
import datetime
import re

import platform_paths
from netbox_client import NetboxClient, load_netbox_config
from device_base import CredentialError, prompt_ip, ANSI_ESCAPE
from output_builders import resolve_nni_from_description
from jira_client import post_comment, valid_ticket

# Reuse existing client classes
from npt1250_client import NPT1250Client
from npt1800_client import NPT1800Client
from mux4216_client import MUX4216Client
from gsx_client import GSXClient, find_plink, find_live_ip

LOC_TO_MUX = {
    "atl": {"name": "ATL1-MUX-02", "ip": "10.243.35.11"},
    "jfk": {"name": "JFK1-MUX-02", "ip": "192.168.246.19"},
    "dfw": {"name": "DFW1-MUX-04", "ip": "10.243.1.17"},
    "lax": {"name": "LAX1-MUX-02", "ip": "10.243.65.4"},
}

TOOL_INFO = {
    'name':     'MUX CEM Loop Script',
    'ran_from': '1250',
    'desc':     ('Runs live prechecks on all hops, then generates a copy/paste-ready '
                 'CEM hairpin loop MOP (Scenario A: facing GSX; Scenario B: facing 1250).'),
    'requires': '1250 IP + DS1 (default) or MUX-direct identifiers',
    'order':    65,
}


# ---- input collection --------------------------------------------------

def collect_inputs(session=None):
    session = session if session is not None else {}

    print("\nInput mode:")
    print("  1) 1250 IP + DS1  (auto-resolves full route) [default]")
    print("  2) MUX-direct     (provide identifiers manually)")
    mode_raw = input("Select [1]: ").strip()
    mode = "mux_direct" if mode_raw == "2" else "from_1250"

    print("\n-- Credentials --")
    infra_user = session.get("infra_user")
    infra_pass = session.get("infra_pass")
    if not (infra_user and infra_pass):
        infra_user = input("Infrastructure username (1250/1800/MUX): ").strip()
        infra_pass = getpass.getpass("Infrastructure password: ")

    jira_ticket = None
    if input("Add to JIRA? (y/n) [n]: ").strip().lower() in ("y", "yes"):
        while True:
            jira_ticket = input("JIRA number (NOE-XXXX): ").strip().upper()
            if valid_ticket(jira_ticket):
                break
            if not jira_ticket:
                jira_ticket = None
                break
            print("  Invalid format. Expected NOE-XXXX.")

    open_when_done = input("Open script file when done? (y/n) [y]: ").strip().lower()
    open_when_done = open_when_done in ("", "y", "yes")

    if mode == "from_1250":
        ip_1250 = prompt_ip("1250 IP", default=session.get("ip_1250"))
        ds1_raw = input("DS1 (single, e.g. ts1/1): ").strip().lower()
        if not ip_1250 or not ds1_raw:
            print("Need a 1250 IP and DS1.")
            return None
        ds1_raw = ds1_raw.replace("&", ",").split(",")[0].strip()

        gsx_user = session.get("gsx_user")
        gsx_pass = session.get("gsx_pass")
        if not (gsx_user and gsx_pass):
            gsx_user = input("GSX username: ").strip()
            gsx_pass = getpass.getpass("GSX password: ")
        if not (infra_user and infra_pass and gsx_user and gsx_pass):
            print("Missing credentials.")
            return None

        return {
            "mode": "from_1250",
            "ip_1250": ip_1250,
            "ds1": ds1_raw,
            "infra_user": infra_user,
            "infra_pass": infra_pass,
            "gsx_user": gsx_user,
            "gsx_pass": gsx_pass,
            "jira_ticket": jira_ticket,
            "open_when_done": open_when_done,
        }

    # ---- MUX-direct mode ----
    def _opt_ip(label):
        """Prompt for an optional IP; return '' if blank, else validated IP."""
        def _valid(s):
            p = s.strip().split(".")
            return (len(p) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in p))
        while True:
            raw = input("{} [blank to skip]: ".format(label)).strip()
            if not raw:
                return ""
            if _valid(raw):
                return raw
            print("  '{}' is not a valid IP address.".format(raw))

    print("\n-- MUX-direct: provide identifiers manually --")
    mux_ip   = prompt_ip("MUX IP")
    mux_name = input("MUX name (e.g. LAX1-MUX-02): ").strip()
    acr_num  = input("ACR group number (e.g. 53): ").strip()
    cem_group = input("CEM group (e.g. 202): ").strip()
    if not (mux_ip and mux_name and acr_num and cem_group):
        print("MUX IP, name, ACR, and CEM group are required.")
        return None

    print("\n-- Optional: provide other hop info for precheck commands --")
    print("  (leave blank to skip prechecks for that hop)")
    ip_1250  = _opt_ip("1250 IP")
    ds1_1250 = input("DS1 on 1250 (e.g. ts1/1): ").strip().lower() if ip_1250 else ""
    ip_1800  = _opt_ip("1800 IP")
    oc_iface = input("OC interface on 1800 (e.g. oc-ts3/1): ").strip().lower() if ip_1800 else ""
    gsx_t1   = input("GSX T1 (e.g. T1-1-7-2-2 or 7-2-2) [blank to skip]: ").strip().upper()
    gsx_ip   = _opt_ip("GSX IP") if gsx_t1 else ""

    gsx_user = gsx_pass = ""
    if gsx_ip:
        gsx_user = session.get("gsx_user") or input("GSX username: ").strip()
        gsx_pass = session.get("gsx_pass") or getpass.getpass("GSX password: ")

    if not (infra_user and infra_pass):
        print("Missing infrastructure credentials.")
        return None

    return {
        "mode": "mux_direct",
        "mux_ip": mux_ip,
        "mux_name": mux_name,
        "acr_num": acr_num,
        "cem_group": cem_group,
        "ip_1250": ip_1250,
        "ds1": ds1_1250,
        "ip_1800": ip_1800,
        "oc_iface": oc_iface,
        "gsx_t1": gsx_t1,
        "gsx_ip": gsx_ip,
        "infra_user": infra_user,
        "infra_pass": infra_pass,
        "gsx_user": gsx_user,
        "gsx_pass": gsx_pass,
        "jira_ticket": jira_ticket,
        "open_when_done": open_when_done,
    }


# ---- precheck helpers --------------------------------------------------

def _banner(text, char="─", width=78):
    return "── {} {}".format(text, char * max(0, width - len(text) - 4))


def _capture(client, cmd, note=None):
    """Run a command; return (display_cmd, output_text). Strips ANSI
    escape sequences (the '[K' erase-line artifacts some devices emit)
    so generated scripts/reports render clean."""
    try:
        out = client.run(cmd)
    except Exception as e:
        out = "[ERROR running command: {}]".format(e)
    out = ANSI_ESCAPE.sub("", out or "").replace("\r", "")
    display = cmd
    if note:
        pad = max(1, 60 - len(cmd))
        display = "{}{}{}".format(cmd, " " * pad, note)
    return display, out.rstrip()


def _section(header, pairs):
    """
    Build a precheck section string.
    header: e.g. '# NPT-1250 (10.x.x.x)'
    pairs: list of (display_cmd, output_text) tuples
    """
    lines = ["", header]
    for cmd_str, out in pairs:
        lines.append(cmd_str)
        if out:
            lines.append(out)
        lines.append("")
    return "\n".join(lines)


# ---- circuit dict helpers ----------------------------------------------

def _t1_normalize(raw):
    """Accept 'T1-1-7-2-2' or '7-2-2'; always return 'T1-1-X-X-X'."""
    raw = raw.strip().upper()
    if raw.startswith("T1-"):
        return raw
    parts = raw.split("-")
    if len(parts) == 3:
        return "T1-1-{}-{}-{}".format(*parts)
    return raw


def _ds1_parts(ds1):
    """ts1/7 -> (slot='ts1', port='7')"""
    m = re.match(r'(ts\d+)/(\d+)', ds1.lower())
    if m:
        return m.group(1), m.group(2)
    return ds1, ""


# ---- main run dispatcher -----------------------------------------------

def run(params):
    if params.get("mode") == "mux_direct":
        return _run_mux_direct(params)
    else:
        return _run_from_1250(params)


# ---- full route resolution (1250+DS1 mode) -----------------------------

def _run_from_1250(params):
    ip_1250    = params["ip_1250"]
    ds1        = params["ds1"]
    infra_user = params["infra_user"]
    infra_pass = params["infra_pass"]
    gsx_user   = params["gsx_user"]
    gsx_pass   = params["gsx_pass"]

    nb_url, nb_token = load_netbox_config()
    netbox = NetboxClient(nb_url, nb_token)
    plink_path = find_plink()

    print("\n[Loop Script] Resolving route: {} {}".format(ip_1250, ds1))

    circuit = {}    # filled in as we resolve each hop
    precheck_sections = []

    # ---- HOP 1: 1250 ----
    print("  [1250] connecting ...")
    c1250 = NPT1250Client(ip_1250, infra_user, infra_pass)
    try:
        c1250.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] 1250 connect failed: {}".format(e))
        return None
    host_1250 = c1250.hostname
    print("  [1250] {}".format(host_1250))

    ds1_map, _ = c1250.query_batch([ds1])
    if ds1 not in ds1_map:
        print("[!] Could not resolve DS1 {} on {}".format(ds1, host_1250))
        c1250.close()
        return None
    info_1250 = ds1_map[ds1]
    hub_ip     = info_1250["hub_ip"]
    muse_id    = info_1250["muse_id"]
    loopback   = info_1250["loopback_ip"]

    circuit["ip_1250"]   = ip_1250
    circuit["host_1250"] = host_1250
    circuit["loopback"]  = loopback
    circuit["ds1"]       = ds1
    circuit["muse_id"]   = muse_id
    circuit["hub_ip"]    = hub_ip

    ds1_slot, ds1_port = _ds1_parts(ds1)

    # 1250 prechecks
    print("  [1250] running prechecks ...")
    pairs_1250 = []
    pairs_1250.append(_capture(c1250, "show ne alarms | grep ds1"))
    pairs_1250.append(_capture(
        c1250,
        'show interfaces physical status slot {} | grep "ds1/{} "'.format(ds1_slot, ds1_port)))
    pairs_1250.append(_capture(c1250, "show interfaces physical pm 24hour ds1-{}".format(ds1)))
    pairs_1250.append(_capture(c1250, "show interfaces physical pm 15min ds1-{}".format(ds1)))
    c1250.close()
    precheck_sections.append(_section(
        "# NPT-1250  {}  ({})".format(host_1250, ip_1250), pairs_1250))

    # ---- HOP 2: 1800 ----
    print("  [1800] connecting to {} ...".format(hub_ip))
    c1800 = NPT1800Client(hub_ip, infra_user, infra_pass)
    try:
        c1800.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] 1800 connect failed: {}".format(e))
        return None
    hub_hostname = c1800.hostname
    print("  [1800] {}".format(hub_hostname))

    (oc12_map, _, hub_name, hub_hostname,
     loc_code, _, peerless_cust) = c1800.query_batch(hub_ip, {ds1: info_1250})

    if ds1 in peerless_cust:
        print("[!] {} is a Peerless circuit -- no MUX/GSX hop. Loop script not applicable.")
        c1800.close()
        return None
    if ds1 not in oc12_map:
        print("[!] Could not resolve OC interface for {} on 1800".format(ds1))
        c1800.close()
        return None

    oc12     = oc12_map[ds1]
    oc_base  = oc12.split(":")[0]

    circuit["hub_ip"]       = hub_ip
    circuit["hub_name"]     = hub_name
    circuit["hub_hostname"] = hub_hostname
    circuit["oc12"]         = oc12
    circuit["oc_base"]      = oc_base
    circuit["loc_code"]     = loc_code

    # NNI resolution -> xe sub-interface
    prefix = host_1250[:-4] if len(host_1250) > 4 else host_1250
    try:
        desc = c1800.run(
            "show interfaces description | grep {} | no-more".format(prefix))
        xe_logical, xe_physical, nni_status = resolve_nni_from_description(desc)
    except Exception:
        xe_logical = xe_physical = None
        nni_status = "error"

    circuit["xe_logical"]  = xe_logical   # e.g. xe-ts16/2.960
    circuit["xe_physical"] = xe_physical  # e.g. xe-ts16/2
    if xe_logical and "." in xe_logical:
        circuit["xe_vlan"] = xe_logical.split(".")[-1]  # e.g. 960
    else:
        circuit["xe_vlan"] = None

    # 1800 prechecks
    print("  [1800] running prechecks ...")
    pairs_1800 = []
    pairs_1800.append(_capture(
        c1800,
        "show interfaces description | grep {}".format(prefix),
        note="← find sub-int by 1250 hostname prefix"))
    if xe_logical:
        pairs_1800.append(_capture(c1800, "show interfaces {}".format(xe_logical)))
        xe_unit = circuit.get("xe_vlan", "")
        if xe_physical and xe_unit:
            pairs_1800.append(_capture(
                c1800,
                "show configuration interfaces {} unit {} | display-set".format(
                    xe_physical, xe_unit)))
            pairs_1800.append(_capture(
                c1800,
                "show configuration protocols ldp | grep {} | display-set".format(xe_unit)))
            pairs_1800.append(_capture(
                c1800,
                ("show configuration protocols ospf protocol-instance 0 area 0.0.0.0 "
                 "interface {} | display-set".format(xe_logical))))
    pairs_1800.append(_capture(
        c1800,
        "show interfaces physical pm 24hour {}".format(oc_base),
        note="← OC baseline"))
    pairs_1800.append(_capture(
        c1800,
        "show ne alarms | grep {}".format(oc_base),
        note="← confirm no path alarms"))
    pairs_1800.append(_capture(
        c1800,
        "show ne alarms | grep R-DS1AIS",
        note="← remote AIS across hub"))
    c1800.close()
    precheck_sections.append(_section(
        "# NPT-1800  {}  ({})".format(hub_hostname, hub_ip), pairs_1800))

    # ---- HOP 3: MUX ----
    mux_info = LOC_TO_MUX.get(loc_code)
    if not mux_info:
        print("[!] No MUX mapping for loc '{}'".format(loc_code))
        return None
    mux_ip   = mux_info["ip"]
    mux_name = mux_info["name"]
    print("  [MUX] connecting to {} ...".format(mux_name))

    cmux = MUX4216Client(mux_ip, infra_user, infra_pass)
    try:
        cmux.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] MUX connect failed: {}".format(e))
        return None
    print("  [MUX] {}".format(mux_name))

    mux_results, _, _, _, errs = cmux.query_batch(
        mux_name, mux_ip, {ds1: oc12}, {ds1: hub_hostname}, netbox)

    if ds1 in errs or ds1 not in mux_results:
        print("[!] MUX resolution failed: {}".format(errs.get(ds1, "no result")))
        cmux.close()
        return None

    mr = mux_results[ds1]
    acr_num      = mr["acr_group"]
    cem_group    = mr["cem_group"]
    active_sonet = mr.get("active_sonet")

    # acr_group / active_sonet are None when the CEM could not be located in
    # any ACR group (mux4216_client stopped guessing one, 2026-10-01). Both
    # are interpolated straight into device commands below -- 'show run
    # controller SONET-ACR None' and 'show cem interface CEM None' -- so bail
    # out here rather than sending malformed commands to a production MUX and
    # then parsing whatever error comes back.
    if not acr_num or not active_sonet:
        print("[!] {}: ACR/active SONET path undetermined on the MUX "
              "(CEM {} not found in any ACR group) -- cannot build the MUX "
              "loop commands.".format(ds1, cem_group or "?"))
        cmux.close()
        return None
    gsx_ce_iface = mr.get("gsx_ce_iface", "?")
    gsx_cem_id   = mr.get("gsx_cem_id", "?")
    gsx_node     = mr.get("gsx_node", "?")
    gsx_slot     = mr.get("gsx_slot", "?")
    gsx_port     = mr.get("gsx_port", "?")
    gsx_t1_num   = mr.get("gsx_t1", "?")
    conn_name    = mr.get("conn_name")

    circuit.update({
        "mux_ip":        mux_ip,
        "mux_name":      mux_name,
        "acr_num":       acr_num,
        "cem_group":     cem_group,
        "active_sonet":  active_sonet,
        "gsx_ce_iface":  gsx_ce_iface,
        "gsx_cem_id":    gsx_cem_id,
        "gsx_node":      gsx_node,
        "gsx_slot":      gsx_slot,
        "gsx_port":      gsx_port,
        "gsx_t1_num":    gsx_t1_num,
        "conn_name":     conn_name,
    })

    # MUX prechecks
    print("  [MUX] running prechecks ...")
    pairs_mux = []
    pairs_mux.append(("terminal length 0", ""))
    pairs_mux.append(_capture(
        cmux, "show cem interface CEM {}".format(active_sonet),
        note="← 1800-facing physical (0 circuits expected)"))
    pairs_mux.append(_capture(
        cmux, "show interfaces CEM {}".format(active_sonet),
        note="← note output errors as baseline"))
    pairs_mux.append(_capture(
        cmux, "show cem interface cem {}".format(gsx_ce_iface),
        note="← GSX-facing physical (28 circuits up expected)"))
    pairs_mux.append(_capture(
        cmux,
        "show cem circuit interface cem-acr {} {}".format(acr_num, cem_group),
        note="← Pkts Missing = 0, Failure Counts = 0, no Lbits/Rbits"))
    pairs_mux.append(_capture(
        cmux,
        r'show xconnect all | include CE{}:{}\('.format(acr_num, cem_group),
        note="← confirm both legs UP"))
    pairs_mux.append(_capture(
        cmux,
        "show acr group {} detail cem | include CE{}|{}".format(
            acr_num, acr_num, cem_group),
        note="← working/protect state"))
    if conn_name:
        pairs_mux.append(_capture(
            cmux, "show run | include {}".format(conn_name),
            note="← SAVE THIS OUTPUT FOR ROLLBACK VERIFICATION"))
    pairs_mux.append(_capture(
        cmux, "show facility-alarm status | include CEM"))
    if conn_name:
        pairs_mux.append(_capture(
            cmux, "show connection name {}".format(conn_name),
            note="← verify production binding"))
    cmux.close()
    precheck_sections.append(_section(
        "# {}  ({})".format(mux_name, mux_ip), pairs_mux))

    # ---- HOP 4: GSX ----
    gsx_t1_full = "T1-1-{}-{}-{}".format(gsx_slot, gsx_port, gsx_t1_num)
    gsx_ip_actual = None

    if plink_path and gsx_node and gsx_node != "?":
        print("  [GSX] resolving {} ...".format(gsx_node))
        gsx_dev = netbox.derive_gsx_device_name(gsx_node, hub_hostname)
        _nbw    = []
        cards   = netbox.gsx_mgmt_ips(gsx_dev, _nbw)
        if not cards and _nbw:
            print("  [!] {}".format(_nbw[0]))
        if cards:
            _, gsx_ip_actual = find_live_ip(cards)

    pairs_gsx = []
    if gsx_ip_actual and plink_path:
        print("  [GSX] connecting to {} ({}) ...".format(gsx_node, gsx_ip_actual))
        g = GSXClient(gsx_ip_actual, gsx_user, gsx_pass, plink_path)
        try:
            g.connect()
            gsx_t3_full = "T3-1-{}-{}".format(gsx_slot, gsx_port)
            pairs_gsx.append(_capture(g, "SHOW T3 {} STATUS".format(gsx_t3_full),
                note="← T3 parent status (DOWN here = all T1s on this port affected)"))
            pairs_gsx.append(_capture(
                g, "SHOW T1 {} STATUS".format(gsx_t1_full),
                note="← record UP -- baseline state"))
            pairs_gsx.append(_capture(g, "SHOW T1 {} ADMIN".format(gsx_t1_full)))
            pairs_gsx.append(_capture(
                g, "SHOW T1 {} PERFMON CURRENT FIFTEENMIN".format(gsx_t1_full),
                note="← baseline PM snapshot"))
        except Exception as e:
            print("  [!] GSX pull failed: {}".format(e))
            pairs_gsx.append(("# GSX pull failed: {}".format(e), ""))
        finally:
            g.close()
        circuit["gsx_ip"] = gsx_ip_actual
    else:
        print("  [GSX] plink not available or IP not resolved -- generating commands only")
        gsx_t3_full = "T3-1-{}-{}".format(gsx_slot, gsx_port)
        pairs_gsx.append(("# GSX IP could not be resolved -- run these commands manually:", ""))
        pairs_gsx.append(("SHOW T3 {} STATUS".format(gsx_t3_full),
                          "← T3 parent status (DOWN here = all T1s on this port affected)"))
        pairs_gsx.append(("SHOW T1 {} STATUS".format(gsx_t1_full),
                          "← record UP -- baseline state"))
        pairs_gsx.append(("SHOW T1 {} ADMIN".format(gsx_t1_full), ""))
        pairs_gsx.append(("SHOW T1 {} PERFMON CURRENT FIFTEENMIN".format(gsx_t1_full),
                          "← baseline PM snapshot"))
        circuit["gsx_ip"] = "?"

    precheck_sections.append(_section(
        "# {}  ({})".format(gsx_node, circuit.get("gsx_ip", "?")), pairs_gsx))

    # ---- GENERATE AND SAVE ----
    script = _build_script(circuit, precheck_sections)
    out_path = _write(script, ds1)
    if not out_path:
        return None

    # JIRA
    jira_ticket = params.get("jira_ticket")
    if jira_ticket and out_path:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        summary = "\n".join([
            "CEM hairpin loop script generated (prechecks run live; loop "
            "commands are copy/paste-ready in the attachment -- NOT executed).",
            "Circuit : {} {} ({})".format(
                circuit.get("host_1250", "?"), circuit.get("ds1", "?"),
                circuit.get("muse_id", "?")),
            "Path    : 1800 {}  {}".format(
                circuit.get("hub_name", "?"), circuit.get("oc12", "?")),
            "MUX     : {}  ACR {}  CEM {}  (conn: {})".format(
                circuit.get("mux_name", "?"), circuit.get("acr_num", "?"),
                circuit.get("cem_group", "?"), circuit.get("conn_name", "?")),
            "GSX     : {}  ({})".format(
                circuit.get("gsx_node", "?"), circuit.get("gsx_ip", "?")),
        ])
        ok, msg = post_comment(
            jira_ticket, host_1250, 1, script[:4000], "",
            attach_path=out_path, summary_text=summary)
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if params.get("open_when_done") and out_path:
        try:
            import os as _os
            _os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open file: {}".format(e))

    return out_path


# ---- MUX-direct mode ---------------------------------------------------

def _run_mux_direct(params):
    infra_user = params["infra_user"]
    infra_pass = params["infra_pass"]
    mux_ip     = params["mux_ip"]
    mux_name   = params["mux_name"]
    acr_num    = params["acr_num"]
    cem_group  = params["cem_group"]

    circuit = {
        "mux_ip":    mux_ip,
        "mux_name":  mux_name,
        "acr_num":   acr_num,
        "cem_group": cem_group,
        "ds1":       params.get("ds1", ""),
        "ip_1250":   params.get("ip_1250", ""),
        "hub_ip":    params.get("ip_1800", ""),
        "oc_base":   params.get("oc_iface", ""),
        "gsx_ip":    params.get("gsx_ip", ""),
    }

    # Normalize GSX T1
    gsx_t1_raw  = params.get("gsx_t1", "")
    gsx_t1_full = _t1_normalize(gsx_t1_raw) if gsx_t1_raw else ""
    if gsx_t1_full:
        parts = gsx_t1_full.replace("T1-1-", "").split("-")
        circuit["gsx_slot"]   = parts[0] if len(parts) > 0 else "?"
        circuit["gsx_port"]   = parts[1] if len(parts) > 1 else "?"
        circuit["gsx_t1_num"] = parts[2] if len(parts) > 2 else "?"
        circuit["gsx_node"]   = "GSX"
    else:
        circuit["gsx_slot"] = circuit["gsx_port"] = circuit["gsx_t1_num"] = "?"
        circuit["gsx_node"] = "?"

    precheck_sections = []

    # MUX connection
    print("\n[Loop Script] Connecting to {} ...".format(mux_name))
    cmux = MUX4216Client(mux_ip, infra_user, infra_pass)
    try:
        cmux.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] MUX connect failed: {}".format(e))
        return None

    # Discover connection name
    conn_name = cmux.get_connection_name(acr_num, cem_group)
    circuit["conn_name"] = conn_name
    circuit["active_sonet"] = "?"
    circuit["gsx_ce_iface"] = "?"
    circuit["gsx_cem_id"]   = "?"

    # Try to derive gsx_ce_iface from xconnect
    try:
        raw_xc = cmux.run(
            r'show xconnect all | include CE{}:{}\('.format(acr_num, cem_group))
        for line in raw_xc.splitlines():
            ces = re.findall(r'CE([\d/]+):(\d+)', line)
            for ce_iface, ce_id in ces:
                if "/" in ce_iface:
                    circuit["gsx_ce_iface"] = ce_iface
                    circuit["gsx_cem_id"]   = ce_id
                    break
    except Exception:
        pass

    # MUX prechecks
    print("  [MUX] running prechecks ...")
    pairs_mux = []
    pairs_mux.append(("terminal length 0", ""))
    pairs_mux.append(_capture(
        cmux,
        "show cem circuit interface cem-acr {} {}".format(acr_num, cem_group),
        note="← Pkts Missing = 0, Failure Counts = 0, no Lbits/Rbits"))
    pairs_mux.append(_capture(
        cmux,
        r'show xconnect all | include CE{}:{}\('.format(acr_num, cem_group),
        note="← confirm both legs UP"))
    pairs_mux.append(_capture(
        cmux,
        "show acr group {} detail cem | include CE{}|{}".format(
            acr_num, acr_num, cem_group),
        note="← working/protect state"))
    if conn_name:
        pairs_mux.append(_capture(
            cmux, "show run | include {}".format(conn_name),
            note="← SAVE THIS OUTPUT FOR ROLLBACK VERIFICATION"))
        pairs_mux.append(_capture(
            cmux, "show connection name {}".format(conn_name),
            note="← verify production binding"))
    pairs_mux.append(_capture(cmux, "show facility-alarm status | include CEM"))
    cmux.close()
    precheck_sections.append(_section(
        "# {}  ({})".format(mux_name, mux_ip), pairs_mux))

    # Other hops: generate commands only
    ds1       = circuit.get("ds1", "")
    ip_1250   = circuit.get("ip_1250", "")
    oc_base   = circuit.get("oc_base", "")
    hub_ip    = circuit.get("hub_ip", "")
    gsx_ip    = circuit.get("gsx_ip", "")

    if ip_1250 and ds1:
        ds1_slot, ds1_port = _ds1_parts(ds1)
        precheck_sections.append("\n# NPT-1250  ({})  -- run manually\n"
            "show ne alarms | grep ds1\n"
            'show interfaces physical status slot {} | grep "ds1/{} "\n'
            "show interfaces physical pm 24hour ds1-{}\n"
            "show interfaces physical pm 15min ds1-{}\n".format(
                ds1_slot, ds1_port, ds1, ds1))

    if hub_ip and oc_base:
        precheck_sections.append("\n# NPT-1800  ({})  -- run manually\n"
            "show interfaces physical pm 24hour {}\n"
            "show ne alarms | grep {}\n"
            "show ne alarms | grep R-DS1AIS\n".format(hub_ip, oc_base, oc_base))

    if gsx_t1_full:
        precheck_sections.append("\n# GSX  ({})  -- run manually\n"
            "SHOW T1 {} STATUS\n"
            "SHOW T1 {} ADMIN\n"
            "SHOW T1 {} PERFMON CURRENT FIFTEENMIN\n".format(
                gsx_ip or "IP not provided",
                gsx_t1_full, gsx_t1_full, gsx_t1_full))

    script = _build_script(circuit, precheck_sections)
    out_path = _write(script, circuit.get("ds1") or "mux_direct")
    if not out_path:
        return None

    jira_ticket = params.get("jira_ticket")
    if jira_ticket and out_path:
        summary = "\n".join([
            "CEM hairpin loop script generated (MUX-direct mode; loop "
            "commands are copy/paste-ready in the attachment -- NOT executed).",
            "MUX     : {}  ACR {}  CEM {}  (conn: {})".format(
                mux_name, circuit.get("acr_num", "?"),
                circuit.get("cem_group", "?"),
                circuit.get("conn_name", "?")),
            "GSX     : {}  ({})".format(
                circuit.get("gsx_node", "?"), circuit.get("gsx_ip", "?")),
        ])
        ok, msg = post_comment(
            jira_ticket, mux_name, 1, script[:4000], "",
            attach_path=out_path, summary_text=summary)
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if params.get("open_when_done") and out_path:
        try:
            import os as _os
            _os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open file: {}".format(e))

    return out_path


# ---- script builder ----------------------------------------------------

def _build_script(circuit, precheck_sections):
    acr_num      = circuit.get("acr_num", "?")
    cem_group    = circuit.get("cem_group") or "?"
    gsx_ce_iface = circuit.get("gsx_ce_iface", "?")
    gsx_cem_id   = circuit.get("gsx_cem_id", "?")
    conn_name    = circuit.get("conn_name") or "<CONNECTION_NAME>"
    conn_missing = not circuit.get("conn_name")
    active_sonet = circuit.get("active_sonet") or "?"
    ds1          = circuit.get("ds1", "?")
    host_1250    = circuit.get("host_1250", circuit.get("ip_1250", "?"))
    hub_hostname = circuit.get("hub_hostname", circuit.get("hub_ip", "?"))
    mux_name     = circuit.get("mux_name", "?")
    gsx_node     = circuit.get("gsx_node", "?")
    gsx_slot     = circuit.get("gsx_slot", "?")
    gsx_port     = circuit.get("gsx_port", "?")
    gsx_t1_num   = circuit.get("gsx_t1_num", "?")
    gsx_t1_full  = "T1-1-{}-{}-{}".format(gsx_slot, gsx_port, gsx_t1_num)
    oc12         = circuit.get("oc12", circuit.get("oc_base", "?"))
    ds1_slot, ds1_port = _ds1_parts(ds1)

    # Hairpin command ingredients
    # Production:  connect {conn} CEM-ACR{acr} {cem}   CEM{gsx_ce}/{gsx_id}
    # GSX loop:    connect {conn} CEM{gsx_ce}/{gsx_id}  CEM{gsx_ce}/{gsx_id}
    # 1250 loop:   connect {conn} CEM-ACR{acr} {cem}   CEM-ACR{acr} {cem}

    prod_leg_acr  = "CEM-ACR{} {}".format(acr_num, cem_group)
    prod_leg_gsx  = "CEM{} {}".format(gsx_ce_iface, gsx_cem_id)
    prod_connect  = "connect {} {} {}".format(conn_name, prod_leg_acr, prod_leg_gsx)
    gsx_loop      = "connect {} {} {}".format(conn_name, prod_leg_gsx, prod_leg_gsx)
    acr_loop      = "connect {} {} {}".format(conn_name, prod_leg_acr, prod_leg_acr)

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = []

    # ── HEADER ──
    lines += [
        "NCS 4216 CEM HAIRPIN LOOP — CIRCUIT SCRIPT",
        "Generated: {}".format(ts),
        "=" * 78,
        "",
        "Circuit  : {} on {}  |  MUSE {}".format(
            ds1.upper(), host_1250, circuit.get("muse_id", "?")),
        "1800     : {}".format(hub_hostname),
        "OC       : {}".format(oc12),
        "MUX      : {}  (ACR {}  CEM {})".format(mux_name, acr_num, cem_group),
        "GSX      : {}  {}".format(gsx_node, gsx_t1_full),
        "Conn     : {}".format(conn_name),
        "",
        "IP REFERENCE",
        "  {}  (NPT-1250) : {}".format(host_1250, circuit.get("ip_1250", "?")),
        "  {}  (NPT-1800) : {}".format(hub_hostname, circuit.get("hub_ip", "?")),
        "  {}  (MUX)     : {}".format(mux_name, circuit.get("mux_ip", "?")),
        "  {}  (GSX)      : {}".format(gsx_node, circuit.get("gsx_ip", "?")),
        "",
        "=" * 78,
    ]

    if conn_missing:
        lines += [
            "",
            "!! WARNING: connection name could NOT be auto-discovered.",
            "!! Every 'connect' / 'no connect' line below shows <CONNECTION_NAME>",
            "!! as a placeholder. Find the real name with:",
            "!!     show run | include CEM-ACR{} {}".format(acr_num, cem_group),
            "!! and replace <CONNECTION_NAME> throughout before pasting.",
            "=" * 78,
        ]

    # ── SECTION 1: PRECHECKS (live output) ──
    lines += [
        "",
        _banner("1. PRECHECKS & BASELINES"),
    ]
    for sec in precheck_sections:
        if isinstance(sec, str):
            lines.append(sec)
        else:
            lines.append(sec)

    # ── SECTION 2A: SCENARIO A — LOOP FACING GSX ──
    lines += [
        "",
        "=" * 78,
        _banner("2. SCENARIO A: LOOP FACING GSX (TURN UP & VALIDATION)"),
        "",
        "# {}  — Apply hairpin loop facing GSX".format(mux_name),
        "configure terminal",
        " no {}".format(prod_connect),
        " {}".format(gsx_loop),
        "commit",
        "end",
        "",
        "# {}  — Clear circuit counters to observe live status".format(mux_name),
        "clear cem circuit interface cem-acr {} {}".format(acr_num, cem_group),
        "",
        "# VALIDATION — GSX SIDE",
        "SHOW T1 {} STATUS".format(gsx_t1_full) +
            "                    ← MUST BE UP / NO ALARM (reflecting own signal)",
        "SHOW T1 {} PERFMON CURRENT FIFTEENMIN".format(gsx_t1_full) +
            "   ← monitor for frame/bit errors",
        "",
        "# VALIDATION — {}".format(mux_name),
        "show connection name {}".format(conn_name) +
            "                     ← verify both segments bound to {}".format(prod_leg_gsx),
        r'show xconnect all | include CE{}:{}\('.format(acr_num, cem_group) +
            "         ← confirm hairpin legs show {}:{}".format(gsx_ce_iface, gsx_cem_id),
        "",
        "# VALIDATION — 1250 ISOLATION CHECK",
        'show interfaces physical status slot {} | grep "ds1/{} "'.format(ds1_slot, ds1_port) +
            "   ← expect DOWN / AIS (cut off from MUX)",
    ]

    # ── SECTION 2B: RESTORE FROM GSX LOOP ──
    lines += [
        "",
        "=" * 78,
        _banner("3. SCENARIO A: RESTORE FROM GSX LOOP"),
        "",
        "# {}  — Remove GSX hairpin, restore production connect".format(mux_name),
        "configure terminal",
        " no {}".format(gsx_loop),
        " {}".format(prod_connect),
        "commit",
        "end",
        "",
        "# {}  — Verify production cross-connect state".format(mux_name),
        "show connection name {}".format(conn_name) +
            "                     ← verify state is restored to production",
    ]

    # ── SECTION 3A: SCENARIO B — LOOP FACING 1250 ──
    lines += [
        "",
        "=" * 78,
        _banner("4. SCENARIO B: LOOP FACING 1250 (TURN UP & VALIDATION)"),
        "",
        "# {}  — Apply hairpin loop facing 1250".format(mux_name),
        "configure terminal",
        " no {}".format(prod_connect),
        " {}".format(acr_loop),
        "commit",
        "end",
        "",
        "# {}  — Clear circuit counters to observe live status".format(mux_name),
        "clear cem circuit interface cem-acr {} {}".format(acr_num, cem_group),
        "",
        "# VALIDATION — {}".format(mux_name),
        "show cem circuit interface cem-acr {} {}".format(acr_num, cem_group) +
            "   ← ACTIVE, Pkts Missing should NOT accumulate",
        "show connection name {}".format(conn_name) +
            "                     ← verify both segments bound to {}".format(prod_leg_acr),
        r'show xconnect all | include CE{}:{}\('.format(acr_num, cem_group) +
            "         ← confirm hairpin legs show CE{}:{}".format(acr_num, cem_group),
        "",
        "# VALIDATION — NPT-1250 SIDE",
        'show interfaces physical status slot {} | grep "ds1/{} "'.format(ds1_slot, ds1_port) +
            "   ← confirm UP / Loop state active",
        "show interfaces physical pm 15min ds1-{}".format(ds1) +
            "          ← confirm PMs accumulating from test pattern",
        "",
        "# VALIDATION — GSX ISOLATION CHECK",
        "SHOW T1 {} STATUS".format(gsx_t1_full) +
            "                    ← MUST BE DOWN / LOF / AIS (cut off from circuit)",
    ]

    # ── SECTION 3B: RESTORE FROM 1250 LOOP ──
    lines += [
        "",
        "=" * 78,
        _banner("5. SCENARIO B: RESTORE FROM 1250 LOOP"),
        "",
        "# {}  — Remove 1250 hairpin, restore production connect".format(mux_name),
        "configure terminal",
        " no {}".format(acr_loop),
        " {}".format(prod_connect),
        "commit",
        "end",
        "",
        "# {}  — Verify production cross-connect state".format(mux_name),
        "show connection name {}".format(conn_name) +
            "                     ← verify state is restored to production",
    ]

    # ── SECTION 4: POSTCHECKS (commands only) ──
    lines += [
        "",
        "=" * 78,
        _banner("6. POSTCHECKS & CLEANUP"),
        "",
        "# {}  ({})".format(gsx_node, circuit.get("gsx_ip", "?")),
        "SHOW T1 {} STATUS".format(gsx_t1_full) +
            "                    ← MUST BE UP / NO ALARM",
        "SHOW T1 {} PERFMON CURRENT FIFTEENMIN".format(gsx_t1_full) +
            "   ← compare against precheck baseline",
        "",
        "# {}  ({})".format(mux_name, circuit.get("mux_ip", "?")),
        "show connection name {}".format(conn_name) +
            "                     ← verify active production connection",
        "show cem circuit interface cem-acr {} {}".format(acr_num, cem_group) +
            "   ← Pkts Missing / Failure Counts stable",
        r'show xconnect all | include CE{}:{}\('.format(acr_num, cem_group) +
            "         ← both legs UP, normal production cross-connect",
        "show acr group {} detail cem | include CE{}|{}".format(
            acr_num, acr_num, cem_group) +
            "   ← clock restored, working/protect healthy",
        "show facility-alarm status | include CEM",
        "",
        "# NPT-1800  {}  ({})".format(hub_hostname, circuit.get("hub_ip", "?")),
        "show interfaces {}".format(circuit.get("xe_logical", "<xe-sub-interface>")),
        "show interfaces physical pm 24hour {}".format(circuit.get("oc_base", oc12)) +
            "   ← compare to precheck baseline",
        "show ne alarms | grep {}".format(circuit.get("oc_base", oc12)),
        "",
        "# NPT-1250  {}  ({})".format(host_1250, circuit.get("ip_1250", "?")),
        "show ne alarms | grep ds1",
        'show interfaces physical status slot {} | grep "ds1/{} "'.format(
            ds1_slot, ds1_port),
        "show interfaces physical pm 15min ds1-{}".format(ds1) +
            "          ← compare delta to precheck baseline",
        "show interfaces physical pm 24hour ds1-{}".format(ds1),
        "",
        "=" * 78,
        "END OF SCRIPT",
    ]

    return "\n".join(lines)


# ---- file I/O ----------------------------------------------------------

def _write(script_text, circuit_id):
    safe = re.sub(r'[^\w.-]', '_', circuit_id)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("MUX_Loop_Script_{}_{}.txt".format(safe, ts))
    try:
        with open(out_path, "w") as f:
            f.write(script_text)
        print("\n[written] {}".format(out_path))
        return out_path
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None


def main():
    print("=" * 64)
    print("  MUX CEM LOOP SCRIPT GENERATOR")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
