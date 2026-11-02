#!/usr/bin/env python3
"""
site_sweep.py -- 1250-side site health sweep (no route trace required).

Auto-discovers every MUSE-assigned DS1 on a given NPT-1250 and runs
1250-side TDM/CES PM checks on all of them in a single pass. Also
collects GE fiber uplink health (NNI physical + EVC logical) from the
same device. No DS1 input required -- no route trace past the 1250
(no 1800 / MUX / GSX hop).

Use as a first-pass triage: see which DS1s are clean, which are dirty,
and which are worth running through the full Autotester. Runtime is
~3-5 min regardless of DS1 count (all queries are local to the 1250).

Trigger format (JIRA):   standard -- IP + infra creds
Output:  SiteSweep_<hostname>_<timestamp>.txt

Built by P. Capps.
"""

import os
import re
import sys
import datetime
import getpass

import platform_paths
import loop_check
from device_base import CredentialError, prompt_ip
from npt1250_client import NPT1250Client
from jira_client import post_report, valid_ticket, post_slack, load_jira_config
from secure_config import load_config, load_config_silent

from output_builders import (
    _check_counters, _cd, _CES_DISABLED_RE,
    CES_COUNTERS, TDM_COUNTERS,
    check_fiber_health, check_evc_health,
    check_ospf_ldp_health,
    FIBER_RX_DBM_FLOOR,
    SEP, SEP2,
)

# ── Launcher contract ─────────────────────────────────────────────────────────
TOOL_INFO = {
    'name':     'Site Sweep',
    'ran_from': 'NPT-1250',
    'desc':     'Auto-discovers all MUSE-assigned DS1s on a 1250 and checks '
                '1250-side TDM/CES PM health for each — no DS1 input required. '
                'Includes GE fiber uplink and EVC health. Use to triage a site '
                'before running a full Autotester trace.',
    'requires': 'infra creds',
    'order':    7,
}

OUTPUT_DIR  = platform_paths.BASE_DIR


# ── DS1 auto-discovery ────────────────────────────────────────────────────────
def _discover_ds1s(c1250):
    """
    Pull all MUSE-assigned DS1s from the device config in one command.
    Returns (ds1_list, loopback_ip, config_map).
      ds1_list   -- sorted by slot then channel (ts1/1 before ts1/5 before ts3/2)
      loopback_ip -- lo0 address, or "unknown"
      config_map  -- {ds1: {"muse_id": ..., "hub_ip": ...}}
    """
    raw_cfg = c1250.run(
        'show configuration | display-set | match "peer-address" | no-more')
    config_map = {}
    for line in raw_cfg.splitlines():
        m_iface = re.search(r'(ds1-ts\d+/\d+) ', line)
        m_muse  = re.search(r'(MUSE_\d+)', line)
        m_peer  = re.search(r'peer-address\s+([\d.]+)', line)
        if m_iface and m_muse and m_peer:
            key = m_iface.group(1).replace("ds1-", "")
            config_map[key] = {
                "muse_id": m_muse.group(1),
                "hub_ip":  m_peer.group(1),
            }

    raw_lo = c1250.run(
        'show configuration | display-set | match "lo0" | no-more')
    loopback_ip = "unknown"
    for line in raw_lo.splitlines():
        m = re.search(r'lo0.*inet.*address\s+([\d.]+)', line, re.IGNORECASE)
        if m:
            loopback_ip = m.group(1)
            break

    def _sort_key(d):
        parts = d.replace("ts", "").split("/")
        try:
            return (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return (999, 999)

    ds1_list = sorted(config_map.keys(), key=_sort_key)
    return ds1_list, loopback_ip, config_map


# ── Fiber uplink collection ───────────────────────────────────────────────────
def _collect_fiber(c1250):
    """
    GE fiber uplink PMs + laser readings for ge-ts2/1 and ge-ts5/1.
    Mirrors the HOP 1 fiber block in autotester_standalone.py exactly --
    in-service gate: only checks ports that have a configured .0 sub-interface.
    Returns (fiber_results, evc_results) in the shape check_fiber_health()
    and check_evc_health() expect.
    """
    fiber_results = {}
    evc_results   = {}
    try:
        intf_summary = c1250.run("show interfaces | no-more")
    except Exception:
        intf_summary = ""
    gate_ok = bool(intf_summary.strip())

    for port, slot in (("ge-ts2/1", "ts2"), ("ge-ts5/1", "ts5")):
        subif = "{}.0".format(port)
        if gate_ok and subif not in intf_summary:
            print("  {} -- no .0 sub-interface, not in service, skipping".format(port))
            continue
        try:
            pm24  = c1250.run(
                "show interfaces physical pm 24hour {} | no-more".format(port))
            pm15  = c1250.run(
                "show interfaces physical pm 15min {} | no-more".format(port))
            laser = c1250.run(
                "show chassis transceiver information slot {} port1 | grep laser | no-more"
                .format(slot))
            tca = c1250.run(
                "show chassis transceiver tca slot {} port1 | no-more".format(slot))
            combined = (pm24 or "") + "\n" + (pm15 or "")
            if combined.strip() and "Monitored Seconds" in combined:
                fiber_results[port] = {"pm": combined, "laser": laser or "",
                                       "tca": tca or ""}
            evc = c1250.run(
                "show interfaces pm 24hour {} | no-more".format(subif))
            if evc and "Monitored Seconds" in evc:
                evc_results[subif] = evc
        except Exception as e:
            print("  [!] fiber/EVC read {} failed: {}: {}".format(
                port, type(e).__name__, e))
    return fiber_results, evc_results


# ── OSPF / LDP collection ─────────────────────────────────────────────────────
def _collect_ospf_ldp(c1250):
    """
    OSPF adjacency (event-log derived -- confirmed live 2026-08-07 this
    platform has no direct uptime field) + LDP session uptime (real
    device-reported field). Mirrors the same collection autotester_standalone
    does on the 1250 hop.
    Returns (ospf_events_raw, ldp_session_raw).
    """
    try:
        ospf_events_raw = c1250.run("show ne events unreported | no-more")
    except Exception as e:
        ospf_events_raw = ""
        print("  [!] OSPF events read failed: {}: {}".format(type(e).__name__, e))
    try:
        ldp_session_raw = c1250.run("show ldp session | no-more")
    except Exception as e:
        ldp_session_raw = ""
        print("  [!] LDP session read failed: {}: {}".format(type(e).__name__, e))
    return ospf_events_raw, ldp_session_raw


# ── Per-DS1 verdict ───────────────────────────────────────────────────────────
def _assess_ds1(ds1, pm):
    """
    Derive TDM and CES clean/dirty verdicts from 1250-side PM text.
    Returns (tdm_cd, ces_cd, tdm_flags, ces_flags).
    Matches the exact same logic run_summary_line() uses for the 1250 hop.
    """
    phys_los  = bool(re.search(r'ds1/\d+\s+LOS', pm))
    alarm_los = bool(re.search(r'DS1LOS-L', pm) and
                     re.search(r'port-name=ds1-{}'.format(re.escape(ds1)),
                               pm, re.IGNORECASE))
    tdm_hits  = _check_counters(pm, TDM_COUNTERS)

    if phys_los or alarm_los:
        tdm_cd    = _cd(True, "DS1LOS")
        tdm_flags = ["DS1LOS"]
    elif tdm_hits:
        tdm_cd    = _cd(True, tdm_hits[0].split(":")[0].strip())
        tdm_flags = tdm_hits
    else:
        tdm_cd    = _cd(False)
        tdm_flags = []

    if _CES_DISABLED_RE.search(pm):
        ces_cd    = "Disabled"
        ces_flags = []
    else:
        ces_hits = _check_counters(pm, CES_COUNTERS)
        if ces_hits:
            ces_cd    = _cd(True, ces_hits[0].split(":")[0].strip())
            ces_flags = ces_hits
        else:
            ces_cd    = _cd(False)
            ces_flags = []

    return tdm_cd, ces_cd, tdm_flags, ces_flags


# ── Report builder ────────────────────────────────────────────────────────────
def _build_report(host_1250, ip_1250, loopback_ip, ds1_list, config_map,
                  pm_results, fiber_results, evc_results, stamp,
                  ospf_events_raw=None, ldp_session_raw=None):
    """
    Build the full text report and return:
      (full_text, fiber_flags, evc_flags, ospf_ldp_flags, dirty_ds1s, sweep_results)
    sweep_results: {ds1: (tdm_cd, ces_cd, tdm_flags, ces_flags)}
    """
    out = []

    def emit(s=""):
        out.append(s)

    emit(SEP)
    emit("  SITE SWEEP  --  {}  ({})".format(host_1250, ip_1250))
    emit("  {}".format(stamp))
    emit(SEP)
    emit("  Loopback : {}".format(loopback_ip))
    emit("  DS1s     : {} MUSE-assigned".format(len(ds1_list)))
    emit(SEP)
    emit("")

    # ── Fiber uplink ──────────────────────────────────────────────────────
    fiber_flags, fiber_info = (check_fiber_health(fiber_results)
                               if fiber_results else ([], []))
    evc_flags = check_evc_health(evc_results) if evc_results else []

    emit(SEP)
    emit("  GE FIBER UPLINK HEALTH")
    emit(SEP)
    if not fiber_results and not evc_results:
        emit("  (No in-service GE uplink ports found.)")
    else:
        if fiber_flags:
            for f in fiber_flags:
                emit("  [ !! ] NNI {}".format(f))
        else:
            emit("  [ OK ] NNI PMs clean")
        for i in fiber_info:
            emit("         {}".format(i))
        if evc_flags:
            for f in evc_flags:
                emit("  [ !! ] EVC {}".format(f))
        else:
            emit("  [ OK ] EVC PMs clean")
    emit("")

    # ── OSPF / LDP ────────────────────────────────────────────────────────
    ospf_ldp_flags, ospf_ldp_info = check_ospf_ldp_health(ospf_events_raw, ldp_session_raw)

    emit(SEP)
    emit("  OSPF / LDP HEALTH")
    emit(SEP)
    if not ospf_events_raw and not ldp_session_raw:
        emit("  (No OSPF/LDP data collected.)")
    else:
        if ospf_ldp_flags:
            for f in ospf_ldp_flags:
                emit("  [ !! ] {}".format(f))
        for i in ospf_ldp_info:
            emit("  [ OK ] {}".format(i))
    emit("")

    # ── Sweep table ───────────────────────────────────────────────────────
    emit(SEP)
    emit("  DS1 STATUS SWEEP  --  1250-side TDM / CES")
    emit(SEP)

    sweep_results = {}
    dirty_ds1s    = []

    for ds1 in ds1_list:
        pm = pm_results.get(ds1, "")
        tdm_cd, ces_cd, tdm_flags, ces_flags = _assess_ds1(ds1, pm)
        sweep_results[ds1] = (tdm_cd, ces_cd, tdm_flags, ces_flags)

        is_dirty = ("Dirty" in tdm_cd or "Dirty" in ces_cd)
        if is_dirty:
            dirty_ds1s.append(ds1)
        _loop = config_map.get(ds1, {}).get("loop")
        icon = "[ !! ]" if (is_dirty or _loop) else "[ OK ]"
        muse = config_map.get(ds1, {}).get("muse_id", "?")
        _loop_tag = ("  LOOP:{}".format(
            ",".join(x["type"] for x in _loop)) if _loop else "")
        emit("  {}  {:<8}  TDM {} / CES {}  ({}){}".format(
            icon, ds1.upper(), tdm_cd, ces_cd, muse, _loop_tag))

    emit("")
    if dirty_ds1s:
        emit("  {} of {} DS1(s) flagged: {}".format(
            len(dirty_ds1s), len(ds1_list),
            ", ".join(d.upper() for d in dirty_ds1s)))
    else:
        emit("  All {} DS1(s) clean on 1250-side.".format(len(ds1_list)))
    emit("")

    # ── Per-DS1 PM detail ─────────────────────────────────────────────────
    emit(SEP)
    emit("  PER-DS1 PM DETAIL")
    emit(SEP)
    for ds1 in ds1_list:
        pm = pm_results.get(ds1, "")
        emit("")
        emit(SEP2)
        emit("  {}  --  {}  (hub {})".format(
            ds1.upper(),
            config_map.get(ds1, {}).get("muse_id", "?"),
            config_map.get(ds1, {}).get("hub_ip", "?")))
        emit(SEP2)
        if pm:
            emit(pm)
        else:
            emit("  (no PM data collected)")

    return ("\n".join(out), fiber_flags, evc_flags, ospf_ldp_flags,
            dirty_ds1s, sweep_results)


# ── JIRA/Slack summary ────────────────────────────────────────────────────────
def _build_summary(host_1250, ip_1250, ds1_list, sweep_results,
                   fiber_flags, evc_flags, ospf_ldp_flags, dirty_ds1s, stamp):
    """Compact always-visible summary for the JIRA comment and Slack post."""
    lines = [
        "Site Sweep -- {} ({}) -- {}".format(host_1250, ip_1250, stamp),
        "",
        "Checked {} MUSE-assigned DS1(s).".format(len(ds1_list)),
        "",
    ]
    if fiber_flags:
        lines.append("GE Uplink: [ !! ] {}".format("; ".join(fiber_flags)))
    elif evc_flags:
        lines.append("GE Uplink: [ !! ] EVC {}".format("; ".join(evc_flags)))
    else:
        lines.append("GE Uplink: [ OK ] clean")

    if ospf_ldp_flags:
        lines.append("OSPF/LDP: [ !! ] {}".format("; ".join(ospf_ldp_flags)))
    else:
        lines.append("OSPF/LDP: [ OK ] see report for uptime detail")
    lines.append("")

    if dirty_ds1s:
        lines.append("{} of {} DS1(s) flagged:".format(
            len(dirty_ds1s), len(ds1_list)))
        for ds1 in dirty_ds1s:
            tdm_cd, ces_cd, _, _ = sweep_results[ds1]
            lines.append("  {} -- TDM {} / CES {}".format(
                ds1.upper(), tdm_cd, ces_cd))
    else:
        lines.append("All {} DS1(s) clean on 1250-side.".format(len(ds1_list)))

    return "\n".join(lines)


# ── Launcher contract ─────────────────────────────────────────────────────────
def collect_inputs(session=None):
    session = session or {}
    cfg = load_config()
    print("\n-- Site Sweep --")

    ip_1250 = prompt_ip("1250 IP")
    if not ip_1250:
        return None

    username = (cfg.get("INFRA_USER", "")
                or session.get("username", "")
                or session.get("infra_user", ""))
    password = (cfg.get("INFRA_PASS", "")
                or session.get("password", "")
                or session.get("infra_pass", ""))
    if not (username and password):
        print("\n-- Infrastructure Credentials --")
        if not username:
            username = input("Username: ").strip()
        if not password:
            password = getpass.getpass("Password: ")
        if not (username and password):
            print("Credentials required.")
            return None

    raw = input("\nPost to JIRA? (NOE-XXXX or leave blank): ").strip().upper()
    jira_ticket = raw if (raw and valid_ticket(raw)) else None

    open_when_done = (
        input("Open results file when done? (y/n) [n]: ").strip().lower()
        in ("y", "yes"))

    return {
        "ip_1250":        ip_1250,
        "username":       username,
        "password":       password,
        "jira_ticket":    jira_ticket,
        "open_when_done": open_when_done,
        # session harvest keys (match infra_user/infra_pass convention)
        "infra_user":     username,
        "infra_pass":     password,
    }


def run(params):
    """
    Execute site sweep. No prompting -- all inputs come from params.
    Returns out_path (str) on success, None on connect failure.
    """
    ip_1250        = params["ip_1250"]
    username       = params["username"]
    password       = params["password"]
    jira_ticket    = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", False)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print("\n" + SEP)
    print("  SITE SWEEP  --  {}".format(ip_1250))
    print("  {}".format(stamp))
    print(SEP)

    # ── Connect ───────────────────────────────────────────────────────────
    print("\n[1250] Connecting to {}...".format(ip_1250))
    c1250 = NPT1250Client(ip_1250, username, password)
    try:
        c1250.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] Connect failed: {}: {}".format(type(e).__name__, e))
        return None

    # try/finally added 2026-10-01. Only the no-DS1s path closed the session;
    # an exception in discovery, PM collection, fiber or OSPF/LDP returned
    # without closing. The explicit close() in the no-DS1s branch is now
    # redundant and removed -- two places closing one session is how a
    # double-close or a missed close happens.
    try:
        host_1250 = c1250.hostname
        print("  Host: {}".format(host_1250))

        # ── Discover DS1s ─────────────────────────────────────────────────
        print("[1250] Discovering MUSE-assigned DS1s...")
        ds1_list, loopback_ip, config_map = _discover_ds1s(c1250)
        if not ds1_list:
            print("[!] No MUSE-assigned DS1s found on this device.")
            return None
        print("  Found {} DS1(s): {}".format(
            len(ds1_list), ", ".join(d.upper() for d in ds1_list)))

        # ── DS1 PMs ───────────────────────────────────────────────────────
        print("[1250] Collecting DS1 PMs ({} DS1s)...".format(len(ds1_list)))
        pm_results = c1250.query_ds1_pms(ds1_list)

        # ── Loop detection ────────────────────────────────────────────────
        # A looped DS1 reads clean PMs, so flag it before the clean/dirty
        # verdict. One grep; stash per-DS1 into config_map (already threaded).
        try:
            _lraw = c1250.run(loop_check.LOOP_CONFIG_CMD)
            for _d in ds1_list:
                _lp = loop_check.loops_for_ds1(_lraw, _d)
                if _lp:
                    config_map.setdefault(_d, {})["loop"] = _lp
        except Exception as e:
            print("  [!] loop check skipped: {}: {}".format(type(e).__name__, e))

        # ── Fiber uplink ──────────────────────────────────────────────────
        print("[1250] Collecting fiber uplink health...")
        fiber_results, evc_results = _collect_fiber(c1250)

        # ── OSPF / LDP ────────────────────────────────────────────────────
        print("[1250] Collecting OSPF/LDP health...")
        ospf_events_raw, ldp_session_raw = _collect_ospf_ldp(c1250)
    finally:
        c1250.close()

    # ── Build report ──────────────────────────────────────────────────────
    print("\nBuilding report...")
    full_text, fiber_flags, evc_flags, ospf_ldp_flags, dirty_ds1s, sweep_results = \
        _build_report(host_1250, ip_1250, loopback_ip, ds1_list, config_map,
                      pm_results, fiber_results, evc_results, stamp,
                      ospf_events_raw, ldp_session_raw)

    summary_text = _build_summary(
        host_1250, ip_1250, ds1_list, sweep_results,
        fiber_flags, evc_flags, ospf_ldp_flags, dirty_ds1s, stamp)

    # Print sweep table to console
    for line in summary_text.splitlines():
        print(line)

    # ── Write file ────────────────────────────────────────────────────────
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(
        OUTPUT_DIR, "SiteSweep_{}_{}.txt".format(host_1250, ts))
    try:
        with open(out_path, "w") as f:
            f.write(full_text)
        print("\n  Written: {}".format(out_path))
    except Exception as e:
        print("[!] Write failed: {}".format(e))
        out_path = None

    if open_when_done and out_path and os.path.exists(out_path):
        try:
            os.startfile(out_path)
        except Exception:
            pass

    # ── JIRA ──────────────────────────────────────────────────────────────
    if jira_ticket:
        try:
            jira_email, jira_token = load_jira_config()
        except Exception as e:
            print("[JIRA] Could not load credentials: {}".format(e))
            jira_email = jira_token = None
        if jira_email and jira_token:
            print("\n[JIRA] Posting to {}...".format(jira_ticket))
            ok, msg = post_report(
                ticket=jira_ticket,
                intro="Site Sweep -- {} -- {} -- {}".format(
                    host_1250, ip_1250, stamp),
                panel_title="Site Sweep -- DS1 PM Detail",
                report_text=full_text,
                email=jira_email,
                token=jira_token,
                attach_path=out_path,
                summary_text=summary_text,
            )
            print("[JIRA] {}".format(msg if ok else "FAILED: " + msg))
        else:
            print("[JIRA] Skipped -- no credentials.")

    # ── Slack ─────────────────────────────────────────────────────────────
    try:
        cfg = load_config_silent()
        slack_webhook = cfg.get("SLACK_WEBHOOK", "")
    except Exception:
        slack_webhook = ""
    if slack_webhook:
        print("\n[Slack] Posting...")
        slack_msg = (
            "*Site Sweep -- {}* ({})\n_{}_\n\n```{}```".format(
                host_1250, ip_1250, stamp, summary_text))
        ok, msg = post_slack(slack_webhook, slack_msg)
        print("[Slack] {}".format(msg if ok else "FAILED: " + msg))

    return out_path


def main():
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
