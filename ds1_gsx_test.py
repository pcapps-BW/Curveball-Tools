#!/usr/bin/env python3
"""
ds1_gsx_test.py -- DS1 Test Builder: AIS / Loops toward the GSX.

Single-page DS1 testing workflow:
  1. Resolve the full route per DS1 (1250 -> 1800 -> MUX -> GSX), same
     chain the Autotester and MUX Loop tool use.
  2. Extract the EXACT chassis tokens (slot / ces-card type / port /
     sts-1 / vt-15) from each device's own config render -- never guessed.
     Every generated change command is stamped VERIFIED (with the config
     line it was checked against) or UNVERIFIED (with what was searched).
  3. Generate a copy/paste-ready script: live prechecks (real output
     embedded), GSX counter clears, TEST ON commands, monitoring
     commands, TEST OFF / back-out commands, postchecks.

NOTHING config-touching is ever executed by this tool -- reads only.
The generated commands are for the user to run manually.

!!! The 1800 AIS commands are flagged UNVERIFIED-SYNTAX per P. Capps --
    review before first production use. !!!

Command reference (provided/verified by P. Capps 2026-08-05):
  1250 AIS:  set|delete chassis slot <ts> ces-card <card> port ds1/<n>
                 ds1-path force-ais-psn-bound            (AIS toward GSX)
                 ds1-path force-defect-tdm-bound ais     (AIS toward LEC)
  1250 LOOP: set chassis slot <ts> ces-card <card> port ds1/<n>
                 maintenance loopback facility|terminal|release
                 (facility faces LEC; terminal faces BW core)
  1800 AIS (SYNTAX UNVERIFIED):
             set chassis slot <ts> ces-card <card> port <ocN>/<p>
                 sts-1 <s> vt-15 <v> ds1 force-ais-psn-bound   (toward 1250)
                 ... force-defect-tdm-bound ais                (toward MUX/GSX)
                 ... disable                                   (both directions)
  1800 LOOP: set chassis slot <ts> ces-card <card> port <ocN>/<p>
                 sts-1 <s> vt-15 <v> ds1 maintenance loopback
                 terminal|facility|release
  GSX CLEAR: CONFIGURE T1 T1-1-S-P-N PERFMON RESET COUNTERS ALL

Built by P. Capps.
"""

import re
import getpass
import datetime

import platform_paths
from netbox_client import NetboxClient, load_netbox_config
from device_base import CredentialError, prompt_ip
from jira_client import post_report, valid_ticket

from npt1250_client import NPT1250Client
from npt1800_client import NPT1800Client
from mux4216_client import MUX4216Client
from gsx_client import GSXClient, find_plink, find_live_ip

# Same MUX mapping the loop tool uses.
from mux_loop_script import LOC_TO_MUX, _capture, _section, _ds1_parts
from ds1_util import expand_ds1_list

TOOL_INFO = {
    'name':     'DS1 Test Builder (AIS / Loops to GSX)',
    'ran_from': '1250',
    'desc':     ('Resolves the full route, runs live prechecks, then generates a '
                 'copy/paste test script: GSX counter clears, AIS or loopback '
                 'up/down commands (1250 or 1800), monitoring, back-out, postchecks. '
                 'No config changes are executed.'),
    'requires': '1250 IP + DS1(s), test type (AIS/loop) + direction, infra + GSX creds',
    'order':    70,
}

SEP  = "=" * 70
SEP2 = "-" * 70

# Valid selections (form/CLI validate against these).
DEVICES    = ("1250", "1800")
TESTS      = ("ais", "loop")
AIS_DIRS_1250 = {
    "toward_gsx": ("force-ais-psn-bound",        "AIS toward GSX (PSN side)"),
    "toward_lec": ("force-defect-tdm-bound ais", "AIS toward LEC (TDM side)"),
}
AIS_DIRS_1800 = {
    "toward_1250": ("force-ais-psn-bound",        "AIS toward 1250 (PSN side)"),
    "toward_gsx":  ("force-defect-tdm-bound ais", "AIS toward MUX/GSX (TDM side)"),
    "both":        ("disable",                    "DS1 disabled (AIS both directions)"),
}
LOOP_DIRS_1250 = {
    "facility": ("facility", "Facility loop -- faces the LEC"),
    "terminal": ("terminal", "Terminal loop -- faces the BW core (toward GSX)"),
}
LOOP_DIRS_1800 = {
    "facility": ("facility", "Facility loop -- typically loops the OC/line side "
                             "back toward the MUX/GSX (verify orientation)"),
    "terminal": ("terminal", "Terminal loop -- typically loops the packet side "
                             "back toward the 1250 (verify orientation)"),
}


# ---- chassis token extraction (never guess -- read the device config) ----

def extract_1250_chassis(config_text, ds1_slot, ds1_port):
    """
    Find 'chassis slot <ts> ces-card <card>' for this DS1's slot in the
    1250's own config render. Returns (card, verified_line or None).
    """
    pat = re.compile(
        r'chassis slot {} ces-card (\S+)'.format(re.escape(ds1_slot)))
    for ln in (config_text or "").splitlines():
        m = pat.search(ln)
        if m:
            # Prefer a line that names this exact DS1 port, else any slot line.
            if "port ds1/{} ".format(ds1_port) in ln + " ":
                return m.group(1), ln.strip()
    for ln in (config_text or "").splitlines():
        m = pat.search(ln)
        if m:
            return m.group(1), ln.strip()
    return None, None


def extract_1800_chassis(config_text, expect_sts=None, expect_vt=None):
    """
    Find 'chassis slot <ts> ces-card <card> port <ocN/p> sts-1 <s> vt-15 <v> ds1'
    in the 1800's config render for this circuit. When expect_sts/expect_vt
    (from the resolved OC string) are given, prefer the exactly-matching
    line; a mismatch or no match at all comes back unverified.

    Returns dict(slot, card, port, sts, vt, line, verified) or None.
    """
    pat = re.compile(
        r'chassis slot (\S+) ces-card (\S+) port (\S+) '
        r'sts-1 (\d+) vt-15 (\d+) ds1\b')
    candidates = []
    for ln in (config_text or "").splitlines():
        m = pat.search(ln)
        if m:
            candidates.append({
                "slot": m.group(1), "card": m.group(2), "port": m.group(3),
                "sts": m.group(4), "vt": m.group(5), "line": ln.strip(),
            })
    if not candidates:
        return None
    if expect_sts is not None and expect_vt is not None:
        for c in candidates:
            if c["sts"] == str(expect_sts) and c["vt"] == str(expect_vt):
                c["verified"] = True
                return c
        # Config lines exist but none match the resolved STS/VT -- return
        # the first with verified=False so the mismatch is loud.
        c = candidates[0]
        c["verified"] = False
        return c
    c = candidates[0]
    c["verified"] = False
    return c


# ---- change-command builders ----------------------------------------------

def build_1250_commands(slot, card, ch, test, direction):
    """Returns (on_cmds, off_cmds, label)."""
    base = "chassis slot {} ces-card {} port ds1/{}".format(slot, card, ch)
    if test == "ais":
        suffix, label = AIS_DIRS_1250[direction]
        on  = ["set {} ds1-path {}".format(base, suffix)]
        off = ["delete {} ds1-path {}".format(base, suffix.split()[0])]
        return on, off, label
    suffix, label = LOOP_DIRS_1250[direction]
    on  = ["set {} maintenance loopback {}".format(base, suffix)]
    off = ["set {} maintenance loopback release".format(base)]
    return on, off, label


def build_1800_commands(slot, card, port, sts, vt, test, direction):
    """Returns (on_cmds, off_cmds, label)."""
    base = ("chassis slot {} ces-card {} port {} sts-1 {} vt-15 {} ds1"
            .format(slot, card, port, sts, vt))
    if test == "ais":
        suffix, label = AIS_DIRS_1800[direction]
        on  = ["set {} {}".format(base, suffix)]
        off = ["delete {} {}".format(base, suffix.split()[0])]
        return on, off, label
    suffix, label = LOOP_DIRS_1800[direction]
    on  = ["set {} maintenance loopback {}".format(base, suffix)]
    off = ["set {} maintenance loopback release".format(base)]
    return on, off, label


# ---- input collection (CLI parity) ----------------------------------------

def collect_inputs(session=None):
    session = session if session is not None else {}

    ip_1250 = prompt_ip("1250 IP", default=session.get("ip_1250"))
    if not ip_1250:
        return None
    ds1_raw = input("DS1(s) (e.g. ts1/1, ts1/2): ").strip().lower()
    ds1_list, _ds1_notes = expand_ds1_list(ds1_raw)
    if not ds1_list:
        print("Need at least one DS1.")
        return None

    print("\nTest device:")
    print("  1) 1250  [default]")
    print("  2) 1800")
    device = "1800" if input("Select [1]: ").strip() == "2" else "1250"

    print("\nTest type:")
    print("  1) AIS  [default]")
    print("  2) Loop")
    test = "loop" if input("Select [1]: ").strip() == "2" else "ais"

    dirs = (AIS_DIRS_1250 if device == "1250" else AIS_DIRS_1800) if test == "ais" \
        else (LOOP_DIRS_1250 if device == "1250" else LOOP_DIRS_1800)
    keys = list(dirs)
    print("\nDirection:")
    for i, k in enumerate(keys, 1):
        print("  {}) {} -- {}".format(i, k, dirs[k][1]))
    sel = input("Select [1]: ").strip()
    try:
        direction = keys[int(sel) - 1] if sel else keys[0]
    except (ValueError, IndexError):
        direction = keys[0]

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

    return {
        "ip_1250": ip_1250, "ds1_list": ds1_list,
        "device": device, "test": test, "direction": direction,
        "infra_user": infra_user, "infra_pass": infra_pass,
        "gsx_user": gsx_user, "gsx_pass": gsx_pass,
        "jira_ticket": jira_ticket, "open_when_done": open_when_done,
    }


# ---- main ------------------------------------------------------------------

def run(params, result_sink=None):
    """result_sink: optional dict the web layer passes in; resolved GSX
    targets are published into it mid-run so the page can auto-fill the
    GSX Monitor form. CLI callers just omit it."""
    ip_1250    = params["ip_1250"]
    ds1_list   = params["ds1_list"]
    device     = params["device"]
    test       = params["test"]
    direction  = params["direction"]
    infra_user = params["infra_user"]
    infra_pass = params["infra_pass"]
    gsx_user   = params.get("gsx_user", "")
    gsx_pass   = params.get("gsx_pass", "")

    dirs = (AIS_DIRS_1250 if device == "1250" else AIS_DIRS_1800) if test == "ais" \
        else (LOOP_DIRS_1250 if device == "1250" else LOOP_DIRS_1800)
    if direction not in dirs:
        print("[!] Invalid direction '{}' for {} {}".format(direction, device, test))
        return None

    nb_url, nb_token = load_netbox_config()
    netbox = NetboxClient(nb_url, nb_token)
    plink_path = find_plink()

    print("\n[Test Builder] {} {} ({}) on {} -- DS1s: {}".format(
        device, test.upper(), dirs[direction][1], ip_1250, ", ".join(ds1_list)))

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

    ds1_map, _ = c1250.query_batch(ds1_list)
    missing = [d for d in ds1_list if d not in ds1_map]
    if missing:
        print("[!] Could not resolve DS1(s): {}".format(", ".join(missing)))
    ds1_list = [d for d in ds1_list if d in ds1_map]
    if not ds1_list:
        c1250.close()
        return None

    # 1250 chassis tokens -- read the device's own config for each slot.
    print("  [1250] extracting chassis config tokens ...")
    slots = sorted({_ds1_parts(d)[0] for d in ds1_list})
    cfg_1250 = ""
    for slot in slots:
        cfg_1250 += c1250.run(
            'show configuration | display-set | match "chassis slot {} ces-card" | no-more '
            '| no-more'.format(slot)) + "\n"

    chassis_1250 = {}   # ds1 -> (card, verified_line)
    for d in ds1_list:
        slot, ch = _ds1_parts(d)
        card, vline = extract_1250_chassis(cfg_1250, slot, ch)
        chassis_1250[d] = (card, vline)
        if card:
            print("    {} -> ces-card {} [config-verified]".format(d, card))
        else:
            print("    {} -> ces-card NOT FOUND in config [UNVERIFIED]".format(d))

    # 1250 prechecks (live)
    print("  [1250] running prechecks ...")
    precheck_sections = []
    pairs_1250 = []
    pairs_1250.append(_capture(c1250, "show ne alarms | grep ds1"))
    for d in ds1_list:
        slot, ch = _ds1_parts(d)
        pairs_1250.append(_capture(
            c1250,
            'show interfaces physical status slot {} | grep "ds1/{} "'.format(slot, ch)))
        pairs_1250.append(_capture(
            c1250, "show interfaces physical pm 15min ds1-{}".format(d)))
    c1250.close()
    precheck_sections.append(_section(
        "# NPT-1250  {}  ({})".format(host_1250, ip_1250), pairs_1250))

    hub_ip = ds1_map[ds1_list[0]]["hub_ip"]

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

    sub_map = {d: ds1_map[d] for d in ds1_list}
    (oc12_map, _, hub_name, hub_hostname,
     loc_code, _, peerless_cust) = c1800.query_batch(hub_ip, sub_map)

    peerless_ds1s = [d for d in ds1_list if d in peerless_cust]
    if peerless_ds1s:
        print("[!] Peerless circuit(s) skipped (no MUX/GSX hop): {}".format(
            ", ".join(peerless_ds1s)))
        ds1_list = [d for d in ds1_list if d not in peerless_cust]
    unresolved = [d for d in ds1_list if d not in oc12_map]
    if unresolved:
        print("[!] No OC resolution for: {}".format(", ".join(unresolved)))
        ds1_list = [d for d in ds1_list if d in oc12_map]
    if not ds1_list:
        c1800.close()
        return None

    # 1800 chassis tokens: pull the chassis config lines for this circuit's
    # OC ports and cross-check sts-1/vt-15 against the resolved OC string.
    print("  [1800] extracting chassis config tokens ...")
    chassis_1800 = {}   # ds1 -> dict or None
    for d in ds1_list:
        oc12 = oc12_map[d]                       # e.g. oc-ts3/1:8:15
        parts = oc12.split(":")
        oc_base = parts[0]
        sts = parts[1] if len(parts) > 2 else None
        vt  = parts[2] if len(parts) > 2 else None
        m = re.search(r'(ts\d+)/(\d+)', oc_base)
        oc_slot = m.group(1) if m else None
        cfg = c1800.run(
            'show configuration | display-set | match "chassis slot {} ces-card" | no-more '
            '| no-more'.format(oc_slot)) if oc_slot else ""
        # Narrow to this DS1's sts/vt line.
        entry = extract_1800_chassis(cfg, expect_sts=sts, expect_vt=vt)
        chassis_1800[d] = {"entry": entry, "sts": sts, "vt": vt,
                           "oc12": oc12, "oc_slot": oc_slot}
        if entry and entry.get("verified"):
            print("    {} -> {} [config-verified]".format(d, entry["line"][:70]))
        elif entry:
            print("    {} -> config line found but STS/VT MISMATCH "
                  "(resolved {}:{} vs config {}:{}) [UNVERIFIED]".format(
                      d, sts, vt, entry["sts"], entry["vt"]))
        else:
            print("    {} -> NO chassis config line found for slot {} [UNVERIFIED]".format(
                d, oc_slot))

    # 1800 prechecks
    print("  [1800] running prechecks ...")
    pairs_1800 = []
    oc_bases = sorted({oc12_map[d].split(":")[0] for d in ds1_list})
    for ob in oc_bases:
        pairs_1800.append(_capture(
            c1800, "show interfaces physical pm 15min {}".format(ob),
            note="<- OC baseline"))
        pairs_1800.append(_capture(
            c1800, "show ne alarms | grep {}".format(ob),
            note="<- confirm no path alarms"))
    c1800.close()
    precheck_sections.append(_section(
        "# NPT-1800  {}  ({})".format(hub_hostname, hub_ip), pairs_1800))

    # ---- HOP 3: MUX (route only -- no changes here) ----
    mux_info = LOC_TO_MUX.get(loc_code)
    gsx_assignments = {}   # ds1 -> dict(gsx_node, gsx_ip, t1_full)
    if not mux_info:
        print("[!] No MUX mapping for loc '{}' -- GSX assignments unavailable".format(loc_code))
    else:
        print("  [MUX] connecting to {} ...".format(mux_info["name"]))
        cmux = MUX4216Client(mux_info["ip"], infra_user, infra_pass)
        try:
            cmux.connect()
            mux_results, _, _, _, errs = cmux.query_batch(
                mux_info["name"], mux_info["ip"],
                {d: oc12_map[d] for d in ds1_list},
                {d: hub_hostname for d in ds1_list}, netbox)
            cmux.close()
        except CredentialError:
            raise
        except Exception as e:
            print("[!] MUX resolution failed: {}".format(e))
            mux_results, errs = {}, {}

        for d in ds1_list:
            mr = mux_results.get(d)
            if not mr:
                print("    {} -> GSX assignment unresolved ({})".format(
                    d, errs.get(d, "no result")))
                continue
            t1_full = "T1-1-{}-{}-{}".format(
                mr.get("gsx_slot", "?"), mr.get("gsx_port", "?"), mr.get("gsx_t1", "?"))
            gsx_assignments[d] = {
                "gsx_node": mr.get("gsx_node", "?"),
                "t1_full":  t1_full,
                "gsx_ip":   None,
            }
            print("    {} -> {} {}".format(d, mr.get("gsx_node", "?"), t1_full))

    # ---- HOP 4: GSX (read-only prechecks + IP resolution) ----
    gsx_nodes = {v["gsx_node"] for v in gsx_assignments.values()
                 if v["gsx_node"] and v["gsx_node"] != "?"}
    for node in gsx_nodes:
        gsx_ip = None
        if plink_path:
            try:
                gsx_dev = netbox.derive_gsx_device_name(node, hub_hostname)
                _nbw = []
                cards = netbox.gsx_mgmt_ips(gsx_dev, _nbw)
                if not cards and _nbw:
                    print("  [!] {}".format(_nbw[0]))
                if cards:
                    _, gsx_ip = find_live_ip(cards)
            except Exception as e:
                print("  [GSX] IP resolution failed for {}: {}".format(node, e))
        for d, v in gsx_assignments.items():
            if v["gsx_node"] == node:
                v["gsx_ip"] = gsx_ip

        if gsx_ip and plink_path and gsx_user and gsx_pass:
            print("  [GSX] prechecks on {} ({}) ...".format(node, gsx_ip))
            try:
                g = GSXClient(gsx_ip, gsx_user, gsx_pass, plink_path)
                g.connect()
                pairs_gsx = []
                for d, v in gsx_assignments.items():
                    if v["gsx_node"] != node:
                        continue
                    t3_full = "T3-1-{}-{}".format(v.get("gsx_slot","?"), v.get("gsx_port","?"))
                for cmd in ("SHOW T3 {} STATUS".format(t3_full),
                                "SHOW T1 {} STATUS".format(v["t1_full"]),
                                "SHOW T1 {} STATISTICS".format(v["t1_full"])):
                        try:
                            out = g.run_command(cmd)
                        except Exception as e:
                            out = "[ERROR: {}]".format(e)
                        pairs_gsx.append((cmd, (out or "").rstrip()))
                g.close()
                precheck_sections.append(_section(
                    "# GSX  {}  ({})".format(node, gsx_ip), pairs_gsx))
            except Exception as e:
                print("  [GSX] precheck failed: {}".format(e))
        else:
            print("  [GSX] skipping live prechecks for {} (no IP/plink/creds)".format(node))

    # Publish resolved GSX targets for the web page's monitor auto-fill.
    if result_sink is not None:
        result_sink["gsx_targets"] = [
            {"ds1": d, "node": v["gsx_node"], "ip": v.get("gsx_ip") or "",
             "t1": v["t1_full"]}
            for d, v in gsx_assignments.items() if "?" not in v["t1_full"]
        ]

    # ---- BUILD THE SCRIPT ----
    script = _build_script(
        host_1250, ip_1250, hub_hostname, hub_ip, ds1_list, ds1_map, oc12_map,
        device, test, direction, dirs, chassis_1250, chassis_1800,
        gsx_assignments, precheck_sections, mux_info)

    out_path = _write(script, "{}_{}_{}".format(device, test, host_1250))
    if not out_path:
        return None

    jira_ticket = params.get("jira_ticket")
    if jira_ticket and out_path:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        summary = "\n".join([
            "DS1 test script generated -- {} {} ({}). Commands are copy/paste-"
            "ready in the attachment; NOTHING was executed.".format(
                device, test.upper(), dirs[direction][1]),
            "Circuit : {} ({})  DS1(s): {}".format(
                host_1250, ip_1250, ", ".join(ds1_list)),
            "1800    : {} ({})".format(hub_hostname, hub_ip),
        ] + ["GSX     : {}  {}".format(v["gsx_node"], v["t1_full"])
             for v in gsx_assignments.values()])
        intro = "DS1 test script -- {} {} on {} ({} DS1(s)), generated {}.".format(
            device, test.upper(), host_1250, len(ds1_list),
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Split live prechecks into their own collapsed panel, same as the
        # web UI -- the attached/downloaded .txt file (out_path) still has
        # everything together, unchanged.
        precheck_text, script_for_jira = _split_precheck_section(script)
        ok, msg = post_report(
            jira_ticket, intro,
            "DS1 Test Script -- {} {} ({})".format(device, test.upper(), host_1250),
            script_for_jira,
            attach_path=out_path, summary_text=summary,
            precheck_text=precheck_text,
            precheck_title="Prechecks (live output, already run)")
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))
    elif not jira_ticket:
        print("[JIRA] skipped -- no ticket provided.")

    if params.get("open_when_done") and out_path:
        try:
            import os as _os
            _os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open file: {}".format(e))

    return out_path


# ---- script assembly --------------------------------------------------------

def _gsx_by_node(ds1_list, gsx_assignments):
    """Group resolved GSX targets: (node, ip) -> [(ds1, t1_full), ...]."""
    by_node = {}
    for d in ds1_list:
        ga = gsx_assignments.get(d)
        if ga and "?" not in ga["t1_full"]:
            key = (ga["gsx_node"], ga.get("gsx_ip") or "?")
            by_node.setdefault(key, []).append((d, ga["t1_full"]))
    return by_node


# Mirrors the web UI's client-side split (job.js renderReport) so the JIRA
# comment and the on-screen preview separate the same "STEP 1 -- PRECHECKS
# ... STEP 2" block the same way. Falls back to (None, script_text)
# unchanged if the markers aren't found -- non-fatal either way.
_PRECHECK_RE = re.compile(
    r'\n?=+\n\s*STEP 1[^\n]*PRECHECK[^\n]*\n=+\n(.*?)(?=\n=+\n\s*STEP 2\b)',
    re.IGNORECASE | re.DOTALL)


def _split_precheck_section(script_text):
    """Pull the STEP 1 precheck block out of the full script text.
    Returns (precheck_text_or_None, script_text_with_step1_removed)."""
    m = _PRECHECK_RE.search(script_text)
    if not m or not m.group(1).strip():
        return None, script_text
    precheck_text = m.group(1).strip()
    rest = script_text[:m.start()] + script_text[m.end():]
    rest = re.sub(r'\n{3,}', '\n\n', rest)
    return precheck_text, rest


def _build_script(host_1250, ip_1250, hub_hostname, hub_ip, ds1_list, ds1_map,
                  oc12_map, device, test, direction, dirs, chassis_1250,
                  chassis_1800, gsx_assignments, precheck_sections,
                  mux_info=None):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = dirs[direction][1]
    s = []
    s += [SEP,
          "  DS1 TEST SCRIPT -- {} {}".format(device, test.upper()),
          "  {}".format(label),
          "  Generated {}  --  NOTHING in this file has been executed.".format(now),
          SEP, ""]

    if device == "1800" and test == "ais":
        s += ["!!! WARNING: the 1800 AIS command syntax is UNVERIFIED (per P. "
              "Capps 2026-08-05). Review each command against the device "
              "config render before running. !!!", ""]

    s += ["ROUTE", SEP2]
    for d in ds1_list:
        info = ds1_map.get(d, {})
        ga = gsx_assignments.get(d, {})
        s.append("  {}  ({})".format(d, info.get("muse_id", "?")))
        s.append("    1250 : {}  ({})".format(host_1250, ip_1250))
        s.append("    1800 : {}  ({})   {}".format(
            hub_hostname, hub_ip, oc12_map.get(d, "?")))
        if mux_info:
            s.append("    MUX  : {}  ({})".format(mux_info["name"], mux_info["ip"]))
        s.append("    GSX  : {}  ({})   {}".format(
            ga.get("gsx_node", "?"), ga.get("gsx_ip") or "?",
            ga.get("t1_full", "?")))
        s.append("")

    # 1) PRECHECKS (live output embedded)
    s += [SEP, "  STEP 1 -- PRECHECKS (already run -- live output below)", SEP]
    for sec in precheck_sections:
        s.append(sec)
    s.append("")

    # 2) CLEAR GSX COUNTERS
    s += [SEP, "  STEP 2 -- CLEAR GSX COUNTERS (run before testing)", SEP, ""]
    by_node = _gsx_by_node(ds1_list, gsx_assignments)
    if by_node:
        for (node, nip), items in by_node.items():
            s.append("  On {} ({}):".format(node, nip))
            for d, t1 in items:
                s.append("  ! {} :".format(d))
                s.append("  CONFIGURE T1 {} PERFMON RESET COUNTERS ALL".format(t1))
            s.append("")
    else:
        s.append("  (GSX assignment unresolved -- fill in manually: "
                 "CONFIGURE T1 T1-1-S-P-N PERFMON RESET COUNTERS ALL)")
        s.append("")

    # 3) TEST ON
    s += [SEP, "  STEP 3 -- TEST ON  ({} {}: {})".format(device, test.upper(), label), SEP, ""]
    if device == "1250":
        s.append("  On {} ({}):".format(host_1250, ip_1250))
        s.append("")
        for d in ds1_list:
            slot, ch = _ds1_parts(d)
            card, vline = chassis_1250.get(d, (None, None))
            if card:
                on, off, _ = build_1250_commands(slot, card, ch, test, direction)
                s.append("  ! {}  [VERIFIED against config: {}]".format(d, vline))
                s += ["  {}".format(c) for c in on]
            else:
                on, off, _ = build_1250_commands(slot, "<CES-CARD>", ch, test, direction)
                s.append("  ! {}  [UNVERIFIED -- no 'chassis slot {} ces-card' "
                         "line found; fill in the card type]".format(d, slot))
                s += ["  {}".format(c) for c in on]
            s.append("")
    else:
        s.append("  On {} ({}):".format(hub_hostname, hub_ip))
        s.append("")
        for d in ds1_list:
            ce = chassis_1800.get(d) or {}
            entry = ce.get("entry")
            if entry:
                on, off, _ = build_1800_commands(
                    entry["slot"], entry["card"], entry["port"],
                    entry["sts"], entry["vt"], test, direction)
                tag = ("VERIFIED against config: {}".format(entry["line"])
                       if entry.get("verified") else
                       "UNVERIFIED -- config sts/vt {}:{} did not match resolved "
                       "{}:{}; CONFIRM before running".format(
                           entry["sts"], entry["vt"], ce.get("sts"), ce.get("vt")))
                s.append("  ! {}  [{}]".format(d, tag))
                s += ["  {}".format(c) for c in on]
            else:
                s.append("  ! {}  [UNVERIFIED -- no chassis config line found "
                         "for slot {}; resolved path {} -- build the command "
                         "manually from the config render]".format(
                             d, ce.get("oc_slot"), ce.get("oc12")))
            s.append("")

    # 4) MONITOR (clear counters once the test is up, then poll)
    s += [SEP, "  STEP 4 -- MONITOR ON THE GSX (repeat while the test runs)", SEP, ""]
    if by_node:
        for (node, nip), items in by_node.items():
            s.append("  On {} ({}):".format(node, nip))
            for d, t1 in items:
                s.append("  ! {} -- clear once the test is up, then poll:".format(d))
                s.append("  CONFIGURE T1 {} PERFMON RESET COUNTERS ALL".format(t1))
                s.append("  SHOW T1 {} STATUS".format(t1))
                s.append("  SHOW T1 {} STATISTICS".format(t1))
                s.append("  SHOW T1 {} PERFMON CURRENT DAY".format(t1))
            s.append("")
    s.append("  (Or use the web page's GSX Monitor to poll these automatically.)")
    s.append("")

    # 5) TEST OFF / BACK-OUT
    s += [SEP, "  STEP 5 -- TEST OFF / BACK-OUT (ALWAYS run when finished)", SEP, ""]
    if device == "1250":
        s.append("  On {} ({}):".format(host_1250, ip_1250))
        s.append("")
        for d in ds1_list:
            slot, ch = _ds1_parts(d)
            card, _ = chassis_1250.get(d, (None, None))
            _, off, _ = build_1250_commands(slot, card or "<CES-CARD>", ch, test, direction)
            s.append("  ! {} :".format(d))
            s += ["  {}".format(c) for c in off]
            s.append("")
    else:
        s.append("  On {} ({}):".format(hub_hostname, hub_ip))
        s.append("")
        for d in ds1_list:
            ce = chassis_1800.get(d) or {}
            entry = ce.get("entry")
            if entry:
                _, off, _ = build_1800_commands(
                    entry["slot"], entry["card"], entry["port"],
                    entry["sts"], entry["vt"], test, direction)
                s.append("  ! {} :".format(d))
                s += ["  {}".format(c) for c in off]
                s.append("")

    # 6) POSTCHECKS (commands only)
    s += [SEP, "  STEP 6 -- POSTCHECKS (commands only -- run after back-out)", SEP, ""]
    s.append("  On {} ({}):".format(host_1250, ip_1250))
    for d in ds1_list:
        slot, ch = _ds1_parts(d)
        s.append('  show interfaces physical status slot {} | grep "ds1/{} "'.format(slot, ch))
        s.append("  show interfaces physical pm 15min ds1-{}".format(d))
    s.append("  show ne alarms | grep ds1")
    s.append("")
    s.append("  On {} ({}):".format(hub_hostname, hub_ip))
    for ob in sorted({oc12_map[d].split(":")[0] for d in ds1_list}):
        s.append("  show interfaces physical pm 15min {}".format(ob))
        s.append("  show ne alarms | grep {}".format(ob))
    s.append("")
    for (node, nip), items in by_node.items():
        s.append("  On {} ({}):".format(node, nip))
        for d, t1 in items:
            s.append("  SHOW T1 {} STATUS".format(t1))
            s.append("  SHOW T1 {} PERFMON CURRENT DAY".format(t1))
        s.append("")

    s += [SEP, "  END OF SCRIPT -- verify every UNVERIFIED line before use.", SEP]
    return "\n".join(s)


def _write(script_text, circuit_id):
    safe = re.sub(r'[^\w.-]', '_', circuit_id)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("DS1_Test_Script_{}_{}.txt".format(safe, ts))
    try:
        with open(out_path, "w") as f:
            f.write(script_text)
        print("\n[written] {}".format(out_path))
        return out_path
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None


# ---- GSX live monitor (read-only by default -- used by the web app) --------
# Opt-in "clear_first" param sends one CONFIGURE (write) command before
# polling; everything else here is SHOW only.

def run_gsx_monitor(params):
    """
    Poll SHOW T1 <id> STATISTICS on the GSX at a fixed interval for a fixed
    duration, printing a timestamped snapshot each poll (the web live-log
    streams these). Read-only BY DEFAULT -- Returns None (no report file).

    params["clear_first"] (opt-in, default False): if set, sends
    CONFIGURE T1 <id> PERFMON RESET COUNTERS ALL once, right before the
    poll loop starts. This is the one real config-write path in this
    function -- everything else is SHOW only. STEP 2 of the generated
    test script has the same command for anyone who'd rather run it
    manually on the GSX CLI instead.
    """
    import time as _time
    gsx_ip      = params["gsx_ip"]
    t1_id       = params["t1_id"]
    gsx_user    = params["gsx_user"]
    gsx_pass    = params["gsx_pass"]
    interval    = max(5, int(params.get("interval", 15)))
    duration    = min(3600, max(30, int(params.get("duration", 600))))
    clear_first = bool(params.get("clear_first"))

    plink_path = find_plink()
    if not plink_path:
        print("[!] plink not found -- cannot reach the GSX.")
        return None

    print("[GSX Monitor] {} on {} -- every {}s for {}s{}".format(
        t1_id, gsx_ip, interval, duration,
        "  (clearing counters first)" if clear_first else ""))
    g = GSXClient(gsx_ip, gsx_user, gsx_pass, plink_path)
    try:
        g.connect()
    except Exception as e:
        print("[!] GSX connect failed: {}: {}".format(type(e).__name__, e))
        return None

    if clear_first:
        clear_cmd = "CONFIGURE T1 {} PERFMON RESET COUNTERS ALL".format(t1_id)
        print("[GSX Monitor] clearing counters -- {}".format(clear_cmd))
        try:
            out = g.run_command(clear_cmd)
            print("[GSX Monitor] clear result: {}".format(out.strip() or "(no output)"))
        except Exception as e:
            print("[!] counter clear FAILED: {}: {} -- continuing to monitor "
                  "anyway, but PMs below will still include whatever was "
                  "already accumulated.".format(type(e).__name__, e))

    stop_event = params.get("stop_event")   # threading.Event from the web layer

    def _stopped():
        return stop_event is not None and stop_event.is_set()

    end = _time.time() + duration
    poll = 0
    prev = {}   # counter label -> last value, for "+N since last poll" deltas
    try:
        while _time.time() < end and not _stopped():
            poll += 1
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            try:
                out = g.run_command("SHOW T1 {} STATISTICS".format(t1_id))
            except Exception as e:
                print("[{}] poll {} -- ERROR: {}".format(stamp, poll, e))
                out = ""
            # Compact snapshot: non-zero counters (deduped -- counter lines
            # also contain "Alarm", which double-matched before) with a
            # since-last-poll delta, plus any status/alarm lines that
            # aren't counters.
            seen = set()
            interesting = []
            for ln in (out or "").splitlines():
                m = re.match(r'\s*([A-Za-z][A-Za-z/ ()-]*?)\s*:\s*(\d+)\s*$', ln)
                if m:
                    label, val = m.group(1).strip(), int(m.group(2))
                    key = label.lower()
                    if val <= 0 or key in seen:
                        continue
                    seen.add(key)
                    delta = val - prev.get(key, val)
                    prev[key] = val
                    interesting.append("{}: {}{}".format(
                        label, val,
                        "   <-- +{} since last poll".format(delta) if delta > 0 else ""))
                elif re.search(r'(Status|Alarm)', ln) and ln.strip():
                    key = " ".join(ln.split()).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    interesting.append(" ".join(ln.split()))
            if out and interesting:
                print("[{}] poll {}:".format(stamp, poll))
                for i in interesting[:14]:
                    print("    {}".format(i))
            elif out:
                print("[{}] poll {}: all counters zero / no flagged lines".format(stamp, poll))
            # Sleep in 1s steps so Stop reacts quickly.
            slept = 0
            while slept < interval and _time.time() < end and not _stopped():
                _time.sleep(1)
                slept += 1
    finally:
        g.close()
    if _stopped():
        print("[GSX Monitor] STOPPED by user after {} poll(s).".format(poll))
    else:
        print("[GSX Monitor] done -- {} polls.".format(poll))
    return None


def main():
    print("=" * 64)
    print("  DS1 TEST BUILDER -- AIS / LOOPS TO GSX (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
