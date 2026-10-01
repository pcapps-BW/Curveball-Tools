#!/usr/bin/env python3
"""
autotester_standalone.py -- T1 Route Tracer w/ PMs (standalone orchestrator).

One launch -> prompt for creds once -> walk the full route for each DS1:
    1250 (MUSE/hub/loopback + DS1 PMs)
    1800 (MUSE -> OC12 + OC12 stats)   [grouped per hub]
    MUX  (Netbox reconciliation -> SONET/ACR/CEM + GSX assignment + T3)  [per loc]
    GSX  (plink pull of T1 stats)      [non-Peerless only; assignment FROM the MUX]
-> polished .txt matching the SecureCRT Autotester format:
   RUN SUMMARY -> DIAGNOSTIC SUMMARY -> ROUTE BLOCKS -> SHARED INFRA STATS ->
   PER-DS1 PMs.

Data flows in-memory hop to hop -- nothing about downstream devices is entered
by hand. Shared creds for 1250/1800/MUX; separate creds for GSX.

Built by P. Capps.
"""

import sys
import getpass
import datetime

import platform_paths
from netbox_client import NetboxClient, load_netbox_config
from npt1250_client import NPT1250Client
from device_base import CredentialError, prompt_ip
from npt1800_client import NPT1800Client
from mux4216_client import MUX4216Client
from gsx_client import (GSXClient, find_plink, find_live_ip,
                        connect_first_live)
from output_builders import (
    build_route_block, build_peerless_block,
    build_diagnostic_summary, run_summary_line, SEP, SEP2,
    resolve_nni_from_description, build_jira_summary,
)
from jira_client import post_report, valid_ticket, post_slack, JIRA_BASE
from ds1_util import expand_ds1_list

# loc -> MUX (from SecureCRT LOC_TO_MUX)
LOC_TO_MUX = {
    "atl": {"name": "ATL1-MUX-02", "ip": "10.243.35.11"},
    "jfk": {"name": "JFK1-MUX-02", "ip": "192.168.246.19"},
    "dfw": {"name": "DFW1-MUX-04", "ip": "10.243.1.17"},
    "lax": {"name": "LAX1-MUX-02", "ip": "10.243.65.4"},
}


# Launcher menu metadata (read by launcher.py auto-discovery).
TOOL_INFO = {
    'name': 'Autotester',
    'ran_from': '1250',
    'desc': 'Traces a T1 circuit end-to-end (1250 -> 1800 -> MUX -> GSX), pulls PMs at each hop, and flags faults.',
    'requires': '1250 IP, DS1(s), infrastructure creds, GSX creds',
    'order': 10,
}


def collect_inputs(session=None):
    """Prompt for inputs; return params dict (or None to abort)."""
    session = session if session is not None else {}

    ip_1250 = prompt_ip("1250 IP", default=session.get("ip_1250"))

    default_ds1 = session.get("ds1_list")
    prompt = "DS1(s) comma-sep (e.g. ts3/8,ts3/9)"
    if default_ds1:
        prompt += " [{}]".format(",".join(default_ds1))
    ds1_raw = input(prompt + ": ").strip()
    if not ds1_raw and default_ds1:
        ds1_list = list(default_ds1)
    else:
        # Normalize: lowercase + accept & as an alternate separator
        ds1_list, _ds1_notes = expand_ds1_list(ds1_raw)
    if not ip_1250 or not ds1_list:
        print("Need a 1250 IP and at least one DS1.")
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

    from secure_config import load_config_silent, save_config_key
    slack_webhook = load_config_silent().get("SLACK_WEBHOOK", "")
    if not slack_webhook:
        raw = input("\nSlack webhook URL (leave blank to skip): ").strip()
        if raw:
            slack_webhook = raw
            save_config_key("SLACK_WEBHOOK", slack_webhook)

    print("\n-- Credentials --")
    infra_user = session.get("infra_user")
    infra_pass = session.get("infra_pass")
    if not (infra_user and infra_pass):
        infra_user = input("Infrastructure username (1250/1800/MUX): ").strip()
        infra_pass = getpass.getpass("Infrastructure password: ")
    gsx_user = session.get("gsx_user")
    gsx_pass = session.get("gsx_pass")
    if not (gsx_user and gsx_pass):
        gsx_user = input("GSX username: ").strip()
        gsx_pass = getpass.getpass("GSX password: ")
    if not (infra_user and infra_pass and gsx_user and gsx_pass):
        print("Missing credentials.")
        return None

    return {
        "ip_1250": ip_1250,
        "ds1_list": ds1_list,
        "infra_user": infra_user,
        "infra_pass": infra_pass,
        "gsx_user": gsx_user,
        "gsx_pass": gsx_pass,
        "jira_ticket": jira_ticket,
        "open_when_done": open_when_done,
        "slack_webhook": slack_webhook,
    }


def run(params):
    """Execute the full route trace given a params dict (no prompting)."""
    ip_1250 = params["ip_1250"]
    ds1_list = params["ds1_list"]
    infra_user = params["infra_user"]
    infra_pass = params["infra_pass"]
    gsx_user = params["gsx_user"]
    gsx_pass = params["gsx_pass"]
    jira_ticket = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", True)

    # Netbox
    nb_url, nb_token = load_netbox_config()
    netbox = NetboxClient(nb_url, nb_token)
    print("\nNetbox config loaded: {}".format(netbox.ok()))

    plink_path = find_plink()
    print("plink found: {}".format(bool(plink_path)))

    # Two output streams:
    #   progress()  -> console only (live hop-by-hop status; kept OUT of the file)
    #   emit()      -> BOTH the console and the .txt report (the full polished output)
    report = []
    def progress(s=""):
        print(s)
    def emit(s=""):
        report.append(s)
        print(s)

    # ---------- HOP 1: 1250 ----------
    progress("\n" + "=" * 64)
    progress("  HOP 1 -- NPT-1250 ({})".format(ip_1250))
    progress("=" * 64)
    c1250 = NPT1250Client(ip_1250, infra_user, infra_pass)
    try:
        c1250.connect()
    except CredentialError:
        raise
    except Exception as e:
        progress("[!] 1250 connect failed: {}: {}".format(type(e).__name__, e))
        return _write(["1250 connect FAILED: {}: {}".format(type(e).__name__, e)],
               "connect_fail")
    host_1250 = c1250.hostname
    progress("  host: {}".format(host_1250))

    ds1_map, step1_errors = c1250.query_batch(ds1_list)
    for ds1, info in ds1_map.items():
        progress("  {} -> MUSE {} | hub {} | loopback {}".format(
            ds1, info["muse_id"], info["hub_ip"], info["loopback_ip"]))
    for ds1, err in step1_errors.items():
        progress("  {} -> ERROR: {}".format(ds1, err))

    ds1_pms = c1250.query_ds1_pms(list(ds1_map.keys()))

    # Fiber/IXC uplink health for the DIAGNOSTIC SUMMARY (per-1250, read-only).
    #   NNI physical  -> ge-tsX/1        (errors, laser)
    #   EVC logical   -> ge-tsX/1.0      (Rx discards)
    # IN-SERVICE GATE: a fiber port is only health-checked if its .0 sub-interface
    # is configured (all uplink config lives on ge-tsX/1.0). The sub-int appears
    # in "show interfaces" regardless of up/down, so a configured-but-down port
    # is still checked and flagged (real fault); a truly unused port with no .0
    # is skipped entirely (avoids false -40 dBm "dark port" noise).
    try:
        intf_summary = c1250.run("show interfaces | no-more")
    except Exception:
        intf_summary = ""
    gate_ok = bool(intf_summary.strip())

    fiber_results = {}
    evc_results = {}
    for port, slot in (("ge-ts2/1", "ts2"), ("ge-ts5/1", "ts5")):
        subif = "{}.0".format(port)
        # Skip a port with no configured sub-interface. If the summary pull
        # failed, don't gate (check anyway) rather than silently drop the check.
        if gate_ok and subif not in intf_summary:
            progress("  {} has no sub-interface -- not in service, skipping".format(port))
            continue
        try:
            pm24 = c1250.run("show interfaces physical pm 24hour {} | no-more".format(port))
            pm15 = c1250.run("show interfaces physical pm 15min {} | no-more".format(port))
            laser = c1250.run(
                "show chassis transceiver information slot {} port1 | grep laser | no-more".format(slot))
            tca = c1250.run(
                "show chassis transceiver tca slot {} port1 | no-more".format(slot))
            combined_pm = (pm24 or "") + "\n" + (pm15 or "")
            if combined_pm.strip() and "Monitored Seconds" in combined_pm:
                fiber_results[port] = {"pm": combined_pm, "laser": laser or "",
                                       "tca": tca or ""}
            evc = c1250.run("show interfaces pm 24hour {} | no-more".format(subif))
            if evc and "Monitored Seconds" in evc:
                evc_results[subif] = evc
        except Exception as e:
            progress("  [!] fiber/EVC read {} failed: {}: {}".format(
                port, type(e).__name__, e))

    # OSPF adjacency (event-log derived -- this NPT-1250 image has no direct
    # uptime field on 'show ospf interface/neighbor', confirmed live
    # 2026-08-07) + LDP session uptime (a real device-reported field). Per-
    # 1250, shared across all DS1s in this run, same as the fiber check above.
    try:
        ospf_events_raw = c1250.run("show ne events unreported | no-more")
    except Exception as e:
        ospf_events_raw = ""
        progress("  [!] OSPF events read failed: {}: {}".format(type(e).__name__, e))
    try:
        ldp_session_raw = c1250.run("show ldp session | no-more")
    except Exception as e:
        ldp_session_raw = ""
        progress("  [!] LDP session read failed: {}: {}".format(type(e).__name__, e))

    c1250.close()

    if not ds1_map:
        progress("[!] No valid DS1s on this 1250. Exiting.")
        return _write(["No valid DS1s found on {}.".format(host_1250)], host_1250)

    # ---------- HOP 2: 1800 (grouped by hub IP) ----------
    progress("\n" + "=" * 64)
    progress("  HOP 2 -- NPT-1800")
    progress("=" * 64)

    # group ds1s by hub ip
    hub_groups = {}
    for ds1, info in ds1_map.items():
        hub_groups.setdefault(info["hub_ip"], {})[ds1] = info

    oc12_results   = {}   # ds1 -> oc12
    oc12_stats_all = {}   # key -> stats text
    hub_hostnames  = {}   # ds1 -> 1800 hostname
    loc_codes      = {}   # ds1 -> loc
    peerless_all   = {}   # ds1 -> slot (customer)
    hub_results    = {}   # ds1 -> {hub_name, hub_ip, hub_hostname} (for builders)
    for hub_ip, group in hub_groups.items():
        c1800 = NPT1800Client(hub_ip, infra_user, infra_pass)
        try:
            c1800.connect()
        except CredentialError:
            raise
        except Exception as e:
            progress("[!] 1800 {} connect failed: {}: {}".format(hub_ip, type(e).__name__, e))
            continue
        (oc12_map, oc12_stats, hub_name, hub_hostname,
         loc_code, errs, peerless_cust) = c1800.query_batch(hub_ip, group)

        # NNI/EVC resolution on the 1800 (per-1250; all this 1250's DS1s share
        # the same fiber uplink -> same NNI). Read-only. Uses the 1250 hostname:
        # strip the last 4 chars (trailing LATA digits) and grep the 1800's
        # interface descriptions for that site prefix.
        nni_result = {}
        prefix = host_1250[:-4] if len(host_1250) > 4 else host_1250
        try:
            desc = c1800.run(
                "show interfaces description | grep {} | no-more".format(prefix))
            logical, physical, status = resolve_nni_from_description(desc)
            if status == "ambiguous":
                nni_result = {"status": "ambiguous", "prefix": prefix}
                progress("  NNI ambiguous: multiple matches for '{}' -- skipping".format(prefix))
            elif status == "none":
                nni_result = {"status": "none", "prefix": prefix}
                progress("  NNI not found for '{}' on 1800".format(prefix))
            else:
                phys24 = c1800.run(
                    "show interfaces physical pm 24hour {} | no-more".format(physical))
                phys15 = c1800.run(
                    "show interfaces physical pm 15min {} | no-more".format(physical))
                evc24 = c1800.run(
                    "show interfaces pm 24hour {} | no-more".format(logical))
                nni_result = {
                    "status": "ok",
                    "logical": logical,
                    "physical": physical,
                    "phys_pm": (phys24 or "") + "\n" + (phys15 or ""),
                    "evc_pm": evc24 or "",
                }
                progress("  NNI {} (EVC {})".format(physical, logical))
        except Exception as e:
            nni_result = {"status": "error", "prefix": prefix}
            progress("  [!] NNI resolve/read failed: {}: {}".format(type(e).__name__, e))

        c1800.close()

        progress("  hub {} ({}) loc={}".format(hub_hostname, hub_name, loc_code))
        for ds1, oc12 in oc12_map.items():
            oc12_results[ds1] = oc12
            hub_hostnames[ds1] = hub_hostname
            loc_codes[ds1] = loc_code
            hub_results[ds1] = {"hub_name": hub_name, "hub_ip": hub_ip,
                                "hub_hostname": hub_hostname,
                                "nni": nni_result}
            progress("    {} -> {}".format(ds1, oc12))
        for ds1, err in errs.items():
            progress("    {} -> ERROR: {}".format(ds1, err))
        for k, v in oc12_stats.items():
            oc12_stats_all[k] = v
        for ds1, slot in peerless_cust.items():
            peerless_all[ds1] = slot
        if peerless_cust:
            progress("    Peerless (customer): {}".format(peerless_cust))

    # ---------- HOP 3: MUX (grouped by loc) ----------
    progress("\n" + "=" * 64)
    progress("  HOP 3 -- Cisco NCS 4216 MUX")
    progress("=" * 64)

    # group non-Peerless ds1s by loc
    mux_groups = {}
    for ds1, oc12 in oc12_results.items():
        if ds1 in peerless_all:
            continue   # Peerless has no MUX/GSX
        loc = loc_codes.get(ds1, "")
        mux_groups.setdefault(loc, {})[ds1] = oc12

    mux_results_all = {}
    t3_stats_all    = {}
    for loc, ds1_oc12_map in mux_groups.items():
        mux_info = LOC_TO_MUX.get(loc)
        if not mux_info:
            progress("  [!] No MUX mapping for loc '{}'".format(loc))
            continue
        hostmap = {d: hub_hostnames.get(d, "") for d in ds1_oc12_map}
        cmux = MUX4216Client(mux_info["ip"], infra_user, infra_pass)
        try:
            cmux.connect()
        except CredentialError:
            raise
        except Exception as e:
            progress("  [!] MUX {} connect failed: {}: {}".format(
                mux_info["ip"], type(e).__name__, e))
            continue
        mux_results, t3_stats, mn, mip, errs = cmux.query_batch(
            mux_info["name"], mux_info["ip"], ds1_oc12_map, hostmap, netbox)
        cmux.close()

        for ds1, r in mux_results.items():
            mux_results_all[ds1] = r
            progress("  {} -> SONET {} ACR {} CEM {} | GSX {}-{}-{}-{}".format(
                ds1, r["active_sonet"], r["acr_group"], r["cem_group"],
                r["gsx_node"] or "?", r["gsx_slot"] or "?",
                r["gsx_port"] or "?", r["gsx_t1"] or "?"))
            if r["netbox_conflict"]:
                progress("     FLAG: {}".format(r["netbox_conflict"]))
            if r.get("gsx_flag"):
                progress("     FLAG: {}".format(r["gsx_flag"]))
        for ds1, err in errs.items():
            progress("  {} -> ERROR: {}".format(ds1, err))
        for k, v in t3_stats.items():
            t3_stats_all[k] = v

    # ---------- HOP 4: GSX (per non-Peerless ds1, assignment FROM MUX) ----------
    progress("\n" + "=" * 64)
    progress("  HOP 4 -- GSX")
    progress("=" * 64)

    gsx_results    = {}   # ds1 -> list of (label, cmd, output)
    gsx_t3_results = {}   # ds1 -> raw SHOW T3 STATUS output (parent T3 of the T1)
    if not plink_path:
        progress("  [!] plink not found -- skipping GSX hop.")
    else:
        for ds1, r in mux_results_all.items():
            node = r.get("gsx_node")
            slot = r.get("gsx_slot")
            port = r.get("gsx_port")
            t1   = r.get("gsx_t1")
            if not (node and slot and port and t1):
                progress("  {} -> incomplete GSX assignment, skipping".format(ds1))
                continue
            host_1800 = hub_hostnames.get(ds1, "")
            gsx_dev = netbox.derive_gsx_device_name(node, host_1800)
            cards = netbox.gsx_mgmt_ips(gsx_dev)
            if not cards:
                progress("  {} -> no GSX mgmt IPs from Netbox for {}".format(ds1, gsx_dev))
                continue
            # Tries each mgmt IP until one both pings AND accepts SSH -- a GSX
            # can answer ICMP and still refuse the login (ATLGSX02, 2026-10-01).
            card_key, live_ip, g = connect_first_live(
                cards, gsx_user, gsx_pass, plink_path, progress=progress)
            if not live_ip:
                progress("  {} -> no GSX mgmt IP that pings AND accepts SSH"
                         .format(ds1))
                continue
            r["gsx_ip"] = live_ip   # stash for the route block, even if the pull below fails
            progress("  {} -> GSX {} ({}) card {} -> T1-1-{}-{}-{}".format(
                ds1, node, live_ip, card_key, slot, port, t1))
            try:
                stats = g.pull_t1_stats(slot, port, t1)
                gsx_results[ds1] = stats
                # Pull parent T3 STATUS to distinguish T3-level outage from a
                # DS1-specific failure in the run summary (one T3 ticket vs 28).
                gsx_t3_results[ds1] = g.get_t3_status(slot, port)
            except Exception as e:
                progress("     [!] GSX pull failed: {}: {}".format(type(e).__name__, e))
            finally:
                g.close()

    # ---------- STAGE B: POLISHED OUTPUT (SecureCRT order/format) ----------
    step1_errors_all = dict(step1_errors)  # ds1 -> error (1250 no-MUSE etc.)

    # Flatten GSX per-DS1 stats into a single text blob per DS1 for counter
    # review. Built here (before the run summary) so the summary line can also
    # check GSX T1 Operational/Alarm Status -- previously this was built after
    # run_summary_line was called, so those fields were never evaluated there.
    gsx_pm_results = {}
    for ds1, stats in gsx_results.items():
        gsx_pm_results[ds1] = "\n".join(
            "{}\n{}".format(cmd, out) for (label, cmd, out) in stats)

    # 1) RUN SUMMARY (fault-direction one-liners)
    run_summary_block = [SEP, "  RUN SUMMARY  --  {}".format(host_1250), SEP, ""]
    for ds1 in ds1_map:
        run_summary_block.append(run_summary_line(
            ds1, ds1_map, ds1_pms, oc12_results, hub_results,
            mux_results_all, oc12_stats_all, t3_stats_all, peerless_all,
            fiber_results, evc_results, gsx_pm_results=gsx_pm_results,
            gsx_t3_results=gsx_t3_results))
    run_summary_block += ["", SEP]
    for ln in run_summary_block:
        emit(ln)

    # 2) PER-CHANNEL: FULL ROUTE + DIAGNOSTIC SUMMARY, grouped together.
    # Each DS1's full route is emitted immediately above that same DS1's own
    # diagnostic summary -- easier to digest than two separate combined
    # sections that each span every channel.

    diag_text_parts  = []
    route_diag_parts = []   # per-DS1 "Full T1 Route + Diagnostic Summary" only
                            # (no shared infra stats / raw PMs) -- captured
                            # alongside the emit() calls below so a caller
                            # (e.g. morning_check.py, via params["_extra_out"])
                            # can get just this condensed slice instead of
                            # re-parsing it back out of the full report text.
    emit("")
    for ds1 in ds1_map:
        info = ds1_map[ds1]
        hub  = hub_results.get(ds1, {})
        oc12 = oc12_results.get(ds1, "")
        if ds1 in peerless_all:
            route_text = build_peerless_block(
                ds1, host_1250, info.get("loopback_ip", "?"), info.get("muse_id", "?"),
                hub.get("hub_name", "?"), hub.get("hub_ip", "?"), oc12,
                peerless_all[ds1])
        elif ds1 in mux_results_all:
            route_text = build_route_block(
                ds1, host_1250, info.get("loopback_ip", "?"), info.get("muse_id", "?"),
                hub.get("hub_name", "?"), hub.get("hub_ip", "?"), oc12,
                mux_results_all[ds1])
        else:
            route_text = ""
        if route_text:
            emit(route_text)
        emit("")

        ds1_diag_text = build_diagnostic_summary(
            [ds1], ds1_map, ds1_pms, oc12_results,
            hub_results, mux_results_all, oc12_stats_all,
            t3_stats_all, peerless_all, step1_errors_all, gsx_pm_results,
            fiber_results, evc_results, ospf_events_raw, ldp_session_raw,
            gsx_t3_results=gsx_t3_results)
        emit(ds1_diag_text)
        emit("")
        diag_text_parts.append(ds1_diag_text)
        route_diag_parts.append(
            (route_text + "\n\n" if route_text else "") + ds1_diag_text)

    # Kept for the JIRA payload below (Run Summary + Diagnostic Summary panel).
    diag_text = "\n".join(diag_text_parts)

    # ---- "In the weeds" stats below ----

    # 4) SHARED INFRASTRUCTURE STATS
    emit("\n" + SEP)
    emit("  SHARED INFRASTRUCTURE STATS")
    emit(SEP)

    emit("\n" + SEP2)
    emit("  NPT-1800 OC STATS")
    emit(SEP2)
    emit("")
    for k, v in oc12_stats_all.items():
        emit(v)

    # MUX section: per-DS1 facility alarms grouped, then per-T3 controller stats
    emit("\n" + SEP2)
    emit("  CISCO 4216 MUX STATS")
    emit(SEP2)
    emit("")
    emit("  >> Per-DS1 Facility Alarms")
    for ds1 in ds1_map:
        r = mux_results_all.get(ds1)
        if r and r.get("facility_alarm"):
            emit("")
            emit("  -- {} --".format(ds1.upper()))
            emit(r["facility_alarm"])

    emit("")
    emit("  >> CEM Circuit Health")
    for ds1 in ds1_map:
        r = mux_results_all.get(ds1)
        if not r:
            continue
        cem_h = r.get("cem_health") or {}
        if not cem_h:
            continue
        emit("")
        emit("  -- {} --".format(ds1.upper()))
        emit("  CEM-ACR{} {}  (conn: {})".format(
            r.get("acr_group", "?"), r.get("cem_group", "?"),
            r.get("conn_name") or "not found"))
        if cem_h.get("cem_circuit_raw"):
            for line in cem_h["cem_circuit_raw"].splitlines():
                emit("    {}".format(line.rstrip()))
        if cem_h.get("xconnect_raw"):
            emit("")
            emit("  >> Xconnect")
            for line in cem_h["xconnect_raw"].splitlines():
                if line.strip():
                    emit("    {}".format(line.rstrip()))
        if cem_h.get("connection_raw"):
            emit("")
            emit("  >> Connection State")
            for line in cem_h["connection_raw"].splitlines():
                emit("    {}".format(line.rstrip()))
        if cem_h.get("acr_detail_raw"):
            emit("")
            emit("  >> ACR Working/Protect")
            for line in cem_h["acr_detail_raw"].splitlines():
                if line.strip():
                    emit("    {}".format(line.rstrip()))

    for k, v in t3_stats_all.items():
        emit(v)

    # 5) PER DS1 PERFORMANCE MONITORS
    emit("\n" + SEP)
    emit("  PER DS1 PERFORMANCE MONITORS")
    emit(SEP)
    for ds1 in ds1_map:
        emit("\n" + SEP2)
        emit("  -- {} --".format(ds1.upper()))
        emit("")
        emit(ds1_pms.get(ds1, "  (none)"))
        if ds1 in peerless_all:
            emit("\n  (Peerless/customer circuit -- no MUX/GSX PMs)")
        elif ds1 in gsx_results:
            emit("\n  >> GSX T1 STATS")
            for label, cmd, out in gsx_results[ds1]:
                emit("  >> {}".format(label))
                emit("  CMD: {}".format(cmd))
                emit(out)
                emit("")

    out_path = _write(report, host_1250)

    # Per-DS1 brief summary -- always visible at the top of the JIRA comment
    # (each DS1 can have different symptoms), whether the full report posts
    # inline or falls back to an attachment. Computed unconditionally (not
    # just when jira_ticket is set) so a caller running this as a sub-step
    # (morning_check.py) can also read it back out via params["_extra_out"]
    # below, instead of re-deriving the same prose from the raw report text.
    jira_summary = build_jira_summary(ds1_map, run_summary_block, diag_text_parts)

    # ---------- POST TO JIRA (if requested up front) ----------
    # Same 2-expand + attachment shape as Morning Check (2026-08-06, for
    # reading-flow parity between the two JIRA-posting paths):
    #   1st expand "Findings Summary" -- the per-DS1 human-readable
    #       paragraph (jira_summary) -- the 10-second read.
    #   2nd expand "Diagnostic Summary & Full T1 Route" -- condensed
    #       per-DS1 route + diagnostic detail (route_diag_parts), NOT the
    #       RUN SUMMARY one-liners or the raw PM dumps.
    #   Per-DS1 Performance Monitors is no longer its own inline expand --
    #   it's already in the full report, which is always attached
    #   (attach_path=out_path). Switched from post_comment()/_build_adf
    #   (which had a hardcoded 2-panel results/PM shape) to post_report()
    #   -- the same general-purpose function Morning Check uses -- rather
    #   than reshaping the older one; post_comment() is left in place for
    #   mux_loop_script.py, which still uses it.
    route_diag = "\n\n".join(route_diag_parts)

    if jira_ticket and out_path:
        progress("\n[JIRA] Posting results to {} ...".format(jira_ticket))
        ok, msg = post_report(
            ticket=jira_ticket,
            intro="Autotester results for {} -- {} DS1(s) tested on {}.".format(
                host_1250, len(ds1_map),
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
            precheck_text=jira_summary,
            precheck_title="Findings Summary",
            panel_title="Diagnostic Summary & Full T1 Route",
            report_text=route_diag,
            attach_path=out_path,
        )
        progress("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    # ---------- POST TO SLACK (2026-08-06 -- same 2 pieces as JIRA) ----------
    # Independent of jira_ticket -- a webhook is a separate concern from a
    # JIRA ticket, same as Morning Check. Not gated on JIRA posting having
    # happened/succeeded.
    slack_webhook = params.get("slack_webhook")
    if slack_webhook:
        progress("\n[Slack] Posting digest ...")
        msg_parts = [
            "*Autotester -- {} -- {} DS1(s) -- {}*".format(
                host_1250, len(ds1_map),
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
        ]
        if jira_ticket:
            msg_parts.append("<{}/browse/{}|{}>".format(JIRA_BASE, jira_ticket, jira_ticket))
        msg_parts.append("")
        msg_parts.append(jira_summary)
        if route_diag.strip():
            msg_parts.append("*Diagnostic Summary & Full T1 Route:*")
            msg_parts.append("```{}```".format(route_diag))
        ok, msg = post_slack(slack_webhook, "\n".join(msg_parts))
        progress("[Slack] {}".format(msg) if ok else "[Slack] FAILED: {}".format(msg))

    # ---------- AUTO-OPEN THE RESULTS FILE ----------
    if open_when_done and out_path:
        try:
            import os as _os
            _os.startfile(out_path)   # Windows: opens in default handler
        except Exception as e:
            progress("[!] could not auto-open file: {}".format(e))

    # ---------- OPTIONAL: hand structured pieces back to a caller ----------
    # Purely additive -- only touched when the caller opts in by passing an
    # empty dict as params["_extra_out"] (morning_check.py does this). Every
    # existing caller (the web UI's Autotester route, this module's own CLI)
    # doesn't set that key, so this is a no-op for them and `return out_path`
    # below is completely unchanged -- avoids changing run()'s return type
    # for callers that already depend on it being a bare path string.
    extra = params.get("_extra_out")
    if extra is not None:
        extra["jira_summary"] = jira_summary
        extra["route_diag"]   = "\n\n".join(route_diag_parts)

    return out_path


def _write(report, host="run"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("Autotester_standalone_{}_{}.txt".format(ts, host))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(report))
        print("\n[written] {}".format(out_path))
        return out_path
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None


def main():
    print("=" * 64)
    print("  AUTOTESTER STANDALONE -- T1 Route Tracer w/ PMs")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
