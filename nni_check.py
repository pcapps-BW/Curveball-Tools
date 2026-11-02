#!/usr/bin/env python3
"""
nni_check.py -- "is this actually a circuit problem, or is the NNI down?"

THE PROBLEM THIS SOLVES
-----------------------
When circuits fail, the NOC works the queue ticket by ticket and opens an IXC
ticket per EVC. If the NNI carrying those circuits is down, every one of those
tickets is the same outage reported N times -- wasted effort, and the actual
fault (one port) is buried under the symptoms (fifty circuits).

So any tool that reports circuit status should also look ONE LEVEL UP and say
whether the interface beneath it is healthy. A circuit-down report is only
actionable if its NNI is up.

TWO LEVELS OF BLAST RADIUS
--------------------------
A 1800 NNI is a sub-interface, e.g. xe-ts16/2.111 -- one 1250 site, whose every
DS1/EVC rides it. Its physical parent, xe-ts16/2, carries MANY such
sub-interfaces, i.e. many sites. So:

    sub-interface down  -> every circuit at that one site
    physical port down  -> every circuit at every site on that port

Both are reported, because the second is the one that turns fifty tickets into
one.

HOW IT DECIDES -- proven commands only
--------------------------------------
Deliberately built from commands already in production use in this suite, with
output formats already parsed and unit-tested, because guessing at device output
is exactly what produced the LAXGSX04 / 11-2-28 wrong-circuit report:

  1. `show ospf neighbor | no-more`
     One command lists every 1250 on the hub with its interface and adjacency
     state. Parsed by ospf_stability.parse_ospf_neighbors(), which is already
     verified against real device output. From this single call we get the
     target NNI's own state AND every sibling on the same physical port --
     which IS the blast radius, at no extra cost.

  2. `show ne alarms | match "<parent>" | no-more`
     Row presence means alarm; no rows means not in alarm. That convention is
     already established elsewhere in this codebase. Rows are COUNTED, not
     column-parsed, so no format is assumed.

Nothing else is queried and nothing is inferred. If a fact isn't available from
those two, this module says "unknown" rather than guessing.

Built by P. Capps.
"""

import re

try:
    from ospf_stability import parse_ospf_neighbors
    _PARSER_OK = True
except Exception:                                        # pragma: no cover
    _PARSER_OK = False

try:
    import assurance
    _ASSURANCE_OK = True
except Exception:                                        # pragma: no cover
    _ASSURANCE_OK = False


# A port-level call needs at least this many siblings down...
PORT_EVENT_MIN_DOWN = 2
# ...and at least this share of the sub-interfaces on the port.
PORT_EVENT_MIN_SHARE = 0.5

# Verdicts
V_NNI_UP      = "nni-up"           # NNI healthy -- a circuit fault is genuine
V_CIRCUIT     = "circuit-specific"  # this NNI down, its neighbours fine
V_MULTI       = "multiple-circuits"  # several down on the port, not a majority
V_PORT_EVENT  = "port-event"        # most/all of the port is down -> ONE ticket
V_UNKNOWN     = "unknown"           # could not determine


def parent_of(iface):
    """
    'xe-ts16/2.111' -> 'xe-ts16/2'.  Returns None when there is no sub-interface.

    Splits on the LAST dot. The physical name itself contains no dot, but
    splitting on the first one mangles anything unexpected -- the same bug that
    turned an IP address into 'interface 10 unit 111' in interface_check.
    """
    s = (iface or "").strip()
    if not s or "." not in s:
        return None
    parent, sub = s.rsplit(".", 1)
    if not sub.isdigit() or "/" not in parent:
        return None
    return parent


def _is_up(state):
    return str(state or "").strip().lower() == "full"


def _parse_ospf_interface_nbr(raw, iface):
    """
    Parse 'show ospf interface <iface>' output for that interface's own row.
    Table looks like:
      Interface       State           Admin  Area           AreaType  ...  Nbr
      xe-ts16/2.330   PointToPoint    up     0.0.0.0        backbone  ...  0

    Nbr (neighbor count) is always the LAST whitespace-separated field on the
    interface's own row -- a direct, un-hedged adjacency-count signal in its
    own right, confirmed live 2026-09-03.

    Returns (found, nbr):
      found=False, nbr=None  -- interface not listed at all (OSPF likely not
                                 configured on it -- different from Nbr=0)
      found=True,  nbr=int   -- interface listed, this many neighbors
      found=True,  nbr=None  -- listed but the trailing field didn't parse
                                 as an int (unfamiliar output -- don't guess)
    """
    for line in (raw or "").splitlines():
        parts = line.split()
        if parts and parts[0] == iface:
            try:
                return True, int(parts[-1])
            except ValueError:
                return True, None
    return False, None


# Below this average PPS (either direction), traffic is assumed to be
# protocol keepalives only (OSPF/LDP hellos), not genuine circuit data --
# user's rough call, not a measured threshold (2026-09-09): "if its
# something like 5pps just mark it as keepalive, or whatever value makes
# sense there."
KEEPALIVE_PPS_THRESHOLD = 5

_RE_PM15_HEADER = re.compile(
    r'^(\S+)\s+Monitored Seconds:\s*(\d+)\s+Suspect Flag:\s*(\S+)', re.MULTILINE)


def parse_interface_pm_15min(raw):
    """
    Parse 'show interfaces physical pm 15min <port>' output.

    Real output shape (confirmed live 2026-09-09, ces01.dfw1 xe-ts16/3):
        xe-ts16/3    Monitored Seconds:    215   Suspect Flag:   FALSE
        Rx Octets                                          :  1876846441
        Rx Pkts                                                :  9202911
        ...
        IN Current PPS                                            :  43395
        IN Average PPS                                            :  43275
        ...
    One key/value pair per line after the header, separated by the LAST
    colon on the line (labels themselves never contain one). 'N/A' fields
    (PCS-*, FEC-*, OSNR_*) are kept as None rather than guessed at zero.

    Returns a dict, or {} if the header line didn't match at all (command
    failed / wrong output / interface not found -- caller must not assume
    zeros in that case, only in a successfully parsed dict).
    """
    if not raw:
        return {}
    hm = _RE_PM15_HEADER.search(raw)
    if not hm:
        return {}

    def _num(v):
        v = (v or "").strip()
        if not v or v.upper() == "N/A":
            return None
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return None

    fields = {}
    for line in raw.splitlines():
        if ":" not in line or "Monitored Seconds" in line:
            continue
        key, _, val = line.rpartition(":")
        key = key.strip()
        if key:
            fields[key] = _num(val)

    return {
        "iface":              hm.group(1),
        "monitored_seconds":  int(hm.group(2)),
        "suspect":            hm.group(3).strip().upper() == "TRUE",
        "rx_octets":          fields.get("Rx Octets"),
        "rx_pkts":            fields.get("Rx Pkts"),
        "tx_octets":          fields.get("Tx Octets"),
        "tx_pkts":            fields.get("Tx Pkts"),
        "in_current_pps":     fields.get("IN Current PPS"),
        "in_average_pps":     fields.get("IN Average PPS"),
        "in_maximum_pps":     fields.get("IN Maximum PPS"),
        "out_current_pps":    fields.get("OUT Current PPS"),
        "out_average_pps":    fields.get("OUT Average PPS"),
        "out_maximum_pps":    fields.get("OUT Maximum PPS"),
        "in_current_bps":     fields.get("IN Current Rate(bps)"),
        "out_current_bps":    fields.get("OUT Current Rate(bps)"),
        # Average Rate(bps) is what pairs with the Average PPS the headline
        # reports. Captured from the same `fields` dict; None if the device
        # does not emit that exact label, in which case the formatter falls
        # back to Current Rate (see _dir_rate).
        "in_average_bps":     fields.get("IN Average Rate(bps)"),
        "out_average_bps":    fields.get("OUT Average Rate(bps)"),
    }


def _fmt_rate(bps):
    """bps integer -> human-readable Gbps/Mbps/Kbps/bps, or None."""
    if bps is None:
        return None
    try:
        b = float(bps)
    except (TypeError, ValueError):
        return None
    if b >= 1e9:
        return "{:.2f} Gbps".format(b / 1e9)
    if b >= 1e6:
        return "{:.2f} Mbps".format(b / 1e6)
    if b >= 1e3:
        return "{:.1f} Kbps".format(b / 1e3)
    return "{:.0f} bps".format(b)


def _dir_rate(pm, d):
    """
    (formatted_rate, which) for direction 'in'/'out'. Prefers the AVERAGE
    bps -- it pairs with the Average PPS the verdict uses -- and falls back
    to the CURRENT rate, which is the only bps field confirmed on every
    device so far. (None, None) when neither is present, so the caller can
    degrade to pps rather than print a wrong unit.
    """
    avg = pm.get(d + "_average_bps")
    if avg is not None:
        return _fmt_rate(avg), "avg"
    cur = pm.get(d + "_current_bps")
    if cur is not None:
        return _fmt_rate(cur), "cur"
    return None, None


# Traffic verdicts
T_PASSING    = "passing"      # both directions carrying real data traffic
T_KEEPALIVE  = "keepalive"    # both directions <= threshold -- protocol only
T_ASYMMETRIC = "asymmetric"   # one direction real traffic, other near-zero
T_NO_TRAFFIC = "no-traffic"   # both directions flat zero
T_UNKNOWN    = "unknown"      # command failed or didn't parse -- not guessed


def assess_nni_traffic(pm):
    """
    Classify a parsed PM-15min dict into a traffic verdict.

    Uses AVERAGE pps over the 15-min interval, not Current -- a single
    "Current" sample can catch a momentary lull or burst; Average reflects
    the whole monitored window, which is what "am I getting flat zeros over
    15 minutes" actually asks.

    Returns (verdict, headline, detail_lines).
    """
    if not pm or pm.get("in_average_pps") is None or pm.get("out_average_pps") is None:
        return (T_UNKNOWN,
                "Could not determine traffic level -- PM command failed or "
                "output didn't parse. Not guessing traffic is fine.",
                [])

    in_pps  = pm["in_average_pps"]
    out_pps = pm["out_average_pps"]
    mins    = pm.get("monitored_seconds", 0) / 60.0
    in_rate, in_which   = _dir_rate(pm, "in")
    out_rate, out_which = _dir_rate(pm, "out")
    _rate_note = {"avg": "avg", "cur": "current"}
    detail = [
        "interval: {:.1f} min monitored{}".format(
            mins, " -- DEVICE-FLAGGED SUSPECT" if pm.get("suspect") else ""),
        "avg IN:  {} pps  ({}{})".format(
            in_pps, in_rate or "no bps counter",
            " " + _rate_note[in_which] if in_which else ""),
        "avg OUT: {} pps  ({}{})".format(
            out_pps, out_rate or "no bps counter",
            " " + _rate_note[out_which] if out_which else ""),
    ]

    if in_pps == 0 and out_pps == 0:
        return (T_NO_TRAFFIC,
                "NO TRAFFIC on this physical NNI -- flat zero pps both "
                "directions over the last {:.1f} min. Not even keepalives.".format(mins),
                detail)
    if in_pps <= KEEPALIVE_PPS_THRESHOLD and out_pps <= KEEPALIVE_PPS_THRESHOLD:
        return (T_KEEPALIVE,
                "KEEPALIVE-ONLY on this physical NNI -- both directions <= "
                "{} pps average over {:.1f} min. Protocol hellos, no "
                "meaningful data traffic.".format(KEEPALIVE_PPS_THRESHOLD, mins),
                detail)
    if in_pps <= KEEPALIVE_PPS_THRESHOLD or out_pps <= KEEPALIVE_PPS_THRESHOLD:
        low_dir = "IN" if in_pps <= KEEPALIVE_PPS_THRESHOLD else "OUT"
        return (T_ASYMMETRIC,
                "ASYMMETRIC traffic on this physical NNI -- {} is <= {} pps "
                "(keepalive-only) while the other direction is passing real "
                "traffic. Possible one-way loss.".format(
                    low_dir, KEEPALIVE_PPS_THRESHOLD),
                detail)
    # Lead with Mbps -- "455563 pps" is not something a human reads as a
    # traffic level (pcapps, 2026-11-02). pps stays in the detail lines and
    # still drives the keepalive/asymmetric verdicts above, where it is the
    # right unit. If the device gave no bps counter at all, fall back to pps
    # rather than print nothing.
    if in_rate and out_rate:
        rate_txt = "{} in / {} out".format(in_rate, out_rate)
    else:
        rate_txt = "{} pps in / {} pps out".format(in_pps, out_pps)
    return (T_PASSING,
            "Traffic passing on this physical NNI -- {} average over "
            "{:.1f} min.".format(rate_txt, mins),
            detail)


def check_nni(c1800, iface, hub_name="", progress=None,
              neighbors_raw=None, alarms_raw=None, iface_raw=None,
              pm_raw=None):
    """
    Assess the NNI carrying `iface` and the physical port beneath it.

    c1800          connected NPT1800Client (or None if raw output is supplied)
    iface          the NNI sub-interface, e.g. 'xe-ts16/2.111'
    neighbors_raw  optional pre-fetched `show ospf neighbor` output (lets a
                   caller that already ran it avoid a second call, and lets
                   tests run with no device)
    alarms_raw     optional pre-fetched `show ne alarms | match <parent>` output
    iface_raw      optional pre-fetched `show ospf interface <iface>` output --
                   only used as a fallback disambiguation probe when `iface`
                   doesn't show up in `neighbors_raw` at all (see step 3)
    pm_raw         optional pre-fetched `show interfaces physical pm 15min
                   <parent>` output, for the traffic-passing check (physical
                   port, NOT the sub-interface -- deliberately separate from
                   the OSPF adjacency check above, both in what it queries
                   and in how it's reported: adjacency-Full is a protocol
                   fact, this is "is data actually moving")

    Returns a dict -- never raises for device/parse reasons:
        iface, parent, hub
        target_state      adjacency state of iface, or None if not present
        siblings          [{interface, state, neighbor_ip, router_id, up}]
        total_on_port, down_on_port, down_share
        parent_alarm_rows int (-1 = not checked)
        traffic           {checked, verdict, headline, detail, pm} -- see
                           assess_nni_traffic()/parse_interface_pm_15min()
        verdict           one of the V_* constants (OSPF/blast-radius only --
                           traffic verdict is separate, see `traffic`)
        headline          one line an engineer can act on
        detail            list of supporting lines
    """
    say = progress or (lambda *_a, **_k: None)
    parent = parent_of(iface)
    out = {
        "iface": iface, "parent": parent, "hub": hub_name,
        "target_state": None, "siblings": [],
        "total_on_port": 0, "down_on_port": 0, "down_share": 0.0,
        "parent_alarm_rows": -1,
        "traffic": {"checked": False, "verdict": T_UNKNOWN,
                    "headline": "", "detail": [], "pm": {}},
        "verdict": V_UNKNOWN, "headline": "", "detail": [],
    }

    if not parent:
        out["headline"] = ("NNI check skipped: '{}' is not a sub-interface "
                           "(expected <port>.<vlan>, e.g. xe-ts16/2.111)".format(iface))
        return out
    if not _PARSER_OK:
        out["headline"] = "NNI check unavailable: OSPF neighbour parser not importable"
        return out

    # ---- 1. one command gives the target AND the whole port ----------------
    if neighbors_raw is None:
        if c1800 is None:
            out["headline"] = "NNI check skipped: no 1800 session and no supplied output"
            return out
        try:
            neighbors_raw = c1800.run("show ospf neighbor | no-more", 60)
        except Exception as e:
            out["headline"] = "NNI check failed: {}: {}".format(type(e).__name__, e)
            return out

    try:
        neighbors = parse_ospf_neighbors(neighbors_raw) or []
    except Exception as e:                                # pragma: no cover
        out["headline"] = "NNI check failed parsing neighbours: {}".format(e)
        return out

    prefix = parent + "."
    for nb in neighbors:
        nb_if = (nb.get("interface") or "").strip()
        if nb_if == iface:
            out["target_state"] = nb.get("state")
        if nb_if.startswith(prefix):
            out["siblings"].append({
                "interface":   nb_if,
                "state":       nb.get("state"),
                "neighbor_ip": nb.get("neighbor_ip"),
                "router_id":   nb.get("router_id"),
                "up":          _is_up(nb.get("state")),
            })

    out["siblings"].sort(key=lambda s: s["interface"])
    out["total_on_port"] = len(out["siblings"])
    out["down_on_port"]  = sum(1 for s in out["siblings"] if not s["up"])
    if out["total_on_port"]:
        out["down_share"] = out["down_on_port"] / float(out["total_on_port"])

    # ---- 2. alarms on the physical port ------------------------------------
    if alarms_raw is None and c1800 is not None:
        try:
            alarms_raw = c1800.run(
                'show ne alarms | match "{}" | no-more'.format(parent), 60)
        except Exception:
            alarms_raw = None
    if alarms_raw is not None:
        # Row presence = alarm; count only, never column-parse.
        out["parent_alarm_rows"] = len(
            [ln for ln in alarms_raw.splitlines()
             if ln.strip() and parent in ln])

    # ---- 2a. is traffic actually passing on the physical port? ------------
    # Deliberately queried against `parent` (the physical NNI), never `iface`
    # (the sub-interface/EVC) -- confirmed live 2026-09-09 that a sub-interface
    # reporting OSPF Full says nothing about whether real data is moving, and
    # conflating the two in one headline read as a port-level claim from a
    # sub-interface fact. `show interfaces physical pm 15min <parent>` gives
    # the device's own already-computed Average/Current pps and bps over the
    # monitored window -- no need to poll twice and compute a delta ourselves.
    if pm_raw is None and c1800 is not None:
        try:
            pm_raw = c1800.run(
                "show interfaces physical pm 15min {} | no-more".format(parent), 30)
        except Exception:
            pm_raw = None
    if pm_raw is not None:
        pm = parse_interface_pm_15min(pm_raw)
        t_verdict, t_headline, t_detail = assess_nni_traffic(pm)
        out["traffic"] = {"checked": True, "verdict": t_verdict,
                          "headline": t_headline, "detail": t_detail, "pm": pm}
        say("  [NNI-TRAFFIC] {}".format(t_headline))

    # ---- 2b. disambiguate an unlisted target with one targeted probe -------
    # "Not in the neighbour table" alone is genuinely ambiguous -- no OSPF on
    # this NNI at all, vs. the adjacency being so far down it's unlisted.
    # Rather than leave that as a permanent hedge, confirm it with one extra
    # command scoped to just this interface -- 'show ospf interface <iface>'
    # explicitly confirms whether OSPF is even configured here, and its Nbr
    # column is a direct adjacency-count signal (confirmed live 2026-09-03).
    # Only fires in the rare unlisted case; the common path stays the single
    # 'show ospf neighbor' call the blast-radius design is built around.
    probe_found, probe_nbr = None, None
    if out["target_state"] is None:
        if iface_raw is None and c1800 is not None:
            try:
                iface_raw = c1800.run("show ospf interface {}".format(iface), 30)
            except Exception:
                iface_raw = None
        if iface_raw is not None:
            probe_found, probe_nbr = _parse_ospf_interface_nbr(iface_raw, iface)
            if probe_found and probe_nbr == 0:
                # Confirmed down, not just absent -- feed it into the normal
                # down-handling branches below (blast-radius aware) instead
                # of a separate one-off verdict. It was never in `siblings`
                # (that list is only populated from the neighbour-table scan
                # above, which is exactly what didn't find it), so add it
                # back in and recompute the port counts -- otherwise
                # total_on_port/down_on_port undercounts this target by one
                # and the "N other sub-interface(s)" headline text goes
                # negative (caught in testing 2026-09-03).
                out["target_state"] = "Down (Nbr 0 via show ospf interface)"
                out["siblings"].append({
                    "interface": iface, "state": out["target_state"],
                    "neighbor_ip": None, "router_id": None, "up": False,
                })
                out["siblings"].sort(key=lambda s: s["interface"])
                out["total_on_port"] = len(out["siblings"])
                out["down_on_port"]  = sum(1 for s in out["siblings"] if not s["up"])
                out["down_share"] = (out["down_on_port"] / float(out["total_on_port"])
                                      if out["total_on_port"] else 0.0)

    # ---- 3. verdict --------------------------------------------------------
    tgt_up = _is_up(out["target_state"])
    if out["target_state"] is None:
        if probe_found and probe_nbr and probe_nbr > 0:
            # Genuine inconsistency between the two commands -- don't
            # silently trust either one.
            out["verdict"] = V_UNKNOWN
            out["headline"] = (
                "{} was not in the OSPF neighbour table on {}, but 'show ospf "
                "interface {}' shows {} neighbor(s) -- inconsistent results, "
                "don't trust either alone. Re-check live.".format(
                    iface, hub_name or "the hub", iface, probe_nbr))
        elif probe_found is False and iface_raw is not None:
            # Probe ran and the interface simply isn't there -- OSPF likely
            # isn't configured on this NNI at all (not necessarily a fault).
            out["verdict"] = V_UNKNOWN
            out["headline"] = (
                "{} is not in the OSPF neighbour table on {}, and 'show ospf "
                "interface {}' shows no such interface -- OSPF doesn't "
                "appear to be configured on this NNI at all.".format(
                    iface, hub_name or "the hub", iface))
        else:
            # Couldn't probe (no device, or the probe call itself failed) --
            # same hedge as before, unchanged.
            out["verdict"] = V_UNKNOWN
            out["headline"] = ("{} is not in the OSPF neighbour table on {} -- "
                               "cannot confirm NNI state from OSPF alone".format(
                                   iface, hub_name or "the hub"))
    elif tgt_up:
        out["verdict"] = V_NNI_UP
        # NOTE (2026-09-09): this only confirms the sub-interface's OSPF
        # adjacency is Full -- protocol-layer up, NOT "traffic is actually
        # passing". Deliberately does not call the sub-interface itself "the
        # NNI" (that name is reserved elsewhere in this codebase -- see
        # autotester_standalone.py -- for the physical port; the sub-interface
        # is the EVC/circuit riding it) to avoid exactly the "NNI xe-ts16/3.548
        # is UP" wording that reads as a port-level claim from a sub-interface
        # fact.
        out["headline"] = (
            "Sub-interface {} is OSPF Full on physical NNI {} -- adjacency is "
            "up, so a circuit fault here is not an NNI-down event. Protocol "
            "state only -- this does NOT confirm traffic is actually "
            "passing.".format(iface, parent))
    elif (out["down_on_port"] >= PORT_EVENT_MIN_DOWN and
          out["down_share"] >= PORT_EVENT_MIN_SHARE):
        out["verdict"] = V_PORT_EVENT
        out["headline"] = (
            "PORT-LEVEL EVENT on {}: {} of {} sub-interfaces are down. "
            "Do NOT open per-circuit IXC tickets -- this is ONE fault affecting "
            "every site on that port.".format(
                parent, out["down_on_port"], out["total_on_port"]))
    elif out["down_on_port"] >= PORT_EVENT_MIN_DOWN:
        out["verdict"] = V_MULTI
        out["headline"] = (
            "{} of {} sub-interfaces on {} are down -- more than this one "
            "circuit. Check for a common upstream cause before ticketing "
            "individually.".format(out["down_on_port"], out["total_on_port"], parent))
    else:
        out["verdict"] = V_CIRCUIT
        out["headline"] = (
            "Sub-interface {} is DOWN but the other {} sub-interface(s) on "
            "physical NNI {} are Full -- this looks genuinely "
            "circuit-specific, not an NNI outage.".format(
                iface, out["total_on_port"] - 1, parent))

    # ---- 4. supporting detail ---------------------------------------------
    d = out["detail"]
    d.append("target {}: OSPF {}".format(iface, out["target_state"] or "not listed"))
    d.append("physical port {}: {} sub-interface(s), {} down ({:.0%})".format(
        parent, out["total_on_port"], out["down_on_port"], out["down_share"]))
    if out["parent_alarm_rows"] > 0:
        d.append("{} alarm row(s) present on {} -- port is in alarm".format(
            out["parent_alarm_rows"], parent))
    elif out["parent_alarm_rows"] == 0:
        d.append("no alarms matching {}".format(parent))
    down = [s for s in out["siblings"] if not s["up"]]
    if down:
        d.append("down on this port: " + ", ".join(
            "{} ({})".format(s["interface"], s["state"]) for s in down[:12]))
        if len(down) > 12:
            d.append("  ... and {} more".format(len(down) - 12))
    say("  [NNI] {}".format(out["headline"]))
    return out


def render_block(res, width=76):
    """Report block for any tool that checks circuit status."""
    sep = "-" * width
    L = [sep, "  NNI / BLAST RADIUS", sep]
    flag = "" if res["verdict"] in (V_NNI_UP,) else "[!] "
    L.append("  {}{}".format(flag, res["headline"]))
    for line in res.get("detail", []):
        L.append("    {}".format(line))
    if res["verdict"] == V_PORT_EVENT:
        L.append("")
        L.append("  ACTION: raise ONE ticket against {} and reference the "
                 "affected sites,".format(res["parent"]))
        L.append("          rather than one IXC ticket per EVC.")

    # ---- traffic-passing check -- deliberately its own section, never ------
    # blended into the OSPF headline above (that's protocol state; this is
    # "is data actually moving").
    traffic = res.get("traffic") or {}
    if traffic.get("checked"):
        t_verdict = traffic.get("verdict")
        t_flag = "" if t_verdict == T_PASSING else "[!] "
        L.append("")
        L.append("  {}TRAFFIC: {}".format(t_flag, traffic.get("headline", "")))
        for line in traffic.get("detail", []):
            L.append("    {}".format(line))
    return "\n".join(L)


def to_findings(res, a):
    """
    Push the verdict into an assurance.Assurance accumulator.

    Categorised as partial-application: reporting a single circuit as faulty
    while its whole port is down is a true statement that gives a false picture.
    """
    if not _ASSURANCE_OK or a is None:
        return
    cat = assurance.CAT_PARTIAL
    if res["verdict"] == V_PORT_EVENT:
        a.critical(cat, res["headline"], evidence="; ".join(res["detail"]))
    elif res["verdict"] == V_MULTI:
        a.warn(cat, res["headline"], evidence="; ".join(res["detail"]))
    elif res["verdict"] == V_UNKNOWN:
        a.warn(assurance.CAT_BLIND, res["headline"])
    else:
        a.info(cat, res["headline"])

    # Traffic verdict is reported independently of the OSPF verdict above --
    # a sub-interface can be OSPF Full and still be carrying no real traffic.
    traffic = res.get("traffic") or {}
    if traffic.get("checked"):
        t_verdict = traffic.get("verdict")
        if t_verdict == T_NO_TRAFFIC:
            a.critical(cat, traffic["headline"], evidence="; ".join(traffic.get("detail", [])))
        elif t_verdict in (T_KEEPALIVE, T_ASYMMETRIC):
            a.warn(cat, traffic["headline"], evidence="; ".join(traffic.get("detail", [])))
        elif t_verdict == T_UNKNOWN:
            a.warn(assurance.CAT_BLIND, traffic["headline"])
        else:
            a.info(cat, traffic["headline"])


if __name__ == "__main__":
    # Fixture built from REAL `show ospf neighbor` lines captured on ces01.jfk1,
    # with xe-ts16/2 forced down to show what a port event looks like.
    real = """
      10.111.132.0    xe-ts16/1.10    Full      10.111.130.254  1     37     22d:11h:11m:51s
      10.111.140.4    xe-ts16/2.426   Down      10.111.130.2    1     32     6d:09h:39m:30s
      10.111.140.8    xe-ts16/2.126   Down      10.111.130.4    1     39     20d:10h:17m:06s
      10.111.140.10   xe-ts16/2.133   Down      10.111.130.5    1     32     15d:11h:20m:29s
      10.111.140.12   xe-ts16/2.220   Full      10.111.130.6    1     38     6d:10h:40m:08s
      10.111.140.16   xe-ts13/3.240   Full      10.111.130.8    1     35     1h:27m:50s
      10.111.140.24   xe-ts13/4.342   Full      10.111.130.12   1     39     9d:13h:29m:27s
    """
    for target in ("xe-ts16/2.426", "xe-ts16/1.10", "xe-ts13/3.240"):
        r = check_nni(None, target, hub_name="ces01.jfk1",
                      neighbors_raw=real, alarms_raw="")
        print(render_block(r))
        print()
