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

PROVEN END TO END on live devices 2026-10-01: DFWGSX04 T1-1-6-1-23 resolved
all ten hops through to MDLDTXMU542 ds1-ts1/27 (MUSE_972). Getting there took
v2.27-v2.33 and every failure was in a lookup, not in the walk:

  v2.27  GSXClient had no `hostname` attribute at all -- hop 1 could never
         name the node, and reported OK anyway
  v2.30  `show run | b <description>` hides the first controller; the tiebreak
         for duplicate descriptions is the per-T1 cem-group, not the
         description
  v2.31  NetBox `connected_endpoints` is empty when the cable PATH is
         unresolved, even though the cable is recorded
  v2.32  `?device=<name>` returns HTTP 400 on a name mismatch, not an empty
         list
  v2.33  NetBox stores site-suffixed names, and a contains search also returns
         the patch panels

Every hop records its own outcome and the run continues as far as it can,
reporting exactly where it stopped and why -- rather than raising and leaving
the operator to guess which hop failed. That design is what made the above
tractable: each live run named one more wrong assumption instead of producing
a traceback.

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
import gsx_client
import autotester_standalone
from gsx_client import GSXClient, find_plink
from device_base import CredentialError
from netbox_client import NetboxClient, load_netbox_config
from secure_config import load_config_silent
from jira_client import post_report, valid_ticket, post_slack, JIRA_BASE

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
        node_src = "login prompt"
        ctx["gsx_t1_stats"] = g.pull_t1_stats(parts["slot"], parts["port"],
                                              parts["t1"])
        ctx["gsx_t3_status"] = g.get_t3_status(parts["slot"], parts["port"])
        # ISUP circuit + trunk group, information only (mgmt/Barlow,
        # 2026-11-02). Read-only, on the GSX we are already logged into.
        _isup = g.pull_isup_circuit(parts["slot"], parts["port"], parts["t1"])
        ctx["gsx_isup_id"]   = _isup.get("isup_id")
        ctx["gsx_isup_raw"]  = _isup.get("isup_raw", "")
        ctx["gsx_trunk_raw"] = _isup.get("trunk_raw", "")
        # Second source, no extra command: every GSX command's output starts
        # 'Node: ATLGSX02  Date: ...'. The prompt is the primary (it is the
        # device naming itself at login), this is the backstop.
        if not node:
            for _label, _cmd, _out in ctx["gsx_t1_stats"]:
                node = gsx_client.node_from_output(_out)
                if node:
                    node_src = "Node: header of {}".format(_label)
                    break
        if not node:
            node = gsx_client.node_from_output(ctx["gsx_t3_status"])
            if node:
                node_src = "Node: header of T3 STATUS"
        ctx["gsx_node"] = node
        # Every hop below needs the node name: the site prefix picks the MUX,
        # and the T3 controller is found by matching 'NODE-slot-port' in the
        # MUX's own descriptions. Without it the walk cannot continue, so this
        # hop FAILS here rather than reporting OK with a '?' and letting the
        # MUX hop report the symptom as its own failure.
        if not node:
            hops.append(h.fail(
                "connected to {} but could not read the node name -- not in "
                "the login prompt and no 'Node:' header in any command "
                "output. Every hop below needs it (site -> MUX, and "
                "NODE-slot-port -> T3 controller).".format(gsx_ip)))
            return hops, ctx
        hops.append(h.done("{} ({}) via {}".format(node, gsx_ip, node_src),
                           node=node))
        log("  GSX {} -> T1-{}-{}-{}-{}".format(
            node, parts["shelf"], parts["slot"], parts["port"],
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
        # Begin at a CONTROLLER HEADER, not at the description.
        #
        # `| b <description>` starts output AT the first matching description
        # line, so the `controller T3` header that owns it is above the dump
        # and that controller is invisible to the parser. On DFW1-MUX-04 that
        # turned a three-way duplicate into a reported two-way one
        # (2026-10-01). `| section` is no help either -- on this platform it
        # behaves like `| include`. See mux4216_client.T3_CONFIG_CMD.
        raw_run = cmux.run(mux4216_client.T3_CONFIG_CMD, 90)
        t3_id, desc, cem_id, note = mux4216_client.t3_for_gsx_t1(
            raw_run, ctx["gsx_node"], parts["slot"], parts["port"],
            parts["t1"])
        if not t3_id:
            hops.append(h.fail("{} -- on {}. Check `{}`.".format(
                note, ctx["mux_name"], mux4216_client.T3_CONFIG_CMD)))
            return hops, ctx
        ctx["t3_id"] = t3_id
        ctx["t3_cem_from_config"] = cem_id
        if note:
            # Resolved, but the duplicate descriptions behind it are still a
            # config problem somebody should fix -- so it rides along to
            # JIRA/Slack rather than living only in the console log.
            ctx["t3_ambiguity_resolved"] = note
            log("    [!] {}".format(note))
        hops.append(h.done(
            "controller T3 {} (description '{}', t1 {} cem-group {}){}".format(
                t3_id, desc, parts["t1"], cem_id,
                "  [see note]" if note else "")))

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
        # Third source for the CEM, free: hop 3 read `t1 23 cem-group 123`
        # out of the running-config, and this hop just read the CEM off the
        # LIVE xconnect. They should agree. If they don't, the config and the
        # forwarding state have drifted -- and the live value is the one
        # actually carrying traffic, so the walk keeps going with it and says
        # so rather than silently preferring either.
        _cfg_cem = ctx.get("t3_cem_from_config")
        if _cfg_cem and str(_cfg_cem) != str(cem):
            ctx["cem_config_conflict"] = (
                "CEM drift on T3 {}: running-config says t1 {} cem-group {}, "
                "the live xconnect says {} -- continuing with the live value"
                .format(ctx["t3_id"], parts["t1"], _cfg_cem, cem))
            log("    [!] {}".format(ctx["cem_config_conflict"]))
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
    nb = netbox.sonet_to_oc_detail(ctx["mux_name"], leg)
    oc_name, hub_host = nb.get("oc_name"), nb.get("hub_hostname")
    if nb.get("ambiguous"):
        hops.append(h.fail("{} -- refusing to pick".format(nb["reason"])))
        return hops, ctx
    if not oc_name or not hub_host:
        # Say WHICH kind of gap this is. "no cabled OC" covered three very
        # different situations -- interface missing from NetBox, interface
        # present with no cable, and cable present but path unresolved -- and
        # they send the operator to three different places. NetBox showed
        # cable #25727 on DFW1-MUX-04 0/13/1 while this hop reported no cable
        # at all (2026-10-01).
        hops.append(h.fail("could not resolve the 1800 OC for {} {}: {}"
                           .format(ctx["mux_name"], leg,
                                   nb.get("reason") or "no reason recorded")))
        return hops, ctx
    ctx.update({"oc_name": oc_name, "hub_hostname": hub_host})
    if nb.get("source") and "path endpoint" not in nb["source"]:
        # Resolved, but from weaker evidence than a traced path. Say so --
        # it is still a NetBox data problem worth fixing even though the
        # trace continued.
        ctx["oc_source_note"] = (
            "{} {} -> {} {} came from {} rather than a resolved NetBox cable "
            "path -- the cable is recorded but the path is incomplete"
            .format(ctx["mux_name"], leg, hub_host, oc_name, nb["source"]))
        log("    [!] {}".format(ctx["oc_source_note"]))
    hops.append(h.done("{} on {}{}".format(
        oc_name, hub_host,
        "   (via {})".format(nb["source"]) if nb.get("source") else "")))

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


def confirm_route(ctx, fwd_mux, started):
    """
    Compare the GSX assignment the FORWARD walk derived against the GSX T1
    the reverse trace started from.

    `started` is {"node","slot","port","t1"} -- what the operator typed.
    `fwd_mux` is the forward run's per-DS1 mux_results entry.

    Returns (agrees, lines). This is the point of chaining the two walks:
    the forward run re-derives MUX, ACR, CEM, T3 and GSX assignment from the
    1250 end using NONE of the reverse trace's lookups. Arriving back at the
    same GSX T1 confirms the reverse trace by an independent path. Arriving
    somewhere else means one of them is wrong, and that is worth more than
    either report on its own.

    A missing forward value is NOT agreement -- it is reported as unchecked.
    """
    fields = [
        ("GSX node", started.get("node"), (fwd_mux or {}).get("gsx_node"),
         True),
        ("slot",     started.get("slot"), (fwd_mux or {}).get("gsx_slot"),
         False),
        ("port",     started.get("port"), (fwd_mux or {}).get("gsx_port"),
         False),
        ("T1",       started.get("t1"),   (fwd_mux or {}).get("gsx_t1"),
         False),
    ]
    agree, differ, unchecked = [], [], []
    for label, want, got, is_name in fields:
        if got in (None, "", "?"):
            unchecked.append(label)
            continue
        if is_name:
            same = gsx_client.node_names_match(str(want), str(got))
        else:
            # Numeric where possible, so '1' and '01' are the same port.
            try:
                same = int(str(want)) == int(str(got))
            except (TypeError, ValueError):
                same = str(want).strip().lower() == str(got).strip().lower()
        (agree if same else differ).append((label, want, got))

    lines = []
    if differ:
        lines.append("  [ !! ] ROUTE CONFLICT -- the forward walk from {} {} "
                     "did NOT come back to the GSX T1 this trace started "
                     "from:".format(ctx.get("host_1250") or "the 1250",
                                    ctx.get("ds1_1250") or "?"))
        for label, want, got in differ:
            lines.append("           {:9} reverse started at {!r}, forward "
                         "derived {!r}".format(label, str(want), str(got)))
        lines.append("           One of the two routes is wrong. Do not act "
                     "on either until this is settled.")
    elif agree and not unchecked:
        lines.append("  [ OK ] ROUTE CONFIRMED -- the forward walk from {} {} "
                     "independently derived the same GSX T1 ({} {}-{}-{}), "
                     "sharing none of this trace's lookups.".format(
                         ctx.get("host_1250") or "the 1250",
                         ctx.get("ds1_1250") or "?",
                         started.get("node"), started.get("slot"),
                         started.get("port"), started.get("t1")))
    elif agree:
        lines.append("  [ -- ] ROUTE PARTLY CONFIRMED -- {} matched; the "
                     "forward walk did not report {}.".format(
                         ", ".join(l for l, _, _ in agree),
                         ", ".join(unchecked)))
    else:
        lines.append("  [ -- ] ROUTE NOT CONFIRMED -- the forward walk "
                     "reported no GSX assignment ({}), so there is nothing "
                     "to compare.".format(", ".join(unchecked) or "no fields"))

    # The ACR/CEM are also derived twice. Not part of the agree/differ
    # verdict above (the GSX T1 is the identity of the circuit), but a
    # mismatch here is still a real contradiction worth printing.
    for label, rev_key, fwd_key in (("ACR", "acr", "acr_group"),
                                    ("CEM", "cem", "cem_group")):
        rev_v, fwd_v = ctx.get(rev_key), (fwd_mux or {}).get(fwd_key)
        if rev_v and fwd_v and str(rev_v) != str(fwd_v):
            lines.append("  [ !! ] {} differs: reverse resolved {}, forward "
                         "resolved {}".format(label, rev_v, fwd_v))
    # Confirmed means EVERY field was checked and every one matched.
    # `not differ and agree` is not enough: a run where only the node and
    # slot came back would return True with the T1 -- the actual identity of
    # the circuit -- never compared, and the summary would print "route
    # confirmed". Anything unchecked is not confirmation.
    return (not differ and bool(agree) and not unchecked), lines


def _slack_summary(ctx):
    """
    The five-line resolved path, used verbatim by BOTH the JIRA
    always-visible summary and the Slack post. One builder, so the two
    channels can never disagree about what the trace found.

    Unresolved hops show '?' rather than being dropped -- a summary that
    silently omits the hop that failed reads as though it succeeded.
    """
    out = [
        "GSX   : {} ({})".format(ctx.get("gsx_node", "?"),
                                 ctx.get("gsx_ip", "?")),
        "MUX   : {}  T3 {} -> CE{}".format(
            ctx.get("mux_name", "?"), ctx.get("t3_id", "?"),
            ctx.get("ce_iface", "?")),
        "ACR   : {} CEM {} (STS {} DS1 {})".format(
            ctx.get("acr", "?"), ctx.get("cem", "?"),
            ctx.get("sts", "?"), ctx.get("ds1_num", "?")),
        "1800  : {} {}  MUSE {}".format(
            ctx.get("hub_hostname", "?"), ctx.get("oc_subchannel", ""),
            ctx.get("muse", "?")),
        "1250  : {} ds1-{}".format(ctx.get("host_1250", "?"),
                                   ctx.get("ds1_1250", "?")),
    ]
    if ctx.get("on_protect"):
        out.append("[!!] ON PROTECT: ACR {} is on {} (protect leg)".format(
            ctx.get("acr", "?"), ctx.get("sonet_active", "?")))
    # A T3 that had to be narrowed from duplicates is a resolved result, but
    # it rests on a config the operator should still go fix -- so it travels
    # with the summary to JIRA/Slack, not just the live log.
    if ctx.get("t3_ambiguity_resolved"):
        out.append("[--] {}".format(ctx["t3_ambiguity_resolved"]))
    if ctx.get("cem_config_conflict"):
        out.append("[!!] {}".format(ctx["cem_config_conflict"]))
    if ctx.get("oc_source_note"):
        out.append("[--] {}".format(ctx["oc_source_note"]))
    # The confirmation verdict is the headline for anyone reading only the
    # summary: it says whether a second, independent walk agreed that this is
    # the circuit. A route conflict belongs at the top of a ticket, not
    # buried in an appended section.
    if ctx.get("route_confirmed") is True:
        out.append("[OK] route confirmed by an independent forward walk from "
                   "{} {}".format(ctx.get("host_1250", "?"),
                                  ctx.get("ds1_1250", "?")))
    elif ctx.get("route_confirmed") is False:
        out.append("[!!] ROUTE CONFLICT -- the forward walk derived a "
                   "different GSX T1. See the report.")
    return "\n".join(out)


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
                ctx.get("port", "?"), ctx.get("t1", "?")),]
    if ctx.get("gsx_isup_id"):
        out.append("    ISUP       : {}".format(ctx["gsx_isup_id"]))
    out += [
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
    # ISUP circuit + trunk group, information only (mgmt/Barlow).
    if ctx.get("gsx_isup_raw") or ctx.get("gsx_trunk_raw"):
        out += [SEP, "  GSX ISUP CIRCUIT  (information only)", SEP]
        if ctx.get("gsx_isup_id"):
            out.append("  ISUP Circuit ID: {}".format(ctx["gsx_isup_id"]))
        out.append("  CMD: SHOW ISUP CIRCUIT SHELF 1 SLOT {} PORT {} DS1 {} "
                   "status".format(ctx.get("slot", "?"), ctx.get("port", "?"),
                                   ctx.get("t1", "?")))
        for ln in (ctx.get("gsx_isup_raw") or "  (no output)").splitlines():
            out.append("    {}".format(ln))
        out.append("")
        if ctx.get("gsx_isup_id"):
            out.append("  CMD: show trunk group {} admin".format(
                ctx["gsx_isup_id"]))
        for ln in (ctx.get("gsx_trunk_raw") or "  (no output)").splitlines():
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
    # Same prompt loop as the forward autotester: blank skips the post, a bad
    # ticket re-prompts rather than being silently dropped.
    jira_ticket = None
    while True:
        raw = input("JIRA number (NOE-XXXX, blank to skip): ").strip().upper()
        if not raw:
            break
        if valid_ticket(raw):
            jira_ticket = raw
            break
        print("  Expected NOE-1234.")
    ans = input("Pull PMs & diagnostics once the route resolves? "
                "[Y/n]: ").strip().lower()
    analyze = not ans.startswith("n")
    return {"gsx_ip": gsx_ip, "t1_id": t1_id, "username": user, "password": pw,
            "gsx_user": guser, "gsx_pass": gpw, "jira_ticket": jira_ticket,
            "analyze": analyze, "open_when_done": True}


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

    # ---- chain into the forward autotester ---------------------------------
    #
    # The reverse trace's job ends at "which circuit is this". Everything
    # after that -- PMs at all four hops, the diagnostic summary, the per-DS1
    # run summary line -- already exists and is proven in
    # autotester_standalone. Reimplementing it here would be a second copy to
    # keep in step with the first, which is how the two drift apart.
    #
    # Only the ENDPOINT is handed over: the 1250 address and the DS1. The
    # forward run re-derives MUSE, OC sub-channel, ACR, CEM, T3 and the GSX
    # assignment itself, from the other end, using none of this trace's
    # lookups -- so its result is an independent check on this one
    # (confirm_route below), not just a continuation.
    #
    # Its own JIRA/Slack/auto-open are suppressed: this tool owns the output
    # and posts ONE combined report rather than two half-reports.
    if params.get("analyze", True) and ctx.get("ds1_1250") and \
            ctx.get("remote_tpe"):
        progress("")
        progress("=" * 58)
        progress("  Route resolved. Running the forward autotester against "
                 "{} {} for PMs and diagnostics.".format(
                     ctx.get("host_1250") or ctx["remote_tpe"],
                     ctx["ds1_1250"]))
        progress("=" * 58)
        extra = {}
        fwd_params = {
            "ip_1250": ctx["remote_tpe"],
            "ds1_list": [ctx["ds1_1250"]],
            "infra_user": params["username"], "infra_pass": params["password"],
            "gsx_user": params["gsx_user"], "gsx_pass": params["gsx_pass"],
            "jira_ticket": None,        # this tool posts, not that one
            "slack_webhook": None,
            "open_when_done": False,
            "_extra_out": extra,
        }
        try:
            autotester_standalone.run(fwd_params)
        except CredentialError:
            raise
        except Exception as e:
            body += ("\n\n" + SEP + "\n  FORWARD ANALYSIS FAILED\n" + SEP +
                     "\n  {}: {}\n  The route above still resolved -- only the"
                     " PM/diagnostic pass failed.".format(
                         type(e).__name__, e))
            extra = {}

        if extra:
            fwd_mux = (extra.get("mux_results") or {}).get(ctx["ds1_1250"], {})
            # slot/port/t1 come from ctx (trace() put parse_t1_id's result
            # there), NOT from a local -- this is run()'s scope.
            agrees, conf_lines = confirm_route(ctx, fwd_mux, {
                "node": ctx.get("gsx_node"), "slot": ctx.get("slot"),
                "port": ctx.get("port"), "t1": ctx.get("t1")})
            ctx["route_confirmed"] = agrees
            ctx["route_confirmation"] = "\n".join(conf_lines)
            section = [SEP, "  INDEPENDENT ROUTE CONFIRMATION", SEP]
            section += conf_lines
            if extra.get("run_summary"):
                section += ["", SEP, "  RUN SUMMARY (forward)", SEP,
                            extra["run_summary"]]
            if extra.get("route_diag"):
                section += ["", extra["route_diag"]]
            body += "\n\n" + "\n".join(section)
            ctx["jira_summary_fwd"] = extra.get("jira_summary") or ""
            for line in conf_lines:
                progress(line)

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

    # ---- optional JIRA post -------------------------------------------------
    # Mirrors the forward autotester: the one-line intro says where the trail
    # ended, the always-visible summary carries the resolved path, and the full
    # report goes in the collapsed panel. A reverse trace that stopped early is
    # still worth posting -- the hops it DID resolve are the finding -- so this
    # posts regardless of whether the walk completed, and says which it was.
    jira_ticket = params.get("jira_ticket")
    if jira_ticket and out_path:
        stopped = next((h for h in hops if not h.ok), None)
        if stopped:
            intro = ("Reverse trace from GSX {} {} -- trail went cold at: {}"
                     .format(ctx.get("gsx_node") or params["gsx_ip"],
                             ctx.get("t1_id") or params["t1_id"], stopped.name))
        else:
            intro = ("Reverse trace from GSX {} {} -- full path resolved to "
                     "{} ds1-{}".format(
                         ctx.get("gsx_node") or params["gsx_ip"],
                         ctx.get("t1_id") or params["t1_id"],
                         ctx.get("host_1250", "?"), ctx.get("ds1_1250", "?")))
        summary = _slack_summary(ctx)
        progress("\n[JIRA] Posting results to {} ...".format(jira_ticket))
        try:
            ok, msg = post_report(
                ticket=jira_ticket,
                intro=intro,
                panel_title="Reverse Trace -- GSX {} {}".format(
                    ctx.get("gsx_node") or params["gsx_ip"],
                    ctx.get("t1_id") or params["t1_id"]),
                report_text=body,
                attach_path=out_path,
                summary_text=summary,
            )
        except Exception as e:
            ok, msg = False, "{}: {}".format(type(e).__name__, e)
        # A failed post must not look like a failed trace -- the report is
        # already written and printed either way.
        progress("  [JIRA] {}".format(msg if msg else ("posted" if ok else "failed")))

    # ---- optional Slack post ------------------------------------------------
    # Independent of jira_ticket -- a webhook is a separate concern from a
    # ticket, same as the forward autotester.
    slack_webhook = params.get("slack_webhook")
    if slack_webhook:
        stopped = next((h for h in hops if not h.ok), None)
        head = "*Reverse Trace* -- GSX {} {}".format(
            ctx.get("gsx_node") or params["gsx_ip"],
            ctx.get("t1_id") or params["t1_id"])
        head += ("\n:warning: trail went cold at *{}* -- {}".format(
                 stopped.name, stopped.detail) if stopped
                 else "\nFull path resolved.")
        msg_parts = [head]
        if jira_ticket:
            msg_parts.append("<{}/browse/{}|{}>".format(
                JIRA_BASE, jira_ticket, jira_ticket))
        msg_parts.append("```\n{}\n```".format(_slack_summary(ctx)))
        try:
            ok, msg = post_slack(slack_webhook, "\n".join(msg_parts))
        except Exception as e:
            ok, msg = False, "{}: {}".format(type(e).__name__, e)
        progress("  [Slack] {}".format(msg if msg else ("posted" if ok else "failed")))
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

    # ---- JIRA/Slack summary ------------------------------------------------
    # Both channels render from _slack_summary, so these also prove they
    # cannot disagree. The second case is the one that matters: a trace that
    # stopped early must still SHOW the hops it never reached, as '?', rather
    # than omitting them and reading like a success.
    _full = {"gsx_ip": "10.1.1.1", "gsx_node": "ATLGSX02",
             "mux_name": "ATL1-MUX-02", "t3_id": "0/8/46", "ce_iface": "0/7/1",
             "acr": "26", "cem": "411", "sts": "4", "ds1_num": "11",
             "hub_hostname": "ces01.atl1", "oc_subchannel": "oc12-ts1/1:1:11",
             "muse": "MUSE-1234", "host_1250": "atl1250-07",
             "ds1_1250": "ts3/8"}
    chk("summary line count (no protect)",
        len(_slack_summary(_full).splitlines()), 5)
    chk("summary 1250 line", _slack_summary(_full).splitlines()[-1],
        "1250  : atl1250-07 ds1-ts3/8")
    _prot = dict(_full, on_protect=True, sonet_active="0/13/1")
    chk("summary adds protect line",
        _slack_summary(_prot).splitlines()[-1],
        "[!!] ON PROTECT: ACR 26 is on 0/13/1 (protect leg)")
    _cold = {"gsx_ip": "10.1.1.1", "gsx_node": "ATLGSX02"}
    chk("cold trace still shows unreached hops",
        _slack_summary(_cold).splitlines()[-1], "1250  : ? ds1-?")
    chk("cold trace line count", len(_slack_summary(_cold).splitlines()), 5)

    # ---- confirm_route: the forward walk as an independent check ----------
    _c = {"host_1250": "MDLDTXMU542", "ds1_1250": "ts1/27",
          "gsx_node": "DFWGSX04", "acr": "26", "cem": "723"}
    _s = {"node": "DFWGSX04", "slot": "6", "port": "1", "t1": "23"}
    chk("full agreement confirms",
        confirm_route(_c, {"gsx_node": "DFWGSX04", "gsx_slot": "6",
                           "gsx_port": "1", "gsx_t1": "23"}, _s)[0], True)
    # Leading zeros and an FQDN are the same assignment, not a conflict.
    chk("zero-padded ports and an FQDN still confirm",
        confirm_route(_c, {"gsx_node": "dfwgsx04.bandwidth.com",
                           "gsx_slot": "06", "gsx_port": "01",
                           "gsx_t1": "23"}, _s)[0], True)
    # The case this whole mechanism exists for: same port, wrong T1.
    chk("a different T1 is a conflict",
        confirm_route(_c, {"gsx_node": "DFWGSX04", "gsx_slot": "6",
                           "gsx_port": "1", "gsx_t1": "28"}, _s)[0], False)
    chk("a different chassis is a conflict",
        confirm_route(_c, {"gsx_node": "DFWGSX09", "gsx_slot": "6",
                           "gsx_port": "1", "gsx_t1": "23"}, _s)[0], False)
    # Missing data must never read as agreement -- a partial check that
    # returned True would print "route confirmed" with the T1 never compared.
    chk("a partial check is NOT confirmation",
        confirm_route(_c, {"gsx_node": "DFWGSX04", "gsx_slot": "6"}, _s)[0],
        False)
    chk("no forward data is NOT confirmation",
        confirm_route(_c, {}, _s)[0], False)
    chk("'?' placeholders are NOT confirmation",
        confirm_route(_c, {"gsx_node": "?", "gsx_slot": "?", "gsx_port": "?",
                           "gsx_t1": "?"}, _s)[0], False)
    # An ACR/CEM disagreement is still printed when the T1 agrees.
    _ok, _lines = confirm_route(
        _c, {"gsx_node": "DFWGSX04", "gsx_slot": "6", "gsx_port": "1",
             "gsx_t1": "23", "acr_group": "52", "cem_group": "999"}, _s)
    chk("ACR/CEM drift is reported even on a confirmed T1",
        (_ok, sum("differs" in l for l in _lines)), (True, 2))
    chk("confirmed route reaches the summary headline",
        "[OK] route confirmed" in _slack_summary(
            dict(_c, route_confirmed=True)), True)
    chk("a conflict reaches the summary headline",
        "[!!] ROUTE CONFLICT" in _slack_summary(
            dict(_c, route_confirmed=False)), True)
    chk("an unanalyzed run claims neither",
        "route confirm" in _slack_summary(_c).lower(), False)

    # ---- duplicate T3 descriptions ----------------------------------------
    # VERBATIM from DFW1-MUX-04, 2026-10-01. THREE controllers carry
    # 'DFWGSX04-6-1'. 0/11/12 is described but has no cem-groups at all
    # (never provisioned); 8/1/11 is provisioned for a different T1; only
    # 8/1/12 has a cem-group for t1 23. The description cannot settle this --
    # it is per-PORT, while the question is per-DS1.
    _RAW_DFW = (
        "controller T3 0/11/12\n no snmp trap link-status\n"
        " description DFWGSX04-6-1\n threshold sd-ber 6\n framing m13\n"
        " cablelength short\n!\n"
        "controller T3 8/1/11\n description DFWGSX04-6-1\n"
        " t1 5 cem-group 105 unframed\n!\n"
        "controller T3 8/1/12\n no snmp trap link-status\n"
        " description DFWGSX04-6-1\n framing m13\n"
        " t1 1 cem-group 101 unframed\n t1 2 cem-group 102 unframed\n"
        " t1 23 cem-group 123 unframed\n!\n")

    _blocks = mux4216_client.parse_t3_controller_blocks(_RAW_DFW)
    chk("unprovisioned controller has no cem-groups",
        _blocks["0/11/12"]["cem_groups"], {})
    chk("provisioned controller keeps its cem-groups",
        _blocks["8/1/12"]["cem_groups"], {1: "101", 2: "102", 23: "123"})
    chk("all three duplicates are visible",
        [c[0] for c in mux4216_client.t3_candidates_for_gsx_assignment(
            _RAW_DFW, "DFWGSX04", 6, 1)], ["0/11/12", "8/1/11", "8/1/12"])

    chk("t1 23 resolves to the provisioned controller",
        mux4216_client.t3_for_gsx_t1(_RAW_DFW, "DFWGSX04", 6, 1, 23)[:3],
        ("8/1/12", "DFWGSX04-6-1", "123"))
    chk("t1 5 resolves to the OTHER provisioned controller",
        mux4216_client.t3_for_gsx_t1(_RAW_DFW, "DFWGSX04", 6, 1, 5)[:3],
        ("8/1/11", "DFWGSX04-6-1", "105"))
    chk("a T1 provisioned nowhere resolves to nothing",
        mux4216_client.t3_for_gsx_t1(_RAW_DFW, "DFWGSX04", 6, 1, 9)[0], None)
    chk("the ruled-out controllers are named in the note",
        all(x in (mux4216_client.t3_for_gsx_t1(
                      _RAW_DFW, "DFWGSX04", 6, 1, 23)[3] or "")
            for x in ("0/11/12", "8/1/11")), True)

    # Two controllers provisioned for the SAME T1 is a real duplicate, and
    # this must NOT pick one.
    _RAW_TRUE_DUP = _RAW_DFW.replace(" t1 5 cem-group 105 unframed",
                                     " t1 23 cem-group 923 unframed")
    chk("a true per-T1 duplicate refuses to choose",
        mux4216_client.t3_for_gsx_t1(_RAW_TRUE_DUP, "DFWGSX04", 6, 1, 23)[0],
        None)

    # A dump that begins mid-block (what `| b <description>` produces) has an
    # ownerless first description. It must stay ownerless -- guessing is how
    # the three-way duplicate got reported as a two-way one.
    chk("ownerless leading description is not attributed",
        list(mux4216_client.parse_t3_controller_blocks(
            " description DFWGSX04-6-1\n!\n"
            "controller T3 8/1/12\n description DFWGSX04-6-1\n!\n")),
        ["8/1/12"])

    # End-anchored: '6-1' must never match '6-10'. The unanchored version of
    # this is what produced the LAXGSX04 wrong-circuit report.
    chk("6-1 does not match 6-10",
        mux4216_client.t3_candidates_for_gsx_assignment(
            "controller T3 0/8/50\n description DFWGSX04-6-10\n!\n",
            "DFWGSX04", 6, 1), [])
    # The single-answer contract is unchanged for its existing caller.
    chk("single-answer helper still reports ambiguity",
        mux4216_client.t3_for_gsx_assignment(_RAW_DFW, "DFWGSX04", 6, 1),
        (None, "__AMBIGUOUS__"))

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
