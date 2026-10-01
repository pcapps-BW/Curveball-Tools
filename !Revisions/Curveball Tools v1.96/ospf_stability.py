#!/usr/bin/env python3
"""
ospf_stability.py -- OSPF adjacency stability monitor for NPT-1800 hubs.

Polls every NPT-1800 hub found in NetBox (filtered by HUB_NAME_FILTER from
autotester_config.txt), runs 'show ospf neighbor detail' on each, and tracks
OSPF session stability per 1250 site over time.

Drop detection (two independent mechanisms -- catches both slow and fast bounces):
  1. Uptime reset: current uptime < stored uptime - POLL_TOLERANCE_S
     (session restarted between polls)
  2. Transitions delta: Num State Transitions jumped by > 1 since last poll
     (fast bounce within a single poll cycle that uptime alone would miss)

State persisted to:  platform_paths.path("ospf_stability_state.json")
  (C:\Claude\ospf_stability_state.json on Windows, ~/.claude/... on Linux)
  Keyed by OSPF router-ID (= 1250 loopback IP). Router-ID is the stable
  identifier -- it survives interface IP renumbering and is always present
  in 'show ospf neighbor detail' output regardless of OSPF version.

Hostname resolution: loopback-IP -> hostname map built at startup by scanning
existing 1250 discovery .txt files in OUTPUT_DIR. Sites not yet discovered
show their loopback IP until a discovery run is performed.

TOOL_INFO contract: works as a Curveball web tool (run via Flask job machinery)
and as a standalone CLI script. No JIRA ticket per run -- this is a monitoring
tool, not a per-circuit investigation.

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
from netbox_client import NetboxClient, load_netbox_config
from npt1800_client import NPT1800Client
from npt1250_client import NPT1250Client
from device_base import CredentialError
from jira_client import post_slack
from secure_config import load_config, load_config_silent
from output_builders import check_down_history

# ── Constants ─────────────────────────────────────────────────────────────────
OUTPUT_DIR       = platform_paths.BASE_DIR
STATE_FILE       = os.path.join(OUTPUT_DIR, "ospf_stability_state.json")
CONFIG_PATH      = platform_paths.path("autotester_config.txt")

# Uptime tolerance: if new_uptime < stored_uptime - this value, treat as reset.
# Accounts for clock skew and the fact that uptime ticks forward between polls.
# Set to 2x the expected poll interval (default 10 min -> 1200 s).
POLL_TOLERANCE_S = 1200

# Widest reporting window. Doubles as the pre-filter threshold: a site whose
# every adjacency leg has been up longer than this provably had no drop inside
# the window, so its log does not need reading.
SWEEP_WINDOW_DAYS = 30

# Concurrent SSH sessions during the 1250 event-log sweep.
#
# Measured on a 183-site baseline run: ~110 s per device, NOT the ~10 s first
# assumed. The cost is the device scanning its whole event log to service the
# grep, not the SSH handshake. At 8 workers that is only ~4 logins/minute, so
# the auth-load concern that motivated a low number does not apply -- auth cares
# about login RATE, and long-lived sessions keep the rate low.
#
# 24 workers is still only ~13 logins/minute and cuts a full baseline from
# roughly 45 min to under 15.
SWEEP_WORKERS = 24

# Two adjacency legs whose uptimes agree within this many seconds came up in the
# same event -- so they went DOWN in the same event, which points at the site's
# transport rather than either hub. Hub polls are staggered by a fraction of a
# second and uptime is reported to the second, so a couple of minutes is ample
# slack without merging genuinely independent bounces.
LEG_SYNC_TOLERANCE_S = 120

# Write the state file every N devices during the sweep, so a crash, a dropped
# link, or a sleeping laptop costs at most this many devices instead of the
# entire run. Without it, nothing reached disk until the sweep finished -- a
# 45-minute window in which any interruption meant starting over.
CHECKPOINT_EVERY = 24

# Max parallel SSH threads when polling multiple hubs.
MAX_WORKERS      = 8

# Uptime format from device: "20d:06h:41m:27s" or "0d:02h:05m:10s"
_UPTIME_RE = re.compile(
    r'(\d+)d:(\d+)h:(\d+)m:(\d+)s', re.IGNORECASE)

# Neighbor block separator: blank line or next "Address" header line
_NEIGHBOR_BLOCK_RE = re.compile(
    r'Address\s+Interface\s+state\s+ID\s+Pri\s+Dead\s+UpTime', re.IGNORECASE)


# ── Launcher contract ─────────────────────────────────────────────────────────
TOOL_INFO = {
    'name':     'OSPF Stability',
    'ran_from': 'NPT-1800 hubs (via NetBox)',
    'desc':     'Polls all NPT-1800 hubs from NetBox and tracks OSPF adjacency '
                'stability per 1250 site -- uptime, drop count (24 h / 72 h / '
                '7 d / 30 d), and last-drop timestamp. Alerts to Slack on new '
                'drops. State persists between runs.',
    'requires': 'infra creds + NetBox',
    'order':    8,
}


# ── Uptime parsing ────────────────────────────────────────────────────────────
def _parse_uptime_s(uptime_str):
    """
    Parse device uptime string to total seconds (int).

    Handles multiple formats emitted by different NPT-1800 software versions:
      '20d:06h:41m:27s'  -- full form (most common)
      '06h:41m:27s'      -- hours only, no days field
      '41m:27s'          -- minutes only (very short uptime)
    Returns None if no format matches (caller falls back to raw string display).
    """
    raw = uptime_str or ""
    # Full form: Xd:Xh:Xm:Xs
    m = _UPTIME_RE.search(raw)
    if m:
        d, h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return d * 86400 + h * 3600 + mi * 60 + s
    # Hours form: Xh:Xm:Xs (no days)
    hm = re.search(r'(\d+)h:(\d+)m:(\d+)s', raw, re.IGNORECASE)
    if hm:
        return int(hm.group(1)) * 3600 + int(hm.group(2)) * 60 + int(hm.group(3))
    # Minutes form: Xm:Xs
    mm = re.search(r'(\d+)m:(\d+)s', raw, re.IGNORECASE)
    if mm:
        return int(mm.group(1)) * 60 + int(mm.group(2))
    return None


def _fmt_uptime(seconds):
    """Format total seconds back to human 'Xd Xh Xm'."""
    if seconds is None:
        return "unknown"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return "{}d {}h {}m".format(d, h, m)


def _fmt_uptime_fixed(seconds, raw_str=""):
    """
    Fixed-width 12-char uptime for column-aligned text output: ' 20d 06h 41m'.
    Days right-aligned in 3 chars; hours and minutes zero-padded.
    If seconds is None (parse miss), falls back to the raw uptime string
    truncated to 12 chars rather than the misleading word 'unknown'.
    """
    if seconds is None:
        fallback = (raw_str or "?").strip()
        return "{:<12}".format(fallback[:12])
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return "{:3d}d {:02d}h {:02d}m".format(d, h, m)


# ── Neighbor parsing ──────────────────────────────────────────────────────────
def parse_ospf_neighbors(raw):
    """
    Parse 'show ospf neighbor detail' output from an NPT-1800.

    Returns list of dicts:
      {
        "neighbor_ip":   "10.x.x.x",    # Address column (1250 interface IP)
        "interface":     "xe-ts16/1.10",
        "state":         "Full",
        "router_id":     "10.y.y.y",    # ID column = 1250 loopback = stable key
        "uptime_str":    "20d:06h:41m:27s",
        "uptime_s":      1746087,
        "transitions":   5,             # Num State Transitions
      }
    Neighbors where router_id cannot be parsed are skipped.
    """
    neighbors = []
    if not raw:
        return neighbors

    # Split into per-neighbor blocks on the summary line:
    #   "  X.X.X.X    xe-ts16/1.10    Full      Y.Y.Y.Y  1     30     20d:..."
    # We parse the summary line for the core fields, then scan the next few
    # lines for "Num State Transitions".
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Summary line: starts with whitespace, then an IP, then more fields
        m = re.match(
            r'^\s+([\d.]+)\s+(\S+)\s+(\S+)\s+([\d.]+)\s+\d+\s+\d+\s+(\S+)',
            line)
        if m:
            neighbor_ip = m.group(1)
            interface   = m.group(2)
            state       = m.group(3)
            router_id   = m.group(4)
            uptime_str  = m.group(5)
            uptime_s    = _parse_uptime_s(uptime_str)

            # Skip entries where router_id looks like a priority or dead-timer
            # (shouldn't happen given the regex, but guard anyway)
            if not re.match(r'^\d+\.\d+\.\d+\.\d+$', router_id):
                i += 1
                continue

            # Scan forward for "Num State Transitions" (within next 8 lines)
            transitions = 0
            for j in range(i + 1, min(i + 9, len(lines))):
                tm = re.search(r'Num State Transitions:\s*(\d+)', lines[j])
                if tm:
                    transitions = int(tm.group(1))
                    break

            neighbors.append({
                "neighbor_ip": neighbor_ip,
                "interface":   interface,
                "state":       state,
                "router_id":   router_id,
                "uptime_str":  uptime_str,
                "uptime_s":    uptime_s,
                "transitions": transitions,
            })
        i += 1
    return neighbors


# ── 1250 device event-log parsing ─────────────────────────────────────────────
#
# Entries look like this (ONE logical entry, wrapped by the terminal):
#
#   ****** ospfNbrStateChange   2026-08-12,16:03:49    ospf   //object=ospf-lif
#   //instance=master//protocolinstance=0//area=0.02026-08-12,16:03:49 {ospfRouterId:10.
#   111.130.108,ospfNbrIpAddr:10.111.132.217,ospfNbrAddressLessIndex:0,
#   ospfNbrRtrId:10.111.130.254,ospfNbrState:down}
#
# Two things make naive line-based parsing wrong:
#   1. The terminal HARD-WRAPS mid-token ('ospfRouterId:10.' / '111.130.108'),
#      so entries must be split on the '******' marker, never on newlines.
#   2. The timestamp appears TWICE (again concatenated after 'area=0.0'), so we
#      always take the FIRST match in an entry.
#
# The log is dominated by ospfFrrPrefixProtectionChange (fast-reroute prefix
# protection churn) at roughly 99% of lines. Those are NOT adjacency drops and
# must be excluded -- counting them would inflate drop counts enormously.
# A single adjacency bounce is one 'ospfNbrState:down' followed by the
# re-establishment sequence (init -> twoWay -> exchangeStart -> exchange ->
# loading -> full), so drops == count of 'down' transitions.
_NE_ENTRY_SPLIT = re.compile(r'\*{4,}')
_NE_TS_RE       = re.compile(r'(\d{4})-(\d{2})-(\d{2}),(\d{2}):(\d{2}):(\d{2})')
_NE_NBR_STATE   = re.compile(r'ospfNbrState:\s*(\w+)')
_NE_NBR_RTRID   = re.compile(r'ospfNbrRtrId:\s*([\d.]+)')
_NE_NBR_IP      = re.compile(r'ospfNbrIpAddr:\s*([\d.]+)')

# Command run on each 1250. Grepping on the device keeps the transfer tiny --
# the unfiltered log is megabytes because of the FRR churn described above.
#
# '| no-more' IS MANDATORY. Without it the device paginates at a '---(more)---'
# prompt and the read returns only the FIRST page -- which on these devices is
# the OLDEST end of the log. A site with 18 months of history would report its
# oldest event correctly (making the log look deep and healthy) while every
# recent bounce sat unread on later pages, producing a confident zero.
#
# Verified on BTRGLAGW492: log held a clean down->full sequence at
# 2026-07-21 08:02 UTC matching its 28d uptime exactly, yet the report showed
# 30d Drops = 0 and Oldest Evt = 560 days. Oldest read, newest truncated.
# This was the single cause of the fleet-wide 79% SUSPECT rate -- the devices
# log OSPF adjacency changes reliably; the read was cut short.
DEVICE_EVENT_CMD = 'show ne events unreported | grep ospfNbrStateChange | no-more'

# LDP session-down bounce history, read from the HUB (1800) side, keyed on
# each site's OSPF router-ID (= 1250 loopback IP) -- same value already used
# to key everything else in this tool, no separate derivation needed.
#
# Deliberately LDP-only here, NOT broadened to a bare 'Down' grep (unlike
# interface_check.py's version of this check): this tool already tracks OSPF
# adjacency drops separately and more authoritatively, straight from each
# 1250's own event log via _sweep_1250/parse_ne_events_ospf_down (see
# drop_events / count_drops_since / the 24h-72h-7d-30d columns in the main
# report table). A bare 'Down' grep against a hub's FULL event log with no
# per-site filter would match every 'Down' state across every NE element over
# 300+ days of retention -- a much larger transfer per poll than this tool's
# existing per-event-type greps, for data this tool already has from a better
# source. LDP has no such existing source, so it alone is added here.
#
# Uses the FULL log, not '| unreported', deliberately: confirmed live that
# 'unreported' gets crowded out by unrelated churn within roughly an hour to a
# day, while the full log held LDP-Session-Down entries 300+ days old. One
# call per hub covers every site on it -- grep only on the event name here,
# then match each site's loopback client-side (check_down_history), rather
# than one grep-per-site round trip.
LDP_EVENT_CMD = 'show ne events | grep LDP-Session-Down | no-more'

# The NPT-1250 runs its clock in UTC (verified: device reported 15:21:34 +0000
# while the workstation read 11:21 EDT). Its event log therefore timestamps in
# UTC, while poll times and the report stamp are workstation-local.
#
# Left unconverted this silently corrupts the counts, not just the display: a
# UTC timestamp reads ~4 h MORE RECENT than the event actually was, so a bounce
# 26 h old would be pulled into the 24 h bucket. Every device timestamp is
# converted to local at parse time so the whole tool stays in one frame.
DEVICE_CLOCK_IS_UTC = True


def _device_dt_to_local(dt):
    """
    Convert a device (UTC) timestamp to workstation-local time.

    The offset is read at call time rather than cached, so the tool stays correct
    across a DST change while running. An event on the far side of a DST boundary
    can be off by one hour, which is immaterial against 24 h / 72 h / 7 d / 30 d
    windows but worth knowing.
    """
    if not DEVICE_CLOCK_IS_UTC:
        return dt
    return dt + (datetime.datetime.now() - datetime.datetime.utcnow())


def _is_real_nbr_entry(entry):
    """
    True only for text that is genuinely an adjacency-state log entry.

    Requires the FIELD 'ospfNbrState:' (with colon), not merely the event name
    'ospfNbrStateChange'. The distinction matters: the echoed command line
    itself -- 'show ne events unreported | grep ospfNbrStateChange' -- contains
    the event name and no timestamp, so gating on the name alone counted the
    echo as a failed parse on every single device. That produced exactly one
    bogus UNPARSED per site and a report footer warning that counts might be
    understated, which was untrue. A real entry always carries the field.
    """
    return 'ospfNbrState:' in entry


def parse_ne_events_ospf_down(raw):
    """
    Parse 'show ne events unreported' output from a 1250 and extract genuine
    OSPF adjacency DOWN transitions.

    Returns (down_events, oldest_dt, unparsed_count):
      down_events   list of dicts, chronological:
                      {"detected": "2026-08-12T16:03:49",   # device local time
                       "nbr_rtr_id": "10.111.130.254",      # the hub
                       "nbr_ip":     "10.111.132.217",
                       "source":     "device_log"}
      All timestamps are converted from the device's UTC clock to workstation
      LOCAL time before being returned, so they are directly comparable with
      poll times and window cutoffs.

      oldest_dt     datetime of the OLDEST entry seen (any OSPF event type).
                    This bounds how far back the counts can be trusted -- the
                    log is a finite ring buffer, NOT a 30-day archive.
      unparsed      count of entries that looked like nbr-state-changes but
                    could not be parsed. Non-zero means do not trust the count.
    """
    down_events = []
    oldest_dt   = None
    unparsed    = 0
    if not raw:
        return down_events, oldest_dt, unparsed

    for chunk in _NE_ENTRY_SPLIT.split(raw):
        if not chunk.strip():
            continue
        # Rejoin terminal wrapping: strip newlines WITHOUT inserting a space,
        # so 'ospfRouterId:10.' + '111.130.108' -> 'ospfRouterId:10.111.130.108'
        entry = re.sub(r'[\r\n]+', '', chunk)

        tm = _NE_TS_RE.search(entry)          # FIRST timestamp only
        if not tm:
            if _is_real_nbr_entry(entry):
                unparsed += 1
            continue
        try:
            dt = datetime.datetime(
                int(tm.group(1)), int(tm.group(2)), int(tm.group(3)),
                int(tm.group(4)), int(tm.group(5)), int(tm.group(6)))
        except ValueError:
            if _is_real_nbr_entry(entry):
                unparsed += 1
            continue
        # Device logs in UTC -- normalise to local before any comparison
        dt = _device_dt_to_local(dt)

        # Track log depth across every OSPF event type, not just nbr changes
        if oldest_dt is None or dt < oldest_dt:
            oldest_dt = dt

        if 'ospfNbrStateChange' not in entry:
            continue

        sm = _NE_NBR_STATE.search(entry)
        if not sm:
            unparsed += 1
            continue
        if sm.group(1).lower() != 'down':
            continue                          # init/twoWay/.../full = recovery

        rid_m = _NE_NBR_RTRID.search(entry)
        ip_m  = _NE_NBR_IP.search(entry)
        down_events.append({
            "detected":   dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "nbr_rtr_id": rid_m.group(1) if rid_m else "",
            "nbr_ip":     ip_m.group(1) if ip_m else "",
            "source":     "device_log",
        })

    down_events.sort(key=lambda e: e["detected"])
    return down_events, oldest_dt, unparsed


# ── Hostname lookup ───────────────────────────────────────────────────────────
def build_loopback_hostname_map(output_dir=OUTPUT_DIR):
    """
    Scan existing 1250 discovery/autotester output files in output_dir for
    loopback-IP -> hostname mappings.

    Looks for lines like:
      "  Loopback : 10.111.x.x"  (site_sweep / health_check style)
      "  HOST : ALBYGAMA444"      (used together with loopback line)
    Or hostname embedded in the filename (e.g. Autotester_standalone_..._ALBYGAMA444.txt).

    Returns {loopback_ip: hostname} dict. Best-effort -- missing entries just
    show the IP in the spreadsheet until a discovery run populates them.
    """
    lb_map = {}
    try:
        files = [f for f in os.listdir(output_dir)
                 if f.endswith(".txt") and
                 any(f.startswith(p) for p in
                     ("Autotester_standalone", "SiteSweep", "health_check",
                      "DS1_PMs", "fiber_util", "ospf_events"))]
    except Exception:
        return lb_map

    # Regex for loopback lines in report output
    _LB_RE   = re.compile(r'Loopback\s*[:\|]\s*([\d.]+)', re.IGNORECASE)
    _HOST_RE = re.compile(r'(?:HOST|Hostname)\s*[:\|]\s*(\S+)', re.IGNORECASE)
    # Hostname from filename: last underscore-separated token before .txt
    _FNAME_RE = re.compile(r'_([A-Z0-9\-]+)\.txt$', re.IGNORECASE)

    for fname in files:
        fpath = os.path.join(output_dir, fname)
        try:
            with open(fpath, "r", errors="ignore") as f:
                content = f.read(8192)   # only need the header section
        except Exception:
            continue

        loopback = None
        hostname = None

        lb_m = _LB_RE.search(content)
        if lb_m:
            loopback = lb_m.group(1)
        host_m = _HOST_RE.search(content)
        if host_m:
            hostname = host_m.group(1)

        # Fall back to filename-embedded hostname
        if not hostname:
            fn_m = _FNAME_RE.search(fname)
            if fn_m:
                hostname = fn_m.group(1)

        if loopback and hostname and loopback not in lb_map:
            lb_map[loopback] = hostname

    return lb_map


# ── State persistence ─────────────────────────────────────────────────────────
def load_state(path=STATE_FILE):
    """Load state JSON; return {} on missing/corrupt file."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state, path=STATE_FILE):
    """Atomically write state JSON."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print("  [!] State save failed: {}".format(e))


# ── Drop count helpers ────────────────────────────────────────────────────────
def count_drops_since(drop_events, hours, now=None):
    """
    Count drop events within the last N hours.

    Windows are nested by construction (24h subset of 72h subset of 7d subset
    of 30d), so a bounce inside 24 h necessarily counts in all four.

    Times are LOCAL -- events carry device-local timestamps from the 1250 event
    log, so the cutoff must be computed in the same frame.
    """
    base = now or datetime.datetime.now()
    cutoff_iso = (base - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    return sum(1 for e in drop_events if e.get("detected", "") >= cutoff_iso)


def merge_drop_events(existing, incoming):
    """
    Merge device-log drop events into the stored history, de-duplicating on
    (timestamp, neighbour router-id).

    This is what lets the tool outlive the device's log retention. The 1250
    event log is a finite ring buffer -- the sample device only held ~6 days --
    so on its own it can never fill a 30-day window. By merging each run's log
    into a persistent store keyed on the exact device timestamp, coverage grows
    with every run until the full 30 days is real measured data rather than
    inference. Re-reading the same event on the next run is a no-op.

    Returns (merged_list, added_count).
    """
    seen = set()
    merged = []
    for e in (existing or []) + (incoming or []):
        key = (e.get("detected", ""), e.get("nbr_rtr_id", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(e)
    merged.sort(key=lambda e: e.get("detected", ""))
    added = len(merged) - len(existing or [])
    return merged, max(0, added)


# ── Per-hub poll ──────────────────────────────────────────────────────────────
def _poll_hub(hub_name, hub_ip, username, password, lb_map, state,
              results_lock, seen_rids, new_events_by_rid, run_id,
              progress=print):
    """
    SSH one 1800 hub, parse neighbors, detect drops, record into state.

    This function does NOT build report rows.  A dual-homed 1250 appears as a
    neighbor on two different hubs, so building rows here produced one row per
    ADJACENCY (duplicate sites) with drop counts snapshotted mid-poll -- before
    the other hub had contributed its events.  Rows are instead built once, after
    all hubs finish, by _aggregate_results().

    Mutates in-place under results_lock:
      state             -- per-site record incl. per-hub 'hub_state' legs
      seen_rids         -- set of router-IDs observed this run
      new_events_by_rid -- {router_id: [new events from all hubs]}
    """
    progress("[{}] Connecting ({})...".format(hub_name, hub_ip))
    c1800 = NPT1800Client(hub_ip, username, password)
    try:
        c1800.connect()
    except CredentialError:
        raise
    except Exception as e:
        progress("  [!] {} connect failed: {}".format(hub_name, e))
        return

    try:
        raw = c1800.run("show ospf neighbor detail | no-more")
    except Exception as e:
        progress("  [!] {} ospf query failed: {}".format(hub_name, e))
        c1800.close()
        return

    # LDP session-down bounce history for every site on this hub, in one call
    # (see LDP_EVENT_CMD). Best-effort -- a failure here must not take down the
    # OSPF poll this function exists for, so it's logged and swallowed, not
    # raised.
    try:
        ldp_raw = c1800.run(LDP_EVENT_CMD)
    except Exception as e:
        progress("  [!] {} LDP event query failed: {}".format(hub_name, e))
        ldp_raw = ""
    finally:
        try:
            c1800.close()
        except Exception:
            pass

    neighbors = parse_ospf_neighbors(raw)
    progress("  {} -- {} neighbor(s)".format(hub_name, len(neighbors)))

    # LOCAL time, not UTC. Device event logs timestamp in the device's own local
    # clock, and those events are the source of drop counts -- so every timestamp
    # in this tool is local to keep window arithmetic in one consistent frame.
    now_iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    with results_lock:
        for nb in neighbors:
            rid = nb["router_id"]
            hostname = lb_map.get(rid, rid)   # fall back to IP if unknown

            # Initialize state entry if new
            if rid not in state:
                state[rid] = {
                    "hostname":    hostname,
                    "router_id":   rid,
                    "neighbor_ip": nb["neighbor_ip"],
                    "hub_1800":    hub_name,
                    "interface":   nb["interface"],
                    "drop_events": [],
                }

            stored = state[rid]
            # Always update hostname if we now know it
            if hostname != rid:
                stored["hostname"] = hostname

            # LDP session-down bounce history for this site's loopback, read
            # from THIS hub's own event log this poll. Raw device-local
            # timestamp strings, exactly as shown on the CLI -- deliberately
            # NOT folded into drop_events/count_drops_since, which are keyed
            # to a different device's log with its own timestamp format (see
            # parse_ne_events_ospf_down). Kept as its own field, reported
            # separately, so neither pipeline has to guess at the other's
            # timestamp convention.
            #
            # Merged (not overwritten) across hubs: a dual-homed site's two
            # legs sit on two different hubs, each with its OWN event log of
            # that leg's LDP session -- overwriting on the second hub's poll
            # would silently drop the first leg's history.
            # Sort/dedup by the UTC ISO key, not the formatted display string --
            # 'MM/YY - ...' sorts wrong across a year boundary (e.g. "01/27"
            # lexically precedes "12/26").
            new_hits = {h["detected_utc"].isoformat(): h["formatted"]
                        for h in check_down_history(ldp_raw, rid)}
            prior_map = dict(stored.get("ldp_down_hits_map", {}) or {})
            prior_map.update(new_hits)
            stored["ldp_down_hits_map"] = prior_map
            stored["ldp_down_hits"] = [prior_map[k] for k in sorted(prior_map)]

            # NOTE: hub polling deliberately records NO drop events.
            #
            # Drop counts come exclusively from each 1250's own event log
            # (see _sweep_1250), which carries exact device timestamps for every
            # adjacency down transition. Inferring drops here as well would
            # double-count the same bounce -- once from the hub's uptime/
            # transition counters and once from the device log -- and hub-side
            # inference cannot see history older than the current uptime anyway.
            # Single source of truth keeps provenance unambiguous.
            #
            # What hub polling DOES provide: current adjacency state, uptime,
            # which hubs carry the site, and the uptime signal that drives the
            # sweep pre-filter.

            # Update per-hub leg record. 'run_id' marks legs observed in THIS
            # run, which is how _aggregate_results() distinguishes live legs
            # from stale historical ones left in the state file.
            # Carry the PREVIOUS run's uptime and transition counter forward
            # before overwriting them. select_sweep_targets() diffs against
            # these to decide whether this leg could possibly have new log
            # entries -- without them the old values are gone by filter time.
            leg_prev    = (stored.get("hub_state") or {}).get(hub_name) or {}
            prev_uptime = leg_prev.get("uptime_s")
            prev_trans  = leg_prev.get("transitions")

            stored.setdefault("hub_state", {})[hub_name] = {
                "uptime_s":         nb["uptime_s"],
                "uptime_str":       nb["uptime_str"],
                "transitions":      nb["transitions"],
                "state":            nb["state"],
                "interface":        nb["interface"],
                "neighbor_ip":      nb["neighbor_ip"],
                "run_id":           run_id,
                "last_poll":        now_iso,
                "prev_uptime_s":    prev_uptime,
                "prev_transitions": prev_trans,
            }

            # Mark this site as seen in this run
            seen_rids.add(rid)

            stored["neighbor_ip"] = nb["neighbor_ip"]
            stored["last_poll"]   = now_iso


# ── 1250 event-log sweep ──────────────────────────────────────────────────────
def select_sweep_targets(state, seen_rids, run_id, window_days=30):
    """
    Decide which 1250s actually need their event log read.

    This is a PROVABLE filter, not a heuristic. Any OSPF adjacency drop resets
    that adjacency's uptime. So if every live leg of a site reports uptime >=
    window_days, that site demonstrably had no adjacency drop inside the window
    and its counts are known to be zero without contacting it at all.

    Only sites with at least one leg younger than the window can possibly have
    dropped, so only those get swept. On a fleet where most sites are stable this
    typically removes a third to a half of the work with zero loss of accuracy.

    A SECOND provable filter applies once a site has been swept successfully at
    least once. The hubs report each adjacency's uptime AND its cumulative
    'Num State Transitions'. If neither has moved since the previous run, that
    leg cannot have gone down in between, so its event log cannot hold anything
    we have not already recorded -- re-reading it would return identical entries
    that dedup discards. Those sites are skipped as 'unchanged' and their counts
    are simply recomputed from stored history against the current clock.

    This is what makes run 2 onward cheap: the first run reads every log that
    could contain a drop, and later runs only touch sites that actually moved.

    Returns (targets, proven_zero, unchanged) -- lists of router-IDs.
    """
    targets, proven_zero, unchanged = [], [], []
    threshold_s = window_days * 86400
    for rid in sorted(seen_rids):
        stored = state.get(rid) or {}
        legs = [d for d in (stored.get("hub_state") or {}).values()
                if d.get("run_id") == run_id]
        if not legs:
            continue

        ups = [d.get("uptime_s") for d in legs]

        # Filter 1 -- every leg up longer than the window: a drop inside the
        # window is impossible, so counts are exactly zero without contacting it.
        if all(u is not None for u in ups) and min(ups) >= threshold_s:
            proven_zero.append(rid)
            continue

        # Filter 2 -- nothing has changed since a previous successful sweep.
        if stored.get("sweep_status") == "ok" and stored.get("log_oldest"):
            changed = False
            for d in legs:
                pu, pt = d.get("prev_uptime_s"), d.get("prev_transitions")
                cu, ct = d.get("uptime_s"), d.get("transitions")
                # No prior baseline for this leg -> cannot prove it was quiet
                if pu is None or pt is None or cu is None:
                    changed = True
                    break
                # Uptime went backwards -> the session restarted
                if cu < pu - POLL_TOLERANCE_S:
                    changed = True
                    break
                # Transition counter moved -> a state change occurred, even if
                # it recovered too fast for uptime to reveal it
                if ct != pt:
                    changed = True
                    break
                # Currently not Full -> always re-read
                if str(d.get("state", "")).lower() != "full":
                    changed = True
                    break
            if not changed:
                unchanged.append(rid)
                continue

        targets.append(rid)
    return targets, proven_zero, unchanged


def _sweep_1250(rid, username, password, state, lock, stats,
                new_events_by_rid, progress=print):
    """
    SSH one 1250, read its OSPF adjacency event log, merge drops into state.

    Target IP is the site's OSPF router-ID, which on this network is the 1250's
    loopback address.

    Records per site (under lock):
      drop_events   merged, de-duplicated, exact device timestamps
      log_oldest    oldest entry in the device log -- bounds trustworthy depth
      log_unparsed  entries that looked like adjacency events but did not parse
      sweep_status  'ok' | 'unreachable' | 'error'

    A site we cannot reach is marked unreachable and its counts are left blank
    rather than filled with an estimate -- a fabricated number in a column
    labelled as measured is worse than an honest gap.
    """
    c = NPT1250Client(rid, username, password)
    try:
        c.connect()
    except CredentialError:
        raise
    except Exception as e:
        with lock:
            state.setdefault(rid, {})["sweep_status"] = "unreachable"
            state[rid]["sweep_error"] = str(e)[:120]
            stats["unreachable"] += 1
        progress("  [!] {} unreachable: {}".format(rid, str(e)[:70]))
        return

    try:
        raw = c.run(DEVICE_EVENT_CMD, timeout=90)
    except Exception as e:
        with lock:
            state.setdefault(rid, {})["sweep_status"] = "error"
            state[rid]["sweep_error"] = str(e)[:120]
            stats["errors"] += 1
        progress("  [!] {} event-log query failed: {}".format(rid, str(e)[:70]))
        return
    finally:
        try:
            c.close()
        except Exception:
            pass

    downs, oldest_dt, unparsed = parse_ne_events_ospf_down(raw)

    with lock:
        stored = state.setdefault(rid, {})
        prior_keys = {(e.get("detected", ""), e.get("nbr_rtr_id", ""))
                      for e in stored.get("drop_events", [])}
        merged, added = merge_drop_events(stored.get("drop_events", []), downs)
        # Events genuinely new to us this run -- drives the Slack alert. On the
        # very first run every historical event is "new", so alerting is
        # suppressed for baseline runs by the caller.
        if added:
            fresh = [e for e in downs
                     if (e.get("detected", ""), e.get("nbr_rtr_id", ""))
                     not in prior_keys]
            if fresh:
                new_events_by_rid.setdefault(rid, []).extend(fresh)
        stored["drop_events"]  = merged
        stored["sweep_status"] = "ok"
        stored["log_unparsed"] = unparsed
        stored.pop("sweep_error", None)

        # Keep the EARLIEST log start ever observed. Once a run has seen back to
        # a given date, later runs cannot un-see it even after the device's ring
        # buffer has rolled past that point.
        if oldest_dt:
            new_oldest = oldest_dt.strftime("%Y-%m-%dT%H:%M:%S")
            prev = stored.get("log_oldest")
            stored["log_oldest"] = min(prev, new_oldest) if prev else new_oldest

        stats["ok"] += 1
        stats["new_events"] += added
        if unparsed:
            stats["unparsed"] += unparsed

    if added or unparsed:
        progress("  {} -- {} drop(s) in log, {} new{}".format(
            rid, len(downs), added,
            ", {} UNPARSED".format(unparsed) if unparsed else ""))


def _run_sweep(targets, username, password, state, workers,
               new_events_by_rid, progress=print):
    """
    Sweep the target 1250s for their event logs using a bounded worker pool.

    Concurrency is capped (default 8) to avoid bursting the auth infrastructure
    with hundreds of simultaneous SSH logins.
    """
    lock  = threading.Lock()
    stats = {"ok": 0, "unreachable": 0, "errors": 0,
             "new_events": 0, "unparsed": 0}
    queue = list(targets)
    qlock = threading.Lock()
    total = len(queue)
    done  = {"n": 0}

    def _tick():
        """
        Count a finished device and checkpoint state to disk every N.

        The write happens while holding `lock` because save_state() serialises
        the whole state dict -- letting another worker mutate it mid-dump would
        raise 'dictionary changed size during iteration'. Serialising briefly is
        free next to a ~110 s SSH session.
        """
        with lock:
            done["n"] += 1
            n = done["n"]
            if n % CHECKPOINT_EVERY == 0 or n == total:
                save_state(state)
                progress("  [checkpoint] {}/{} devices done, state saved.".format(
                    n, total))

    def _worker():
        while True:
            with qlock:
                if not queue:
                    return
                rid = queue.pop(0)
            try:
                _sweep_1250(rid, username, password, state, lock, stats,
                            new_events_by_rid, progress)
            except CredentialError:
                with qlock:
                    queue.clear()       # bad creds -- stop the whole sweep
                return
            except Exception as e:
                progress("  [!] {} unexpected: {}".format(rid, str(e)[:70]))
            finally:
                # Checkpoint even on failure, so an unreachable device's status
                # is durable and a retry does not re-walk ground already covered.
                try:
                    _tick()
                except Exception:
                    pass

    threads = []
    for _ in range(max(1, min(workers, len(queue) or 1))):
        t = threading.Thread(target=_worker, daemon=True)
        threads.append(t)
        t.start()
        time.sleep(0.15)                # slight stagger on ramp-up
    for t in threads:
        t.join()
    return stats


# ── Per-site aggregation ──────────────────────────────────────────────────────
def _aggregate_results(state, seen_rids, new_events_by_rid, run_id,
                       skipped_rids=(), unchanged_rids=()):
    """
    Build exactly ONE report row per 1250 site (router-ID), aggregating every
    OSPF adjacency leg observed in this run.

    Why this exists: a dual-homed 1250 is a neighbor on two hubs.  Rows must be
    per-SITE, not per-adjacency, otherwise the site appears twice with different
    numbers -- and drop counts must be computed here, after every hub has
    contributed its events, so the counts always agree with the Last Drop
    timestamp rendered from the same event list.

    Aggregation rules:
      state       -- worst leg wins.  Any leg not Full means the site has a
                     problem, even if its other leg is healthy.
      uptime      -- MINIMUM across legs.  The most recently established
                     adjacency is the one that reflects recent instability;
                     reporting the longest uptime would mask a leg that just
                     bounced.
      hubs        -- all hubs carrying this site, comma-joined.
      drop counts -- computed once from the site's full merged event list.
    """
    results = []
    skipped_rids   = set(skipped_rids or ())
    unchanged_rids = set(unchanged_rids or ())
    for rid in sorted(seen_rids):
        stored = state.get(rid) or {}
        hub_state = stored.get("hub_state", {}) or {}

        # Only legs observed in THIS run -- a hub that stopped carrying the site
        # keeps its historical leg record but must not affect current reporting.
        live_legs = [(h, d) for h, d in hub_state.items()
                     if d.get("run_id") == run_id]
        if not live_legs:
            continue
        live_legs.sort(key=lambda t: t[0])

        # ---- Leg-synchronisation pattern -------------------------------------
        # For a dual-homed site, comparing the legs' UPTIMES answers a question
        # the drop counts cannot: did both adjacencies come up at the same moment?
        # If they did, they went down at the same moment, which points at the
        # site's transport rather than either hub. Observed on DLTHMNME624, where
        # 3 of 4 drops hit both hubs in the same second.
        #
        # This is derived entirely from hub-reported uptime, which is exact -- no
        # log attribution required, nothing inferred.
        leg_ups = [d.get("uptime_s") for _, d in live_legs
                   if d.get("uptime_s") is not None]
        if len(live_legs) < 2:
            pattern = "single-homed"
        elif len(leg_ups) < len(live_legs):
            pattern = "unknown"           # a leg's uptime didn't parse
        elif (max(leg_ups) - min(leg_ups)) <= LEG_SYNC_TOLERANCE_S:
            pattern = "legs together"     # same event took both -> site/transport
        else:
            pattern = "legs differ"       # independent -> hub or path specific

        hubs   = [h for h, _ in live_legs]
        events = stored.get("drop_events", [])

        # Provenance. Counts are only shown when they were actually measured.
        #   measured    -- device log read this run
        #   proven_zero -- every leg up longer than the window, so no drop is
        #                  possible; skipped by the pre-filter, still exact
        #   unreachable -- could not read the log; counts left blank
        status = stored.get("sweep_status", "")
        if rid in skipped_rids:
            source = "proven_zero"
        elif rid in unchanged_rids:
            # Previously measured; uptime and transition counters both unmoved,
            # so stored history is still complete and current.
            source = "cached"
        elif status == "ok":
            source = "measured"
        elif status in ("unreachable", "error"):
            source = "unreachable"
        else:
            source = "no_data"

        # How far back the counts are verified. proven_zero needs no log.
        log_oldest = stored.get("log_oldest", "")
        log_days = None
        if source == "proven_zero":
            log_days = int((min(leg_ups) if leg_ups else 0) // 86400)
        elif log_oldest:
            try:
                dt = datetime.datetime.strptime(log_oldest, "%Y-%m-%dT%H:%M:%S")
                log_days = max(0, (datetime.datetime.now() - dt).days)
            except Exception:
                log_days = None

        if source in ("measured", "proven_zero", "cached"):
            counts = {
                "drop_24h": count_drops_since(events, 24),
                "drop_72h": count_drops_since(events, 72),
                "drop_7d":  count_drops_since(events, 168),
                "drop_30d": count_drops_since(events, 720),
            }
        else:
            counts = {"drop_24h": None, "drop_72h": None,
                      "drop_7d": None, "drop_30d": None}

        # ---- ONE ROW PER ADJACENCY LEG ---------------------------------------
        # Previously one row per SITE, which forced two lossy aggregations:
        # state = worst leg, uptime = minimum leg. Both hid the healthy/unhealthy
        # split on the ~68 dual-homed sites, and neither answered the operational
        # question "which circuit, to which hub". The interface -- the actual
        # circuit identifier -- was discarded entirely.
        #
        # State, uptime and interface are per-leg and EXACT. Drop counts remain
        # SITE-level: the device event log identifies its peer by router-id, and
        # the hub poll never learns each hub's own router-id, so attributing an
        # event to one leg would require an inference this tool does not have
        # evidence for. Column headers say "Site" so the scope is unambiguous.
        for hub_name, leg in live_legs:
            results.append(dict(
                router_id    = rid,
                hostname     = stored.get("hostname", rid),
                hub_1800     = hub_name,
                hub_count    = len(live_legs),
                legs_pattern = pattern,
                interface    = leg.get("interface") or "",
                neighbor_ip  = leg.get("neighbor_ip") or "",
                state        = str(leg.get("state") or "?"),
                uptime_str   = leg.get("uptime_str", ""),
                uptime_s     = leg.get("uptime_s"),
                transitions  = leg.get("transitions"),
                new_events   = new_events_by_rid.get(rid, []),
                source       = source,
                log_oldest   = log_oldest,
                log_days     = log_days,
                log_unparsed = stored.get("log_unparsed", 0) or 0,
                sweep_error  = stored.get("sweep_error", ""),
                ldp_down_hits = stored.get("ldp_down_hits", []) or [],
                **counts
            ))
    return results


# ── Port-level rollup (higher-level outage detection) ─────────────────────────
def port_rollup(results, min_down=2):
    """
    Group adjacencies by their physical port and report ports with multiple
    sub-interfaces down.

    WHY: the NOC works the queue circuit by circuit and opens an IXC ticket per
    EVC. When a whole NNI port is down, every one of those tickets is the same
    outage reported N times -- effort wasted, and the actual fault (one port)
    buried under its symptoms (many circuits).

    An 1800 NNI is a sub-interface (xe-ts16/2.111 = one 1250 site, all of whose
    EVCs ride it). Its physical parent (xe-ts16/2) carries many such
    sub-interfaces, i.e. many sites. So a parent-level failure is one ticket, not
    fifty.

    Costs NOTHING extra: every interface and state here already came back from
    the `show ospf neighbor detail` call each hub gets anyway.

    Returns a list of dicts, worst first:
        {hub, port, total, down, down_share, sites:[(hostname, iface, state)]}
    """
    ports = {}
    for r in results:
        iface = (r.get("interface") or "").strip()
        if not iface or "." not in iface or "/" not in iface:
            continue
        parent, sub = iface.rsplit(".", 1)
        if not sub.isdigit():
            continue
        key = (r.get("hub_1800", ""), parent)
        p = ports.setdefault(key, {"hub": key[0], "port": parent,
                                   "total": 0, "down": 0, "sites": []})
        p["total"] += 1
        if str(r.get("state", "")).strip().lower() != "full":
            p["down"] += 1
            p["sites"].append((r.get("hostname", "?"), iface,
                               r.get("state", "?")))
    out = []
    for p in ports.values():
        if p["down"] >= min_down:
            p["down_share"] = p["down"] / float(p["total"] or 1)
            p["sites"].sort()
            out.append(p)
    out.sort(key=lambda p: (-p["down"], -p["down_share"], p["hub"], p["port"]))
    return out


# ── Timestamp display ─────────────────────────────────────────────────────────
def count_trust(uptime_s, d24, d72, d7, d30):
    """
    Per-row verdict on whether the drop counts can be believed.

    Uses a contradiction that needs no knowledge of log retention: an adjacency
    reporting uptime of 6 h came up 6 h ago, which means it went DOWN 6 h ago,
    so a 24 h count of zero is impossible. Uptime comes from the hub and is
    exact; the counts come from the device event log. When they disagree, the
    log is the one that is wrong.

    This replaces trying to infer trust from log depth. Measured on the fleet,
    'Log Days' was useless for the purpose -- devices claiming 4,000+ days of
    history were missing bounces from the same morning -- because the event
    queue is not a time-ordered buffer. The contradiction check catches those
    rows regardless.

    Returns "OK", "SUSPECT", or "n/a".
    """
    if d24 is None:
        return "n/a"                      # unreachable, nothing to judge
    if uptime_s is None:
        return "OK"
    for hrs, v in ((24, d24), (72, d72), (168, d7), (720, d30)):
        if v == 0 and uptime_s < hrs * 3600:
            return "SUSPECT"
    return "OK"


def _fmt_local_stamp(iso_str, fmt="%Y-%m-%d %H:%M"):
    """
    Render a stored ISO timestamp for display.

    Every timestamp in this tool is LOCAL time and stored in one frame, so this
    only reformats -- it does not shift. Drop events carry the 1250's own local
    clock from its event log, and poll times use local now(), which keeps window
    arithmetic and display consistent with no timezone conversion anywhere.
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return iso_str          # unparseable -- show as-is rather than blank
    return dt.strftime(fmt)


# ── NetBox hostname resolution ────────────────────────────────────────────────
def _resolve_hostnames_netbox(nb, router_ids, progress=print):
    """
    Query NetBox IPAM for device hostnames of the given loopback IPs.

    Called after every poll for any router-IDs whose hostname is still the
    bare IP (i.e., not yet resolved from a previous run or the lb_map scan).
    Results are applied to both the in-memory results list and the state dict
    so the xlsx and the state cache reflect real hostnames immediately.

    Returns {router_id: hostname} for every IP that NetBox could resolve.
    """
    resolved = {}
    if not nb.ok() or not router_ids:
        return resolved
    for rid in router_ids:
        name = nb.hostname_by_ip(rid)
        if name:
            resolved[rid] = name
            progress("  [NetBox] {} -> {}".format(rid, name))
        else:
            progress("  [NetBox] {} -> (not found in IPAM)".format(rid))
    return resolved


# ── Report builder ────────────────────────────────────────────────────────────
def _build_report(results, hub_names, stamp, elapsed_s):
    """
    Build pipe-delimited, fixed-width plain-text report for the progress log.

    Column layout (each field padded to fixed width so | delimiters align):
      ST  Hostname               | Hub-1800        | State  | Uptime       | 24h | 72h |  7d | 30d
    ST = '!!' for any issue, '  ' for clean.
    Uptime via _fmt_uptime_fixed() -> always 12 chars (' 20d 06h 41m').
    """
    SEP  = "=" * 84
    SEP2 = "-" * 84

    HDR = ("   {:<20} | {:<13} | {:<14} | {:<6} | {:12} | "
           "{:>4} | {:>4} | {:>4} | {:>4}".format(
               "Hostname", "Hub-1800", "Interface", "State", "Uptime",
               "24h", "72h", "7d", "30d"))
    DIV = ("   " + "-"*20 + "-+-" + "-"*13 + "-+-" + "-"*14 + "-+-" + "-"*6 +
           "-+-" + "-"*12 + "-+-" + "-"*4 + "-+-" + "-"*4 + "-+-" + "-"*4 +
           "-+-" + "-"*4)

    lines = [
        SEP,
        "  OSPF STABILITY REPORT  --  {}".format(stamp),
        "  Hubs polled: {}  |  Adjacencies: {}  |  Sites: {}  |  Elapsed: {}s".format(
            len(hub_names), len(results),
            len({r["router_id"] for r in results}), int(elapsed_s)),
        SEP,
        "",
        HDR,
        DIV,
    ]

    # Sort: currently-down first, then by drop count (24h desc), then hostname.
    # Unmeasured counts (None) sort alongside zero rather than crashing.
    def _sort_key(r):
        down = 0 if r["state"].lower() == "full" else 1
        return (down, -(r["drop_24h"] or 0), r["hostname"].lower())

    def _n(v):
        """Render a count; '--' when the site could not be measured."""
        return "--" if v is None else v

    for r in sorted(results, key=_sort_key):
        flagged = (r["state"].lower() != "full") or bool(r["drop_24h"])
        icon = "!!" if flagged else ("??" if r["drop_24h"] is None else "  ")
        raw_up = r.get("uptime_str", "")
        uptime_s = r.get("uptime_s") or _parse_uptime_s(raw_up)
        lines.append("{} {:<20} | {:<13} | {:<14} | {:<6} | {} | "
                     "{:>4} | {:>4} | {:>4} | {:>4}".format(
                         icon,
                         r["hostname"][:20],
                         r["hub_1800"][:13],
                         (r.get("interface") or "")[:14],
                         r["state"][:6],
                         _fmt_uptime_fixed(uptime_s, raw_up),
                         _n(r["drop_24h"]), _n(r["drop_72h"]),
                         _n(r["drop_7d"]),  _n(r["drop_30d"]),
                     ))

    lines.append("")
    # Flagged detail block
    flagged = [r for r in results
               if r["state"].lower() != "full" or bool(r["drop_24h"])]
    if flagged:
        lines.append(SEP2)
        lines.append("  FLAGGED SITES ({})".format(len(flagged)))
        lines.append(SEP2)
        for r in flagged:
            raw_up   = r.get("uptime_str", "")
            uptime_s = r.get("uptime_s") or _parse_uptime_s(raw_up)
            lines.append("  {} | {} {}  state={}  up={}  [{}]  "
                         "site drops: 24h={} 72h={} 7d={} 30d={}".format(
                             r["hostname"], r["hub_1800"],
                             r.get("interface") or "?",
                             r["state"], _fmt_uptime_fixed(uptime_s, raw_up),
                             r.get("legs_pattern") or "?",
                             _n(r["drop_24h"]), _n(r["drop_72h"]),
                             _n(r["drop_7d"]),  _n(r["drop_30d"])))
    else:
        lines.append("  All {} sites OSPF Full -- no drops in last 24 hours.".format(
            len(results)))

    # Data-quality footer: say plainly what was and was not measured.
    unreach  = [r for r in results if r["source"] == "unreachable"]
    proven   = [r for r in results if r["source"] == "proven_zero"]
    measured = [r for r in results if r["source"] == "measured"]
    cached   = [r for r in results if r["source"] == "cached"]
    lines.append("")
    lines.append(SEP2)
    lines.append("  DATA SOURCE")
    lines.append(SEP2)
    lines.append("  {} read from device event log this run".format(len(measured)))
    lines.append("  {} cached (counters unchanged since last run -- no new drops "
                 "possible)".format(len(cached)))
    lines.append("  {} proven zero (all legs up > {} d, no log read needed)".format(
        len(proven), SWEEP_WINDOW_DAYS))
    if unreach:
        lines.append("  {} UNREACHABLE -- counts shown as '--', not estimated".format(
            len(unreach)))
        for r in unreach[:15]:
            lines.append("      {} ({}) {}".format(
                r["hostname"], r["router_id"], r.get("sweep_error", "")[:50]))
        if len(unreach) > 15:
            lines.append("      ... and {} more".format(len(unreach) - 15))

    # ── LDP SESSION-DOWN HISTORY ──────────────────────────────────────────────
    # Bounce history for the LDP session to each site's loopback, read from the
    # hub's own event log (see LDP_EVENT_CMD). Separate from the OSPF drop
    # counts above -- different device, different log, different timestamp
    # format -- so shown as its own list rather than merged into a column.
    ldp_flagged = [r for r in results if r.get("ldp_down_hits")]
    if ldp_flagged:
        lines.append("")
        lines.append(SEP2)
        lines.append("  LDP SESSION-DOWN HISTORY ({} site(s) with events)".format(
            len(ldp_flagged)))
        lines.append(SEP2)
        for r in ldp_flagged:
            hits = r["ldp_down_hits"]
            lines.append("  {} ({})  hub={}  -- {} event(s)".format(
                r["hostname"], r["router_id"], r["hub_1800"], len(hits)))
            for ts in hits[:10]:
                lines.append("      {}".format(ts))
            if len(hits) > 10:
                lines.append("      ... and {} more".format(len(hits) - 10))

    # ── PORT-LEVEL EVENTS — the "one ticket, not fifty" section ──────────────
    # Placed high in the footer deliberately: if a whole NNI port is down, that
    # is the finding, and the per-circuit rows above are its symptoms.
    port_events = port_rollup(results)
    if port_events:
        lines.append("")
        lines.append(SEP)
        lines.append("  PORT-LEVEL EVENTS ({}) -- read these BEFORE ticketing circuits".format(
            len(port_events)))
        lines.append(SEP)
        for p in port_events:
            majority = p["down_share"] >= 0.5
            lines.append("  [{}] {} {} -- {} of {} sub-interfaces DOWN ({:.0%})".format(
                "!!" if majority else "! ", p["hub"], p["port"],
                p["down"], p["total"], p["down_share"]))
            if majority:
                lines.append("       ONE fault affecting every site on this port. "
                             "Raise a single ticket against {},".format(p["port"]))
                lines.append("       not an IXC ticket per EVC.")
            for host, iface, st in p["sites"][:12]:
                lines.append("         {:<22} {:<16} {}".format(host[:22], iface, st))
            if len(p["sites"]) > 12:
                lines.append("         ... and {} more site(s)".format(
                    len(p["sites"]) - 12))

    # Trust audit: how many rows have counts that contradict their own uptime.
    suspect = 0
    for r in results:
        ups = r.get("uptime_s") or _parse_uptime_s(r.get("uptime_str", ""))
        if count_trust(ups, r["drop_24h"], r["drop_72h"],
                       r["drop_7d"], r["drop_30d"]) == "SUSPECT":
            suspect += 1
    if suspect:
        pct = 100.0 * suspect / max(1, len(results))
        lines.append("  [!] {} of {} rows ({:.0f}%) marked SUSPECT -- uptime shows a".format(
            suspect, len(results), pct))
        lines.append("      bounce inside a window whose count reads zero. Those devices")
        lines.append("      did not log the event; treat their counts as a floor.")
        lines.append("      State, uptime and Last Poll remain exact for every row.")

    lines.append("")
    return "\n".join(lines)


# ── Slack alert ───────────────────────────────────────────────────────────────
def _build_slack_alert(new_drop_results, stamp):
    """Build Slack message for runs with new drop events."""
    lines = ["*OSPF Stability Alert* -- {}".format(stamp), ""]
    for r in new_drop_results:
        lines.append("• *{}* ({}) -- {} new drop(s)  |  state: {}  |  uptime: {}".format(
            r["hostname"], r["hub_1800"],
            len(r["new_events"]), r["state"], r["uptime_str"]))
    return "\n".join(lines)


# ── Excel report builder ──────────────────────────────────────────────────────
def _build_xlsx(results, state, hub_names, stamp, elapsed_s, out_path):
    """
    Write OSPF stability data to an Excel workbook at out_path.

    Columns: Hostname | Router-ID | Hub 1800 | State | Uptime |
             24h Drops | 72h Drops | 7d Drops | 30d Drops | Last Drop | Last Poll

    Row color coding (sorted: down first, then drop severity, then hostname):
      Red    (#FF9999)  -- state not Full (live session down)
      Yellow (#FFE066)  -- drops detected in last 24 h
      Orange (#FFD966)  -- drops in 72 h window, none in 24 h
      White             -- drops in 7d/30d window, none in 72 h
      Green  (#C6EFCE)  -- zero drops in last 30 days

    Returns True on success, False if openpyxl is unavailable.
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False   # caller will warn: pip install openpyxl

    # ── Color / style constants ──
    # Three states only -- clean enough for a demo, unambiguous at a glance:
    #   Red    = OSPF session not Full (live down)
    #   Amber  = session Full but drops detected in last 24 h
    #   Green  = zero drops in last 30 days (pristine)
    #   White  = (no fill) drops exist in history but nothing in last 24 h
    FILL_RED    = PatternFill("solid", fgColor="FF9999")   # down now
    FILL_AMBER  = PatternFill("solid", fgColor="FFBF00")   # drops in 24 h
    FILL_GREEN  = PatternFill("solid", fgColor="92D050")   # clean 30 d
    FILL_GREY   = PatternFill("solid", fgColor="D9D9D9")   # not measured
    FILL_HDR    = PatternFill("solid", fgColor="1F4E79")
    FONT_TITLE  = Font(name="Arial", bold=True, size=12, color="1F4E79")
    FONT_HDR    = Font(name="Arial", bold=True, size=10, color="FFFFFF")
    FONT_BODY   = Font(name="Arial", size=10)
    ALIGN_CTR   = Alignment(horizontal="center", vertical="center")
    ALIGN_LEFT  = Alignment(horizontal="left",   vertical="center")
    thin        = Side(style="thin", color="CCCCCC")
    BORDER      = Border(left=thin, right=thin, top=thin, bottom=thin)

    COL_HEADERS = [
        "Hostname", "Router-ID (Loopback)", "Hub 1800",
        "Interface", "Legs", "State", "Uptime",
        "Site 24h", "Site 72h", "Site 7d", "Site 30d",
        "Last Drop", "Count Trust", "Oldest Evt (d)", "Source", "Last Poll",
    ]
    # One row per ADJACENCY now, so Hub holds a single name and Interface -- the
    # circuit identifier -- is exposed. Drop columns are prefixed "Site" because
    # they remain site-scoped while State/Uptime/Interface are per-leg.
    COL_WIDTHS = [26, 20, 15, 16, 14, 8, 14,
                  10, 10, 10, 10, 18, 13, 14, 14, 18]
    # Columns (1-based) that should be centre-aligned in data rows:
    # Legs(5) State(6) Site counts(8-11) Count Trust(13) Oldest Evt(14)
    CTR_COLS = {5, 6, 8, 9, 10, 11, 13, 14}
    NCOLS    = len(COL_HEADERS)          # 14 -> A..N
    LAST_COL = get_column_letter(NCOLS)  # "N"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "OSPF Stability"

    # ── Row 1: title ──
    ws.row_dimensions[1].height = 22
    ws.merge_cells("A1:{}1".format(LAST_COL))
    tc = ws["A1"]
    tc.value = ("OSPF Stability Report  |  {}  |  {} hub(s) polled  |  {} sites"
                .format(stamp, len(hub_names), len(results)))
    tc.font      = FONT_TITLE
    tc.alignment = ALIGN_LEFT

    # ── Row 2: column headers ──
    ws.row_dimensions[2].height = 18
    for col, (hdr, width) in enumerate(zip(COL_HEADERS, COL_WIDTHS), start=1):
        cell = ws.cell(row=2, column=col, value=hdr)
        cell.font      = FONT_HDR
        cell.fill      = FILL_HDR
        cell.alignment = ALIGN_CTR
        cell.border    = BORDER
        ws.column_dimensions[get_column_letter(col)].width = width

    # ── Sort results: down first, then 24h drops desc, then hostname ──
    def _sort_key(r):
        down = 0 if r["state"].lower() == "full" else 1
        # Unmeasured counts are None -- coerce so sorting never raises, and put
        # unreachable sites just below live-down ones so they are not buried.
        unknown = 1 if r["drop_24h"] is None else 0
        # Hub last so a dual-homed site's two legs sit next to each other, which
        # is what makes the "legs together" / "legs differ" split readable.
        return (down, -unknown, -(r["drop_24h"] or 0),
                r["hostname"].lower(), r["hub_1800"])

    # ── Data rows ──
    for row_idx, r in enumerate(sorted(results, key=_sort_key), start=3):
        # Row fill. Grey means "we could not measure this" -- deliberately not
        # green, so an unreachable site never reads as healthy.
        if r["source"] == "unreachable":
            fill = FILL_GREY
        elif r["state"].lower() != "full":
            fill = FILL_RED         # session currently down
        elif r["drop_24h"]:
            fill = FILL_AMBER       # bounced in the last 24 h
        elif r["drop_30d"] == 0:
            fill = FILL_GREEN       # pristine -- zero drops in 30 days
        else:
            fill = None             # some history, nothing recent -- white

        # Last drop timestamp. Everything is already local time (device event
        # logs are device-local), so no conversion is applied.
        #
        # Only surfaced when the 30-day count is non-zero: an event older than
        # the widest window is excluded from the counts, and showing it here
        # anyway produced rows reading "0 drops" beside a populated Last Drop.
        s_entry   = state.get(r["router_id"], {})
        events    = s_entry.get("drop_events", [])
        last_drop = ""
        if events and r["drop_30d"]:
            raw_ts = max((e.get("detected", "") for e in events), default="")
            last_drop = _fmt_local_stamp(raw_ts)

        last_poll = _fmt_local_stamp(s_entry.get("last_poll", ""))

        uptime_s = r.get("uptime_s") or _parse_uptime_s(r.get("uptime_str", ""))

        # Unmeasured counts render as "--" rather than 0, so a site we could not
        # reach is never mistaken for a site with no drops.
        def _cell(v):
            return "--" if v is None else v

        SOURCE_LABEL = {
            "measured":    "Device log",
            "cached":      "Cached (no chg)",
            "proven_zero": "Proven zero",
            "unreachable": "UNREACHABLE",
            "no_data":     "No data",
        }

        row_vals = [
            r["hostname"],
            r["router_id"],
            r["hub_1800"],
            r.get("interface") or "",
            r.get("legs_pattern") or "",
            r["state"],
            _fmt_uptime(uptime_s),
            _cell(r["drop_24h"]),
            _cell(r["drop_72h"]),
            _cell(r["drop_7d"]),
            _cell(r["drop_30d"]),
            last_drop,
            count_trust(uptime_s, r["drop_24h"], r["drop_72h"],
                        r["drop_7d"], r["drop_30d"]),
            "--" if r.get("log_days") is None else r["log_days"],
            SOURCE_LABEL.get(r["source"], r["source"]),
            last_poll,
        ]

        ws.row_dimensions[row_idx].height = 16
        for col, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.font      = FONT_BODY
            cell.border    = BORDER
            cell.alignment = ALIGN_CTR if col in CTR_COLS else ALIGN_LEFT
            if fill:
                cell.fill = fill

    # ── Freeze panes + auto-filter ──
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = "A2:{}{}".format(LAST_COL, 1 + len(results))

    # ── Summary section (columns P+, floats alongside the data rows) ─────────
    # Data occupies A..N, so column O is the spacer and the summary starts at P.
    ws.column_dimensions["Q"].width = 3
    for col_letter in ("R", "S", "T", "U", "V", "W", "X", "Y"):
        ws.column_dimensions[col_letter].width = 13
    ws.column_dimensions["R"].width = 16

    # Row 2 (same as column header row): summary section title
    ws.merge_cells("R2:Y2")
    sc = ws["R2"]
    sc.value     = "OSPF Stability — Summary"
    sc.font      = FONT_HDR
    sc.fill      = FILL_HDR
    sc.alignment = ALIGN_CTR
    sc.border    = BORDER

    # Row 3: description
    ws.merge_cells("R3:Y5")
    dc = ws["R3"]
    dc.value = (
        "Every NPT-1800 hub router in NetBox is polled for its OSPF adjacencies, "
        "identifying each connected 1250 CPE site and its current session state.\n"
        "Drop counts are then read from each 1250's own event log "
        "(ospfNbrStateChange / down), giving exact device-timestamped history "
        "rather than an estimate. Sites whose every adjacency has been up longer "
        "than 30 days are provably drop-free and are not contacted.\n"
        "'Log Days' shows how far back each device's log verifies. Where that is "
        "under 30 days the 30-day count is a lower bound that fills in as history "
        "accumulates across runs. Hostnames resolved live from NetBox IPAM."
    )
    dc.font      = Font(name="Arial", size=9, italic=True, color="595959")
    dc.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws.row_dimensions[3].height = 60   # room for the wrapped description

    # Row 6: run stats
    ws.merge_cells("R6:Y6")
    rc = ws["R6"]
    n_meas   = sum(1 for r in results if r["source"] == "measured")
    n_proven = sum(1 for r in results if r["source"] == "proven_zero")
    n_unre   = sum(1 for r in results if r["source"] == "unreachable")
    rc.value = (
        "Hubs: {}   |   Adjacencies: {}   |   Measured: {}   |   Proven zero: {}   |   "
        "Unreachable: {}   |   {}   |   {}s"
        .format(len(hub_names), len(results), n_meas, n_proven, n_unre,
                stamp, int(elapsed_s))
    )
    rc.font      = Font(name="Arial", size=9, bold=True, color="1F4E79")
    rc.alignment = ALIGN_LEFT

    # ── Chart data table (rows 8-10) ─────────────────────────────────────────
    # Each window gets a column; rows are: header / Stable count / Issues count.
    # The PieChart references these ranges directly.
    DROW_HDR  = 8
    DROW_STAB = 9
    DROW_ISSU = 10
    COL_SLICE = 18   # R -- row labels
    COL_24H   = 19   # S
    COL_72H   = 20   # T
    COL_7D    = 21   # U
    COL_30D   = 22   # V

    _hdr_font = Font(name="Arial", bold=True, size=9)
    _dat_font = Font(name="Arial", size=9)

    for col, lbl in [(COL_SLICE, ""), (COL_24H, "Last 24h"),
                     (COL_72H, "Last 72h"), (COL_7D, "Last 7d"),
                     (COL_30D, "Last 30d")]:
        c = ws.cell(row=DROW_HDR, column=col, value=lbl)
        c.font   = _hdr_font
        c.border = BORDER

    for row, label in [(DROW_STAB, "Stable"), (DROW_ISSU, "Issues")]:
        c = ws.cell(row=row, column=COL_SLICE, value=label)
        c.font = _dat_font

    # Charts count only sites we could actually measure, so unreachable sites
    # never inflate the "stable" slice.
    measurable = [r for r in results
                  if r["source"] in ("measured", "proven_zero", "cached")]
    total = len(measurable)
    for col, key in [(COL_24H, "drop_24h"), (COL_72H, "drop_72h"),
                     (COL_7D, "drop_7d"),   (COL_30D, "drop_30d")]:
        stable = sum(1 for r in measurable if r[key] == 0)
        for row, val in [(DROW_STAB, stable), (DROW_ISSU, total - stable)]:
            c = ws.cell(row=row, column=col, value=val)
            c.font      = _dat_font
            c.border    = BORDER
            c.alignment = ALIGN_CTR

    # ── Four pie charts: 2x2 grid starting at row 12 ─────────────────────────
    try:
        from openpyxl.chart import PieChart, Reference
        from openpyxl.chart.series import DataPoint

        chart_configs = [
            (COL_24H, "Stable — Last 24h", "R12"),
            (COL_72H, "Stable — Last 72h", "W12"),
            (COL_7D,  "Stable — Last 7d",  "R27"),
            (COL_30D, "Stable — Last 30d", "W27"),
        ]
        for data_col, title, anchor in chart_configs:
            pie        = PieChart()
            pie.title  = title
            pie.style  = 10
            pie.width  = 11    # cm
            pie.height = 8     # cm

            # Data: series title in header row, values in STAB and ISSU rows
            data = Reference(ws, min_col=data_col, max_col=data_col,
                             min_row=DROW_HDR, max_row=DROW_ISSU)
            # Categories: "Stable" / "Issues" labels in the slice column
            cats = Reference(ws, min_col=COL_SLICE, max_col=COL_SLICE,
                             min_row=DROW_STAB, max_row=DROW_ISSU)
            pie.add_data(data, titles_from_data=True)
            pie.set_categories(cats)

            # Slice colors: green = stable, red = issues
            try:
                pt0 = DataPoint(idx=0)
                pt0.graphicalProperties.solidFill = "92D050"   # green
                pt1 = DataPoint(idx=1)
                pt1.graphicalProperties.solidFill = "FF4444"   # red
                pie.series[0].dPt = [pt0, pt1]
            except Exception:
                pass   # color is cosmetic -- chart still renders without it

            ws.add_chart(pie, anchor)

    except Exception:
        pass   # charts are optional -- workbook is still valid without them

    # ── Legend sheet ──
    ws2 = wb.create_sheet("Legend")
    ws2.column_dimensions["A"].width = 14
    ws2.column_dimensions["B"].width = 50
    ws2["A1"].value = "Color Legend"
    ws2["A1"].font  = Font(name="Arial", bold=True, size=11)
    legend_rows = [
        (FILL_RED,   "Red",   "OSPF session currently NOT Full (live down)"),
        (FILL_AMBER, "Amber", "Session Full but a drop occurred in last 24 h"),
        (None,       "White", "Drops in history (>24 h ago) -- nothing recent"),
        (FILL_GREEN, "Green", "Zero drops in last 30 days -- fully clean"),
        (FILL_GREY,  "Grey",  "NOT MEASURED -- device unreachable; counts show '--'"),
    ]
    for i, (lfill, label, desc) in enumerate(legend_rows, start=2):
        c1 = ws2.cell(row=i, column=1, value=label)
        c2 = ws2.cell(row=i, column=2, value=desc)
        c1.font = Font(name="Arial", size=10)
        c2.font = Font(name="Arial", size=10)
        if lfill:
            c1.fill = lfill

    # Column meanings that are easy to misread on a stability report
    notes_start = len(legend_rows) + 4
    ws2.cell(row=notes_start, column=1, value="Column notes").font = Font(
        name="Arial", bold=True, size=11)
    col_notes = [
        ("Row scope", "ONE ROW PER ADJACENCY, not per site. A dual-homed site "
                      "appears twice -- once per hub -- with its two legs "
                      "adjacent. Hostname / Router-ID / Site* columns repeat; "
                      "Hub / Interface / State / Uptime are that leg's own."),
        ("Interface", "The hub-side interface carrying this adjacency, e.g. "
                      "xe-ts16/1.10 -- the circuit identifier to act on."),
        ("Uptime", "This leg's own uptime, exact from the hub. Was previously "
                   "the MINIMUM across legs, which hid the healthy/unhealthy "
                   "split on dual-homed sites."),
        ("Legs", "single-homed = one adjacency. 'legs together' = both legs' "
                 "uptimes agree within 2 min, so one event took both -- points "
                 "at site transport, not either hub. 'legs differ' = "
                 "independent, so hub- or path-specific."),
        ("Site 24h/72h/7d/30d", "SITE-scoped drop counts, repeated on each of a "
                                "site's rows. The device event log names its "
                                "peer by router-id and the hub poll never learns "
                                "each hub's own router-id, so per-leg "
                                "attribution would be an inference this tool "
                                "has no evidence for. Compare the two legs' "
                                "UPTIMES instead -- see Legs."),
        ("Count Trust", "OK = drop counts are consistent with uptime. "
                        "SUSPECT = uptime shows the adjacency came up inside a "
                        "window whose count reads zero, which is impossible -- "
                        "the device did not log that bounce, so treat this row's "
                        "counts as a floor, not a total. n/a = not measured."),
        ("Oldest Evt (d)", "Age of the oldest OSPF event found on that device. "
                           "This is NOT guaranteed retention: the event queue is "
                           "not time-ordered, and devices reporting thousands of "
                           "days here were still missing same-day bounces. Use "
                           "Count Trust to judge a row, not this."),
        ("Source", "Device log = read from the 1250 this run. Cached = counters "
                   "unchanged since last run, so stored history is still "
                   "complete. Proven zero = every leg up >30 d so no drop is "
                   "possible. UNREACHABLE = not measured, counts blank."),
        ("Drop counts", "Windows are nested: a drop inside 24 h also counts in "
                        "72 h, 7 d and 30 d."),
    ]
    for j, (name, desc) in enumerate(col_notes, start=notes_start + 1):
        a = ws2.cell(row=j, column=1, value=name)
        b = ws2.cell(row=j, column=2, value=desc)
        a.font = Font(name="Arial", size=10, bold=True)
        b.font = Font(name="Arial", size=10)
        b.alignment = Alignment(vertical="top", wrap_text=True)
    ws2.column_dimensions["B"].width = 78

    wb.save(out_path)
    return True


# ── Main run() ────────────────────────────────────────────────────────────────
def run(params, progress=print):
    """
    Execute one OSPF stability poll cycle.
    params keys:
      username       -- infra SSH username
      password       -- infra SSH password
      slack_alert    -- bool, post Slack on new drops (default True)
      open_when_done -- bool
    Returns out_path (str) or None on total failure.
    """
    username    = params["username"]
    password    = params["password"]
    slack_alert = params.get("slack_alert", True)
    open_when_done = params.get("open_when_done", False)

    stamp   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    t_start = time.time()

    # ── NetBox: discover hubs ─────────────────────────────────────────────
    nb_url, nb_token = load_netbox_config()
    nb = NetboxClient(nb_url, nb_token)
    if not nb.ok():
        progress("[!] NetBox not configured -- check NETBOX_URL / NETBOX_TOKEN in autotester_config.txt")
        return None

    # Config-driven name filter (default "ces01")
    hub_filter = _read_hub_filter()
    progress("[NetBox] Discovering 1800 hubs (filter='{}')...".format(hub_filter))
    hub_names = nb.hub_1800_devices(name_contains=hub_filter)
    if not hub_names:
        progress("[!] No hub devices found in NetBox matching '{}'.".format(hub_filter))
        return None
    progress("  Found {} hub(s): {}".format(len(hub_names), ", ".join(sorted(hub_names))))

    # Resolve management IPs for each hub (prefer MSA, fall back to MSB)
    hub_targets = []   # [(hub_name, hub_ip), ...]
    for hname in sorted(hub_names):
        slots = nb.hub_1800_mgmt_ips(hname)
        ip = None
        for slot_key in ("MSA", "MSB"):
            ips = slots.get(slot_key, [])
            if ips:
                ip = ips[0]
                break
        if ip:
            hub_targets.append((hname, ip))
            progress("  {} -> {}".format(hname, ip))
        else:
            progress("  [!] {} -- no MGMT MCP SLOT_MS* IP in NetBox, skipping".format(hname))

    if not hub_targets:
        progress("[!] No reachable hub IPs found. Check NetBox IPAM data.")
        return None

    # ── Load state + hostname map ─────────────────────────────────────────
    progress("\n[State] Loading...")
    state = load_state()
    progress("  {} sites in state file.".format(len(state)))

    progress("[Hostnames] Building loopback->hostname map from existing reports...")
    lb_map = build_loopback_hostname_map()
    progress("  {} hostname mappings found.".format(len(lb_map)))

    # ── Poll hubs (parallel) ──────────────────────────────────────────────
    progress("\n[Poll] Starting {} hub(s)...".format(len(hub_targets)))
    # run_id marks every adjacency leg observed in this run, so aggregation can
    # distinguish live legs from stale historical ones in the state file.
    run_id            = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    seen_rids         = set()
    new_events_by_rid = {}
    results_lock      = threading.Lock()
    threads           = []

    for hub_name, hub_ip in hub_targets:
        t = threading.Thread(
            target=_poll_hub,
            args=(hub_name, hub_ip, username, password,
                  lb_map, state, results_lock, seen_rids,
                  new_events_by_rid, run_id, progress),
            daemon=True)
        threads.append(t)
        t.start()
        # Stagger starts slightly to avoid simultaneous auth floods
        time.sleep(0.3)

    for t in threads:
        t.join()

    progress("\n[Poll] Hubs complete -- {} adjacency leg(s), {} unique site(s)".format(
        sum(len([d for d in (state.get(r) or {}).get("hub_state", {}).values()
                 if d.get("run_id") == run_id]) for r in seen_rids),
        len(seen_rids)))

    # ── Sweep 1250 event logs for TRUE drop history ───────────────────────
    # Pre-filter first: sites whose every leg has been up longer than the
    # reporting window provably cannot have dropped inside it, so they are
    # exact without being contacted.
    targets, proven_zero, unchanged = select_sweep_targets(
        state, seen_rids, run_id, window_days=SWEEP_WINDOW_DAYS)
    progress("\n[Sweep] {} site(s) need their event log read.".format(len(targets)))
    progress("  Skipped {} (all legs up > {} d -- drops provably zero).".format(
        len(proven_zero), SWEEP_WINDOW_DAYS))
    progress("  Skipped {} (uptime and transition counters unchanged since last "
             "run -- no new drops possible).".format(len(unchanged)))

    sweep_stats = {"ok": 0, "unreachable": 0, "errors": 0,
                   "new_events": 0, "unparsed": 0}
    if targets:
        est = (len(targets) / float(max(1, SWEEP_WORKERS))) * 10.0
        progress("  Reading logs at {} concurrent session(s) "
                 "-- rough estimate {:.0f}s...".format(SWEEP_WORKERS, est))
        sweep_stats = _run_sweep(targets, username, password, state,
                                 SWEEP_WORKERS, new_events_by_rid, progress)
        progress("  Sweep done: {} ok, {} unreachable, {} error(s), "
                 "{} new drop event(s) recorded.".format(
                     sweep_stats["ok"], sweep_stats["unreachable"],
                     sweep_stats["errors"], sweep_stats["new_events"]))
        if sweep_stats["unparsed"]:
            progress("  [!] {} log entry(ies) could not be parsed -- "
                     "counts for those sites may be understated.".format(
                         sweep_stats["unparsed"]))

    # ── Aggregate: one row per site, after hubs AND sweep are complete ────
    results = _aggregate_results(state, seen_rids, new_events_by_rid, run_id,
                                 skipped_rids=proven_zero,
                                 unchanged_rids=unchanged)

    elapsed   = time.time() - t_start
    n_sites   = len({r["router_id"] for r in results})
    n_dual    = len({r["router_id"] for r in results if r["hub_count"] > 1})
    n_together = len({r["router_id"] for r in results
                      if r.get("legs_pattern") == "legs together"})
    progress("\n[Poll] Complete -- {} adjacency row(s) across {} unique site(s) "
             "in {:.0f}s ({} dual-homed).".format(
                 len(results), n_sites, elapsed, n_dual))
    if n_together:
        progress("  {} dual-homed site(s) have BOTH legs at matching uptime "
                 "-- one event took both, so look at site transport rather "
                 "than either hub.".format(n_together))

    # Surface port-level outages in the live log too, not just the report --
    # this is the finding that stops the NOC opening a ticket per EVC.
    for p in port_rollup(results):
        progress("  [!!] PORT EVENT {} {} -- {}/{} sub-interfaces DOWN ({:.0%}). "
                 "{}".format(
                     p["hub"], p["port"], p["down"], p["total"], p["down_share"],
                     "ONE ticket against the port, not per EVC."
                     if p["down_share"] >= 0.5 else
                     "Check for a common upstream cause."))

    # ── Resolve hostnames via NetBox for any still-unresolved router-IDs ─
    # A site is "unresolved" when its hostname equals its router-ID (bare IP).
    # Query NetBox IPAM every run -- fast, and catches renamed/new devices.
    unresolved = [r["router_id"] for r in results
                  if r["hostname"] == r["router_id"]]
    if unresolved:
        progress("\n[NetBox] Resolving {} hostname(s) via IPAM...".format(
            len(unresolved)))
        nb_resolved = _resolve_hostnames_netbox(nb, unresolved, progress)
        if nb_resolved:
            # Update in-memory results (used by report + xlsx this run)
            for r in results:
                if r["router_id"] in nb_resolved:
                    r["hostname"] = nb_resolved[r["router_id"]]
            # Cache in state so next run skips re-querying known hostnames
            for rid, hostname in nb_resolved.items():
                if rid in state:
                    state[rid]["hostname"] = hostname
            progress("  Resolved {}/{} hostname(s).".format(
                len(nb_resolved), len(unresolved)))

    # ── Save state ────────────────────────────────────────────────────────
    save_state(state)
    progress("[State] Saved ({} sites total).".format(len(state)))

    # ── Build text report (for progress log) ─────────────────────────────
    hub_names_polled = sorted(set(r["hub_1800"] for r in results))
    report_text = _build_report(results, hub_names_polled, stamp, elapsed)
    for line in report_text.splitlines():
        progress(line)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Write Excel report (primary deliverable) ──────────────────────────
    xlsx_path = os.path.join(OUTPUT_DIR, "OSPF_Stability_{}.xlsx".format(ts))
    out_path  = None
    progress("\n[Excel] Writing {}...".format(os.path.basename(xlsx_path)))
    if _build_xlsx(results, state, hub_names_polled, stamp, elapsed, xlsx_path):
        out_path = xlsx_path
        progress("  Written: {}".format(xlsx_path))
    else:
        # openpyxl unavailable -- fall back to text file
        progress("[!] openpyxl not installed.  Fix:  pip install openpyxl")
        progress("    Falling back to .txt output this run.")
        txt_path = os.path.join(OUTPUT_DIR, "OSPF_Stability_{}.txt".format(ts))
        try:
            with open(txt_path, "w") as f:
                f.write(report_text)
            out_path = txt_path
            progress("  Written: {}".format(txt_path))
        except Exception as e:
            progress("[!] Write failed: {}".format(e))

    if open_when_done and out_path and os.path.exists(out_path):
        try:
            os.startfile(out_path)
        except Exception:
            pass

    # ── Slack alert ───────────────────────────────────────────────────────
    if slack_alert:
        new_drop_results = [r for r in results if r["new_events"]]
        if new_drop_results:
            try:
                cfg = load_config_silent()
                webhook = cfg.get("SLACK_WEBHOOK", "")
            except Exception:
                webhook = ""
            if webhook:
                msg = _build_slack_alert(new_drop_results, stamp)
                ok, slack_msg = post_slack(webhook, msg)
                progress("[Slack] {}".format(slack_msg if ok else "FAILED: " + slack_msg))
            else:
                progress("[Slack] No webhook configured -- skipping alert.")
        else:
            progress("[Slack] No new drops this cycle -- no alert sent.")

    return out_path


# ── Config helper ─────────────────────────────────────────────────────────────
def _read_hub_filter(path=CONFIG_PATH):
    """Read HUB_NAME_FILTER from config file; default 'ces01'."""
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("HUB_NAME_FILTER="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return "ces01"


# ── CLI entrypoint ────────────────────────────────────────────────────────────
def collect_inputs(session=None):
    session = session or {}
    cfg = load_config()
    print("\n-- OSPF Stability Monitor --")

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

    slack_alert = (
        input("\nAlert Slack on new drops? (y/n) [y]: ").strip().lower()
        not in ("n", "no"))
    open_when_done = (
        input("Open report when done? (y/n) [n]: ").strip().lower()
        in ("y", "yes"))

    return {
        "username":       username,
        "password":       password,
        "infra_user":     username,
        "infra_pass":     password,
        "slack_alert":    slack_alert,
        "open_when_done": open_when_done,
    }


def main():
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
