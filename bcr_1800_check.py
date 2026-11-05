#!/usr/bin/env python3
"""
bcr_1800_check.py -- BCR to 1800 Check.

For each hub, reports the BGP session between the site's BCR (backbone core
router, `bcr01.<site>`, Cisco IOS-XR) and its NPT-1800 (`ces01.<site>`), plus
the physical link state on the 1800 side. One view across all four hubs.

Why both ends
-------------
The BCR's `show bgp summary` gives the SESSION state for both ends at once --
uptime, Established/not, prefixes. But a healthy-looking "down" could be a dead
cable on the 1800's physical port, which the BGP line alone won't localize. So
the 1800 side is read too, for the BCR-facing interface's link state (pcapps
2026-11-05: "if the cable died on the 1800 side i'd know").

Discovery
---------
BCRs come from NetBox (`bcr01` name filter -- confirmed present, 2026-11-05),
same path as the 1800 hubs. Each BCR's paired 1800 is resolved by site through
hub_registry (the single source of hub identity).

Scope / confirm-live
--------------------
- The BCR `show bgp summary` parse is grounded on a real bcr01.jfk1 sample
  (2026-11-05). The 1800 peer is picked by its 4-byte private AS (>=4200000000
  -- internal), which on the sample is 4200000635 (the 635 is the site/bundle id).
- The 1800-side read uses `show interfaces description` (a confirmed command) and
  reports the BCR-facing port's Admin/Link state -- the "cable died" signal. The
  exact last-link-flapped TIMESTAMP on Neptune is NOT yet confirmed against a live
  sample, so it is not fabricated; when a sample is in hand, add it here.

Read-only. No device is configured by this tool.

Built by P. Capps.
"""

import re
import time
import datetime
import getpass

import platform_paths
import hub_registry
from bcr_client import BCRClient
from npt1800_client import NPT1800Client
from device_base import CredentialError
from netbox_client import NetboxClient, load_netbox_config

OUTPUT_DIR = platform_paths.BASE_DIR

# 4-byte private ASN range (RFC 6996). An internal BGP peer (the 1800) uses one;
# an external customer/carrier would not. Used to pick the 1800 peer out of the
# BCR's neighbor table.
PRIVATE_ASN_MIN = 4200000000
PRIVATE_ASN_MAX = 4294967294

# Sites to check. US-focused for now (pcapps, 2026-11-05) -- NetBox may list BCRs
# at other sites, but only these four production hubs are in scope. These are the
# hub_registry production sites (ATL/JFK/DFW/LAX), not LAB. Widen this set when
# international hubs come into scope.
US_SITES = {"ATL", "JFK", "DFW", "LAX"}


TOOL_INFO = {
    'name':     'BCR to 1800 Check',
    'ran_from': 'All BCRs (via NetBox) + paired 1800s',
    'desc':     'For each hub, reports the BGP session between the site BCR '
                '(bcr01.<site>, IOS-XR) and its NPT-1800 -- uptime, '
                'Established/not, prefixes -- plus the physical link state on the '
                '1800 side (catches a dead cable the BGP line alone would not '
                'localize). BCRs discovered from NetBox; paired 1800 resolved by '
                'site. Read-only.',
    'requires': 'infra creds + NetBox',
    'order':    13,
}


# ── show bgp summary parsing (IOS-XR) ────────────────────────────────────────────
# Grounded on a real bcr01.jfk1 row (2026-11-05):
#   X.X.X.3   0 4200000635  235246  81348  12310320  0  0  1w2d  3
# Columns: Neighbor Spk RemoteAS MsgRcvd MsgSent TblVer InQ OutQ Up/Down St/PfxRcd
_BGP_ROW_RE = re.compile(
    r'^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+'   # 1 neighbor IP
    r'(\d+)\s+'                            # 2 Spk
    r'(\d+)\s+'                            # 3 RemoteAS
    r'(\d+)\s+'                            # 4 MsgRcvd
    r'(\d+)\s+'                            # 5 MsgSent
    r'(\d+)\s+'                            # 6 TblVer
    r'(\d+)\s+'                            # 7 InQ
    r'(\d+)\s+'                            # 8 OutQ
    r'(\S+)\s+'                            # 9 Up/Down (uptime, or Down time)
    r'(\S+)\s*$',                          # 10 St/PfxRcd (number = Established)
    re.MULTILINE)


def parse_bgp_summary(raw):
    """
    Parse IOS-XR `show bgp summary` neighbor rows.

    Returns a list of dicts:
      {neighbor, remote_as, up_down, st_pfx, established, pfx_rcvd, internal}

    established  -- True when St/PfxRcd is a number (IOS-XR prints the received
                   prefix count there once the session is Established; a STATE
                   word like Idle/Active/Connect means it is not).
    internal     -- True when remote_as is in the 4-byte private range (the 1800).
    Never raises; [] when nothing parses.
    """
    out = []
    for m in _BGP_ROW_RE.finditer(raw or ""):
        neighbor  = m.group(1)
        remote_as = int(m.group(3))
        up_down   = m.group(9)
        st_pfx    = m.group(10)
        established = st_pfx.isdigit()
        out.append({
            "neighbor":    neighbor,
            "remote_as":   remote_as,
            "up_down":     up_down,
            "st_pfx":      st_pfx,
            "established": established,
            "pfx_rcvd":    int(st_pfx) if established else None,
            "internal":    PRIVATE_ASN_MIN <= remote_as <= PRIVATE_ASN_MAX,
        })
    return out


def pick_1800_peer(neighbors):
    """
    The neighbor that is the paired 1800: the internal (private-AS) one.

    Returns (peer_dict, note). note is '' on a clean single match, or a warning
    when 0 or >1 internal peers were found (surfaced, not resolved silently).
    """
    internal = [n for n in neighbors if n["internal"]]
    if len(internal) == 1:
        return internal[0], ""
    if not internal:
        if len(neighbors) == 1:
            # No private-AS peer, but only one neighbor total -- use it, say so.
            return neighbors[0], ("no private-AS (internal) peer found; using the "
                                  "only neighbor present")
        return None, ("no internal (private-AS) BGP peer among {} neighbor(s) -- "
                      "cannot identify the 1800 peer".format(len(neighbors)))
    return internal[0], ("{} internal BGP peers found ({}) -- using the first; "
                         "verify".format(
                             len(internal),
                             ", ".join(n["neighbor"] for n in internal)))


# ── 1800-facing interface on the BCR description ────────────────────────────────
# BCR Te description: "Internal: ces01.jfk1 (s13p1)" -> the 1800 is ces01.jfk1 on
# slot 13 / port 1.
_BCR_IFACE_DESC_RE = re.compile(
    r'(ces01\.\w+)\s*\(s(\d+)p(\d+)\)', re.IGNORECASE)


def parse_bcr_facing_1800(desc_raw):
    """
    From the BCR's `show interfaces description` output, find the 1800 it faces
    and the 1800 slot/port. Returns {hostname_1800, slot, port} or None.
    """
    m = _BCR_IFACE_DESC_RE.search(desc_raw or "")
    if not m:
        return None
    return {"hostname_1800": m.group(1), "slot": m.group(2), "port": m.group(3)}


# ── 1800-side link state ────────────────────────────────────────────────────────
# IMPORTANT (pcapps, 2026-11-05): the Neptune 1800 does NOT list these uplink
# ports in `show interfaces` or `show interfaces description` -- they read as if
# they don't exist. The ONLY command that shows them is `show interfaces physical`:
#
#   Port               Admin      Oper    PHY Type    Media Type  Ifindex  HoldTimeUp  HoldTimeDown
#   xe-ts13/1          enable     Up      10GBase-R   Optical     11       20          20
#
# There is no description column, so the BCR-facing port can't be found by text;
# it is derived by NAME from the BCR's own facing description ("(s13p1)" ->
# xe-ts13/1) and looked up here. `show interfaces physical` has no last-flap
# timestamp (HoldTimeUp/Down are debounce timers, not history), so the dead-cable
# signal is the Oper state.
_N1800_PHYS_RE = re.compile(
    r'^\s*(\S+)\s+(enable|disable)\s+(\S+)', re.IGNORECASE | re.MULTILINE)


def parse_1800_physical(raw):
    """
    Parse `show interfaces physical` into {port: {admin, oper}}.
      admin : 'enable' | 'disable'   (lowercased)
      oper  : device casing kept ('Up' / 'Down' / ...)
    Header and separator lines don't fit (Admin col isn't enable/disable), so
    they're skipped. {} when nothing parses.
    """
    out = {}
    for m in _N1800_PHYS_RE.finditer(raw or ""):
        out[m.group(1)] = {"admin": m.group(2).lower(), "oper": m.group(3)}
    return out


def derive_1800_port(facing):
    """
    The 1800 port name for the BCR-facing link, from the BCR description's
    slot/port: (s13p1) -> 'xe-ts13/1'. These BCR<->1800 uplinks are 10G (xe-).
    None when no facing description was parsed.
    """
    if not facing:
        return None
    return "xe-ts{}/{}".format(facing["slot"], facing["port"])


# ── Per-hub check ────────────────────────────────────────────────────────────────

def check_one(bcr_host, bcr_ip, username, password, netbox, progress=print):
    """
    Check one BCR<->1800 pair. Returns a result dict (never raises except on a
    credential error, which must halt the whole run).
    """
    res = {"bcr_host": bcr_host, "bcr_ip": bcr_ip, "site": None,
           "bgp": None, "bgp_note": "", "n1800_host": None, "n1800_link": None,
           "errors": []}

    site = hub_registry.site_from_hostname(bcr_host)
    res["site"] = site

    # ---- BCR side: show bgp summary (+ facing-1800 description) ----
    bgp_raw = desc_raw = ""
    c = BCRClient(bcr_ip, username, password)
    try:
        c.connect()
        bgp_raw = c.run("show bgp summary")
        try:
            desc_raw = c.run("show interfaces description")
        except Exception:
            desc_raw = ""
    except CredentialError:
        raise
    except Exception as e:
        res["errors"].append("BCR {} ({}): {}: {}".format(
            bcr_host, bcr_ip, type(e).__name__, str(e)[:80]))
        progress("  [!] {} unreachable: {}".format(bcr_host, str(e)[:60]))
        return res
    finally:
        try:
            c.close()
        except Exception:
            pass

    neighbors = parse_bgp_summary(bgp_raw)
    peer, note = pick_1800_peer(neighbors)
    res["bgp"] = peer
    res["bgp_note"] = note
    facing = parse_bcr_facing_1800(desc_raw)

    # ---- 1800 side: link state of the BCR-facing port ----
    hub = hub_registry.hub_info(site) if site else None
    n1800_host = (facing or {}).get("hostname_1800") or (hub or {}).get("hostname")
    n1800_ip   = (hub or {}).get("ip")
    res["n1800_host"] = n1800_host
    port_name = derive_1800_port(facing)
    res["n1800_port"] = port_name
    if not n1800_ip:
        res["errors"].append("no 1800 mgmt IP for site {} (hub_registry)".format(site))
    elif not port_name:
        res["errors"].append("could not derive the 1800 port (no BCR facing "
                             "description parsed) -- 1800 link not checked")
    else:
        c18 = NPT1800Client(n1800_ip, username, password)
        try:
            c18.connect()
            phys = parse_1800_physical(c18.run("show interfaces physical | no-more"))
            if port_name in phys:
                res["n1800_link"] = {"iface": port_name,
                                     "admin": phys[port_name]["admin"],
                                     "oper":  phys[port_name]["oper"]}
            else:
                res["n1800_link"] = {"iface": port_name, "admin": None,
                                     "oper": None, "missing": True}
        except CredentialError:
            raise
        except Exception as e:
            res["errors"].append("1800 {} ({}): {}: {}".format(
                n1800_host, n1800_ip, type(e).__name__, str(e)[:80]))
        finally:
            try:
                c18.close()
            except Exception:
                pass

    _flag = "[ OK]" if (peer and peer["established"]) else "[!!]"
    progress("  {} {} <-> {}  {}".format(
        _flag, bcr_host, n1800_host or "?",
        "BGP up {}".format(peer["up_down"]) if peer and peer["established"]
        else "BGP NOT established" if peer else "no BGP peer"))
    return res


# ── Report ───────────────────────────────────────────────────────────────────────

def build_report(results, stamp, elapsed):
    lines = ["BCR to 1800 Check  --  {}".format(stamp), "=" * 64, "",
             hub_registry.describe_source(), ""]
    for r in sorted(results, key=lambda x: (x.get("site") or "", x["bcr_host"])):
        site = r.get("site") or "?"
        lines.append("{}  --  {}  <->  {}".format(
            site, r["bcr_host"], r.get("n1800_host") or "?"))

        peer = r.get("bgp")
        if peer:
            if peer["established"]:
                lines.append("  [ OK ] BGP peer {} (AS {}) Established -- up {}, "
                             "{} prefix(es)".format(
                                 peer["neighbor"], peer["remote_as"],
                                 peer["up_down"], peer["pfx_rcvd"]))
            else:
                lines.append("  [ !! ] BGP peer {} (AS {}) NOT established -- "
                             "state {} (down {})".format(
                                 peer["neighbor"], peer["remote_as"],
                                 peer["st_pfx"], peer["up_down"]))
        else:
            lines.append("  [ -- ] BGP peer to the 1800 not identified")
        if r.get("bgp_note"):
            lines.append("         note: {}".format(r["bgp_note"]))

        link = r.get("n1800_link")
        if link is None:
            lines.append("  [ -- ] 1800 BCR-facing link: not read")
        elif link.get("missing"):
            lines.append("  [ -- ] 1800 port {} not found in 'show interfaces "
                         "physical'".format(link["iface"]))
        elif (link.get("oper") or "").lower() == "up":
            lines.append("  [ OK ] 1800 {} Oper Up (admin {})".format(
                link["iface"], link["admin"]))
        else:
            lines.append("  [ !! ] 1800 {} Oper {} (admin {})  "
                         "<< physical/cable on the 1800 side".format(
                             link["iface"], link.get("oper"), link["admin"]))

        for e in r.get("errors", []):
            lines.append("  [ !! ] {}".format(e))
        lines.append("")

    # Note what is/isn't proven, so a reader knows.
    lines += ["--- NOTES ---",
              "BGP parse grounded on a live bcr01.jfk1 sample. The 1800-side state "
              "is the Oper column of 'show interfaces physical' (the 1800 does NOT "
              "list these ports in 'show interfaces'); the BCR-facing port is "
              "derived by name from the BCR description ((s13p1) -> xe-ts13/1).",
              "No last-link-flapped timestamp is available on the 1800 for these "
              "ports (show interfaces physical has only hold timers), so Oper "
              "Up/Down is the dead-cable signal -- not a flap history.",
              "Elapsed: {:.0f}s".format(elapsed), ""]
    return "\n".join(lines)


# ── Entry points ─────────────────────────────────────────────────────────────────

def collect_inputs(session=None):
    username       = input("Infrastructure username: ").strip()
    password       = getpass.getpass("Infrastructure password: ")
    open_when_done = input("Open report when done? (y/n): ").strip().lower() == "y"
    return {"username": username, "password": password,
            "open_when_done": open_when_done}


def run(params, progress=print):
    username       = params["username"]
    password       = params["password"]
    open_when_done = params.get("open_when_done", False)

    stamp   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    t_start = time.time()

    progress("[NetBox] Discovering BCRs (bcr01)...")
    nb_url, nb_token = load_netbox_config()
    nb = NetboxClient(nb_url, nb_token)
    if not nb.ok():
        progress("[!] NetBox not configured -- check NETBOX_URL / NETBOX_TOKEN.")
        return None
    all_bcrs = nb.bcr_devices("bcr01")
    if not all_bcrs:
        progress("[!] No BCR devices found in NetBox matching 'bcr01'.")
        return None
    # Keep only the in-scope US hub sites (ATL/JFK/DFW/LAX); skip anything else
    # NetBox returns.
    bcrs = [b for b in all_bcrs if hub_registry.site_from_hostname(b) in US_SITES]
    skipped = [b for b in all_bcrs if b not in bcrs]
    if skipped:
        progress("  (skipping {} out-of-scope BCR(s): {})".format(
            len(skipped), ", ".join(sorted(skipped))))
    if not bcrs:
        progress("[!] No in-scope BCRs (US sites {}) among: {}".format(
            ", ".join(sorted(US_SITES)), ", ".join(sorted(all_bcrs))))
        return None
    progress("  {} BCR(s) in scope: {}".format(len(bcrs), ", ".join(sorted(bcrs))))

    results = []
    for bcr_host in sorted(bcrs):
        ips, warns, _ = nb.device_mgmt_ips(bcr_host)
        if not ips:
            progress("  [!] {}: no mgmt IP in NetBox ({})".format(
                bcr_host, "; ".join(warns)[:60]))
            results.append({"bcr_host": bcr_host, "bcr_ip": None,
                            "site": hub_registry.site_from_hostname(bcr_host),
                            "bgp": None, "bgp_note": "", "n1800_host": None,
                            "n1800_link": None,
                            "errors": ["no mgmt IP in NetBox"]})
            continue
        try:
            results.append(check_one(bcr_host, ips[0], username, password, nb, progress))
        except CredentialError:
            progress("[!!] Credential error -- halting. Check password.")
            return None

    elapsed  = time.time() - t_start
    report   = build_report(results, stamp, elapsed)
    ts_file  = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out_path = "{}/bcr_1800_check_{}.txt".format(OUTPUT_DIR, ts_file)
    import os
    out_path = os.path.join(OUTPUT_DIR, "bcr_1800_check_{}.txt".format(ts_file))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    progress("\n[Done] Report: {}".format(out_path))

    if open_when_done:
        try:
            import os
            os.startfile(out_path)
        except Exception:
            pass
    return out_path


# ── Self-test (off-box) ──────────────────────────────────────────────────────────

def self_test(verbose=True):
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    # The real bcr01.jfk1 row (2026-11-05).
    raw = ("Neighbor        Spk    AS MsgRcvd MsgSent  TblVer  InQ OutQ  Up/Down  St/PfxRcd\n"
           "10.0.0.3          0 4200000635  235246   81348 12310320    0    0     1w2d          3\n"
           "203.0.113.9       0      65001    1000     900      5    0    0     Idle          Idle\n")
    nb = parse_bgp_summary(raw)
    chk("two neighbor rows parsed", len(nb), 2)
    chk("internal peer established", (nb[0]["established"], nb[0]["pfx_rcvd"]), (True, 3))
    chk("internal peer uptime", nb[0]["up_down"], "1w2d")
    chk("internal flag by private AS", nb[0]["internal"], True)
    chk("external Idle not established", nb[1]["established"], False)
    chk("external not internal", nb[1]["internal"], False)

    peer, note = pick_1800_peer(nb)
    chk("picks the internal peer", peer["neighbor"], "10.0.0.3")
    chk("clean pick, no note", note, "")

    # BCR facing-1800 description.
    f = parse_bcr_facing_1800("TenGigE0/0/0/34  up up  Internal: ces01.jfk1 (s13p1)")
    chk("facing 1800 hostname", f["hostname_1800"], "ces01.jfk1")
    chk("facing 1800 slot/port", (f["slot"], f["port"]), ("13", "1"))

    # 1800-side: `show interfaces physical` (real ces01.jfk1 output, 2026-11-05).
    phys_raw = (
        "Port               Admin      Oper                 PHY Type                Media Type        Ifindex     HoldTimeUp  HoldTimeDown\n"
        "-----------------  ---------  -------------------  ----------------------- ----------------- ----------  ----------  ------------\n"
        "xe-ts13/1          enable     Up                   10GBase-R               Optical           11          20          20\n"
        "xe-ts16/2          enable     Up                   10GBase-R               Optical           20          20          20\n"
        "mnga               enable     Up                   10/100/1000Base-T       Copper            639664128   0           0\n")
    phys = parse_1800_physical(phys_raw)
    chk("physical ports parsed (header/sep skipped)", len(phys), 3)
    chk("xe-ts13/1 Up", phys["xe-ts13/1"]["oper"], "Up")
    chk("xe-ts13/1 admin enable", phys["xe-ts13/1"]["admin"], "enable")
    chk("no header row as a port", "Port" in phys, False)
    # Derive the 1800 port from the BCR facing description.
    chk("derive xe-ts13/1 from (s13p1)",
        derive_1800_port({"slot": "13", "port": "1"}), "xe-ts13/1")
    chk("derive None with no facing", derive_1800_port(None), None)
    # A down port is catchable.
    down = parse_1800_physical("xe-ts13/1  enable  Down  10GBase-R\n")
    chk("down oper captured", down["xe-ts13/1"]["oper"], "Down")

    # US-site scope filter (the four production hubs; others skipped).
    chk("US BCR in scope", hub_registry.site_from_hostname("bcr01.jfk1") in US_SITES, True)
    chk("non-US BCR out of scope", hub_registry.site_from_hostname("bcr01.lhr1") in US_SITES, False)

    print()
    if fails:
        print("FAILED:")
        for f in fails:
            print("  " + f)
        return False
    print("All bcr_1800_check self-tests passed.")
    return True


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    run(collect_inputs())
