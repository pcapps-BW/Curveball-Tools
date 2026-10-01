#!/usr/bin/env python3
"""
reverse_autotester.py -- trace a circuit GSX -> MUX -> 1800 -> 1250.

The forward autotester starts at a 1250 DS1 and walks outward. This starts at
a GSX T1 and walks inward, for when all you have is a GSX alarm or assignment.

Every hop uses a lookup that was built and unit-tested against real captured
output (2026-10-01):

  GSX T1 id              -> shelf/slot/port/t1          (arithmetic)
  GSX node + slot/port   -> MUX T3 controller           mux4216_client.
                                                        t3_for_gsx_assignment()
  T3 controller          -> CE interface                ce_candidates_for_t3(),
                                                        VERIFIED, not guessed
  CE interface           -> ACR : CEM                   show xconnect all
  CEM                    -> STS + DS1                   arithmetic
  ACR                    -> SONET (working/protect)     show acr group
  SONET                  -> 1800 OC + hostname          netbox_client.
                                                        sonet_to_oc()
  OC sub-channel         -> MUSE + remote-tpe (=1250)   chassis_config.
                                                        parse_layer2_transport()
  1250 + MUSE            -> DS1                         site_sweep._discover_ds1s()

READ-ONLY throughout: `show`/`SHOW` only, plus session-only paging.

⚠ THE PER-HOP PARSERS ARE TESTED; THIS ORCHESTRATION IS NOT yet proven against
live devices. Every hop therefore records its own outcome and the run
continues as far as it can, reporting exactly where it stopped and why --
rather than raising and leaving the operator to guess which hop failed. A
partial trace that says where it stopped is useful; a traceback is not.

Built by P. Capps.
"""

import os
import re
import sys
import datetime

import platform_paths
import chassis_config
import mux4216_client
import site_sweep
from mux4216_client import MUX4216Client
from npt1800_client import NPT1800Client
from npt1250_client import NPT1250Client
from gsx_client import GSXClient, find_plink
from device_base import CredentialError
from netbox_client import NetboxClient, load_netbox_config
from secure_config import load_config_silent

SEP  = "=" * 58
SEP2 = "-" * 58

TOOL_INFO = {
    "name": "Reverse Autotester (GSX -> 1250)",
    "slug": "reverse_autotester",
    "ran_from": "GSX",
    "desc": ("Traces a circuit backwards from a GSX T1 to the 1250 DS1, "
             "resolving MUX / ACR / CEM / OC / MUSE along the way."),
    "requires": "GSX IP, GSX T1 id, infrastructure creds, GSX creds",
    "order": 11,
}

# GSX T1 identifiers appear as 'T1-1-11-1-11' or bare '1-11-1-11', i.e.
# shelf-slot-port-t1. The forward tool builds the same string from its parts.
_T1_ID_RE = re.compile(
    r'^(?:T1-)?(?P<shelf>\d+)-(?P<slot>\d+)-(?P<port>\d+)-(?P<t1>\d+)$',
    re.IGNORECASE)

# 'ATLGSX02' -> 'atl'. Same site token set hub_registry uses.
_NODE_SITE_RE = re.compile(r'^(atl|jfk|dfw|lax|lab)', re.IGNORECASE)


def parse_t1_id(raw):
    """
    'T1-1-11-1-11' -> {'shelf','slot','port','t1'} or None.

    Accepts the bare form too, because that is how an engineer reads it off a
    GSX alarm.
    """
    m = _T1_ID_RE.match((raw or "").strip())
    if not m:
        return None
    return {k: m.group(k) for k in ("shelf", "slot", "port", "t1")}


def site_from_gsx_node(node):
    """'ATLGSX02' -> 'atl'. None when no site token is present."""
    m = _NODE_SITE_RE.match((node or "").strip())
    return m.group(1).lower() if m else None


def cem_from_sts_ds1(sts, ds1):
    """
    STS + DS1 -> CEM group, the same formula the forward tool uses:
    STS concatenated with a zero-padded 2-digit DS1. STS 1, DS1 11 -> '111'.
    """
    return "{}{}".format(int(sts), str(int(ds1)).zfill(2))


def sts_ds1_from_cem(cem):
    """
    Inverse of cem_from_sts_ds1: '111' -> ('1', '11'), '411' -> ('4', '11').

    The last two digits are the DS1, everything before is the STS. Returns
    (None, None) for anything shorter than 3 digits, rather than inventing a
    split.
    """
    s = str(cem or "").strip()
    if not s.isdigit() or len(s) < 3:
        return None, None
    return s[:-2].lstrip("0") or "0", s[-2:].lstrip("0") or "0"


class Hop(object):
    """One step of the trace, with its own success/failure recorded."""

    def __init__(self, name):
        self.name = name
        self.ok = False
        self.detail = ""
        self.data = {}

    def done(self, detail="", **data):
        self.ok = True
        self.detail = detail
        self.data.update(data)
        return self

    def fail(self, detail):
        self.ok = False
        self.detail = detail
        return self

    def line(self):
        return "  [{}] {:<34} {}".format(
            " OK " if self.ok else " !! ", self.name, self.detail)


def _verify_ce_iface(cmux, t3_id, want_desc, log):
    """
    Resolve the CE interface name for a T3 controller BY VERIFICATION.

    The running-config prints `controller T3 0/8/45` while the forward tool
    reports `CE8/0/45` -- the same path with the first two components
    transposed. Rather than assume that transposition, each candidate form is
    read back with the forward tool's own command and accepted only if the
    description matches what we were looking for.

    Returns (ce_iface, raw) or (None, reason).
    """
    tried = []
    for cand in mux4216_client.ce_candidates_for_t3(t3_id):
        cmd = "show controllers t3 {} | inc [Dd]escription".format(cand)
        try:
            raw = cmux.run(cmd, 30)
        except Exception as e:
            tried.append("{}: {}: {}".format(cand, type(e).__name__, e))
            continue
        if want_desc.lower() in (raw or "").lower():
            log("    CE interface {} confirmed (description matches)".format(cand))
            return cand, raw
        tried.append("{}: description did not match".format(cand))
    return None, "; ".join(tried) or "no candidate forms to try"


def trace(gsx_ip, t1_id, infra_user, infra_pass, gsx_user, gsx_pass,
          netbox=None, plink_path=None, log=None):
    """
    Walk GSX -> MUX -> 1800 -> 1250. Returns (hops, ctx).

    Never raises for a hop failure: each Hop records its own outcome and the
    walk stops at the first one that cannot continue, so the report can say
    exactly where the trail went cold.
    """
    log = log or (lambda s="": None)
    hops, ctx = [], {"gsx_ip": gsx_ip, "t1_id": t1_id}

    # ---- HOP 0: parse the identifier ----
    h = Hop("GSX T1 identifier")
    parts = parse_t1_id(t1_id)
    if not parts:
        hops.append(h.fail("'{}' is not shelf-slot-port-t1 (e.g. T1-1-11-1-11)"
                           .format(t1_id)))
        return hops, ctx
    ctx.update(parts)
    hops.append(h.done("shelf {shelf} slot {slot} port {port} T1 {t1}"
                       .format(**parts), **parts))

    # ---- HOP 1: the GSX itself ----
    h = Hop("GSX")
    g = None
    try:
        g = GSXClient(gsx_ip, gsx_user, gsx_pass, plink_path or find_plink())
        g.connect()
        node = (getattr(g, "hostname", "") or "").strip()
        ctx["gsx_node"] = node
        ctx["gsx_t1_stats"] = g.pull_t1_stats(parts["slot"], parts["port"],
                                              parts["t1"])
        ctx["gsx_t3_status"] = g.get_t3_status(parts["slot"], parts["port"])
        hops.append(h.done("{} ({})".format(node or "?", gsx_ip), node=node))
        log("  GSX {} -> T1-{}-{}-{}-{}".format(
            node or gsx_ip, parts["shelf"], parts["slot"], parts["port"],
            parts["t1"]))
    except CredentialError:
        raise
    except Exception as e:
        hops.append(h.fail("{}: {}".format(type(e).__name__, e)))
        return hops, ctx
    finally:
        if g is not None:
            try:
                g.close()
            except Exception:
                pass

    # ---- HOP 2: which MUX ----
    h = Hop("MUX identity")
    site = site_from_gsx_node(ctx.get("gsx_node"))
    if not site:
        hops.append(h.fail("cannot derive a site from GSX node '{}' -- "
                           "expected a name starting atl/jfk/dfw/lax"
                           .format(ctx.get("gsx_node") or "?")))
        return hops, ctx
    ctx["site"] = site

    # Reuse the forward tool's resolver: hardcoded map first, then NetBox,
    # every address tried, and the connection verified against the device's
    # own hostname (v2.21/v2.22).
    import autotester_standalone as fwd
    cands = fwd._mux_candidates(site, {t1_id: ""}, {}, netbox, log)
    if not cands:
        hops.append(h.fail("no MUX address for site '{}' from LOC_TO_MUX or "
                           "NetBox".format(site)))
        return hops, ctx
    cmux, mux_info, attempts = fwd._connect_mux(cands, infra_user, infra_pass,
                                                log)
    if cmux is None:
        hops.append(h.fail("no reachable MUX for site '{}' -- tried {}: {}"
                           .format(site, len(attempts), "; ".join(attempts))))
        return hops, ctx
    ctx["mux_name"], ctx["mux_ip"] = mux_info["name"], mux_info["ip"]
    hops.append(h.done("{} ({}) via {}".format(
        mux_info["name"], mux_info["ip"], mux_info["source"])))

    try:
        # ---- HOP 3: GSX assignment -> T3 controller ----
        h = Hop("MUX T3 controller")
        want = mux4216_client.gsx_assignment_desc(
            ctx["gsx_node"], parts["slot"], parts["port"])
        # `| i` loses the controller context entirely -- `| b` is required.
        raw_run = cmux.run("show run | b {}".format(want), 60)
        t3_id, desc = mux4216_client.t3_for_gsx_assignment(
            raw_run, ctx["gsx_node"], parts["slot"], parts["port"])
        if desc == "__AMBIGUOUS__":
            hops.append(h.fail("more than one T3 controller is described as "
                               "'{}' -- refusing to pick".format(want)))
            return hops, ctx
        if not t3_id:
            hops.append(h.fail("no T3 controller described as '{}' on {}"
                               .format(want, ctx["mux_name"])))
            return hops, ctx
        ctx["t3_id"] = t3_id
        hops.append(h.done("controller T3 {} (description '{}')".format(
            t3_id, desc)))

        # ---- HOP 4: T3 controller -> CE interface (verified) ----
        h = Hop("GSX-facing CE interface")
        ce_iface, why = _verify_ce_iface(cmux, t3_id, want, log)
        if not ce_iface:
            hops.append(h.fail("could not confirm a CE interface for T3 {}: {}"
                               .format(t3_id, why)))
            return hops, ctx
        ctx["ce_iface"] = ce_iface
        hops.append(h.done("CE{} (confirmed by reading its description back)"
                           .format(ce_iface)))

        # ---- HOP 5: CE -> ACR : CEM ----
        h = Hop("ACR / CEM")
        raw_xc = cmux.run("show xconnect all | i CE{}:".format(ce_iface), 60)
        # Anchored so CE8/0/45:111 is not matched by a search for :11.
        pairs = []
        for m in re.finditer(r'CE(\d+):(\d+)(?!\d)', raw_xc or ""):
            acr, cem = m.group(1), m.group(2)
            if (acr, cem) not in pairs:
                pairs.append((acr, cem))
        want_cem = None
        for acr, cem in pairs:
            sts, ds1 = sts_ds1_from_cem(cem)
            if ds1 and int(ds1) == int(parts["t1"]):
                want_cem = (acr, cem, sts, ds1)
                break
        if not want_cem:
            hops.append(h.fail(
                "no ACR:CEM on CE{} whose CEM maps to DS1 {} (found {})"
                .format(ce_iface, parts["t1"],
                        ", ".join("{}:{}".format(a, c) for a, c in pairs)
                        or "none")))
            return hops, ctx
        acr, cem, sts, ds1 = want_cem
        ctx.update({"acr": acr, "cem": cem, "sts": sts, "ds1_num": ds1})
        hops.append(h.done("ACR {} CEM {} -> STS {} DS1 {}".format(
            acr, cem, sts, ds1)))

        # ---- HOP 6: ACR -> SONET ----
        h = Hop("ACR -> SONET")
        raw_acr = cmux.run("show acr group", 60)
        acr_rows = []
        for line in (raw_acr or "").splitlines():
            m = re.match(r'\s*(\d+)\s+SONET\s+([\d/]+)\s+SONET\s+([\d/]+)'
                         r'(?:\s+SONET\s+([\d/]+))?', line)
            if m:
                acr_rows.append({"acr": m.group(1), "working": m.group(2),
                                 "protect": m.group(3),
                                 "active": m.group(4) or ""})
        row = next((r for r in acr_rows if r["acr"] == acr), None)
        if not row:
            hops.append(h.fail("ACR {} not present in `show acr group` on {}"
                               .format(acr, ctx["mux_name"])))
            return hops, ctx
        on_protect = bool(row["active"] and row["active"] != row["working"])
        ctx.update({"sonet_working": row["working"],
                    "sonet_protect": row["protect"],
                    "sonet_active": row["active"],
                    "on_protect": on_protect})
        hops.append(h.done(
            "working {} / protect {} / active {}{}".format(
                row["working"], row["protect"], row["active"] or "(none)",
                "   << ON PROTECT" if on_protect else "")))
    finally:
        try:
            cmux.close()
        except Exception:
            pass

    # ---- HOP 7: SONET -> 1800 OC ----
    h = Hop("SONET -> 1800 OC")
    if netbox is None:
        hops.append(h.fail("NetBox not configured -- the SONET-to-OC cable is "
                           "only recorded there"))
        return hops, ctx
    # Prefer the ACTIVE leg; it is the path actually carrying the circuit.
    leg = ctx.get("sonet_active") or ctx.get("sonet_working")
    oc_name, hub_host = netbox.sonet_to_oc(ctx["mux_name"], leg)
    if oc_name == "__AMBIGUOUS__":
        hops.append(h.fail("several MUX interfaces match {} in NetBox -- "
                           "refusing to pick".format(leg)))
        return hops, ctx
    if not oc_name or not hub_host:
        hops.append(h.fail("NetBox has no cabled OC for {} {}".format(
            ctx["mux_name"], leg)))
        return hops, ctx
    ctx.update({"oc_name": oc_name, "hub_hostname": hub_host})
    hops.append(h.done("{} on {}".format(oc_name, hub_host)))

    # ---- HOP 8: OC sub-channel -> MUSE + the 1250's address ----
    h = Hop("1800 sub-channel -> 1250")
    import hub_registry
    site_code = hub_registry.site_from_hostname(hub_host) or site.upper()
    hub_ip, _src, _w = hub_registry.site_to_ip(site_code)
    if not hub_ip:
        hops.append(h.fail("no management IP for hub site '{}'".format(site_code)))
        return hops, ctx
    base = re.sub(r':.*$', '', oc_name)
    sub = "{}:{}:{}".format(base, ctx["sts"], ctx["ds1_num"])
    ctx["oc_subchannel"] = sub
    c1800 = None
    try:
        c1800 = NPT1800Client(hub_ip, infra_user, infra_pass)
        c1800.connect()
        raw_l2 = c1800.run(
            "show configuration interfaces {} | display-set | no-more".format(sub),
            60)
    except CredentialError:
        raise
    except Exception as e:
        hops.append(h.fail("1800 {} ({}): {}: {}".format(
            hub_host, hub_ip, type(e).__name__, e)))
        return hops, ctx
    finally:
        if c1800 is not None:
            try:
                c1800.close()
            except Exception:
                pass

    # The scoped command strips the interface prefix, hence the hint.
    by_muse, unparsed = chassis_config.parse_layer2_transport(
        raw_l2, iface_hint=sub)
    if not by_muse:
        hops.append(h.fail("no layer2-transport block on {} {} -- is the "
                           "sub-channel right?".format(hub_host, sub)))
        return hops, ctx
    if len(by_muse) > 1:
        hops.append(h.fail("{} carries {} MUSE circuits ({}) -- refusing to "
                           "pick".format(sub, len(by_muse),
                                         ", ".join(sorted(by_muse)))))
        return hops, ctx
    ent = list(by_muse.values())[0]
    ctx["muse"] = ent["muse"]
    ctx["remote_tpe"] = ent["remote_tpe"]
    ctx["l2t"] = ent
    ctx["l2t_unparsed"] = unparsed
    if not ent["remote_tpe"]:
        hops.append(h.fail("{} has no remote-tpe address, so the 1250 cannot "
                           "be identified".format(ent["muse"])))
        return hops, ctx
    hops.append(h.done("{} -> 1250 at {}{}".format(
        ent["muse"], ent["remote_tpe"],
        "  (label {})".format(ent["label"]) if ent["label"] else "")))

    # ---- HOP 9: the 1250 DS1 ----
    h = Hop("1250 DS1")
    c1250 = None
    try:
        c1250 = NPT1250Client(ctx["remote_tpe"], infra_user, infra_pass)
        c1250.connect()
        ctx["host_1250"] = (getattr(c1250, "hostname", "") or "").strip()
        ds1_list, loopback, config_map = site_sweep._discover_ds1s(c1250)
    except CredentialError:
        raise
    except Exception as e:
        hops.append(h.fail("1250 {}: {}: {}".format(
            ctx["remote_tpe"], type(e).__name__, e)))
        return hops, ctx
    finally:
        if c1250 is not None:
            try:
                c1250.close()
            except Exception:
                pass

    matches = [d for d, v in (config_map or {}).items()
               if (v.get("muse_id") or "").upper() == ctx["muse"].upper()]
    ctx["loopback_1250"] = loopback
    if not matches:
        hops.append(h.fail(
            "{} is not assigned to any DS1 on {} ({} MUSE circuits found) -- "
            "the 1800 and the 1250 disagree".format(
                ctx["muse"], ctx.get("host_1250") or ctx["remote_tpe"],
                len(config_map or {}))))
        return hops, ctx
    if len(matches) > 1:
        hops.append(h.fail("{} appears on {} DS1s ({}) -- refusing to pick"
                           .format(ctx["muse"], len(matches),
                                   ", ".join(sorted(matches)))))
        return hops, ctx
    ctx["ds1_1250"] = matches[0]
    hops.append(h.done("{}  ds1-{}  ({})".format(
        ctx.get("host_1250") or "?", matches[0], ctx["muse"])))
    return hops, ctx


def render(hops, ctx):
    """The report. Shows the trail, then whatever the walk resolved."""
    out = [SEP, "  REVERSE TRACE  --  GSX {} T1 {}".format(
        ctx.get("gsx_node") or ctx.get("gsx_ip", "?"), ctx.get("t1_id", "?")),
        "  Run: {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
        SEP, ""]
    for h in hops:
        out.append(h.line())
    stopped = next((h for h in hops if not h.ok), None)
    out += ["", SEP2]
    if stopped:
        out += ["  TRAIL WENT COLD AT: {}".format(stopped.name),
                "  {}".format(stopped.detail),
                "  Everything above this line resolved; nothing below was "
                "attempted."]
    else:
        out.append("  FULL PATH RESOLVED")
    out += [SEP2, ""]

    out += ["  PATH",
            "    GSX        : {} ({})  T1-{}-{}-{}-{}".format(
                ctx.get("gsx_node", "?"), ctx.get("gsx_ip", "?"),
                ctx.get("shelf", "?"), ctx.get("slot", "?"),
                ctx.get("port", "?"), ctx.get("t1", "?")),
            "    MUX        : {} ({})".format(
                ctx.get("mux_name", "?"), ctx.get("mux_ip", "?")),
            "    T3 / CE    : T3 {} -> CE{}".format(
                ctx.get("t3_id", "?"), ctx.get("ce_iface", "?")),
            "    ACR / CEM  : ACR {} CEM {} (STS {} DS1 {})".format(
                ctx.get("acr", "?"), ctx.get("cem", "?"),
                ctx.get("sts", "?"), ctx.get("ds1_num", "?")),
            "    SONET      : working {} / protect {} / active {}".format(
                ctx.get("sonet_working", "?"), ctx.get("sonet_protect", "?"),
                ctx.get("sonet_active") or "(none)"),
            "    1800       : {} {}".format(
                ctx.get("hub_hostname", "?"), ctx.get("oc_subchannel", "")),
            "    MUSE       : {}".format(ctx.get("muse", "?")),
            "    1250       : {} ds1-{}  (loopback {})".format(
                ctx.get("host_1250", "?"), ctx.get("ds1_1250", "?"),
                ctx.get("loopback_1250", "?")),
            ""]

    if ctx.get("on_protect"):
        out += ["  [ !! ] ON PROTECT: ACR {} is running on {} (protect); "
                "working leg {} is".format(
                    ctx.get("acr", "?"), ctx.get("sonet_active", "?"),
                    ctx.get("sonet_working", "?")),
                "         not carrying the circuit.", ""]

    l2t = ctx.get("l2t") or {}
    if l2t:
        out += ["  CIRCUIT RECORD (from the 1800's own config)"]
        for k, label in (("label", "NMS label"), ("customer", "Customer"),
                         ("pw_type", "PW type"), ("pw_id", "PW id"),
                         ("l2vpn_id", "L2VPN id"), ("mtu", "L2 service MTU"),
                         ("peer_address", "Peer address")):
            if l2t.get(k):
                out.append("    {:<16} {}".format(label, l2t[k]))
        if l2t.get("label_vlan"):
            out.append("    {:<16} {} (cross-check against the EVC "
                       "sub-interface)".format("Label VLAN", l2t["label_vlan"]))
        out.append("")
    if ctx.get("l2t_unparsed"):
        out += ["  [ !! ] layer2-transport options this parser does not know:"]
        for muse, rest in ctx["l2t_unparsed"][:6]:
            out.append("         {}: {}".format(muse, rest))
        out.append("")

    gsx = ctx.get("gsx_t1_stats") or []
    if gsx:
        out += [SEP, "  GSX T1 DETAIL", SEP]
        for label, cmd, res in gsx:
            out += ["", "  >> {}".format(label), "  CMD: {}".format(cmd)]
            for ln in (res or "").splitlines():
                out.append("    {}".format(ln))
        out.append("")
    if ctx.get("gsx_t3_status"):
        out += [SEP2, "  GSX PARENT T3 STATUS", SEP2]
        for ln in ctx["gsx_t3_status"].splitlines():
            out.append("    {}".format(ln))
    return "\n".join(out)


def collect_inputs(session=None):
    """Prompt for inputs; return params dict (or None to abort)."""
    import getpass
    session = session if session is not None else {}
    print()
    print("Reverse Autotester -- traces GSX -> MUX -> 1800 -> 1250.")
    print("Read-only: 'show' commands only.")
    print()
    gsx_ip = input("GSX IP: ").strip()
    if not gsx_ip:
        print("Missing GSX IP.")
        return None
    t1_id = input("GSX T1 (e.g. T1-1-11-1-11): ").strip()
    if not parse_t1_id(t1_id):
        print("Expected shelf-slot-port-t1, e.g. T1-1-11-1-11.")
        return None
    user = session.get("infra_user") or input("Infrastructure username: ").strip()
    pw   = session.get("infra_pass") or getpass.getpass("Infrastructure password: ")
    guser = session.get("gsx_user") or input("GSX username: ").strip()
    gpw   = session.get("gsx_pass") or getpass.getpass("GSX password: ")
    if not (user and pw and guser and gpw):
        print("Missing credentials.")
        return None
    return {"gsx_ip": gsx_ip, "t1_id": t1_id, "username": user, "password": pw,
            "gsx_user": guser, "gsx_pass": gpw, "open_when_done": True}


def run(params):
    """Execute the reverse trace given a params dict (no prompting)."""
    progress = params.get("progress") or (lambda s="": print(s))

    netbox = None
    try:
        url, token = load_netbox_config()
        nb = NetboxClient(url, token)
        netbox = nb if nb.ok() else None
    except Exception as e:
        progress("  [!] NetBox unavailable: {}: {}".format(type(e).__name__, e))
    if netbox is None:
        progress("  [!] NetBox not configured -- the SONET-to-OC hop will fail")

    hops, ctx = trace(params["gsx_ip"], params["t1_id"],
                      params["username"], params["password"],
                      params["gsx_user"], params["gsx_pass"],
                      netbox=netbox, log=progress)
    body = render(hops, ctx)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = (ctx.get("host_1250") or ctx.get("gsx_node")
           or params["gsx_ip"]).replace("/", "_")
    out_path = platform_paths.path("Reverse_Autotester_{}_{}.txt".format(
        stamp, tag))
    try:
        with open(out_path, "w") as f:
            f.write(body)
    except Exception as e:
        progress("  [!] could not write {}: {}".format(out_path, e))
        print(body)
        return None

    progress("")
    progress(body)
    progress("")
    progress("  written: {}".format(out_path))
    if params.get("open_when_done") and os.name == "nt":
        try:
            os.startfile(out_path)      # noqa: S606
        except Exception:
            pass
    return out_path


def self_test(verbose=True):
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    chk("T1 id with prefix", parse_t1_id("T1-1-11-1-11"),
        {"shelf": "1", "slot": "11", "port": "1", "t1": "11"})
    chk("T1 id bare", parse_t1_id("1-11-1-11"),
        {"shelf": "1", "slot": "11", "port": "1", "t1": "11"})
    chk("T1 id lowercase", parse_t1_id("t1-1-7-1-16")["t1"], "16")
    chk("T1 id junk", parse_t1_id("ts1/27"), None)
    chk("T1 id too few parts", parse_t1_id("T1-1-11-1"), None)

    chk("site from node", site_from_gsx_node("ATLGSX02"), "atl")
    chk("site from node lax", site_from_gsx_node("LAXGSX01"), "lax")
    chk("site from unknown node", site_from_gsx_node("ZZZGSX01"), None)

    # CEM <-> STS/DS1 must round-trip, and match the forward formula.
    chk("cem from sts/ds1", cem_from_sts_ds1(1, 11), "111")
    chk("cem from sts/ds1 (4,11)", cem_from_sts_ds1(4, 11), "411")
    chk("cem from sts/ds1 (7,23)", cem_from_sts_ds1(7, 23), "723")
    chk("sts/ds1 from cem 111", sts_ds1_from_cem("111"), ("1", "11"))
    chk("sts/ds1 from cem 411", sts_ds1_from_cem("411"), ("4", "11"))
    chk("sts/ds1 from cem 723", sts_ds1_from_cem("723"), ("7", "23"))
    chk("sts/ds1 from cem 1301", sts_ds1_from_cem("1301"), ("13", "1"))
    chk("sts/ds1 from short", sts_ds1_from_cem("11"), (None, None))
    for sts, ds1 in ((1, 11), (4, 11), (7, 23), (13, 1)):
        rt = sts_ds1_from_cem(cem_from_sts_ds1(sts, ds1))
        chk("round-trip STS {} DS1 {}".format(sts, ds1),
            (int(rt[0]), int(rt[1])), (sts, ds1))

    # A failed hop must stop the trail and say so, not raise.
    hops, ctx = trace("10.0.0.1", "nonsense", "u", "p", "gu", "gp")
    chk("bad T1 id stops at hop 0", len(hops), 1)
    chk("  and is marked failed", hops[0].ok, False)
    rep = render(hops, ctx)
    chk("  report names where it stopped",
        "TRAIL WENT COLD AT: GSX T1 identifier" in rep, True)
    chk("  report still prints the PATH skeleton", "PATH" in rep, True)

    if verbose:
        if fails:
            print("\nFAILED:")
            for f in fails:
                print("  !! " + f)
        else:
            print("\nAll reverse_autotester self-tests passed.")
    return not fails


def main():
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    p = collect_inputs()
    if p:
        run(p)


if __name__ == "__main__":
    main()
