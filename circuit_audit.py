#!/usr/bin/env python3
"""
circuit_audit.py -- configured-circuit audit across the four production 1800 hubs.

THE QUESTION THIS ANSWERS
    ospf_stability.py reads `show ospf neighbor` -- i.e. adjacencies the hub
    KNOWS ABOUT. That is the wrong side of the problem for an audit: a site
    that has been hard-down long enough for its neighbour entry to age out
    stops appearing at all. It cannot report a circuit it can no longer see.
    That is `structurally-blind` in assurance.py's terms.

    This tool starts from CONFIGURATION instead. Every configured
    sub-interface is a circuit we expect to be up (pcapps, 2026-09-17). The
    configured set is the denominator; the adjacency table is the numerator;
    the difference is the audit.

    Deliberately a SEPARATE tool. ospf_stability.py is not modified -- it is a
    trend monitor keyed on router-ID and persisted across runs; this is a
    point-in-time inventory keyed on sub-interface. Different question,
    different key, different failure modes.

SOURCE COMMANDS (all read-only)
    show interfaces description | no-more     <- the configured inventory
    show ospf neighbor | no-more              <- current adjacency state

    `show ldp adjacency` is deliberately NOT parsed. Its output format has
    never been captured in this project, and guessing a device output format
    is how the LAXGSX04 wrong-circuit report happened. Add it when a real
    sample exists.

OUTPUT
    Excel. One sheet per hub with every sub-interface labeled, plus a GAPS
    sheet and a SUMMARY sheet. Nothing is filtered out -- infrastructure and
    unlabeled sub-interfaces are LABELED, not hidden, so a bad classifier
    cannot silently drop a real circuit (pcapps' call, 2026-09-17).

FAILURE POSTURE
    A hub whose commands error or come back pager-truncated is reported as
    UNREAD. It is never rendered as an empty or short inventory. An audit that
    quietly lists 40 of 60 circuits is worse than one that refuses to run --
    `fails-as-success`, and the exact shape of the 79% SUSPECT bug.

Built by P. Capps.
"""

import os
import re
import sys
import time
import datetime
import getpass
import threading

import platform_paths
import hub_registry
from npt1800_client import NPT1800Client
from npt1250_client import NPT1250Client
from device_base import CredentialError
from netbox_client import NetboxClient, load_netbox_config

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.utils import get_column_letter
    _DEP_ERROR = None
except ImportError:
    openpyxl = None
    _DEP_ERROR = "openpyxl is not installed (pip install openpyxl)"


TOOL_INFO = {
    "name": "Circuit Audit (1800 Configured Sub-Interfaces)",
    "slug": "circuit_audit",
    "description": (
        "Audits every configured sub-interface across the four production 1800 "
        "hubs and compares it against live OSPF adjacency state and NetBox. "
        "Answers 'what should be up' rather than 'what is up' -- so a site that "
        "has aged out of the neighbour table still appears."
    ),
}


# ── Constants ─────────────────────────────────────────────────────────────────

IFACE_CMD = "show interfaces description | no-more"
OSPF_CMD  = "show ospf neighbor | no-more"

CMD_TIMEOUT = 90          # description output on a full hub is ~110 lines
AUDIT_WORKERS = 4         # one per hub; they are independent

# LAB is excluded by name. hub_registry carries a LAB entry with a placeholder
# IP (added 2026-09-11, never sourced from NetBox) and it is not a production
# aggregation point. Auditing it would put an unconfirmed IP in a report that
# is meant to be authoritative.
EXCLUDED_SITES = ("LAB",)


# ── IXC / carrier map ────────────────────────────────────────────────────────
#
# Which carrier hands off on which hub NNI parent port.
#
# HARDCODED DELIBERATELY (pcapps, 2026-09-18): these do not change, and
# `show configuration | display-set` takes minutes on an 1800 -- too slow to
# run every audit for a static fact.
#
# PROVENANCE -- verified 2026-09-18 against the devices themselves via
# `show configuration | display-set | grep description` on ces01.jfk1,
# ces01.dfw1 and ces01.lax1. All three returned an IDENTICAL five-port set,
# which is what justifies applying one map to every hub.
#
#   xe-ts13/2  Zayo         confirmed on jfk1, dfw1, lax1
#   xe-ts13/3  Nitel        confirmed on jfk1, dfw1, lax1
#   xe-ts13/4  ATT          confirmed on jfk1, dfw1, lax1
#   xe-ts16/2  Zayo         confirmed on jfk1, dfw1, lax1
#   xe-ts16/3  Windstream   confirmed on jfk1, dfw1, lax1
#
# ces01.atl1 was not sampled directly, but was confirmed by pcapps
# (2026-09-18) to have been built the same way. Recorded as owner-confirmed
# rather than device-confirmed purely so the distinction survives.
#
# ⚠ ONE GAP, recorded rather than papered over:
#
#      ge-ts16/4 / Lumen was supplied in the original list but appears in
#      NONE of the three sampled device configs. It is therefore NOT in the
#      map below. Adding it on the strength of the list alone would be
#      trusting the weaker of two sources against the stronger -- the device
#      is authoritative here, same as everywhere else in this suite. If Lumen
#      is real it is either on atl1, not yet configured, or configured
#      without a description; any of those needs confirming before the entry
#      goes in.
#
# A parent port NOT in this map is reported as UNKNOWN, never blank and never
# guessed -- an unmapped port is missing information, and a blank cell reads
# as "no carrier", which is a different and false claim.
#
# NOTE ces01.jfk1's xe-ts16/1 is absent from both the device config and this
# map: its sub-interfaces carry no description and hold Full adjacencies to
# another hub's router-ID, i.e. inter-hub infrastructure rather than a
# carrier handoff. Two sources agreeing corroborates that classification.

IXC_BY_PORT = {
    "xe-ts13/2": "Zayo",
    "xe-ts13/3": "Nitel",
    "xe-ts13/4": "ATT",
    "xe-ts16/2": "Zayo",
    "xe-ts16/3": "Windstream",
}

IXC_UNKNOWN = "UNKNOWN -- port not in IXC map"


# Operational notes for sites that legitimately carry 3 sub-interfaces.
# EDIT THESE as situations resolve -- they are point-in-time operator
# knowledge, not derived facts, and they are kept here rather than buried in
# the Excel writer so they are easy to find and change.
# Keyed on CLLI. Supplied by pcapps 2026-09-18.
SITE_NOTES = {
    "CLEVOH62": "Cleveland: Pending 2nd circuit turnup",
    "TAMPFLXA": "Tampa: 3 circuit count is CORRECT. Site currently up on 3 circuits.",
    "WHPLNYWP": "White Plains: Pending decision on 2nd circuit.",
}


def ixc_for_port(parent):
    """
    Carrier for a hub NNI parent port. Exact match only.

    Deliberately not fuzzy: 'ge-ts16/4' and a hypothetical 'xe-ts16/4' are
    different ports and must not collapse into one another, so the media
    prefix is part of the key.
    """
    return IXC_BY_PORT.get((parent or "").strip(), IXC_UNKNOWN)


# ── EVC circuit IDs (NetBox) ─────────────────────────────────────────────────
#
# Circuit IDs live in NetBox on the 1250's own fibre interface descriptions,
# formatted "<Carrier>: <circuit id>". Confirmed 2026-09-18 on WHPLNYWP132:
#
#   ge-ts2/1   SFP (1GE)         Nitel: NIT555057
#   ge-ts2/2   SFP (1GE)         Zayo: ETYX/389224//ZYO
#   ge-ts5/1   SFP (1GE)         Zayo: ETYX/389151//ZYO
#   ge-ts2/3   SFP (1GE)         (no description)
#   T3/T4 A/B  T1 (1.544 Mbps)   (no description -- TDM, not an EVC)
#
# NetBox is the address book / record store here, not the source of truth for
# what is built -- exactly as it is used elsewhere in this tool. A circuit ID
# is a label we need to report, not a fact about whether the circuit works.

# T1/TDM interface types carry no EVC. Matched on the type label NetBox
# returns, not the interface name, because naming varies and the type does
# not.
_NON_EVC_TYPE_RE = re.compile(r'\bT1\b|\bT3\b|1\.544|\bDS[13]\b', re.I)

CID_NONE      = ""
CID_UNKNOWN   = "UNKNOWN -- NetBox not read"


def parse_circuit_descriptions(ifaces):
    """
    Collect the RAW description text off a device's fibre interfaces.

    Deliberately does NOT parse it (pcapps, 2026-09-18). Two formats were
    already in the wild -- "Zayo: ETYX/389224//ZYO" on WHPLNYWP132 and
    "ZAYO - ETYX/420953//ZYO" on DIXNILXA364 -- and every parse rule added
    its own gap: anything not matching came out blank, which reads as "no
    circuit" rather than "I could not read this". Copying the text verbatim
    cannot create that class of gap at all. A human reading the cell parses
    a format variant instantly; a regex cannot.

    TDM interfaces are still excluded by type -- they are not EVCs.

    Returns (descs, skipped), descs = [(iface_name, raw_text), ...].
    """
    descs, skipped = [], []
    for i in ifaces or []:
        desc = (i.get("description") or "").strip()
        if not desc:
            continue
        if _NON_EVC_TYPE_RE.search(i.get("type") or ""):
            skipped.append((i.get("name", ""), desc))
            continue
        descs.append((i.get("name", ""), desc, (i.get("ip") or "").strip()))
    return descs, skipped


def circuit_id_for(descs, ixc=None, leg_iface=None,
                   hub_nbr_addr=None):
    """
    The EVC circuit text for one hub sub-interface, verbatim.

    Preference, strongest first:
      1. The 1250's hub-facing leg for THIS link, matched on link addressing
         (see leg_facing_hub). An identification, not a guess -- and it works
         when both legs are the same carrier.
      2. The carrier named in the description text. Only used when exactly
         one description mentions this row's IXC, so it cannot silently pick
         between two same-carrier circuits. Needed because Phase 2 cannot
         reach every device, and an unreachable device has no leg data.
      3. Everything, joined and labelled. The reader picks.

    Returns (text, note, resolved) -- `resolved` False only for case 3, so
    the caller can highlight "you must choose" WITHOUT inspecting the text.
    Sniffing for a separator in the text was wrong: a real NetBox
    description can contain one ("NY/KRGS/632510//WXN | WINDSTREAM" on
    ALBYNYSS134), which flagged a correctly-resolved row.
    """
    if descs is None:
        return CID_UNKNOWN, "NetBox interfaces not read for this device", True
    if not descs:
        return CID_NONE, "no interface descriptions in NetBox for this device", True

    # STRONGEST: the hub told us the 1250's address on this link, and NetBox
    # records that address against a specific interface. Exact identity, and
    # it needs no device login -- so it works for sites Phase 2 never reached.
    if hub_nbr_addr:
        want_ip = hub_nbr_addr.strip()
        hits = [(nm, d) for nm, d, ip in descs if ip and ip == want_ip]
        if len(hits) == 1:
            return (hits[0][1],
                    "NetBox IP {} on {} matches the hub's OSPF neighbour"
                    .format(want_ip, hits[0][0]), True)

    if leg_iface:
        parent = leg_iface.rsplit(".", 1)[0]
        exact = [d for nm, d, _ in descs if nm == parent]
        if len(exact) == 1:
            return (exact[0],
                    "1250 leg {} -> NetBox interface {}".format(
                        leg_iface, parent), True)

    if len(descs) == 1:
        return (descs[0][1],
                "only fibre description on this device ({})".format(
                    descs[0][0]), True)

    want = (ixc or "").strip().lower()
    if want:
        hits = [(nm, d) for nm, d, _ in descs if want in d.lower()]
        if len(hits) == 1:
            return (hits[0][1],
                    "matched on carrier '{}' in the description ({})".format(
                        ixc, hits[0][0]), True)

    return (" | ".join("{}: {}".format(nm, d) for nm, d, _ in descs),
            "{} fibre descriptions; link pairing unavailable and the carrier "
            "does not single one out".format(len(descs)), False)


# ── Row classification ───────────────────────────────────────────────────────

KIND_CUSTOMER   = "CUSTOMER"       # sub-interface with a site description
KIND_UNLABELED  = "UNLABELED"      # sub-interface, no description
KIND_PARENT     = "PARENT-PORT"    # physical port (no .unit)
KIND_LOOPBACK   = "LOOPBACK"
KIND_MGMT       = "MGMT"

# Physical parents come back with the Admin column EMPTY and only Link set:
#     xe-ts13/3                      up
#     xe-ts13/3.132        up        up        WHPLNYWP
# Confirmed live on ces01.jfk1 2026-09-17. So the row parser cannot assume a
# fixed column count, and "no dot in the name" is the reliable parent test.
_IFACE_LINE_RE = re.compile(
    r'^\s*(?P<iface>[a-z0-9]+[a-z0-9\-]*(?:/\d+)?(?:\.\d+)?)\s+'
    r'(?P<rest>.*)$', re.IGNORECASE)

_SUBIF_RE = re.compile(r'^(?P<parent>.+?)\.(?P<unit>\d+)$')

# Description convention, confirmed 2026-09-17 on ces01.jfk1:
#   most rows carry a bare 8-char CLLI      WHPLNYWP  on xe-ts13/3.132
#   one row spells the convention out       PITBPADT234 on xe-ts13/3.234
# and the 1250 hostnames seen in this project follow the same shape
# (SLKCUTMA660, BTRGLAGW492, DLTHMNME624) -- CLLI + VLAN.
#
# So the expected 1250 hostname is derived as CLLI + unit, with the unit NOT
# re-appended when the description already ends in it. This is a HYPOTHESIS:
# the tool reports its NetBox match rate so a wrong assumption shows up as a
# visible column of misses rather than a silently empty cross-check.
_CLLI_RE = re.compile(r'^(?P<clli>[A-Z]{4}[A-Z0-9]{2,6}?)(?P<suffix>\d+)?$')


KIND_HUB_LEG = "HUB-LEG"           # 1250 side: a sub-interface facing a hub

ROLE_HUB  = "1800"
ROLE_1250 = "1250"


def classify(iface, admin, link, desc, role=ROLE_HUB):
    """
    Label a row. Nothing is dropped -- everything gets a kind.

    ROLE MATTERS, and getting it wrong inverts the meaning of a row.

    On an 1800 hub, a sub-interface with NO description is infrastructure
    (xe-ts16/1.5 and xe-ts16/1.10 on ces01.jfk1 -- inter-hub links carrying a
    Full adjacency to another hub's router-id, not customer circuits).

    On a 1250, the hub-facing leg has no description AT ALL -- confirmed live
    on AGSTGAMT442 2026-09-17, whose only non-loopback interface is a bare
    `ge-ts5/1.0`. Descriptions are a hub-side convention only.

    So the identical row shape means "infrastructure, ignore" on one device
    and "this IS the circuit" on the other. A single role-blind classifier
    would either discard every 1250 leg or promote every inter-hub link to a
    customer circuit. Hence the explicit role.
    """
    base = iface.lower()
    if base.startswith("lo"):
        return KIND_LOOPBACK
    if base.startswith("mng") or base.startswith("fxp") or base.startswith("em"):
        return KIND_MGMT
    if not _SUBIF_RE.match(iface):
        return KIND_PARENT
    if role == ROLE_1250:
        # Any non-loopback, non-mgmt sub-interface on a 1250 is a hub-facing
        # leg. Counting these is how intended homing is established without
        # consulting any database.
        return KIND_HUB_LEG
    return KIND_CUSTOMER if desc else KIND_UNLABELED


def count_1250_homes(rows):
    """
    How many hubs this 1250 is CONFIGURED to reach.

    Configuration, not state -- so it survives an outage. A dual-homed site
    with one dead leg still shows two configured legs here, which is exactly
    the case the hub-side view cannot resolve.

    Returns (n_legs, [iface, ...]).
    """
    legs = [r["iface"] for r in (rows or []) if r["kind"] == KIND_HUB_LEG]
    return len(legs), legs


def clli_from(desc, unit):
    """
    Strip a trailing VLAN off a description to get the bare CLLI site code.

    Descriptions come both ways on the same hub (confirmed ces01.jfk1
    2026-09-17): most are a bare 8-char CLLI (`WHPLNYWP` on .132), one spells
    the convention out (`PITBPADT234` on .234). Homing rules are counted per
    SITE, so both forms have to collapse to the same key or a dual-homed site
    written two different ways would look like two separate sites and the
    count would silently pass.
    """
    d = (desc or "").strip().upper()
    if not d:
        return None
    if unit and d.endswith(str(unit)) and len(d) > len(str(unit)):
        return d[:-len(str(unit))]
    return d


def derive_expected_1250(desc, unit):
    """
    Derive the 1250 hostname a sub-interface's description implies.

    Returns (hostname, how) where `how` records the derivation so the report
    can show WHY a name was expected -- 'as-written' when the description
    already carries the unit, 'clli+vlan' when the unit was appended.

    Returns (None, 'no-description') for an unlabeled sub-interface rather
    than inventing a name.
    """
    d = (desc or "").strip().upper()
    if not d:
        return None, "no-description"
    if unit and d.endswith(str(unit)):
        return d, "as-written"
    if unit:
        return "{}{}".format(d, unit), "clli+vlan"
    return d, "as-written"


# ── Parsers ──────────────────────────────────────────────────────────────────

def _cmd_failed(out):
    """
    Detect a command the DEVICE rejected, or output mangled by the pager.

    Same guard as interface_check.py. Without it the tool would append the
    device's own error text as data and then report a short inventory as
    though it were complete.
    """
    low = (out or "").lower()
    for marker in ("syntax error", "unknown command", "invalid command",
                   "missing argument"):
        if marker in low:
            return "device rejected the command"
    for marker in ("--more--", "---more---", "press 'h' for instructions",
                   "press space to continue", "press <space>"):
        if marker in low:
            return "output truncated by pager (missing | no-more)"
    return None


def parse_interfaces_description(raw, role=ROLE_HUB):
    """
    Parse `show interfaces description | no-more` from an NPT-1800 or NPT-1250.

    Both device types emit the SAME format -- confirmed live on ces01.jfk1 and
    AGSTGAMT442, 2026-09-17. `role` only changes how rows are classified (see
    classify()), never how they are parsed.

    Confirmed live on ces01.jfk1 2026-09-17. Real shape:

        Interface            Admin     Link      Description
        xe-ts13/3                      up
        xe-ts13/3.132        up        up        WHPLNYWP
        xe-ts16/1.10         up        up
        lo0.0                up        up
        mng.0                up        up

    Note the physical parent row has NO Admin value -- two tokens after the
    name, not three. Splitting on whitespace and indexing positionally would
    read 'up' as the Admin state of a parent and shift every later field. So
    rows are parsed by token count, and a parent's admin is recorded as ''.

    Returns (rows, error). `rows` is a list of dicts; `error` is a string when
    the output is unusable, in which case rows is empty -- never a partial
    list presented as complete.
    """
    err = _cmd_failed(raw)
    if err:
        return [], err

    lines = (raw or "").splitlines()
    if not lines:
        return [], "no output"

    rows = []
    saw_header = False
    for ln in lines:
        s = ln.rstrip()
        if not s.strip():
            continue
        # Header and prompt lines
        if re.match(r'^\s*Interface\s+Admin\s+Link', s, re.IGNORECASE):
            saw_header = True
            continue
        if s.strip().endswith(">"):          # e.g. "pcapps@ces01.jfk1>"
            continue
        if "@" in s and s.strip().endswith(">"):
            continue
        if s.lstrip().startswith("show "):   # echoed command
            continue

        toks = s.split()
        if not toks:
            continue
        iface = toks[0]
        # An interface name always contains a digit or a dash; this rejects
        # stray prose lines without needing an exhaustive name pattern.
        if not re.search(r'[\d\-]', iface):
            continue

        # Token layout:
        #   3+ tokens with admin+link:   iface admin link [desc...]
        #   2 tokens (parent port):      iface link
        if len(toks) >= 3 and toks[1].lower() in ("up", "down") \
                and toks[2].lower() in ("up", "down"):
            admin = toks[1].lower()
            link  = toks[2].lower()
            desc  = " ".join(toks[3:]).strip()
        elif len(toks) == 2 and toks[1].lower() in ("up", "down"):
            admin = ""                        # parent port -- no admin column
            link  = toks[1].lower()
            desc  = ""
        else:
            # Unrecognised shape. Keep it, flagged, rather than dropping it --
            # a silently skipped line is a silently missing circuit.
            rows.append({
                "iface": iface, "admin": "", "link": "", "description": "",
                "unit": None, "parent": None, "kind": "UNPARSED",
                "expected_1250": None, "derivation": "unparsed",
                "raw_line": s.strip(),
            })
            continue

        m = _SUBIF_RE.match(iface)
        unit   = int(m.group("unit")) if m else None
        parent = m.group("parent") if m else iface
        kind   = classify(iface, admin, link, desc, role=role)
        exp, how = (derive_expected_1250(desc, unit)
                    if kind == KIND_CUSTOMER else (None, "n/a"))

        rows.append({
            "iface": iface, "admin": admin, "link": link,
            "description": desc, "unit": unit, "parent": parent,
            "kind": kind, "expected_1250": exp, "derivation": how,
            "raw_line": s.strip(),
        })

    if not saw_header and not rows:
        return [], "output did not look like 'show interfaces description'"
    return rows, None


def parse_ospf_neighbor(raw):
    """
    Parse `show ospf neighbor | no-more` from an NPT-1800.

    Format verified from real ces01.jfk1 output (the fixture in nni_check.py):

        10.111.132.0    xe-ts16/1.10    Full   10.111.130.254  1  37  22d:11h:11m:51s
        10.111.140.4    xe-ts16/2.426   Down   10.111.130.2    1  32  6d:09h:39m:30s

        address         interface       state  router-id       pri dead uptime

    IMPORTANT: Down neighbours DO appear in this table. So a sub-interface
    missing from it entirely is a different and more serious condition than
    one present with state Down -- the former means no adjacency has formed or
    the entry has aged out completely. The audit distinguishes the two.

    Returns (by_iface, error).
    """
    err = _cmd_failed(raw)
    if err:
        return {}, err

    by_iface = {}
    for ln in (raw or "").splitlines():
        toks = ln.split()
        if len(toks) < 4:
            continue
        addr, iface, state, rid = toks[0], toks[1], toks[2], toks[3]
        if not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', addr):
            continue
        if "." not in iface or "/" not in iface:
            continue
        by_iface[iface] = {
            "nbr_addr": addr,
            "state": state,
            "router_id": rid,
            "uptime": toks[6] if len(toks) > 6 else "",
        }
    return by_iface, None


# ── Gap evaluation ───────────────────────────────────────────────────────────

VERDICT_OK          = "OK"
VERDICT_LINK_DOWN   = "LINK DOWN"
VERDICT_ADMIN_DOWN  = "ADMIN DOWN"
VERDICT_NO_ADJ      = "NO ADJACENCY"
VERDICT_ADJ_NOTFULL = "ADJ NOT FULL"
VERDICT_INFRA       = "-"


def evaluate(row, ospf_by_iface):
    """
    Decide the verdict for one configured sub-interface.

    Order matters: an admin-down interface is a deliberate state, not a fault,
    so it is reported as such and NOT also reported as missing an adjacency --
    otherwise every intentionally disabled circuit shows up as a gap and the
    report becomes noise nobody reads (`reporting-into-void`).
    """
    if row["kind"] not in (KIND_CUSTOMER, KIND_UNLABELED):
        return VERDICT_INFRA, ""

    if row["admin"] == "down":
        return VERDICT_ADMIN_DOWN, "administratively disabled -- expected?"
    if row["link"] == "down":
        return VERDICT_LINK_DOWN, "configured and enabled, link is down"

    adj = ospf_by_iface.get(row["iface"])
    if adj is None:
        if row["kind"] == KIND_UNLABELED:
            return VERDICT_INFRA, "no description, no adjacency"
        return (VERDICT_NO_ADJ,
                "link up but absent from the OSPF neighbour table entirely")
    if adj["state"].lower() != "full":
        return VERDICT_ADJ_NOTFULL, "neighbour state {}".format(adj["state"])
    return VERDICT_OK, ""


def is_gap(verdict):
    return verdict in (VERDICT_LINK_DOWN, VERDICT_NO_ADJ,
                       VERDICT_ADJ_NOTFULL, VERDICT_ADMIN_DOWN)


# ── Homing rules ─────────────────────────────────────────────────────────────
#
# Stated by pcapps 2026-09-17:
#   - a site should have exactly ONE sub-interface per hub
#   - a site is homed on at most TWO hubs (single- or dual-homed)
#   => more than 2 configured sub-interfaces for one site is wrong, and more
#      than 1 on a single hub is wrong even when the total is within 2.
#
# Counted on the CLLI (see clli_from), NOT the derived hostname. Two rows at
# one CLLI derive to two different hostnames by design -- if the count were
# keyed on the derived name, a duplicate could never be detected at all,
# because the derivation itself makes them unique. That is the trap here.

HOMING_OK          = "OK"
HOMING_DUP_ON_HUB  = "DUPLICATE ON HUB"
HOMING_OVER_HOMED  = "OVER-HOMED (>2)"
HOMING_BOTH        = "DUPLICATE ON HUB + OVER-HOMED"


def analyse_homing(all_rows_by_hub):
    """
    Evaluate the homing rules across every audited hub at once.

    `all_rows_by_hub` is {hub_name: [row, ...]}. Only CUSTOMER rows count --
    an unlabeled infrastructure sub-interface has no site and cannot violate a
    site rule.

    Returns {clli: {"hubs": {hub: [iface,...]}, "total": n, "verdict": ...}}.

    NOTE this is the one check in this tool that CANNOT be done per-hub. A
    site over-homed across three hubs looks perfectly legal on each hub
    individually. Running the audit against a subset of hubs therefore cannot
    prove the >2 rule, and the report says so rather than implying a clean
    result -- `partial-application`.
    """
    sites = {}
    for hub, rows in (all_rows_by_hub or {}).items():
        for r in rows:
            if r["kind"] != KIND_CUSTOMER:
                continue
            clli = clli_from(r["description"], r["unit"])
            if not clli:
                continue
            e = sites.setdefault(clli, {"hubs": {}, "total": 0})
            e["hubs"].setdefault(hub, []).append(r["iface"])
            e["total"] += 1

    for clli, e in sites.items():
        dup_on_hub  = any(len(v) > 1 for v in e["hubs"].values())
        over_homed  = e["total"] > 2
        if dup_on_hub and over_homed:
            e["verdict"] = HOMING_BOTH
        elif dup_on_hub:
            e["verdict"] = HOMING_DUP_ON_HUB
        elif over_homed:
            e["verdict"] = HOMING_OVER_HOMED
        else:
            e["verdict"] = HOMING_OK
        e["homed_on"] = sorted(e["hubs"].keys())
    return sites


def homing_violations(sites):
    """Just the offenders, worst first, for the GAPS sheet."""
    order = {HOMING_BOTH: 0, HOMING_OVER_HOMED: 1, HOMING_DUP_ON_HUB: 2}
    bad = [(c, e) for c, e in (sites or {}).items()
           if e["verdict"] != HOMING_OK]
    return sorted(bad, key=lambda t: (order.get(t[1]["verdict"], 9), t[0]))


# ── Phase 1: the hubs ────────────────────────────────────────────────────────

def audit_hub(site, ip, username, password):
    """
    Read one 1800 hub. Returns a dict; `error` non-empty means UNREAD.

    An UNREAD hub yields NO rows. It is never rendered as a short inventory,
    because a hub that returned 40 of its 96 circuits and reported success is
    the `fails-as-success` shape that produced the 79% SUSPECT bug.
    """
    # Name resolution follows hub_registry's stated authority order:
    #   device's own hostname > NetBox/table > the site label we were handed.
    # Start with the table so an early failure still has something to report,
    # then upgrade once the device tells us who it is.
    tbl_name, tbl_src, tbl_warns = hub_registry.ip_to_name(ip)

    out = {
        "site": site, "ip": ip,
        "hub_name": tbl_name or site,
        "device_hostname": "", "name_source": tbl_src or "table",
        "rows": [], "ospf": {}, "error": None,
        "notes": list(tbl_warns or []),
    }
    c = None
    try:
        c = NPT1800Client(ip, username, password)
        c.connect()

        # The device is the authority. Upgrade the name, and CONTRADICT the
        # table out loud if they disagree -- a table that says DFW while the
        # box says lax1 is the exact drift that mislabelled LAX circuits as
        # DFW for two months.
        dev_host = getattr(c, "hostname", "") or ""
        out["device_hostname"] = dev_host
        dev_name = hub_registry.name_from_hostname(dev_host)
        if dev_name:
            if tbl_name and dev_name != tbl_name:
                out["notes"].append(
                    "NAME CONFLICT: device reports '{}' ({}), table says '{}' "
                    "({}) -- using the device".format(
                        dev_host, dev_name, tbl_name, tbl_src))
            out["hub_name"] = dev_name
            out["name_source"] = "device hostname"
        elif dev_host:
            out["notes"].append(
                "device hostname '{}' did not yield a site -- name is from {}"
                .format(dev_host, tbl_src))

        raw_if = c.run(IFACE_CMD, CMD_TIMEOUT)
        rows, err = parse_interfaces_description(raw_if, role=ROLE_HUB)
        if err:
            out["error"] = "{}: {}".format(IFACE_CMD, err)
            return out
        out["rows"] = rows

        raw_ospf = c.run(OSPF_CMD, CMD_TIMEOUT)
        ospf, oerr = parse_ospf_neighbor(raw_ospf)
        if oerr:
            # The inventory is still good; only state is missing. Say so
            # explicitly rather than reporting every circuit as NO ADJACENCY.
            out["notes"].append(
                "OSPF state unavailable ({}) -- adjacency verdicts suppressed"
                .format(oerr))
            out["ospf"] = None
        else:
            out["ospf"] = ospf
    except CredentialError as e:
        out["error"] = "credentials rejected: {}".format(e)
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, e)
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return out


def production_hubs():
    """
    The four production hubs, LAB excluded by name.

    Resolved live through hub_registry so a NetBox change is picked up, and
    any drift warning it raises is surfaced rather than swallowed.
    """
    out, warns = [], []
    for site in hub_registry.sites():
        if site.upper() in EXCLUDED_SITES:
            continue
        ip, src, w = hub_registry.site_to_ip(site)
        warns.extend(w or [])
        if not ip:
            warns.append("{}: no IP resolved -- skipped".format(site))
            continue
        out.append((site, ip))
    return out, warns


def run_phase1(username, password, progress=None):
    """
    Audit the four production hubs. Returns (hubs, sites, warnings).

    `sites` is the homing analysis across every hub that was READ. Hubs that
    failed are excluded from it and named in `warnings` -- because the >2-homes
    rule cannot be evaluated against a partial set of hubs, and presenting it
    as if it could is `partial-application`.
    """
    def say(msg):
        if progress:
            progress(msg)
        else:
            print(msg)

    targets, warnings = production_hubs()
    say("  auditing {} production hubs ({})".format(
        len(targets), ", ".join(s for s, _ in targets)))
    for w in warnings:
        say("  [!] {}".format(w))

    results, lock = {}, threading.Lock()

    def worker(site, ip):
        r = audit_hub(site, ip, username, password)
        with lock:
            results[site] = r
            if r["error"]:
                say("  [!] {} UNREAD -- {}".format(site, r["error"]))
            else:
                n = sum(1 for x in r["rows"] if x["kind"] == KIND_CUSTOMER)
                say("  {} ok -- {} circuits".format(site, n))
            for note in r["notes"]:
                say("  [!] {}: {}".format(site, note))

    threads = [threading.Thread(target=worker, args=(s, i), daemon=True)
               for s, i in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Key on hub_name, which is now always a plain string (the device's own
    # name where available). Guard against a duplicate key anyway: two hubs
    # resolving to one name would silently merge their circuit lists and
    # corrupt every homing count.
    read = {}
    for r in results.values():
        if r["error"]:
            continue
        key = r["hub_name"]
        if key in read:
            warnings.append(
                "two hubs both resolved to the name '{}' ({} and {}) -- "
                "homing counts would be wrong, so the second is excluded"
                .format(key, key, r["ip"]))
            continue
        read[key] = r["rows"]
    unread = [s for s, r in results.items() if r["error"]]
    if unread:
        warnings.append(
            "hubs UNREAD: {} -- the >2-homes rule cannot be proven without "
            "every hub, so over-homing across the missing hub(s) would not be "
            "detected".format(", ".join(sorted(unread))))

    sites = analyse_homing(read)
    return results, sites, warnings


def fetch_circuit_ids(hostnames, progress=None, workers=8):
    """
    {hostname: (by_carrier, unparsed)} for every device, from NetBox.

    A hostname NetBox cannot answer for maps to (None, []) -- distinct from
    (empty dict, []), which means "NetBox answered and there are no circuit
    descriptions". Those two must not collapse, or a NetBox outage silently
    reports every site as having no circuits.

    Returns (by_host, warnings).
    """
    def say(msg):
        if progress:
            progress(msg)
        else:
            print(msg)

    try:
        url, token = load_netbox_config()
    except Exception as e:
        return {}, ["NetBox config unreadable: {} -- circuit IDs unavailable"
                    .format(e)]
    nb = NetboxClient(url, token)
    if not nb.ok():
        return {}, ["NetBox not configured -- circuit IDs unavailable"]

    todo = sorted({h for h in hostnames if h})
    say("  circuit IDs: querying NetBox for {} device(s)".format(len(todo)))

    out, warns, lock = {}, [], threading.Lock()
    failed, done = [], [0]

    def worker(chunk):
        for host in chunk:
            ifaces, err = nb.device_interface_descriptions(host)
            with lock:
                if err:
                    out[host] = (None, [])
                    failed.append("{} ({})".format(host, err))
                else:
                    out[host] = parse_circuit_descriptions(ifaces)
                done[0] += 1
                if done[0] % 50 == 0 or done[0] == len(todo):
                    say("    {}/{}".format(done[0], len(todo)))

    chunks = [todo[i::workers] for i in range(workers)]
    threads = [threading.Thread(target=worker, args=(c,), daemon=True)
               for c in chunks if c]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if failed:
        warns.append(
            "NetBox lookup failed for {} device(s) -- their circuit IDs are "
            "UNKNOWN, not empty. First few: {}".format(
                len(failed), "; ".join(sorted(failed)[:5])))

    # Nothing can "fail to parse" any more -- descriptions are copied
    # verbatim. The only thing dropped is TDM interfaces, which are not EVCs.
    return out, warns


def single_appearance_sites(sites):
    """
    Sites configured on exactly ONE hub -- the only ambiguous population.

    A site on two hubs is provably dual-homed from the hub config alone. A
    site on three or more is provably wrong. Only a single appearance could be
    either a correct single-home or a dual-home missing its second leg, and
    only the 1250's own configuration can tell those apart.
    """
    return sorted(c for c, e in (sites or {}).items() if e["total"] == 1)


# ── Phase 2: the 1250 sweep ──────────────────────────────────────────────────
#
# NetBox is the ADDRESS BOOK here and nothing more -- it answers "what IP do I
# SSH to", never "what is built" or "how is this site homed". Those come from
# the device's own configuration. This is the same split hub_registry applies
# (device hostname authoritative, NetBox resolves the IP), and it is the
# reason a conflicting database can still be useful: we are not asking it the
# question it gets wrong.

SWEEP_WORKERS = 24        # matches ospf_stability.py's proven concurrency
SWEEP_TIMEOUT = 45


def peer_ip_from_hub_addr(hub_addr):
    """
    Derive the 1250's address from the hub's own interface address.

    Addressing scheme (pcapps, 2026-09-17): on a hub<->1250 point-to-point,
    the 1800 is x.x.x.N and the 1250 is x.x.x.N-1.

    Only needed for a site with NO adjacency -- one that IS adjacent exposes
    its loopback directly as the router-ID in the hub's neighbour table, which
    is a fact rather than a derivation and is always preferred.

    Returns None rather than guessing if N-1 would leave the host range.
    """
    m = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3})\.(\d{1,3})$',
                 (hub_addr or "").strip())
    if not m:
        return None
    last = int(m.group(2))
    if last - 1 < 1:
        return None
    return "{}.{}".format(m.group(1), last - 1)


def hub_ip_from_peer(peer_ip):
    """
    Inverse of peer_ip_from_hub_addr: 1250 x.x.x.N-1 -> hub x.x.x.N.
    """
    m = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3})\.(\d{1,3})$',
                 (peer_ip or "").strip())
    if not m:
        return None
    last = int(m.group(2))
    if last + 1 > 254:
        return None
    return "{}.{}".format(m.group(1), last + 1)


def leg_facing_hub(dev_ospf, hub_nbr_addr):
    """
    Which of the 1250's legs faces the hub this row came from.

    The hub's neighbour table reports the 1250's address on the link; the
    1250's neighbour table reports the HUB's address on the same link. With
    the addressing scheme (1800 = x.x.x.N, 1250 = x.x.x.N-1) those two are
    one apart, which pins the pairing exactly -- no guessing by carrier, and
    it works even when both legs are the same carrier.

    Falls back to same-/30 matching if the +1 does not line up, so an
    addressing exception degrades to "same link" rather than to nothing.

    Returns the 1250 interface name (e.g. 'ge-ts2/1.0'), or None.
    """
    if not dev_ospf or not hub_nbr_addr:
        return None
    want = hub_ip_from_peer(hub_nbr_addr)
    if want:
        for iface, adj in dev_ospf.items():
            if (adj.get("nbr_addr") or "").strip() == want:
                return iface
    # /30 fallback: same first three octets AND within the same 4-address block
    m = re.match(r'^(\d+\.\d+\.\d+)\.(\d+)$', hub_nbr_addr.strip())
    if not m:
        return None
    net, host = m.group(1), int(m.group(2))
    block = host // 4
    for iface, adj in dev_ospf.items():
        m2 = re.match(r'^(\d+\.\d+\.\d+)\.(\d+)$',
                      (adj.get("nbr_addr") or "").strip())
        if m2 and m2.group(1) == net and int(m2.group(2)) // 4 == block:
            return iface
    return None


def address_for_site(clli, sites, hub_results):
    """
    Work out what IP to SSH to for one site, and say where it came from.

    Preference order, strongest first:
      1. router-ID from the hub's OSPF neighbour table -- the 1250's loopback,
         reported by the device itself. This is what ospf_stability.py SSHes
         to, so it is known to work.
      2. N-1 off the hub's neighbour-address column, for a site whose entry
         exists but carries no usable router-ID.
      3. nothing -- reported as no-address, never guessed further.

    NetBox is deliberately not consulted. Every site Phase 2 runs against is
    UP on its one hub, so source 1 covers it; reaching for a database we
    already know disagrees with the network would add a failure mode for no
    gain.

    Returns (ip, source) or (None, reason).
    """
    e = sites.get(clli) or {}
    for hub_name, ifaces in (e.get("hubs") or {}).items():
        res = next((r for r in hub_results.values()
                    if r.get("hub_name") == hub_name and not r["error"]), None)
        if not res or not res.get("ospf"):
            continue
        for iface in ifaces:
            adj = res["ospf"].get(iface)
            if not adj:
                continue
            rid = (adj.get("router_id") or "").strip()
            if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', rid):
                return rid, "router-ID (loopback) from {}".format(hub_name)
            peer = peer_ip_from_hub_addr(adj.get("nbr_addr"))
            if peer:
                return peer, "derived N-1 from {} neighbour address".format(
                    hub_name)
    return None, "no adjacency on its hub -- no router-ID to SSH to"


def audit_1250(hostname, ip, username, password):
    """
    Read one access device. Returns a dict; `error` non-empty means the site's
    intent is UNKNOWN.

    An unreachable device is NEVER assumed single-homed. A dual-homed site
    with one dead leg and an unreachable device would then pass the audit
    silently -- the exact `fails-as-success` shape this tool exists to close.
    """
    out = {"hostname": hostname, "ip": ip, "rows": [], "ospf": {},
           "legs": [], "n_legs": None, "error": None,
           "device_hostname": "", "name_conflict": ""}
    c = None
    try:
        c = NPT1250Client(ip, username, password)
        c.connect()

        # THE DEVICE'S OWN NAME, straight off the prompt -- 'pcapps@CLEVOH62320>'
        # yields 'CLEVOH62320' (pcapps, 2026-09-18). This is authoritative and
        # replaces the CLLI+VLAN derivation, which is only a guess at what the
        # device is called. The derivation stays as a fallback for unreachable
        # devices, and any disagreement between the two is reported rather
        # than silently resolved -- that disagreement IS the map of where the
        # naming convention breaks.
        dev_host = (getattr(c, "hostname", "") or "").strip()
        out["device_hostname"] = dev_host
        if dev_host and hostname and dev_host.upper() != hostname.upper():
            out["name_conflict"] = (
                "derived '{}' but device reports '{}'".format(
                    hostname, dev_host))

        raw_if = c.run(IFACE_CMD, SWEEP_TIMEOUT)
        rows, err = parse_interfaces_description(raw_if, role=ROLE_1250)
        if err:
            out["error"] = "{}: {}".format(IFACE_CMD, err)
            return out
        out["rows"] = rows
        n, legs = count_1250_homes(rows)
        out["n_legs"], out["legs"] = n, legs

        raw_ospf = c.run(OSPF_CMD, SWEEP_TIMEOUT)
        ospf, oerr = parse_ospf_neighbor(raw_ospf)
        out["ospf"] = None if oerr else ospf
    except CredentialError as e:
        out["error"] = "credentials rejected: {}".format(e)
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, e)
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
    return out


INTENT_SINGLE     = "SINGLE-HOMED (confirmed)"
INTENT_MISSING    = "DUAL-HOMED -- LEG MISSING"
INTENT_UNKNOWN    = "UNKNOWN -- device unreachable"
INTENT_NO_ADDRESS = "UNKNOWN -- no reachable address"
INTENT_ODD        = "UNEXPECTED LEG COUNT"


INTENT_CONTRADICTION = "CONTRADICTION -- see detail"


def resolve_intent(dev_result, hubs_configured):
    """
    Given a 1250's own configured leg count and how many hubs have it
    configured, decide what the site actually is.

    `hubs_configured` is how many of the four hubs carry a sub-interface for
    this site (1, for every site Phase 2 is run against).

    TWO SOURCES, COMPARED. The leg count comes from the device's
    CONFIGURATION; the neighbour count comes from the device's live OSPF
    table. Configuration says what was intended, OSPF says what is actually
    adjacent, and the audit reports both rather than trusting either alone.
    Where they disagree in a way that cannot be explained, the verdict is
    CONTRADICTION -- an unexplained disagreement is information, and
    collapsing it into a confident answer is how wrong data gets shipped
    looking right.
    """
    if dev_result is None:
        return INTENT_NO_ADDRESS, "no reachable address to SSH to"
    if dev_result["error"]:
        return INTENT_UNKNOWN, dev_result["error"]

    n = dev_result["n_legs"]
    legs = ", ".join(dev_result.get("legs") or []) or "none"

    # The device's own OSPF table. None means the read failed -- distinct
    # from zero neighbours, and it must not be read as zero.
    ospf = dev_result.get("ospf")
    n_nbr = None if ospf is None else len(ospf)
    nbr_txt = ("neighbour count unavailable" if n_nbr is None
               else "{} OSPF neighbour(s)".format(n_nbr))

    if n == 1:
        if n_nbr is None or n_nbr == 1:
            return INTENT_SINGLE, "1 configured leg ({}); {}".format(
                legs, nbr_txt)
        if n_nbr == 0:
            return (INTENT_CONTRADICTION,
                    "1 configured leg ({}) with 0 OSPF neighbours, yet the "
                    "hub reports an adjacency to this site -- the hub and the "
                    "device disagree".format(legs))
        return (INTENT_CONTRADICTION,
                "1 configured leg ({}) but {} OSPF neighbours -- more "
                "adjacencies than legs".format(legs, n_nbr))

    if n == 2:
        if n_nbr is None:
            return (INTENT_MISSING,
                    "device configures 2 legs ({}) but only {} hub has a "
                    "sub-interface -- second leg not built. NOT corroborated: "
                    "{}".format(legs, hubs_configured, nbr_txt))
        if n_nbr <= 1:
            return (INTENT_MISSING,
                    "CORROBORATED -- device configures 2 legs ({}) and sees "
                    "{}, while only {} hub has a sub-interface. Second leg is "
                    "not built.".format(legs, nbr_txt, hubs_configured))
        return (INTENT_CONTRADICTION,
                "device configures 2 legs ({}) and sees {}, but only {} hub "
                "has a sub-interface configured -- a second hub is adjacent "
                "to this site without a matching sub-interface in the "
                "audit".format(legs, nbr_txt, hubs_configured))

    return (INTENT_ODD,
            "device configures {} hub-facing legs: {}; {}".format(
                n, legs, nbr_txt))


def run_phase2(target_sites, sites, hub_results, username, password,
               progress=None, derived_for=None):
    """
    Sweep the access devices.

    Two jobs, and they need different scopes:

      * Resolving single-homed vs missing-leg only needs SINGLE-hub sites -- a
        site on two hubs is provably dual-homed from hub config alone.
      * Getting each device's REAL hostname needs every site, because the
        CLLI+VLAN derivation is only a guess and NetBox lookups key on the
        answer.

    The caller picks the scope; `resolve_intent` is only meaningful for
    single-hub sites either way.
    """
    def say(msg):
        if progress:
            progress(msg)
        else:
            print(msg)

    derived_for = derived_for or {}
    warns = []
    todo, no_addr = [], []
    for clli in target_sites:
        ip, src = address_for_site(clli, sites, hub_results)
        if ip:
            todo.append((clli, ip, src))
        else:
            no_addr.append((clli, src))

    say("  sweep targets: {} addressable, {} without an address".format(
        len(todo), len(no_addr)))
    if no_addr:
        warns.append(
            "{} single-hub site(s) had no reachable address and are reported "
            "UNKNOWN, not single-homed".format(len(no_addr)))

    derived = sum(1 for _, _, s in todo if s.startswith("derived"))
    if derived:
        say("  {} target(s) addressed by the N-1 rule rather than a "
            "router-ID".format(derived))

    results, lock = {}, threading.Lock()
    done = [0]

    def worker(chunk):
        for clli, ip, src in chunk:
            r = audit_1250(derived_for.get(clli, clli), ip, username, password)
            r["addr_source"] = src
            with lock:
                results[clli] = r
                done[0] += 1
                if done[0] % 25 == 0 or done[0] == len(todo):
                    say("    swept {}/{}".format(done[0], len(todo)))

    chunks = [todo[i::SWEEP_WORKERS] for i in range(SWEEP_WORKERS)]
    threads = [threading.Thread(target=worker, args=(c,), daemon=True)
               for c in chunks if c]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for clli, why in no_addr:
        results[clli] = None          # -> INTENT_NO_ADDRESS

    unreachable = sum(1 for r in results.values() if r and r["error"])
    if unreachable:
        warns.append(
            "{} device(s) unreachable -- intent unknown for those sites"
            .format(unreachable))

    conflicts = [r["name_conflict"] for r in results.values()
                 if r and r.get("name_conflict")]
    if conflicts:
        say("  {} hostname(s) differ from the CLLI+VLAN derivation".format(
            len(conflicts)))
        warns.append(
            "{} device(s) report a hostname unlike the CLLI+VLAN derivation "
            "-- the device name is used. Examples: {}".format(
                len(conflicts), "; ".join(sorted(conflicts)[:4])))
    return results, warns


# ── Self-test ────────────────────────────────────────────────────────────────

_SAMPLE_JFK1 = """
Interface            Admin     Link      Description
xe-ts13/3                      up
xe-ts13/3.132        up        up        WHPLNYWP
xe-ts13/3.234        up        up        PITBPADT234
xe-ts13/3.240        up        up        FRDRMDFR
xe-ts16/1                      up
xe-ts16/1.5          up        up
xe-ts16/1.10         up        up
xe-ts16/2.426        up        up        RLGHNCHO
xe-ts16/2.2132       up        up        WHPLNYWP
lo0.0                up        up
mng.0                up        up
"""

_SAMPLE_OSPF = """
  10.111.132.0    xe-ts16/1.10    Full   10.111.130.254  1  37  22d:11h:11m:51s
  10.111.140.4    xe-ts16/2.426   Down   10.111.130.2    1  32  6d:09h:39m:30s
  10.111.140.12   xe-ts13/3.240   Full   10.111.130.6    1  38  6d:10h:40m:08s
"""


def self_test(verbose=True):
    """
    Prove the parser against the real captured sample before it is trusted on
    four hubs. Returns (passed, failures).
    """
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append("{}: got {!r}, want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    rows, err = parse_interfaces_description(_SAMPLE_JFK1)
    check("parse error", err, None)

    by = {r["iface"]: r for r in rows}

    # Parent ports must not be read as admin-up sub-interfaces.
    check("parent xe-ts13/3 kind",  by["xe-ts13/3"]["kind"],  KIND_PARENT)
    check("parent xe-ts13/3 admin", by["xe-ts13/3"]["admin"], "")
    check("parent xe-ts13/3 link",  by["xe-ts13/3"]["link"],  "up")

    # Labeled customer circuits.
    check("jfk1 .132 kind", by["xe-ts13/3.132"]["kind"], KIND_CUSTOMER)
    check("jfk1 .132 unit", by["xe-ts13/3.132"]["unit"], 132)

    # Unlabeled infrastructure sub-interfaces.
    check("xe-ts16/1.5 kind",  by["xe-ts16/1.5"]["kind"],  KIND_UNLABELED)
    check("xe-ts16/1.10 kind", by["xe-ts16/1.10"]["kind"], KIND_UNLABELED)

    # Loopback / management.
    check("lo0.0 kind", by["lo0.0"]["kind"], KIND_LOOPBACK)
    check("mng.0 kind", by["mng.0"]["kind"], KIND_MGMT)

    # The derivation: description already carrying its unit must NOT double it.
    check("PITBPADT234 derived", by["xe-ts13/3.234"]["expected_1250"],
          "PITBPADT234")
    check("PITBPADT234 how", by["xe-ts13/3.234"]["derivation"], "as-written")

    # The two WHPLNYWP rows must resolve to DISTINCT devices, not collide.
    check("WHPLNYWP .132 derived",  by["xe-ts13/3.132"]["expected_1250"],
          "WHPLNYWP132")
    check("WHPLNYWP .2132 derived", by["xe-ts16/2.2132"]["expected_1250"],
          "WHPLNYWP2132")

    # Verdicts against the OSPF sample.
    ospf, oerr = parse_ospf_neighbor(_SAMPLE_OSPF)
    check("ospf parse error", oerr, None)
    check("ospf rows", len(ospf), 3)

    v, _ = evaluate(by["xe-ts13/3.240"], ospf)
    check("ts13/3.240 (Full)", v, VERDICT_OK)

    v, _ = evaluate(by["xe-ts16/2.426"], ospf)
    check("ts16/2.426 (Down nbr)", v, VERDICT_ADJ_NOTFULL)

    v, _ = evaluate(by["xe-ts13/3.132"], ospf)
    check("ts13/3.132 (absent from table)", v, VERDICT_NO_ADJ)

    # Failure posture: a truncated or rejected read must yield NO rows.
    r2, e2 = parse_interfaces_description(
        "Interface Admin Link Description\nxe-ts13/3.132 up up FOO\n---More---")
    check("pager truncation detected", bool(e2), True)
    check("truncated read returns no rows", len(r2), 0)

    r3, e3 = parse_interfaces_description("syntax error: unknown command")
    check("device rejection detected", bool(e3), True)
    check("rejected read returns no rows", len(r3), 0)

    # ---- homing rules ----
    check("clli of PITBPADT234/234", clli_from("PITBPADT234", 234), "PITBPADT")
    check("clli of WHPLNYWP/132",    clli_from("WHPLNYWP", 132),    "WHPLNYWP")

    # Both jfk1 WHPLNYWP rows must collapse to ONE clli and flag as a
    # duplicate on that hub -- this is the case the derived hostname hides.
    sites = analyse_homing({"ces01.jfk1": rows})
    check("WHPLNYWP counted once", "WHPLNYWP" in sites, True)
    check("WHPLNYWP total subifs", sites["WHPLNYWP"]["total"], 2)
    check("WHPLNYWP verdict", sites["WHPLNYWP"]["verdict"], HOMING_DUP_ON_HUB)
    check("FRDRMDFR verdict", sites["FRDRMDFR"]["verdict"], HOMING_OK)

    # Legal dual-homing: one sub-int on each of two hubs.
    dual = analyse_homing({
        "ces01.jfk1": [{"kind": KIND_CUSTOMER, "description": "ABCDEFGH",
                        "unit": 120, "iface": "xe-ts16/2.120"}],
        "ces01.atl1": [{"kind": KIND_CUSTOMER, "description": "ABCDEFGH",
                        "unit": 220, "iface": "xe-ts16/2.220"}],
    })
    check("legal dual-home verdict", dual["ABCDEFGH"]["verdict"], HOMING_OK)
    check("legal dual-home hubs", dual["ABCDEFGH"]["homed_on"],
          ["ces01.atl1", "ces01.jfk1"])

    # ---- 1250 side, captured live on AGSTGAMT442 2026-09-17 ----
    d1250 = ("Interface            Admin     Link      Description\n"
             "ge-ts5/1                       up        \n"
             "ge-ts5/1.0           up        up        \n"
             "lo0.0                up        up        \n")
    r1250, e1250 = parse_interfaces_description(d1250, role=ROLE_1250)
    check("1250 parse error", e1250, None)
    b = {r["iface"]: r for r in r1250}
    check("1250 parent kind",   b["ge-ts5/1"]["kind"],   KIND_PARENT)
    check("1250 lo0.0 kind",    b["lo0.0"]["kind"],      KIND_LOOPBACK)
    # The role inversion: this same row is UNLABELED infra on a hub.
    check("1250 leg kind",      b["ge-ts5/1.0"]["kind"], KIND_HUB_LEG)
    r_as_hub, _ = parse_interfaces_description(d1250, role=ROLE_HUB)
    check("same row as hub role",
          {r["iface"]: r["kind"] for r in r_as_hub}["ge-ts5/1.0"],
          KIND_UNLABELED)

    n, legs = count_1250_homes(r1250)
    check("AGSTGAMT442 homes", n, 1)
    check("AGSTGAMT442 legs",  legs, ["ge-ts5/1.0"])

    # 1250 OSPF output has a header block AND no uptime column (6 fields, not
    # the hub's 7). Parser must tolerate both.
    n1250 = ("Ospf Neighbors, instance master\n"
             "  Protocol Instance 0:\n"
             "  Address         Interface       state     ID              "
             "Pri   Dead  \n"
             "  10.111.130.99   ge-ts5/1.0      Full      10.111.132.55   "
             "1     32    \n")
    o1250, oe = parse_ospf_neighbor(n1250)
    check("1250 ospf parse error", oe, None)
    check("1250 ospf rows", len(o1250), 1)
    check("1250 ospf iface", list(o1250.keys()), ["ge-ts5/1.0"])
    check("1250 ospf has no uptime", o1250["ge-ts5/1.0"]["uptime"], "")

    # ---- EVC circuit text: RAW copy, no parsing ----
    # Real NetBox shapes, both formats seen live 2026-09-18.
    whpl = [
        {"name": "ge-ts2/1", "type": "SFP (1GE)",
         "description": "Nitel: NIT555057"},
        {"name": "ge-ts2/2", "type": "SFP (1GE)",
         "description": "Zayo: ETYX/389224//ZYO"},
        {"name": "ge-ts2/3", "type": "SFP (1GE)", "description": ""},
        {"name": "T3/T4 A", "type": "T1 (1.544 Mbps)",
         "description": "should be skipped"},
        {"name": "ge-ts5/1", "type": "SFP (1GE)",
         "description": "Zayo: ETYX/389151//ZYO"},
    ]
    d, skipped = parse_circuit_descriptions(whpl)
    check("fibre descriptions found", len(d), 3)
    check("empty description excluded",
          [n for n, _, _ in d], ["ge-ts2/1", "ge-ts2/2", "ge-ts5/1"])
    check("TDM excluded", [n for n, _ in skipped], ["T3/T4 A"])

    # No leg known -> show everything, labelled, verbatim.
    txt, note, _ = circuit_id_for(d)
    check("all shown when leg unknown",
          txt, "ge-ts2/1: Nitel: NIT555057 | ge-ts2/2: Zayo: ETYX/389224//ZYO"
               " | ge-ts5/1: Zayo: ETYX/389151//ZYO")

    # Leg known -> that interface's text, verbatim and alone.
    txt, note, _ = circuit_id_for(d, leg_iface="ge-ts5/1.0")
    check("leg picks its own description", txt, "Zayo: ETYX/389151//ZYO")
    txt, _, _ = circuit_id_for(d, leg_iface="ge-ts2/2.0")
    check("other leg picks other description", txt, "Zayo: ETYX/389224//ZYO")

    # The hyphen format that used to fall through the parser entirely.
    dix, _ = parse_circuit_descriptions([
        {"name": "ge-ts2/1", "type": "SFP (1GE)", "description": ""},
        {"name": "ge-ts5/1", "type": "SFP (1GE)",
         "description": "ZAYO - ETYX/420953//ZYO"}])
    txt, _, _ = circuit_id_for(dix)
    check("hyphen format copied verbatim", txt, "ZAYO - ETYX/420953//ZYO")

    # Any free-text description survives -- no format assumed at all.
    odd, _ = parse_circuit_descriptions([
        {"name": "ge-ts5/1", "type": "SFP (1GE)",
         "description": "backhaul (temp) 94/KRGN/205526//OB  see NOE-1234"}])
    txt, _, _ = circuit_id_for(odd)
    check("free text survives intact",
          txt, "backhaul (temp) 94/KRGN/205526//OB  see NOE-1234")

    # The two empty states stay distinguishable.
    txt, _, _ = circuit_id_for([])
    check("device has no descriptions -> blank", txt, CID_NONE)
    txt, _, _ = circuit_id_for(None)
    check("netbox unread -> UNKNOWN", txt, CID_UNKNOWN)

    # ---- hub<->1250 link pairing (N / N-1) ----
    # ALBQNMMA664: two Zayo legs, one to DFW one to LAX. Carrier cannot tell
    # them apart; the link addressing can.
    dev_ospf = {
        "ge-ts2/1.0": {"nbr_addr": "10.111.134.9",  "state": "Full"},
        "ge-ts5/1.0": {"nbr_addr": "10.111.135.13", "state": "Full"},
    }
    check("DFW link -> its own leg",
          leg_facing_hub(dev_ospf, "10.111.134.8"), "ge-ts2/1.0")
    check("LAX link -> its own leg",
          leg_facing_hub(dev_ospf, "10.111.135.12"), "ge-ts5/1.0")
    check("hub_ip_from_peer", hub_ip_from_peer("10.111.134.8"), "10.111.134.9")
    check("hub_ip_from_peer rejects junk", hub_ip_from_peer("nope"), None)
    check("no dev ospf -> no leg", leg_facing_hub(None, "10.1.1.8"), None)
    check("unrelated address -> no leg",
          leg_facing_hub(dev_ospf, "10.99.99.8"), None)

    # Each hub row now resolves to ONE circuit, not both.
    descs2 = [("ge-ts2/1", "Zayo: ETYX/388887//ZYO", ""),
              ("ge-ts5/1", "Zayo: ETYX/388706//ZYO", "")]
    t, _, _ = circuit_id_for(descs2,
                          leg_iface=leg_facing_hub(dev_ospf, "10.111.134.8"))
    check("DFW row gets its circuit", t, "Zayo: ETYX/388887//ZYO")
    t, _, _ = circuit_id_for(descs2,
                          leg_iface=leg_facing_hub(dev_ospf, "10.111.135.12"))
    check("LAX row gets its circuit", t, "Zayo: ETYX/388706//ZYO")

    # ---- carrier fallback when the device was unreachable ----
    # JCVLFLCL452: Phase 2 could not reach it, so no link pairing. The
    # carrier named in the text resolves each hub row to its own circuit.
    jc = [("ge-ts2/1", "ZAYO - ETYX/388889//ZYO", ""),
          ("ge-ts5/1", "ATT - 52/KRGN/705798//SB", "")]
    t1, _, ok1 = circuit_id_for(jc, ixc="ATT")
    check("ATT row picks the ATT circuit", t1, "ATT - 52/KRGN/705798//SB")
    check("  and is marked resolved", ok1, True)
    t2, _, _ = circuit_id_for(jc, ixc="Zayo")
    check("Zayo row picks the Zayo circuit", t2, "ZAYO - ETYX/388889//ZYO")

    # PTLDOR13672-B: one description is a cross-connect note, not a circuit.
    pt = [("ge-ts2/1", 'XCON TO "A" NPT-1250', ""),
          ("ge-ts5/1", "ZAYO - ETYX/388689//ZYO", "")]
    t3, _, ok3 = circuit_id_for(pt, ixc="Zayo")
    check("carrier skips the XCON note", t3, "ZAYO - ETYX/388689//ZYO")
    check("  resolved", ok3, True)

    # FTMYFLXA939: two Zayo, neither description names the carrier. Nothing
    # can decide, so it must stay unresolved rather than pick one.
    ft = [("ge-ts2/1", "ETYX/422984//ZYO", ""),
          ("ge-ts5/1", "ETYX/422982//ZYO", "")]
    t4, _, ok4 = circuit_id_for(ft, ixc="Zayo")
    check("undecidable stays unresolved", ok4, False)
    check("  and shows both", " | " in t4, True)

    # ALBYNYSS134: a real description CONTAINING a pipe must still count as
    # resolved -- this is what was false-flagging.
    al = [("ge-ts2/1", "NY/KRGS/632510//WXN | WINDSTREAM", "")]
    t5, _, ok5 = circuit_id_for(al, ixc="Windstream")
    check("pipe inside a description is fine", t5,
          "NY/KRGS/632510//WXN | WINDSTREAM")
    check("  not flagged as ambiguous", ok5, True)

    # Link pairing still outranks the carrier when both are available.
    dv = {"ge-ts5/1.0": {"nbr_addr": "10.111.134.9"}}
    t6, _, _ = circuit_id_for(jc, ixc="ATT",
                              leg_iface=leg_facing_hub(dv, "10.111.134.8"))
    check("link pairing wins over carrier", t6, "ATT - 52/KRGN/705798//SB")

    # ---- IXC map ----
    check("ixc xe-ts13/3", ixc_for_port("xe-ts13/3"), "Nitel")
    check("ixc xe-ts16/2", ixc_for_port("xe-ts16/2"), "Zayo")
    check("ixc xe-ts13/2", ixc_for_port("xe-ts13/2"), "Zayo")
    check("ixc xe-ts13/4", ixc_for_port("xe-ts13/4"), "ATT")
    check("ixc xe-ts16/3", ixc_for_port("xe-ts16/3"), "Windstream")
    # ge-ts16/4 / Lumen was in the supplied list but in NO sampled device
    # config, so it is deliberately absent. If this assertion ever fails,
    # someone added it -- make sure a device confirmed it first.
    check("ixc ge-ts16/4 NOT mapped (unconfirmed)",
          ixc_for_port("ge-ts16/4"), IXC_UNKNOWN)
    # The map must contain exactly what the devices reported -- nothing has
    # crept in from memory or a spreadsheet.
    check("ixc map is exactly the 5 device-confirmed ports",
          sorted(IXC_BY_PORT), ["xe-ts13/2", "xe-ts13/3", "xe-ts13/4",
                                "xe-ts16/2", "xe-ts16/3"])
    # media prefix is part of the key -- xe-ts16/4 is NOT ge-ts16/4
    check("ixc xe-ts16/4 unmapped", ixc_for_port("xe-ts16/4"), IXC_UNKNOWN)
    # jfk1's inter-hub port must not acquire a carrier
    check("ixc xe-ts16/1 unmapped", ixc_for_port("xe-ts16/1"), IXC_UNKNOWN)

    # ---- 1250 OSPF corroboration ----
    v, d = resolve_intent({"error": None, "n_legs": 2,
                           "legs": ["ge-ts5/1.0", "ge-ts6/1.0"],
                           "ospf": {"ge-ts5/1.0": {}}}, 1)
    check("2 legs + 1 nbr -> missing, corroborated", v, INTENT_MISSING)
    check("corroboration stated", "CORROBORATED" in d, True)

    v, d = resolve_intent({"error": None, "n_legs": 2,
                           "legs": ["ge-ts5/1.0", "ge-ts6/1.0"],
                           "ospf": None}, 1)
    check("2 legs, OSPF unread -> missing, uncorroborated", v, INTENT_MISSING)
    check("lack of corroboration stated", "NOT corroborated" in d, True)

    # Two adjacencies but only one hub configured -- the hubs and the device
    # disagree, and that must not be flattened into a tidy verdict.
    v, _ = resolve_intent({"error": None, "n_legs": 2,
                           "legs": ["ge-ts5/1.0", "ge-ts6/1.0"],
                           "ospf": {"ge-ts5/1.0": {}, "ge-ts6/1.0": {}}}, 1)
    check("2 legs + 2 nbrs, 1 hub -> contradiction", v, INTENT_CONTRADICTION)

    v, _ = resolve_intent({"error": None, "n_legs": 1, "legs": ["ge-ts5/1.0"],
                           "ospf": {"ge-ts5/1.0": {}}}, 1)
    check("1 leg + 1 nbr -> single", v, INTENT_SINGLE)

    # Hub sees it, device doesn't -- a real disagreement, not a single-home.
    v, _ = resolve_intent({"error": None, "n_legs": 1, "legs": ["ge-ts5/1.0"],
                           "ospf": {}}, 1)
    check("1 leg + 0 nbrs -> contradiction", v, INTENT_CONTRADICTION)

    # OSPF unread must NOT be read as zero neighbours.
    v, _ = resolve_intent({"error": None, "n_legs": 1, "legs": ["ge-ts5/1.0"],
                           "ospf": None}, 1)
    check("1 leg, OSPF unread -> single (not contradiction)", v, INTENT_SINGLE)

    # ---- hub_registry contract ----
    # name_from_hostname() is the PRIMARY authority for a hub's name and is
    # pure string logic (no NetBox refresh), so it is safe to assert here.
    # This exists because the first version of this tool used ip_to_name()'s
    # 3-tuple as if it were a string and died mid-run. A shared module's
    # return shape is exactly the kind of assumption worth pinning.
    check("name_from_hostname('ces01.jfk1')",
          hub_registry.name_from_hostname("ces01.jfk1"), "JFK-1800")
    check("name_from_hostname('ces01.lax1')",
          hub_registry.name_from_hostname("ces01.lax1"), "LAX-1800")
    check("name_from_hostname(junk) is None",
          hub_registry.name_from_hostname("not-a-hub"), None)
    check("name_from_hostname('') is None",
          hub_registry.name_from_hostname(""), None)

    # ---- addressing: N-1 rule and router-ID preference ----
    check("N-1 of 10.111.140.5", peer_ip_from_hub_addr("10.111.140.5"),
          "10.111.140.4")
    check("N-1 of x.x.x.1 refused", peer_ip_from_hub_addr("10.1.1.1"), None)
    check("N-1 of junk refused", peer_ip_from_hub_addr("not-an-ip"), None)

    # router-ID must win over the N-1 derivation when both are available.
    hr = {"JFK": {"hub_name": "ces01.jfk1", "error": None,
                  "ospf": {"xe-ts13/3.240": {"nbr_addr": "10.111.140.13",
                                             "router_id": "10.111.130.6",
                                             "state": "Full", "uptime": "6d"}}}}
    s1 = {"FRDRMDFR": {"hubs": {"ces01.jfk1": ["xe-ts13/3.240"]},
                       "total": 1, "homed_on": ["ces01.jfk1"]}}
    ip, src = address_for_site("FRDRMDFR", s1, hr)
    check("addr prefers router-ID", ip, "10.111.130.6")
    check("addr source labelled", src.startswith("router-ID"), True)

    # No adjacency at all -> no address, and it must NOT fall through to a guess.
    hr2 = {"JFK": {"hub_name": "ces01.jfk1", "error": None, "ospf": {}}}
    ip2, src2 = address_for_site("FRDRMDFR", s1, hr2)
    check("no adjacency -> no address", ip2, None)

    # Intent resolution.
    v, _ = resolve_intent({"error": None, "n_legs": 1,
                           "legs": ["ge-ts5/1.0"]}, 1)
    check("1 leg -> single-homed", v, INTENT_SINGLE)
    v, _ = resolve_intent({"error": None, "n_legs": 2,
                           "legs": ["ge-ts5/1.0", "ge-ts6/1.0"]}, 1)
    check("2 legs, 1 hub -> missing leg", v, INTENT_MISSING)
    v, _ = resolve_intent({"error": "TimeoutError: x", "n_legs": None,
                           "legs": []}, 1)
    check("unreachable -> unknown", v, INTENT_UNKNOWN)
    v, _ = resolve_intent(None, 1)
    check("no address -> unknown", v, INTENT_NO_ADDRESS)

    # Over-homed across three hubs -- legal on each hub alone, illegal overall.
    tri = analyse_homing({
        "ces01.jfk1": [{"kind": KIND_CUSTOMER, "description": "TRIHOMED",
                        "unit": 120, "iface": "xe-ts16/2.120"}],
        "ces01.atl1": [{"kind": KIND_CUSTOMER, "description": "TRIHOMED",
                        "unit": 220, "iface": "xe-ts16/2.220"}],
        "ces01.dfw1": [{"kind": KIND_CUSTOMER, "description": "TRIHOMED",
                        "unit": 320, "iface": "xe-ts16/2.320"}],
    })
    check("over-homed verdict", tri["TRIHOMED"]["verdict"], HOMING_OVER_HOMED)

    if verbose:
        if fails:
            print("\nFAILED ({}):".format(len(fails)))
            for f in fails:
                print("  !! " + f)
        else:
            print("\nAll parser self-tests passed.")
    return (not fails), fails


# ── Excel output ─────────────────────────────────────────────────────────────

_HDR_FONT = None    # built lazily so an openpyxl-less import still works


def _style():
    return {
        "hdr_font": Font(bold=True, color="FFFFFF"),
        "hdr_fill": PatternFill("solid", fgColor="44546A"),
        "bad_fill": PatternFill("solid", fgColor="F8CBAD"),
        "warn_fill": PatternFill("solid", fgColor="FFE699"),
        "orange_fill": PatternFill("solid", fgColor="ED9B33"),
        "orange_light": PatternFill("solid", fgColor="FBD5A5"),
        "yellow_fill": PatternFill("solid", fgColor="FFFF00"),
        "mono": Font(name="Consolas", size=10),
        "bold": Font(bold=True),
    }


def _write_header(ws, headers, st):
    ws.append(headers)
    for c in ws[1]:
        c.font = st["hdr_font"]
        c.fill = st["hdr_fill"]
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def _autosize(ws, max_w=52):
    for col in ws.columns:
        longest = max((len(str(c.value)) for c in col if c.value is not None),
                      default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = \
            min(max_w, max(9, longest + 2))


def _box(ws, row, col_first, col_last, text, fill, bold=True, center=True):
    """One merged, bordered banner cell. Returns the row written."""
    ws.merge_cells(start_row=row, start_column=col_first,
                   end_row=row, end_column=col_last)
    c = ws.cell(row=row, column=col_first)
    c.value = text
    c.font = Font(bold=bold)
    if fill:
        c.fill = fill
    c.alignment = Alignment(horizontal="center" if center else "left",
                            vertical="center", wrap_text=True)
    c.border = Border(*(Side(style="thin"),) * 4)
    return row


def _hostname_for(row, intent):
    """
    The device's real hostname where Phase 2 reached it, else the CLLI+VLAN
    derivation.

    Authority order, same as everywhere else in this suite: the device's own
    prompt beats any derivation. 'pcapps@CLEVOH62320>' is a fact;
    CLLI + VLAN is an inference, and it is demonstrably wrong for some sites.

    Returns (hostname, source) via _hostname_src(); this returns just the name.
    """
    return _hostname_src(row, intent)[0]


def _hostname_src(row, intent):
    """Returns (hostname, 'device'|'derived'|'')."""
    clli = clli_from(row["description"], row["unit"])
    dev = (intent or {}).get(clli)
    if dev and not dev.get("error"):
        h = (dev.get("device_hostname") or "").strip()
        if h:
            return h, "device"
    return (row["expected_1250"] or ""), ("derived" if row["expected_1250"]
                                          else "")


def _cid_for_row(row, ixc, cids, intent, hub_adj=None):
    """
    Circuit ID for one hub sub-interface row, plus a note.

    Uses the 1250's own hub-facing leg to disambiguate where Phase 2 swept
    that site, since the leg's parent IS the NetBox interface holding the
    description. Falls back to carrier matching otherwise.
    """
    host = _hostname_for(row, intent)
    entry = cids.get(host) if cids else None
    by_carrier = entry[0] if entry else (None if cids else None)

    leg = None
    dev = (intent or {}).get(clli_from(row["description"], row["unit"]))
    if dev and not dev.get("error"):
        # Exact pairing: match this hub link to the 1250 leg that faces it.
        leg = leg_facing_hub(dev.get("ospf"), (hub_adj or {}).get("nbr_addr"))
        if not leg:
            legs = dev.get("legs") or []
            if len(legs) == 1:
                leg = legs[0]
    return circuit_id_for(by_carrier, ixc, leg_iface=leg,
                          hub_nbr_addr=(hub_adj or {}).get("nbr_addr"))


def _write_audit_tab(wb, results, st, cids=None, intent=None):
    """
    The front page. First sheet, so the workbook opens to it.

    Layout (per pcapps' mockup 2026-09-18):
      A-H   the circuit rows, filterable
      I-K   CLLI + the two live formula columns
      M-R   explanation boxes and the two summary tables

    NOTE the mockup put the boxes at K with I/J as a gutter. CLLI and the
    per-site counts sit there instead, because every summary figure on this
    sheet is a formula over those columns -- computing them in Python would
    freeze the tables the moment anyone filtered or edited a row, and a stale
    total that still looks authoritative is worse than no total.
    """
    ws = wb.create_sheet("AUDIT", 0)

    headers = ["Hostname", "Hub", "Hub NNI Interface", "NNI Parent", "IXC",
               "EVC Circuit ID", "Link", "OSPF State", "Verdict",
               "CLLI", "Circuits at Site", "Carrier Diversity"]
    _write_header(ws, headers, st)

    hubs, carriers = set(), set()
    for site in sorted(results):
        r = results[site]
        if r["error"]:
            continue
        hubs.add(r["hub_name"])
        ospf = r["ospf"]
        for row in r["rows"]:
            if row["kind"] != KIND_CUSTOMER:
                continue
            ixc = ixc_for_port(row["parent"])
            carriers.add(ixc)
            if ospf is None:
                state, verdict = "", "(no OSPF state)"
            else:
                verdict, _ = evaluate(row, ospf)
                state = (ospf.get(row["iface"]) or {}).get("state", "")
            adj = (ospf.get(row["iface"]) or {}) if ospf else None
            cid, cid_note, cid_ok = _cid_for_row(row, ixc, cids, intent,
                                                 hub_adj=adj)
            host, hsrc = _hostname_src(row, intent)
            ws.append([host, r["hub_name"],
                       row["iface"], row["parent"], ixc, cid, row["link"],
                       state, verdict,
                       clli_from(row["description"], row["unit"]) or ""])
            if hsrc == "derived":
                ws.cell(row=ws.max_row, column=1).fill = st["warn_fill"]
            # Highlight only what genuinely needs a human: several
            # descriptions with no way to choose, or nothing to show.
            if not cid_ok or cid in (CID_NONE, CID_UNKNOWN):
                ws.cell(row=ws.max_row, column=6).fill = st["warn_fill"]

    last = ws.max_row
    if last < 2:
        return ws

    rng_clli = "$J$2:$J${}".format(last)
    rng_ixc  = "$E$2:$E${}".format(last)
    rng_hub  = "$B$2:$B${}".format(last)
    rng_div  = "$L$2:$L${}".format(last)
    rng_cnt  = "$K$2:$K${}".format(last)

    for rw in range(2, last + 1):
        ws.cell(row=rw, column=11).value = \
            "=COUNTIF({},$J{})".format(rng_clli, rw)
        ws.cell(row=rw, column=12).value = (
            '=IF($J{r}="","",IF($K{r}=1,"single-homed",'
            'IF(COUNTIFS({c},$J{r},{i},$E{r})=$K{r},'
            '"SAME CARRIER","diverse")))'
        ).format(r=rw, c=rng_clli, i=rng_ixc)

    ws.auto_filter.ref = "A1:L{}".format(last)
    ws.freeze_panes = "A2"

    # Orange = 3+ sub-interfaces at one site. Conditional, because the count
    # it keys on is itself a formula.
    ws.conditional_formatting.add(
        "A2:L{}".format(last),
        FormulaRule(formula=['$K2>2'], fill=st["orange_fill"],
                    stopIfTrue=False))
    ws.conditional_formatting.add(
        "L2:L{}".format(last),
        FormulaRule(formula=['$L2="SAME CARRIER"'], fill=st["bad_fill"],
                    stopIfTrue=False))

    # ---- right-hand panel ----
    M, R = 14, 19                      # columns N..S
    _box(ws, 2, M, R, "Circuit Audit", None)
    _box(ws, 3, M, R,
         "Uses the equipment itself as the source of truth. Checks both "
         "1800 & 1250.", None, bold=False, center=False)
    _box(ws, 4, M, R,
         "Every configured sub-interface is a circuit we expect up; live "
         "OSPF state says whether it is. The difference is the audit.",
         st["yellow_fill"], bold=True, center=False)

    _box(ws, 7, M, R, "ORANGE HIGHLIGHT = 3x 1800 sub-interfaces configured",
         st["orange_fill"])
    nrow = 8
    for clli in sorted(SITE_NOTES):
        _box(ws, nrow, M, R, SITE_NOTES[clli], st["orange_light"],
             bold=False, center=False)
        nrow += 1

    # ---- table 1: circuits by carrier and hub (all formulas) ----
    hub_list = sorted(hubs)
    t1 = nrow + 2
    _box(ws, t1, M, R, "CIRCUITS BY CARRIER", st["hdr_fill"])
    ws.cell(row=t1, column=M).font = st["hdr_font"]

    hdr = ["IXC", "Total"] + [h.replace("-1800", "") for h in hub_list]
    for i, h in enumerate(hdr):
        c = ws.cell(row=t1 + 1, column=M + i, value=h)
        c.font = st["hdr_font"]
        c.fill = st["hdr_fill"]
        c.alignment = Alignment(horizontal="right" if i else "left")

    for j, ixc in enumerate(sorted(carriers)):
        rw = t1 + 2 + j
        ws.cell(row=rw, column=M, value=ixc)
        ws.cell(row=rw, column=M + 1).value = \
            "=COUNTIF({},{}{})".format(rng_ixc, get_column_letter(M), rw)
        for k, hub in enumerate(hub_list):
            ws.cell(row=rw, column=M + 2 + k).value = \
                '=COUNTIFS({},{}{},{},"{}")'.format(
                    rng_ixc, get_column_letter(M), rw, rng_hub, hub)

    tot = t1 + 2 + len(carriers)
    ws.cell(row=tot, column=M, value="Total").font = st["bold"]
    for k in range(1, 2 + len(hub_list)):
        col = get_column_letter(M + k)
        ws.cell(row=tot, column=M + k).value = "=SUM({0}{1}:{0}{2})".format(
            col, t1 + 2, tot - 1)
        ws.cell(row=tot, column=M + k).font = st["bold"]

    # ---- table 2: same-carrier dual-homes (all formulas) ----
    #
    # Counts SITES, not rows. Each row of a same-carrier site is divided by
    # that site's own circuit count, so a 2-circuit site contributes exactly
    # 1.0 and a 3-circuit one also contributes 1.0. Counting rows and halving
    # would be wrong the moment a site has 3.
    t2 = tot + 3
    _box(ws, t2, M, M + 1, "SAME-CARRIER MULTI-HOMES", st["hdr_fill"])
    ws.cell(row=t2, column=M).font = st["hdr_font"]
    for i, h in enumerate(["Carrier", "Sites"]):
        c = ws.cell(row=t2 + 1, column=M + i, value=h)
        c.font = st["hdr_font"]
        c.fill = st["hdr_fill"]
    for j, ixc in enumerate(sorted(carriers)):
        rw = t2 + 2 + j
        ws.cell(row=rw, column=M, value=ixc)
        ws.cell(row=rw, column=M + 1).value = (
            '=SUMPRODUCT(({d}="SAME CARRIER")*({i}={c}{r})*({n}>1)/'
            'IF({n}=0,1,{n}))'
        ).format(d=rng_div, i=rng_ixc, n=rng_cnt,
                 c=get_column_letter(M), r=rw)

    _box(ws, t2 + 2 + len(carriers) + 1, M, R,
         "A same-carrier site is redundant against a hub failure but NOT "
         "against a carrier failure.", st["yellow_fill"],
         bold=False, center=False)

    for i, w in enumerate([15, 11, 19, 13, 13, 24, 7, 11, 16, 11, 9, 18]):
        ws.column_dimensions[get_column_letter(1 + i)].width = w
    ws.column_dimensions["M"].width = 3
    for i in range(6):
        ws.column_dimensions[get_column_letter(M + i)].width = 16
    return ws


def write_excel(results, sites, warnings, out_path, elapsed_s=None,
                intent=None, cids=None):
    """
    Four sheets: SUMMARY, CIRCUITS, HOMING, GAPS.

    SUMMARY is deliberately first and carries the read status of every hub.
    A reader who opens CIRCUITS first and sees 280 rows has no way to know a
    hub is missing; the summary makes an incomplete run impossible to mistake
    for a complete one.
    """
    st = _style()
    wb = openpyxl.Workbook()
    _write_audit_tab(wb, results, st, cids=cids, intent=intent)

    # Detect unmapped NNI ports BEFORE writing SUMMARY -- a warning computed
    # later would be appended after the sheet that displays warnings has
    # already been written, and would silently never appear.
    warnings = list(warnings or [])
    unmapped = set()
    for r in results.values():
        if r["error"]:
            continue
        for row in r["rows"]:
            if row["kind"] == KIND_CUSTOMER and \
                    ixc_for_port(row["parent"]) == IXC_UNKNOWN:
                unmapped.add(row["parent"])
    if unmapped:
        warnings.append(
            "NNI parent port(s) with no IXC mapping: {} -- carrier shown as "
            "UNKNOWN for circuits on those ports".format(
                ", ".join(sorted(unmapped))))

    # ---- SUMMARY ----
    ws = wb["Sheet"]
    ws.title = "SUMMARY"
    ws.append(["Curveball Circuit Audit -- configured sub-interfaces vs live OSPF state"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Run", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S local")])
    if elapsed_s is not None:
        ws.append(["Elapsed", "{:.0f}s".format(elapsed_s)])
    ws.append(["Source of truth", "device configuration (NetBox deliberately not consulted)"])
    ws.append(["LAB", "excluded"])
    ws.append([])

    ws.append(["Hub", "IP", "Device hostname", "Name from", "Read",
               "Circuits", "Unlabeled", "Parents", "Note"])
    hdr_row = ws.max_row
    for c in ws[hdr_row]:
        c.font = st["hdr_font"]
        c.fill = st["hdr_fill"]

    for site in sorted(results):
        r = results[site]
        note = "; ".join(r["notes"]) if r.get("notes") else ""
        if r["error"]:
            ws.append([r["hub_name"], r["ip"], r.get("device_hostname", ""),
                       r.get("name_source", ""), "UNREAD", "", "", "",
                       "; ".join(x for x in (r["error"], note) if x)])
            for c in ws[ws.max_row]:
                c.fill = st["bad_fill"]
        else:
            n_cust = sum(1 for x in r["rows"] if x["kind"] == KIND_CUSTOMER)
            n_unl  = sum(1 for x in r["rows"] if x["kind"] == KIND_UNLABELED)
            n_par  = sum(1 for x in r["rows"] if x["kind"] == KIND_PARENT)
            ws.append([r["hub_name"], r["ip"], r.get("device_hostname", ""),
                       r.get("name_source", ""), "ok",
                       n_cust, n_unl, n_par, note])
            if note:
                for c in ws[ws.max_row]:
                    c.fill = st["warn_fill"]

    ws.append([])
    ws.append(["Distinct sites (CLLI)", len(sites)])
    ws.append(["Homing violations", len(homing_violations(sites))])
    single = single_appearance_sites(sites)
    ws.append(["Configured on 1 hub", len(single)])
    ws.append(["Configured on 2 hubs",
               sum(1 for e in sites.values() if e["total"] == 2)])
    ws.append([])
    ws.append(["Sites on one hub are AMBIGUOUS from hub config alone -- either "
               "correctly single-homed, or dual-homed with a leg missing. "
               "Resolving that requires reading the 1250's own configuration."])
    ws[ws.max_row][0].font = st["bold"]

    if warnings:
        ws.append([])
        ws.append(["WARNINGS"])
        ws[ws.max_row][0].font = st["bold"]
        for w in warnings:
            ws.append([w])
            ws[ws.max_row][0].fill = st["warn_fill"]
    _autosize(ws)

    # ---- CIRCUITS ----
    ws = wb.create_sheet("CIRCUITS")
    _write_header(ws, ["Hub", "Interface", "Parent", "VLAN", "Kind",
                       "Admin", "Link", "Description", "CLLI",
                       "Expected 1250", "Derivation",
                       "OSPF State", "Nbr Router-ID", "OSPF Uptime",
                       "Verdict", "Detail"], st)
    for site in sorted(results):
        r = results[site]
        if r["error"]:
            continue
        ospf = r["ospf"]
        for row in r["rows"]:
            if ospf is None:
                verdict, detail = "(no OSPF state)", "state read failed"
                adj = {}
            else:
                verdict, detail = evaluate(row, ospf)
                adj = ospf.get(row["iface"]) or {}
            ws.append([
                r["hub_name"], row["iface"], row["parent"], row["unit"],
                row["kind"], row["admin"] or "", row["link"] or "",
                row["description"],
                clli_from(row["description"], row["unit"]) or "",
                row["expected_1250"] or "", row["derivation"],
                adj.get("state", ""), adj.get("router_id", ""),
                adj.get("uptime", ""),
                verdict, detail,
            ])
            if is_gap(verdict):
                for c in ws[ws.max_row]:
                    c.fill = st["bad_fill"]
    ws.auto_filter.ref = ws.dimensions
    _autosize(ws)

    # ---- CIRCUIT LIST (the flat, usable view) ----
    #
    # Columns: A Hostname  B CLLI  C Hub  D Hub NNI Interface  E NNI Parent
    #          F IXC  G Link  H OSPF State  I Verdict
    #          J Circuits at Site  K Carrier Diversity      <- live formulas
    #
    # J and K are Excel formulas rather than values computed in Python, so
    # they stay correct if rows are filtered, sorted, edited or deleted in
    # the workbook. A hardcoded number silently becomes a lie the moment
    # someone changes the sheet -- and a stale number that still looks
    # authoritative is worse than no number.
    #
    # Keyed on CLLI (column B), not Hostname. Two devices at one site derive
    # to different hostnames -- WHPLNYWP132 and WHPLNYWP2132 -- so keying on
    # hostname would count them as separate sites and a same-carrier pair
    # would never be flagged. Same trap the homing rules are built around.
    ws = wb.create_sheet("CIRCUIT LIST")
    _write_header(ws, ["Hostname", "CLLI", "Hub", "Hub NNI Interface",
                       "NNI Parent", "IXC", "EVC Circuit ID", "Link",
                       "OSPF State", "Verdict",
                       "Circuits at Site", "Carrier Diversity"], st)
    for site in sorted(results):
        r = results[site]
        if r["error"]:
            continue
        ospf = r["ospf"]
        for row in r["rows"]:
            if row["kind"] != KIND_CUSTOMER:
                continue
            ixc = ixc_for_port(row["parent"])
            if ospf is None:
                state, verdict = "", "(no OSPF state)"
            else:
                verdict, _ = evaluate(row, ospf)
                state = (ospf.get(row["iface"]) or {}).get("state", "")
            adj = (ospf.get(row["iface"]) or {}) if ospf else None
            cid, cid_note, cid_ok = _cid_for_row(row, ixc, cids, intent,
                                                 hub_adj=adj)
            host, hsrc = _hostname_src(row, intent)
            ws.append([host,
                       clli_from(row["description"], row["unit"]) or "",
                       r["hub_name"], row["iface"], row["parent"], ixc,
                       cid, row["link"], state, verdict])
            if hsrc == "derived":
                ws.cell(row=ws.max_row, column=1).fill = st["warn_fill"]
            if ixc == IXC_UNKNOWN:
                ws.cell(row=ws.max_row, column=6).fill = st["warn_fill"]
            if not cid_ok or cid in (CID_NONE, CID_UNKNOWN):
                ws.cell(row=ws.max_row, column=7).fill = st["warn_fill"]
            if is_gap(verdict):
                for c in ws[ws.max_row]:
                    c.fill = st["bad_fill"]

    last = ws.max_row
    if last > 1:
        rng_clli = "$B$2:$B${}".format(last)
        rng_ixc  = "$F$2:$F${}".format(last)
        for rw in range(2, last + 1):
            # How many circuits this physical site has, anywhere in the fleet.
            ws.cell(row=rw, column=11).value = \
                "=COUNTIF({},$B{})".format(rng_clli, rw)
            # Diversity. A site whose every circuit shares one carrier is
            # redundant against a hub failure but NOT against a carrier
            # failure -- the distinction the topology diagram hides.
            ws.cell(row=rw, column=12).value = (
                '=IF($B{r}="","",'
                'IF($K{r}=1,"single-homed",'
                'IF(COUNTIFS({c},$B{r},{i},$F{r})=$K{r},'
                '"SAME CARRIER","diverse")))'
            ).format(r=rw, c=rng_clli, i=rng_ixc)

        # Conditional formatting, because the values are formulas -- a static
        # fill applied at write time could not know what they evaluate to.
        ws.conditional_formatting.add(
            "L2:L{}".format(last),
            FormulaRule(formula=['$L2="SAME CARRIER"'],
                        fill=st["bad_fill"], stopIfTrue=False))
        ws.conditional_formatting.add(
            "K2:K{}".format(last),
            FormulaRule(formula=['$K2>2'],
                        fill=st["warn_fill"], stopIfTrue=False))

    ws.auto_filter.ref = "A1:L{}".format(max(last, 1))
    _autosize(ws)

    # ---- HOMING ----
    ws = wb.create_sheet("HOMING")
    _write_header(ws, ["CLLI", "Verdict", "Sub-ints", "Hubs", "Homed on",
                       "Interfaces"], st)
    for clli in sorted(sites):
        e = sites[clli]
        ws.append([clli, e["verdict"], e["total"], len(e["hubs"]),
                   ", ".join(e["homed_on"]),
                   " | ".join("{}: {}".format(h, ", ".join(v))
                              for h, v in sorted(e["hubs"].items()))])
        if e["verdict"] != HOMING_OK:
            for c in ws[ws.max_row]:
                c.fill = st["bad_fill"]
    ws.auto_filter.ref = ws.dimensions
    _autosize(ws)

    # ---- INTENT (Phase 2) ----
    if intent:
        ws = wb.create_sheet("INTENT")
        ws.append(["Single-hub sites, resolved against the access device's own "
                   "configuration. UNKNOWN is never read as single-homed."])
        ws["A1"].font = st["bold"]
        ws.append([])
        ws.append(["CLLI", "Hub", "Hub Interface", "Intent", "Legs on device",
                   "Address used", "Address source", "Detail"])
        for c in ws[ws.max_row]:
            c.font = st["hdr_font"]
            c.fill = st["hdr_fill"]
        for clli in sorted(intent):
            dev = intent[clli]
            e = sites.get(clli) or {}
            hub = (e.get("homed_on") or [""])[0]
            ifc = ", ".join((e.get("hubs") or {}).get(hub, []))
            verdict, detail = resolve_intent(dev, e.get("total", 1))
            ws.append([
                clli, hub, ifc, verdict,
                "" if not dev or dev.get("n_legs") is None
                else dev["n_legs"],
                (dev or {}).get("ip", ""),
                (dev or {}).get("addr_source", ""),
                detail,
            ])
            if verdict == INTENT_MISSING:
                for c in ws[ws.max_row]:
                    c.fill = st["bad_fill"]
            elif verdict in (INTENT_UNKNOWN, INTENT_NO_ADDRESS, INTENT_ODD):
                for c in ws[ws.max_row]:
                    c.fill = st["warn_fill"]
        ws.auto_filter.ref = "A3:H{}".format(ws.max_row)
        _autosize(ws)

    # ---- GAPS ----
    ws = wb.create_sheet("GAPS")
    ws.append(["Everything requiring attention. Empty means nothing found -- "
               "check SUMMARY for hub read status before concluding that."])
    ws["A1"].font = st["bold"]
    ws.append([])
    ws.append(["Type", "Hub", "Subject", "Verdict", "Detail"])
    for c in ws[ws.max_row]:
        c.font = st["hdr_font"]
        c.fill = st["hdr_fill"]

    for clli in sorted(intent or {}):
        e = sites.get(clli) or {}
        verdict, detail = resolve_intent(intent[clli], e.get("total", 1))
        if verdict in (INTENT_MISSING, INTENT_ODD):
            ws.append(["REDUNDANCY", (e.get("homed_on") or [""])[0], clli,
                       verdict, detail])

    for clli, e in homing_violations(sites):
        ws.append(["HOMING", ", ".join(e["homed_on"]), clli, e["verdict"],
                   "{} sub-interfaces: {}".format(
                       e["total"],
                       " | ".join("{}: {}".format(h, ", ".join(v))
                                  for h, v in sorted(e["hubs"].items())))])
    for site in sorted(results):
        r = results[site]
        if r["error"]:
            ws.append(["HUB UNREAD", r["hub_name"], r["ip"], "UNREAD",
                       r["error"]])
            continue
        if r["ospf"] is None:
            continue
        for row in r["rows"]:
            verdict, detail = evaluate(row, r["ospf"])
            if is_gap(verdict):
                ws.append(["CIRCUIT", r["hub_name"],
                           "{}  {}".format(row["iface"], row["description"]),
                           verdict, detail])
    _autosize(ws)

    wb.save(out_path)
    return out_path


# ── Tool entry points ────────────────────────────────────────────────────────

def collect_inputs(session=None):
    """Prompt for inputs; return params dict (or None to abort)."""
    session = session or {}
    print()
    print("Circuit Audit -- reads all four production 1800 hubs (LAB excluded).")
    print("Read-only: 'show interfaces description' and 'show ospf neighbor'.")
    print()
    user = session.get("infra_user")
    pw   = session.get("infra_pass")
    if not (user and pw):
        user = input("Infrastructure username: ").strip()
        pw   = getpass.getpass("Infrastructure password: ")
    if not (user and pw):
        print("Missing credentials.")
        return None

    print()
    print("Phase 2 SSHes to every site configured on only ONE hub, to tell a")
    print("correct single-home from a dual-home with a missing leg. That is")
    print("the slow part -- several hundred devices. Phase 1 alone still")
    print("reports the full inventory, dual-homing and any circuit that is")
    print("configured but down.")
    ans = input("Run phase 2 as well? (y/n) [y]: ").strip().lower()
    sweep = ans in ("", "y", "yes")

    return {"username": user, "password": pw, "open_when_done": True,
            "sweep_1250": sweep}


def run(params):
    """Execute the audit given a params dict (no prompting)."""
    if _DEP_ERROR:
        print("[!] {} -- tool unavailable.".format(_DEP_ERROR))
        return None

    user = params["username"]
    pw   = params["password"]
    progress = params.get("progress")

    ok, fails = self_test(verbose=False)
    if not ok:
        # Refuse to run rather than produce a report from a broken parser.
        print("[!] parser self-test FAILED -- refusing to run:")
        for f in fails:
            print("    " + f)
        return None
    print("  parser self-test passed")

    t0 = time.time()
    print("  phase 1: hubs")
    results, sites, warnings = run_phase1(user, pw, progress=progress)

    if not any(not r["error"] for r in results.values()):
        print("[!] every hub was UNREAD -- no report written.")
        for s, r in sorted(results.items()):
            print("    {}: {}".format(s, r["error"]))
        return None

    single = single_appearance_sites(sites)
    intent = None
    if params.get("sweep_1250", True) and sites:
        # Sweep EVERY site, not just the ambiguous ones. Single-hub sites need
        # it to resolve homing; all sites need it for the device's real
        # hostname, which is what NetBox lookups key on.
        derived_for = {}
        for clli, e in sites.items():
            for hub, ifaces in e["hubs"].items():
                res = next((r for r in results.values()
                            if r.get("hub_name") == hub and not r["error"]),
                           None)
                if not res:
                    continue
                for row in res["rows"]:
                    if row["iface"] in ifaces and row["expected_1250"]:
                        derived_for[clli] = row["expected_1250"]
                        break
                if clli in derived_for:
                    break

        print()
        print("  phase 2: {} site(s) ({} single-hub to resolve)".format(
            len(sites), len(single)))
        intent, w2 = run_phase2(sorted(sites), sites, results, user, pw,
                                progress=progress, derived_for=derived_for)
        warnings.extend(w2)
    elif single:
        warnings.append(
            "Phase 2 skipped -- {} single-hub site(s) remain AMBIGUOUS "
            "(correctly single-homed, or dual-homed with a missing leg), and "
            "circuit IDs fall back to the derived hostname"
            .format(len(single)))

    # EVC circuit IDs from NetBox, keyed on the derived 1250 hostname.
    cids = None
    if params.get("circuit_ids", True):
        print()
        # Prefer the hostname the DEVICE reported; fall back to the CLLI+VLAN
        # derivation only where Phase 2 could not reach it.
        hosts = set()
        for r in results.values():
            if r["error"]:
                continue
            for row in r["rows"]:
                if row["kind"] != KIND_CUSTOMER:
                    continue
                h = _hostname_for(row, intent)
                if h:
                    hosts.add(h)
        cids, w3 = fetch_circuit_ids(hosts, progress=progress)
        warnings.extend(w3)

    elapsed = time.time() - t0
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("Circuit_Audit_{}.xlsx".format(stamp))
    write_excel(results, sites, warnings, out_path, elapsed_s=elapsed,
                intent=intent, cids=cids)

    print()
    print("  distinct sites      : {}".format(len(sites)))
    print("  homing violations   : {}".format(len(homing_violations(sites))))
    print("  on one hub          : {}".format(len(single)))
    if intent:
        n_missing = sum(
            1 for c in intent
            if resolve_intent(intent[c], (sites.get(c) or {}).get("total", 1))[0]
            == INTENT_MISSING)
        n_unknown = sum(
            1 for c in intent
            if resolve_intent(intent[c], (sites.get(c) or {}).get("total", 1))[0]
            in (INTENT_UNKNOWN, INTENT_NO_ADDRESS))
        print("    missing 2nd leg   : {}".format(n_missing))
        print("    intent unknown    : {}".format(n_unknown))
    print("  elapsed             : {:.0f}s".format(elapsed))
    print()
    print("  written: {}".format(out_path))

    if params.get("open_when_done") and os.name == "nt":
        try:
            os.startfile(out_path)     # noqa: S606
        except Exception:
            pass
    return out_path


def main():
    if "--self-test" in sys.argv:
        ok, _ = self_test()
        sys.exit(0 if ok else 1)
    p = collect_inputs()
    if p:
        run(p)


if __name__ == "__main__":
    main()
