#!/usr/bin/env python3
"""
card_alarms.py -- fleet-wide equipment/hardware alarm sweep (NPT-1250).

Our monitoring tool cannot see card status, so a failed card (e.g.
`Card-Ctrl-Fail` on a CES card) goes unnoticed until it takes circuits with
it. This sweep polls every NPT-1250 in the fleet, reads its active alarm list,
and reports the EQUIPMENT/HARDWARE alarms -- card, fan, power, MCP, temperature
-- grouped by market, with new-vs-known tracking so a freshly-failed card
stands out from one already known.

Scope: NPT-1250s only (pcapps, 2026-11-03). The 1800s run the same Neptune OS
and the same `show ne alarms`, so extending to them later is a target-list
change, not a parser change -- but they are deliberately out of scope here.

What counts as an equipment alarm
---------------------------------
`show ne alarms` returns rows of: Severity, Timestamp, Alarm Name, Alarm
Entity, Element. Confirmed shapes (Neptune CLI V9.1 guide, and a live 1250
broadcast 2026-11-03):

    critical 2026-01-06,00:45:43  Card-Ctrl-Fail  card   slot=ts3        <- EQUIPMENT
    major    2026-01-06,00:45:43  RxPsnLOPS       ces    interface-name=ds1-ts1/1   <- circuit
    critical 2022-05-13,16:42:56  PPI-LOS         e1-ppi port-name=e1-vs1/1         <- circuit

So the discriminator is the Element: a hardware alarm is pinned to a *slot*
(`slot=...`), a circuit alarm to an *interface/port* (`interface-name=` /
`port-name=`). Classification uses that, plus a known set of hardware entity
names, and -- following the project rule to never silently drop -- anything it
cannot confidently classify is counted and surfaced, not hidden.

⚠ CONFIRM-LIVE: the card / `slot=` shapes are confirmed, but the exact entity
strings for fan / power / MCP / temperature alarms are not yet seen on a live
1250. They are matched by name/entity heuristics below; verify against a real
dump and tighten `_HW_ENTITIES` / `_HW_NAME_HINTS` when one is captured. The
classifier is built so a near-miss shows up as an unclassified alarm (visible)
rather than a dropped one.

1250 discovery
--------------
Router-IDs (loopback IPs = SSH targets) are loaded from
ospf_stability_state.json, exactly like ixc_stability.py. Run OSPF & LDP
Stability at least once first to populate that list.

State persisted to: platform_paths.path("card_alarms_state.json")
  Outer key : OSPF router-ID (= 1250 loopback IP = SSH target)
  Inner     : per-alarm records keyed "<name>|<element>"

Built by P. Capps.
"""

import os
import re
import json
import time
import datetime
import getpass
import threading

import platform_paths
from npt1250_client import NPT1250Client
from device_base import CredentialError
from secure_config import load_config_silent
from jira_client import post_slack
import ospf_stability   # 1250 list source + loopback->hostname map


# ── Constants ──────────────────────────────────────────────────────────────────

OUTPUT_DIR = platform_paths.BASE_DIR
STATE_FILE = os.path.join(OUTPUT_DIR, "card_alarms_state.json")

# Physical-status / alarm reads complete in seconds; 24 workers mirrors the
# other fleet sweeps and keeps total runtime reasonable.
SWEEP_WORKERS = 24

# One read per device. `| no-more` is mandatory on Ribbon/NPT -- without it the
# session paginates and a long alarm list is truncated to its oldest page.
ALARM_CMD = "show ne alarms | no-more"


# ── Tool contract ──────────────────────────────────────────────────────────────

TOOL_INFO = {
    'name':     'Card Alarms (Fleet Equipment Sweep)',
    'ran_from': 'All NPT-1250s (via OSPF Stability state)',
    'desc':     'Fleet-wide hardware alarm sweep. Reads show ne alarms on every '
                'NPT-1250 and reports EQUIPMENT alarms -- card, fan, power, MCP, '
                'temperature -- grouped by market, with new-vs-known tracking so '
                'a newly-failed card stands out from one already known. Fills the '
                'gap left by monitoring that cannot see card status (e.g. '
                'Card-Ctrl-Fail). Requires OSPF & LDP Stability to have run at '
                'least once (uses its state file for the 1250 site list). State '
                'persists between runs; first contact with a device establishes '
                'baseline only. Posts new equipment alarms to Slack when a '
                'webhook is configured.',
    'requires': 'infra creds',
    'order':    11,
}


# ── Alarm parsing ────────────────────────────────────────────────────────────────

# A `show ne alarms` row. Severity is a known keyword; timestamp is a single
# token (YYYY-MM-DD,HH:MM:SS); name and entity are single tokens; element is the
# remainder of the line. Header/separator lines don't start with a severity
# keyword, so they never match.
_ALARM_RE = re.compile(
    r'^\s*(critical|major|minor|warning)\s+'      # 1 severity
    r'(\d{4}-\d{2}-\d{2},\d{2}:\d{2}:\d{2})\s+'   # 2 device timestamp
    r'(\S+)\s+'                                    # 3 alarm name
    r'(\S+)\s+'                                    # 4 alarm entity
    r'(.+?)\s*$',                                  # 5 element (remainder)
    re.IGNORECASE | re.MULTILINE,
)

# Hardware/equipment alarm entities (lowercased). The card case is confirmed;
# the rest are the Neptune hardware entities most likely to appear and are
# CONFIRM-LIVE (see module docstring).
_HW_ENTITIES = {
    "card", "fan", "fan-tray", "fantray", "power", "power-supply", "powersupply",
    "pem", "psu", "mcp", "tmu", "temperature", "chassis", "equipment",
}

# Alarm-name hints that mark a hardware fault even if the entity string is
# unfamiliar (defensive -- see CONFIRM-LIVE note).
_HW_NAME_HINTS = re.compile(
    r'card|fan|power|pem|psu|\bmcp\b|temperature|over-?temp|equipment|hardware',
    re.IGNORECASE,
)

# Element markers that mean a CIRCUIT/facility alarm, not equipment.
_CIRCUIT_ELEMENT = re.compile(r'interface-name=|port-name=', re.IGNORECASE)
# Element marker that pins an alarm to a physical slot = hardware.
_SLOT_ELEMENT = re.compile(r'\bslot=', re.IGNORECASE)


def parse_ne_alarms(raw):
    """
    Parse `show ne alarms` output into a list of dicts:
      {severity, timestamp, name, entity, element}

    severity is lowercased; the rest keep device casing. Returns [] when no
    recognised alarm rows are present (a clean device, or unparseable output --
    the caller distinguishes those by whether the command itself succeeded).
    """
    out = []
    for m in _ALARM_RE.finditer(raw or ""):
        out.append({
            "severity":  m.group(1).lower(),
            "timestamp": m.group(2),
            "name":      m.group(3),
            "entity":    m.group(4),
            "element":   m.group(5).strip(),
        })
    return out


def classify_alarm(a):
    """
    'equipment' | 'circuit' | 'other' for one parsed alarm.

    Rule order, most certain first:
      1. An interface/port element is a circuit alarm, full stop.
      2. A slot element, or a known hardware entity, or a hardware-sounding
         name, is equipment.
      3. Anything else is 'other' -- surfaced in the report as unclassified so
         a classifier miss is visible, never silently dropped.
    """
    entity  = (a.get("entity") or "").lower()
    element = a.get("element") or ""
    name    = a.get("name") or ""
    if _CIRCUIT_ELEMENT.search(element):
        return "circuit"
    if _SLOT_ELEMENT.search(element) or entity in _HW_ENTITIES \
            or _HW_NAME_HINTS.search(name):
        return "equipment"
    return "other"


def alarm_key(a):
    """Stable identity for one alarm instance: name + element (the element
    carries the slot, so two cards on one device stay distinct)."""
    return "{}|{}".format(a.get("name", "?"), a.get("element", "?"))


# Equipment sub-categories, so a run can be filtered to just the hardware the
# operator cares about (e.g. card alarms only) without re-sweeping. "other"
# catches equipment alarms that don't fall into the named buckets (chassis, TMU,
# or an unfamiliar hardware entity) -- it's a real bucket, not a dumping ground,
# so filtering it out never hides a card/fan/power/mcp/temp alarm.
CATEGORIES = ("card", "fan", "power", "mcp", "temperature", "other")

_CAT_ENTITY = {
    "card": {"card"},
    "fan": {"fan", "fan-tray", "fantray"},
    "power": {"power", "power-supply", "powersupply", "pem", "psu"},
    "mcp": {"mcp"},
    "temperature": {"temperature"},
}
_CAT_NAME = {
    "card": re.compile(r'card', re.IGNORECASE),
    "fan": re.compile(r'fan', re.IGNORECASE),
    "power": re.compile(r'power|pem|psu', re.IGNORECASE),
    "mcp": re.compile(r'\bmcp\b', re.IGNORECASE),
    "temperature": re.compile(r'temperature|over-?temp', re.IGNORECASE),
}


def equipment_category(a):
    """
    Sub-category for an alarm already classified as equipment: one of
    card / fan / power / mcp / temperature / other. Entity is the stronger
    signal; the alarm name is the fallback. Order matters only for the name
    fallback -- entity matches are unambiguous.
    """
    entity = (a.get("entity") or "").lower()
    for cat, ents in _CAT_ENTITY.items():
        if entity in ents:
            return cat
    name = a.get("name") or ""
    for cat in ("card", "fan", "power", "mcp", "temperature"):
        if _CAT_NAME[cat].search(name):
            return cat
    return "other"


# ── Market grouping ──────────────────────────────────────────────────────────────

_CLLI_RE = re.compile(r'^[A-Z]{4}[A-Z0-9]{2}[A-Z0-9]{2}', re.IGNORECASE)


def market_of(hostname):
    """
    Market key for grouping. A CLLI hostname (e.g. TOLDOH21326) groups on its
    first 8 chars (place+state+building, e.g. TOLDOH21) so multiple 1250s at one
    site report together. A non-CLLI hostname groups on itself.
    """
    h = (hostname or "").strip()
    return h[:8].upper() if _CLLI_RE.match(h) else (h or "?")


# ── State I/O ──────────────────────────────────────────────────────────────────

def load_state():
    """Load state from disk. {} on missing or corrupt."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    """Atomically write state (write-then-rename)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ── Per-device sweep ───────────────────────────────────────────────────────────

def _sweep_site(rid, hostname, username, password, state, lock, stats, progress):
    """
    Connect to one NPT-1250, read its alarms, record EQUIPMENT alarms and detect
    new/cleared since the last run.

    Updates state[rid] under lock with:
      hostname, market
      alarms        {alarm_key: {severity,name,entity,element,device_ts,since,baseline}}
      new_alarms    equipment alarms genuinely new this run (not first-contact)
      cleared_alarms equipment alarms present last run, gone now
      other_count   non-equipment alarms seen (circuit + unclassified) -- surfaced
      sweep_status  ok | unreachable | error
      last_sweep    UTC ISO
    """
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    c = NPT1250Client(rid, username, password)
    try:
        c.connect()
    except CredentialError:
        raise
    except Exception as e:
        with lock:
            site = state.setdefault(rid, {})
            site["sweep_status"] = "unreachable"
            site["sweep_error"]  = str(e)[:120]
            site["hostname"]     = hostname
            site["market"]       = market_of(hostname)
            stats["unreachable"] += 1
        progress("  [!] {} ({}) unreachable: {}".format(hostname, rid, str(e)[:60]))
        return

    # Prefer the device's OWN hostname (captured from its prompt on connect) over
    # the loopback->hostname map, which is often unresolved and falls back to the
    # IP. Device's own name wins -- the suite's authority order. Captured now,
    # before the session is closed below.
    dev = (getattr(c, "hostname", "") or "").strip()
    disp_host = dev if dev and dev.lower() != "device" else hostname

    try:
        raw = c.run(ALARM_CMD, timeout=30)
    except Exception as e:
        with lock:
            site = state.setdefault(rid, {})
            site["sweep_status"] = "error"
            site["sweep_error"]  = str(e)[:120]
            site["hostname"]     = disp_host
            site["market"]       = market_of(disp_host)
            stats["errors"] += 1
        progress("  [!] {} ({}) alarm read error: {}".format(disp_host, rid, str(e)[:60]))
        return
    finally:
        try:
            c.close()
        except Exception:
            pass

    alarms = parse_ne_alarms(raw)
    equip  = {}          # alarm_key -> parsed alarm (equipment only)
    other  = 0
    for a in alarms:
        kind = classify_alarm(a)
        if kind == "equipment":
            equip[alarm_key(a)] = a
        else:
            other += 1

    with lock:
        prior_site   = state.get(rid, {})
        prior_alarms = prior_site.get("alarms")
        # First contact = we have never recorded this device's alarms before.
        # Everything it shows now is baseline (we can't call any of it "new").
        first_contact = prior_alarms is None
        prior_alarms  = prior_alarms or {}

        new_alarms     = []
        cleared_alarms = []
        rec            = {}

        for key, a in equip.items():
            cat = equipment_category(a)
            if key in prior_alarms:
                prev = prior_alarms[key]
                rec[key] = {
                    "severity":  a["severity"],
                    "name":      a["name"],
                    "entity":    a["entity"],
                    "element":   a["element"],
                    "category":  cat,
                    "device_ts": a["timestamp"],
                    "since":     prev.get("since", now_utc),
                    "baseline":  prev.get("baseline", False),
                }
            else:
                baseline = first_contact
                rec[key] = {
                    "severity":  a["severity"],
                    "name":      a["name"],
                    "entity":    a["entity"],
                    "element":   a["element"],
                    "category":  cat,
                    "device_ts": a["timestamp"],
                    "since":     now_utc,
                    "baseline":  baseline,
                }
                if not baseline:
                    new_alarms.append(rec[key])

        # Cleared: equipment alarms present last run, absent now.
        for key, prev in prior_alarms.items():
            if key not in equip:
                cleared_alarms.append(prev)

        state[rid] = {
            "hostname":       disp_host,
            "market":         market_of(disp_host),
            "alarms":         rec,
            "new_alarms":     new_alarms,
            "cleared_alarms": cleared_alarms,
            "other_count":    other,
            "sweep_status":   "ok",
            "last_sweep":     now_utc,
        }

        stats["ok"] += 1
        stats["equipment"] += len(equip)
        stats["new"] += len(new_alarms)
        stats["cleared"] += len(cleared_alarms)
        if new_alarms:
            stats["sites_with_new"] += 1

        flag = "[!!]" if new_alarms else ("[ !]" if equip else "[ OK]")
        extra = ""
        if equip:
            extra = "  {} equipment alarm(s)".format(len(equip))
        if new_alarms:
            extra += "  +{} NEW".format(len(new_alarms))
        if first_contact and equip:
            extra += "  (baseline)"
        progress("  {} {} ({}){}".format(flag, disp_host, rid, extra))


# ── Report building ────────────────────────────────────────────────────────────

def _sev_rank(sev):
    return {"critical": 0, "major": 1, "minor": 2, "warning": 3}.get(
        (sev or "").lower(), 4)


def _fmt_alarm(a, indent="      "):
    return "{}{:<9} {:<16} {:<8} {}  [{}]".format(
        indent, a.get("severity", "?"), a.get("name", "?"),
        a.get("entity", "?"), a.get("element", "?"), a.get("device_ts", "?"))


def _cats_set(categories):
    """Normalise the category filter to a set; None/empty means all."""
    if not categories:
        return set(CATEGORIES)
    return {c for c in categories if c in CATEGORIES} or set(CATEGORIES)


def _in_cats(a, cats):
    """Is this alarm record in the active category filter? Records from before
    categories existed (no 'category' key) are treated as 'other' so they still
    show under an all-categories view."""
    return a.get("category", "other") in cats


def build_report(state, stamp, elapsed, categories=None):
    """
    Full text report, grouped by market.

    categories: which equipment sub-categories to SHOW (card/fan/power/mcp/
    temperature/other). None or empty = all. The sweep always tracks every
    equipment alarm; this only filters the view, so new-vs-known stays stable
    regardless of what filter the report is run with.

    Sections:
      1. NEW equipment alarms this run
      2. All current equipment alarms, by market
      3. Cleared equipment alarms this run
      4. Sites not reached
      5. Summary (incl. non-equipment alarm count, so nothing is hidden)
    """
    cats  = _cats_set(categories)
    lines = []
    sep   = "=" * 72
    lines += ["Card / Equipment Alarms (NPT-1250 fleet)  --  {}".format(stamp),
              sep]
    if cats != set(CATEGORIES):
        lines.append("Filter: {} alarms only".format(", ".join(sorted(cats))))
    lines.append("")

    ok_sites  = [(r, s) for r, s in state.items() if s.get("sweep_status") == "ok"]
    unr_sites = [(r, s) for r, s in state.items() if s.get("sweep_status") == "unreachable"]
    err_sites = [(r, s) for r, s in state.items() if s.get("sweep_status") == "error"]

    def _hn(s, rid):
        return s.get("hostname", rid)

    # ── 1. NEW this run ──────────────────────────────────────────────────────
    new_sites = [(r, s) for r, s in ok_sites
                 if [a for a in s.get("new_alarms", []) if _in_cats(a, cats)]]
    if new_sites:
        lines += ["*** NEW EQUIPMENT ALARMS THIS RUN ***", ""]
        for rid, s in sorted(new_sites, key=lambda x: (x[1].get("market", ""), _hn(x[1], x[0]))):
            shown = [a for a in s["new_alarms"] if _in_cats(a, cats)]
            lines.append("  {}  [{}]  {}".format(_hn(s, rid), s.get("market", "?"), rid))
            for a in sorted(shown, key=lambda a: _sev_rank(a.get("severity"))):
                lines.append(_fmt_alarm(a))
            lines.append("")
    else:
        lines += ["No new equipment alarms this run.", ""]

    # ── 2. Current equipment alarms, by market ──────────────────────────────
    lines += ["", "--- CURRENT EQUIPMENT ALARMS (by market) ---", ""]
    by_market = {}
    for rid, s in ok_sites:
        if any(_in_cats(v, cats) for v in s.get("alarms", {}).values()):
            by_market.setdefault(s.get("market", "?"), []).append((rid, s))
    if by_market:
        for market in sorted(by_market):
            lines.append("{}".format(market))
            for rid, s in sorted(by_market[market], key=lambda x: _hn(x[1], x[0])):
                al = {k: v for k, v in s.get("alarms", {}).items() if _in_cats(v, cats)}
                tag = "  (baseline)" if al and all(v.get("baseline") for v in al.values()) else ""
                lines.append("  {}  {}  --  {} alarm(s){}".format(
                    _hn(s, rid), rid, len(al), tag))
                for key in sorted(al, key=lambda k: _sev_rank(al[k].get("severity"))):
                    a = al[key]
                    bl = "  (baseline)" if a.get("baseline") else "  since {}".format(a.get("since", "?"))
                    lines.append(_fmt_alarm(a) + bl)
            lines.append("")
    else:
        lines += ["No matching equipment alarms anywhere in the fleet.", ""]

    # ── 3. Cleared this run ─────────────────────────────────────────────────
    cleared_sites = [(r, s) for r, s in ok_sites
                     if [a for a in s.get("cleared_alarms", []) if _in_cats(a, cats)]]
    if cleared_sites:
        lines += ["", "--- CLEARED SINCE LAST RUN ---", ""]
        for rid, s in sorted(cleared_sites, key=lambda x: _hn(x[1], x[0])):
            shown = [a for a in s["cleared_alarms"] if _in_cats(a, cats)]
            lines.append("  {}  [{}]  {}".format(_hn(s, rid), s.get("market", "?"), rid))
            for a in shown:
                lines.append(_fmt_alarm(a))
            lines.append("")

    # ── 4. Not reached ──────────────────────────────────────────────────────
    if unr_sites or err_sites:
        lines += ["", "--- SITES NOT REACHED ---", ""]
        for rid, s in sorted(unr_sites + err_sites, key=lambda x: _hn(x[1], x[0])):
            lines.append("  {:30s}  {}  [{}]  {}".format(
                _hn(s, rid), rid, s.get("sweep_status", "?").upper(),
                s.get("sweep_error", "")[:60]))
        lines.append("")

    # ── 5. Summary ──────────────────────────────────────────────────────────
    # Shown counts respect the filter; totals (all equipment) are given too so a
    # narrow filter never hides how much is really out there.
    def _n_shown(site, field):
        return sum(1 for a in site.get(field, []) if _in_cats(a, cats))

    shown_equip = sum(sum(1 for v in s.get("alarms", {}).values() if _in_cats(v, cats))
                      for _, s in ok_sites)
    shown_new   = sum(_n_shown(s, "new_alarms") for _, s in ok_sites)
    shown_clr   = sum(_n_shown(s, "cleared_alarms") for _, s in ok_sites)
    total_equip = sum(len(s.get("alarms", {})) for _, s in ok_sites)
    total_other = sum(s.get("other_count", 0) for _, s in ok_sites)
    filt = cats != set(CATEGORIES)
    suffix = "  (of {} total equipment)".format(total_equip) if filt else ""

    # Per-alarm-type tally across the fleet (respects the active filter), so a
    # glance at the summary says WHAT is alarming and how much, not just a total.
    # Keyed by alarm name; each carries its category. Sorted most-common first.
    type_counts = {}   # name -> [count, category]
    for _, s in ok_sites:
        for v in s.get("alarms", {}).values():
            if _in_cats(v, cats):
                rowname = v.get("name", "?")
                slot = type_counts.setdefault(rowname, [0, v.get("category", "other")])
                slot[0] += 1

    lines += [
        "", "--- SUMMARY ---", "",
        "Sites reachable:              {}".format(len(ok_sites)),
        "Sites unreachable:            {}".format(len(unr_sites)),
        "Sites error:                  {}".format(len(err_sites)),
        "Equipment alarms shown:       {}{}".format(shown_equip, suffix),
    ]
    if type_counts:
        lines.append("  By alarm type:")
        for rowname, (cnt, cat) in sorted(
                type_counts.items(), key=lambda kv: (-kv[1][0], kv[0])):
            lines.append("    {:<24} {:<13} {}".format(
                rowname, "({})".format(cat), cnt))
    lines += [
        "New equipment alarms shown:   {}".format(shown_new),
        "Cleared this run (shown):     {}".format(shown_clr),
        "Non-equipment alarms seen:    {}  (circuit/facility + unclassified; "
        "not tracked here)".format(total_other),
        "Elapsed:                      {:.0f}s".format(elapsed),
        "",
    ]
    if filt:
        lines.append("Filter active: {}. Re-run with all boxes checked to see "
                     "everything.".format(", ".join(sorted(cats))))
        lines.append("")
    return "\n".join(lines)


# ── Slack alert ────────────────────────────────────────────────────────────────

def _build_slack_alert(new_sites, stamp, cats):
    """Slack message for runs with new equipment alarms in the active filter.
    new_sites is [(rid, site_state), ...]; only alarms in `cats` are reported."""
    lines = ["*Card/Equipment Alarm Alert* -- {}".format(stamp)]
    if cats != set(CATEGORIES):
        lines.append("_filter: {}_".format(", ".join(sorted(cats))))
    lines.append("")
    for rid, s in sorted(new_sites, key=lambda x: (x[1].get("market", ""), x[1].get("hostname", x[0]))):
        shown = [a for a in s["new_alarms"] if _in_cats(a, cats)]
        if not shown:
            continue
        names = ", ".join(sorted({a.get("name", "?") for a in shown}))
        lines.append("• *{}* [{}] -- {} new equipment alarm(s): {}".format(
            s.get("hostname", rid), s.get("market", "?"), len(shown), names))
    return "\n".join(lines)


# ── Main entry point ───────────────────────────────────────────────────────────

def collect_inputs(session=None):
    """CLI input collection. Not called from the web layer."""
    username       = input("Infrastructure username: ").strip()
    password       = getpass.getpass("Infrastructure password: ")
    raw_cats       = input("Categories to show [blank=all; e.g. 'card fan']: ").strip().lower()
    categories     = raw_cats.replace(",", " ").split() if raw_cats else None
    open_when_done = input("Open report when done? (y/n): ").strip().lower() == "y"
    return {"username": username, "password": password,
            "categories": categories, "open_when_done": open_when_done}


def run(params, progress=print):
    """
    One fleet equipment-alarm sweep.

    params: username, password, open_when_done, [slack_alert=True],
            [categories=None] -- which equipment sub-categories to SHOW
            (card/fan/power/mcp/temperature/other); None/empty = all. The sweep
            always collects every equipment alarm; categories only filter the
            report and the Slack alert.
    Returns out_path or None on total failure.
    """
    username       = params["username"]
    password       = params["password"]
    open_when_done = params.get("open_when_done", False)
    slack_alert    = params.get("slack_alert", True)
    categories     = params.get("categories")
    cats           = _cats_set(categories)

    stamp   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    t_start = time.time()

    # 1. Discover 1250s from OSPF Stability state (same source as ixc_stability).
    progress("[Sites] Loading 1250 list from OSPF Stability state...")
    ospf_state_path = os.path.join(OUTPUT_DIR, "ospf_stability_state.json")
    if not os.path.exists(ospf_state_path):
        progress("[!] ospf_stability_state.json not found.")
        progress("    Run 'OSPF & LDP Stability' at least once to populate the site list.")
        return None
    try:
        with open(ospf_state_path, "r", encoding="utf-8") as f:
            ospf_state = json.load(f)
    except Exception as e:
        progress("[!] Could not read OSPF Stability state: {}".format(e))
        return None

    rids = sorted(k for k in ospf_state if not k.startswith("_"))
    if not rids:
        progress("[!] OSPF Stability state is empty. Run 'OSPF & LDP Stability' first.")
        return None
    progress("  {} site(s) found.".format(len(rids)))

    # 2. loopback -> hostname map.
    progress("[Hostnames] Building loopback->hostname map...")
    lb_map = ospf_stability.build_loopback_hostname_map()
    progress("  {} mapping(s) found.".format(len(lb_map)))

    # 3. Load prior state.
    progress("[State] Loading card-alarm state...")
    state = load_state()
    progress("  {} site(s) in state file.".format(len(state)))

    # 4. Fleet sweep.
    progress("\n[Sweep] {} site(s) at {} concurrent workers...".format(
        len(rids), SWEEP_WORKERS))
    stats = {"ok": 0, "unreachable": 0, "errors": 0,
             "equipment": 0, "new": 0, "cleared": 0, "sites_with_new": 0}
    lock = threading.Lock()
    cred_error = threading.Event()

    def _worker(rid):
        if cred_error.is_set():
            return
        hostname = lb_map.get(rid, rid)
        try:
            _sweep_site(rid, hostname, username, password, state, lock, stats, progress)
        except CredentialError:
            cred_error.set()
            progress("[!!] Credential error -- halting sweep. Check password.")

    sem = threading.Semaphore(SWEEP_WORKERS)

    def _bounded(rid):
        with sem:
            _worker(rid)

    threads = [threading.Thread(target=_bounded, args=(rid,), daemon=True) for rid in rids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if cred_error.is_set():
        progress("[!] Sweep aborted after credential error.")
        return None

    progress("\n[Sweep] Done -- {} ok  {} unreachable  {} error(s)  "
             "{} equipment alarm(s)  {} NEW across {} site(s).".format(
                 stats["ok"], stats["unreachable"], stats["errors"],
                 stats["equipment"], stats["new"], stats["sites_with_new"]))

    # 5. Save state.
    save_state(state)

    # 6. Report.
    elapsed  = time.time() - t_start
    report   = build_report(state, stamp, elapsed, categories=categories)
    ts_file  = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out_path = os.path.join(OUTPUT_DIR, "card_alarms_{}.txt".format(ts_file))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    progress("\n[Done] Report: {}".format(out_path))

    # 7. Slack alert -- only when something is genuinely new (matches
    # ospf_stability: a monitor that cries every run gets muted).
    if slack_alert:
        # Only alert on new alarms that fall within the active category filter --
        # the Slack message should match what the operator asked to watch.
        new_sites = [(r, s) for r, s in state.items()
                     if s.get("sweep_status") == "ok"
                     and any(_in_cats(a, cats) for a in s.get("new_alarms", []))]
        if new_sites:
            try:
                webhook = load_config_silent().get("SLACK_WEBHOOK", "")
            except Exception:
                webhook = ""
            if webhook:
                ok, msg = post_slack(webhook, _build_slack_alert(new_sites, stamp, cats))
                progress("[Slack] {}".format(msg if ok else "FAILED: " + msg))
            else:
                progress("[Slack] No webhook configured -- skipping alert.")
        else:
            progress("[Slack] No new equipment alarms this run -- no alert sent.")

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception:
            pass

    return out_path


# ── Self-test (off-box, no credentials) ─────────────────────────────────────────

def self_test(verbose=True):
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    sample = (
        "Severity    Timestamp              Alarm Name       Alarm Entity   Element\n"
        "critical    2026-01-06,00:45:43    Card-Ctrl-Fail   card           slot=ts3\n"
        "major       2026-01-06,00:45:43    RxPsnLOPS        ces            interface-name=ds1-ts1/1\n"
        "critical    2022-05-13,16:42:56    PPI-LOS          e1-ppi         port-name=e1-vs1/1\n"
        "minor       2026-11-03,17:39:45    Fan-Fail         fan            slot=fan1\n"
    )
    parsed = parse_ne_alarms(sample)
    chk("all four alarm rows parsed", len(parsed), 4)
    chk("card alarm name", parsed[0]["name"], "Card-Ctrl-Fail")
    chk("card element", parsed[0]["element"], "slot=ts3")

    kinds = [classify_alarm(a) for a in parsed]
    chk("card -> equipment", kinds[0], "equipment")
    chk("RxPsnLOPS (interface-name) -> circuit", kinds[1], "circuit")
    chk("PPI-LOS (port-name) -> circuit", kinds[2], "circuit")
    chk("Fan-Fail (slot + fan entity) -> equipment", kinds[3], "equipment")

    # An unfamiliar hardware name with no slot still classifies via name hint,
    # and a genuinely unknown line falls to 'other' (surfaced, not dropped).
    chk("power alarm by name hint",
        classify_alarm({"name": "Power-Supply-Fail", "entity": "pem", "element": ""}),
        "equipment")
    chk("unknown -> other",
        classify_alarm({"name": "Frobnicate", "entity": "mystery", "element": "zone=3"}),
        "other")

    # alarm_key distinguishes two cards on one device.
    chk("alarm_key distinguishes slots",
        alarm_key(parsed[0]) != alarm_key({"name": "Card-Ctrl-Fail", "element": "slot=ts4"}),
        True)

    # equipment sub-categories.
    chk("card category", equipment_category(parsed[0]), "card")
    chk("fan category", equipment_category(parsed[3]), "fan")
    chk("power by entity pem",
        equipment_category({"name": "X", "entity": "pem", "element": ""}), "power")
    chk("mcp by name",
        equipment_category({"name": "MCP-Switchover", "entity": "?", "element": ""}), "mcp")
    chk("temperature by name",
        equipment_category({"name": "Over-Temp", "entity": "?", "element": ""}), "temperature")
    chk("unknown hw -> other category",
        equipment_category({"name": "Chassis-X", "entity": "chassis", "element": "slot=1"}), "other")

    # filter view: 'card' only hides the fan alarm but keeps the card alarm.
    chk("filter set normalises blank to all", _cats_set(None), set(CATEGORIES))
    chk("filter set drops junk, keeps valid", _cats_set(["card", "bogus"]), {"card"})
    _card_rec = {"category": "card"}
    _fan_rec  = {"category": "fan"}
    chk("card rec in card filter", _in_cats(_card_rec, {"card"}), True)
    chk("fan rec not in card filter", _in_cats(_fan_rec, {"card"}), False)
    chk("legacy rec (no category) treated as other",
        _in_cats({"name": "x"}, {"other"}), True)

    # market grouping.
    chk("CLLI market stem", market_of("TOLDOH21326"), "TOLDOH21")
    chk("non-CLLI hostname kept", market_of("lab-box"), "lab-box")

    # clean device parses to nothing.
    chk("clean device -> no alarms", parse_ne_alarms("No alarms.\n"), [])

    print()
    if fails:
        print("FAILED:")
        for f in fails:
            print("  " + f)
        return False
    print("All card_alarms self-tests passed.")
    return True


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    run(collect_inputs())
