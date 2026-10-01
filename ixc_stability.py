#!/usr/bin/env python3
"""
ixc_stability.py -- IXC circuit stability monitor (BETA).

Sweeps every NPT-1250 in the fleet and tracks DS1 physical alarm state
(LOS / NoLOS) for every port on every CES slot.  Designed to catch
IXC-side failures that leave the 1250 fibre interface up but impair the
DS1 signal -- the primary failure class OSPF/LDP monitoring cannot see.

Approach
--------
Physical status is read-only hardware state.  No CES PM collection is
enabled or required.  The per-device cost is an SSH session plus one
`show interfaces physical status slot tsX | no-more` per CES slot
(fast; no full-config render).

All DS1 ports on all CES slots are tracked.  Unused ports in persistent
LOS never transition, so they never generate alerts after the initial
baseline run.

Transition classification
--------------------------
  NoLOS -> LOS   : new alarm (circuit likely impaired)
  LOS   -> NoLOS : restoration
  New port seen in any state : stored as baseline -- NOT an alert.
                   (May be an unprovisioned port.  State is established
                   on first contact; transitions are only reported on
                   subsequent runs.)

1250 discovery
--------------
Router-IDs (loopback IPs used as SSH targets) are loaded from
ospf_stability_state.json.  Run OSPF & LDP Stability at least once
before this tool to populate that list.

State persisted to: platform_paths.path("ixc_stability_state.json")
  Outer key  : OSPF router-ID (= 1250 loopback IP = SSH target)
  Inner keys : "tsX/N" port identifiers (e.g. "ts1/7")

BETA
----
Do not use for production monitoring yet.  Pending:
  - Confirmation of LOS/NoLOS transitions against a real IXC event
  - Filtering to MUSE-configured (active) DS1s only (reduces first-run
    noise from unprovisioned ports)
  - Hub-level NNI fallback for sites where the 1250 is unreachable

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
import ospf_stability   # for state-file path + hostname map


# ── Constants ──────────────────────────────────────────────────────────────────

OUTPUT_DIR = platform_paths.BASE_DIR
STATE_FILE = os.path.join(OUTPUT_DIR, "ixc_stability_state.json")

# ts2 / ts5 are GE fibre -- never DS1.  All other slots are CES candidates.
# Not every site has all of these populated; unpopulated slots return no
# parseable status lines and are skipped silently.
CES_SLOTS = ("ts1", "ts3", "ts4", "ts6", "ts7", "ts8")

# Concurrent SSH sessions.  Physical-status commands complete in seconds;
# 24 workers is safe and keeps total fleet runtime reasonable.
SWEEP_WORKERS = 24


# ── Tool contract ──────────────────────────────────────────────────────────────

TOOL_INFO = {
    'name':     'IXC Stability (BETA — DO NOT USE IN PRODUCTION)',
    'ran_from': 'All NPT-1250s (via OSPF Stability state)',
    'desc':     'Fleet-wide DS1 physical alarm sweep.  Tracks LOS/NoLOS state '
                'per active (MUSE-configured) DS1 and reports transitions '
                '(new alarms and restorations since the last run).  Detects '
                'IXC-side failures invisible to OSPF/LDP monitoring: the 1250 '
                'fibre interface stays up, but the DS1 signal is lost.  '
                'Unprovisioned ports (no MUSE peer-address) are excluded. '
                'Requires OSPF & LDP Stability to have run at least once '
                '(uses its state file for the 1250 site list).  State persists '
                'between runs; first run establishes baseline only.  '
                'BETA -- run locally for validation only.',
    'requires': 'infra creds',
    'order':    9,
}


# ── Parsing ────────────────────────────────────────────────────────────────────

# Matches lines from `show interfaces physical status slot tsX | no-more`.
# Confirmed format (FARGNDBC636, 2026-09-15):
#   ds1/1                       NoLOS
#   ds1/7                       LOS
# Note: ports are listed in lexicographic (not numeric) order by the device.
_STATUS_RE = re.compile(
    r'^\s*(ds1/(\d+))\s+(LOS|NoLOS)\s*$',
    re.MULTILINE | re.IGNORECASE,
)

# Matches MUSE peer-address config lines from:
#   show configuration | display-set | grep ds1 | grep "peer-address" | no-more
# Confirmed format (npt1250_client.py, autotester):
#   set interfaces ds1-ts1/7 unit 0 peer-address 10.111.132.255
_MUSE_RE = re.compile(
    r'\bds1-ts(\w+)/(\d+)\b',
    re.IGNORECASE,
)

# Command to list all active (MUSE-configured) DS1s.
# Full-config render: ~90-120 s on device.  Paid once per sweep per device.
MUSE_CMD = ('show configuration | display-set | grep ds1 '
            '| grep "peer-address" | no-more')


def parse_ds1_physical_status(raw, slot):
    """
    Parse output of 'show interfaces physical status slot tsX | no-more'.

    Returns dict mapping port key -> "LOS" or "NoLOS":
      { "ts1/1": "NoLOS", "ts1/7": "LOS", ... }

    Port key format: "{slot}/{port_number}" e.g. "ts1/7" for ds1/7 on ts1.
    Preserves device casing (LOS / NoLOS) as confirmed on live output.

    Returns {} if the output contains no recognised DS1 status lines --
    this is the expected result for an unpopulated or absent slot.
    """
    result = {}
    for m in _STATUS_RE.finditer(raw):
        port_num = m.group(2)   # "7" from "ds1/7"
        status   = m.group(3)   # "LOS" or "NoLOS" (device casing)
        key      = "{}/{}".format(slot, port_num)   # "ts1/7"
        result[key] = status
    return result


def parse_active_muse_ds1s(raw):
    """
    Parse output of the MUSE peer-address config query.

    Returns a set of port keys for DS1s that have an active MUSE tunnel:
      { "ts1/7", "ts3/20", "ts1/23", ... }

    A DS1 in this set is a live circuit -- it carries (or is configured to
    carry) customer traffic.  A DS1 NOT in this set is an unprovisioned port;
    LOS on such a port is expected and not an IXC fault.

    Returns an empty set if the output contains no matches -- caller treats
    this as "MUSE query unavailable, do not filter" (see _sweep_site).
    """
    keys = set()
    for m in _MUSE_RE.finditer(raw):
        slot_num = m.group(1)   # "1" from "ts1"
        port_num = m.group(2)   # "7" from "/7"
        keys.add("ts{}/{}".format(slot_num, port_num))
    return keys


# ── State I/O ──────────────────────────────────────────────────────────────────

def load_state():
    """Load IXC stability state from disk.  Returns {} on missing or corrupt."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    """Atomically write state to disk (write-then-rename)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ── Per-device sweep ───────────────────────────────────────────────────────────

def _sweep_site(rid, hostname, username, password, state, lock, stats, progress):
    """
    Connect to one NPT-1250 and record DS1 physical alarm state for all CES slots.

    Updates state[rid] under lock with:
      hostname      display name
      ds1_state     dict of port-key -> {state, since, baseline}
      transitions   list of transitions detected THIS run (not persisted --
                    rebuilt fresh each run from the state delta)
      sweep_status  "ok" | "unreachable" | "error"
      last_sweep    UTC ISO timestamp

    Transition rules
    ----------------
    - Port seen for the first time (any state): baseline=True, no transition.
    - Subsequent runs: if state changed, append to transitions; update since.
    - Ports that disappear (card removed?) are dropped from state silently.
      They will re-appear as baseline on the next run if the card returns.

    Slot errors (card absent, command rejected) do not abort the whole device;
    the slot is silently skipped and remaining slots are still queried.
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
            stats["unreachable"] += 1
        progress("  [!] {} ({}) unreachable: {}".format(hostname, rid, str(e)[:60]))
        return

    # ── MUSE config query -- which DS1s are provisioned (active circuits)? ──
    # A DS1 with a MUSE peer-address is a live circuit.  One without is an
    # empty port; LOS there is expected and not an IXC fault.
    #
    # If this query fails for any reason (timeout, command error, no output),
    # muse_keys is left as None -- _sweep_site falls back to tracking all
    # DS1s on that device for this run (noisier, but not wrong).
    #
    # Cost: full-config render on device, ~90-120 s.  Paid once per device.
    muse_keys = None
    try:
        muse_raw  = c.run(MUSE_CMD, timeout=150)
        muse_keys = parse_active_muse_ds1s(muse_raw)
    except Exception:
        pass   # fallback: no filtering

    # ── Physical status per CES slot ──────────────────────────────────────
    # Unpopulated slots produce no parseable lines; {} is returned and skipped.
    current_ds1 = {}
    try:
        for slot in CES_SLOTS:
            cmd = "show interfaces physical status slot {} | no-more".format(slot)
            try:
                raw    = c.run(cmd, timeout=30)
                parsed = parse_ds1_physical_status(raw, slot)
                current_ds1.update(parsed)
            except Exception:
                # Slot absent or command failed -- skip, continue to next slot.
                pass
    except Exception as e:
        with lock:
            site = state.setdefault(rid, {})
            site["sweep_status"] = "error"
            site["sweep_error"]  = str(e)[:120]
            site["hostname"]     = hostname
            stats["errors"] += 1
        progress("  [!] {} ({}) query error: {}".format(hostname, rid, str(e)[:60]))
        try:
            c.close()
        except Exception:
            pass
        return
    finally:
        try:
            c.close()
        except Exception:
            pass

    # ── Filter to active (MUSE-configured) DS1s only ──────────────────────
    # If muse_keys is None (query failed), skip filtering -- track everything.
    # If muse_keys is an empty set (device has no MUSE configs at all, which
    # would be unusual), also skip filtering rather than silently dropping all
    # ports: an empty result more likely means a parse miss than a truly bare
    # device.
    if muse_keys:
        current_ds1 = {k: v for k, v in current_ds1.items() if k in muse_keys}
    elif muse_keys is not None:
        # muse_keys == empty set: unexpected -- log and track all
        progress("  [??] {} -- MUSE query returned no configs; tracking all DS1s".format(
            hostname))

    # Compare current snapshot to stored state and detect transitions.
    with lock:
        prior_site = state.get(rid, {})
        prior_ds1  = prior_site.get("ds1_state", {})
        transitions = []
        new_ds1     = {}

        for port, status in current_ds1.items():
            if port in prior_ds1:
                old = prior_ds1[port]["state"]
                if status != old:
                    transitions.append({
                        "port": port,
                        "from": old,
                        "to":   status,
                        "at":   now_utc,
                    })
                new_ds1[port] = {
                    "state":    status,
                    "since":    now_utc if status != old
                                else prior_ds1[port].get("since", now_utc),
                    "baseline": False,
                }
            else:
                # First contact -- establish baseline, no alert.
                new_ds1[port] = {
                    "state":    status,
                    "since":    now_utc,
                    "baseline": True,
                }

        state[rid] = {
            "hostname":     hostname,
            "ds1_state":    new_ds1,
            "transitions":  transitions,
            "sweep_status": "ok",
            "last_sweep":   now_utc,
        }

        total = len(current_ds1)
        los   = sum(1 for s in current_ds1.values() if s == "LOS")
        stats["ok"] += 1
        stats["transitions"] += len(transitions)
        if transitions:
            stats["sites_with_changes"] += 1

        muse_note = "" if muse_keys else "  [MUSE unavail -- all DS1s tracked]"
        flag = "[!!]" if transitions else "[ OK]"
        progress("  {} {}  --  {}/{} active in LOS{}{}".format(
            flag, hostname, los, total,
            "  +{} transition(s)".format(len(transitions)) if transitions else "",
            muse_note,
        ))


# ── Report building ────────────────────────────────────────────────────────────

def _port_sort_key(key):
    """Sort port keys ("ts1/7") by slot then by port number (numeric)."""
    try:
        slot, num = key.rsplit("/", 1)
        return (slot, int(num))
    except ValueError:
        return (key, 0)


def build_report(state, stamp, elapsed):
    """
    Build the full text report from current state.

    Sections:
      1. Transitions this run  -- only non-baseline ports that changed
      2. Current LOS summary   -- all ports currently in LOS, marked if baseline
      3. Unreachable / error sites
      4. Summary statistics
    """
    lines = []
    sep   = "=" * 72
    lines += [
        "IXC Stability (BETA)  --  {}".format(stamp),
        sep,
        "",
    ]

    ok_sites  = [(rid, s) for rid, s in state.items()
                 if s.get("sweep_status") == "ok"]
    unr_sites = [(rid, s) for rid, s in state.items()
                 if s.get("sweep_status") == "unreachable"]
    err_sites = [(rid, s) for rid, s in state.items()
                 if s.get("sweep_status") == "error"]

    def _hn(s, rid):
        return s.get("hostname", rid)

    # ── 1. Transitions ──────────────────────────────────────────────────────
    changed = [(rid, s) for rid, s in ok_sites if s.get("transitions")]
    if changed:
        lines.append("*** DS1 TRANSITIONS THIS RUN ***")
        lines.append("")
        for rid, s in sorted(changed, key=lambda x: _hn(x[1], x[0])):
            lines.append("{:30s}  {}".format(_hn(s, rid), rid))
            for t in s["transitions"]:
                direction = "ALARM    " if t["to"] == "LOS" else "RESTORED "
                lines.append("    {}  {:10s}  {} -> {}  [{}]".format(
                    direction, t["port"], t["from"], t["to"], t["at"]))
            lines.append("")
    else:
        lines.append("No DS1 state transitions detected this run.")
        lines.append("")

    # ── 2. Current LOS summary ─────────────────────────────────────────────
    lines += ["", "--- CURRENT LOS (non-baseline) ---", ""]
    any_los = False
    for rid, s in sorted(ok_sites, key=lambda x: _hn(x[1], x[0])):
        ds1   = s.get("ds1_state", {})
        known_los = {k: v for k, v in ds1.items()
                     if v["state"] == "LOS" and not v.get("baseline")}
        if not known_los:
            continue
        any_los = True
        total = len(ds1)
        lines.append("{:30s}  {}  --  {}/{} in LOS".format(
            _hn(s, rid), rid, len(known_los), total))
        for port in sorted(known_los, key=_port_sort_key):
            since = known_los[port].get("since", "?")
            lines.append("    {}  since {}".format(port, since))
        lines.append("")

    if not any_los:
        lines.append("No confirmed LOS circuits (all known-LOS ports are baseline).")
        lines.append("")

    # Baseline LOS note (first run or new sites)
    baseline_los = {}
    for rid, s in ok_sites:
        bl = {k: v for k, v in s.get("ds1_state", {}).items()
              if v["state"] == "LOS" and v.get("baseline")}
        if bl:
            baseline_los[rid] = (s, bl)

    if baseline_los:
        lines += ["", "--- BASELINE LOS (first contact -- no prior state) ---", ""]
        lines.append("These ports were in LOS on first contact.")
        lines.append("They will generate ALARM transitions on the NEXT run if they clear.")
        lines.append("")
        for rid, (s, bl) in sorted(baseline_los.items(),
                                    key=lambda x: _hn(x[1][0], x[0])):
            lines.append("{:30s}  {}  --  {} baseline-LOS port(s)".format(
                _hn(s, rid), rid, len(bl)))
            for port in sorted(bl, key=_port_sort_key):
                lines.append("    {}".format(port))
            lines.append("")

    # ── 3. Unreachable / error ─────────────────────────────────────────────
    if unr_sites or err_sites:
        lines += ["", "--- SITES NOT REACHED ---", ""]
        for rid, s in sorted(unr_sites + err_sites,
                              key=lambda x: _hn(x[1], x[0])):
            reason = s.get("sweep_status", "?").upper()
            err    = s.get("sweep_error", "")
            lines.append("  {:30s}  {}  [{}]  {}".format(
                _hn(s, rid), rid, reason, err[:60]))
        lines.append("")

    # ── 4. Summary ─────────────────────────────────────────────────────────
    total_circuits = sum(
        len(s.get("ds1_state", {})) for _, s in ok_sites)
    total_known_los = sum(
        sum(1 for v in s.get("ds1_state", {}).values()
            if v["state"] == "LOS" and not v.get("baseline"))
        for _, s in ok_sites)
    total_transitions = sum(
        len(s.get("transitions", [])) for _, s in ok_sites)

    lines += [
        "", "--- SUMMARY ---", "",
        "Sites reachable:             {}".format(len(ok_sites)),
        "Sites unreachable:           {}".format(len(unr_sites)),
        "Sites error:                 {}".format(len(err_sites)),
        "Total DS1 ports seen:        {}".format(total_circuits),
        "Confirmed LOS (non-baseline):{}".format(total_known_los),
        "Transitions this run:        {}".format(total_transitions),
        "Elapsed:                     {:.0f}s".format(elapsed),
        "",
    ]

    return "\n".join(lines)


# ── Main entry point ───────────────────────────────────────────────────────────

def collect_inputs(session=None):
    """CLI input collection.  Not called from the web layer."""
    username       = input("Infrastructure username: ").strip()
    password       = getpass.getpass("Infrastructure password: ")
    open_when_done = input("Open report when done? (y/n): ").strip().lower() == "y"
    return {
        "username":       username,
        "password":       password,
        "open_when_done": open_when_done,
    }


def run(params, progress=print):
    """
    Execute one IXC stability sweep.

    params keys:
      username       -- infra SSH username
      password       -- infra SSH password
      open_when_done -- bool

    Returns out_path (str) or None on total failure.

    Flow:
      1. Load 1250 router-IDs from ospf_stability_state.json
      2. Load IXC stability state (prior DS1 snapshot)
      3. Sweep all 1250s concurrently (SWEEP_WORKERS threads)
      4. Compare current snapshot to prior state -> transitions
      5. Save updated state
      6. Write text report
    """
    username       = params["username"]
    password       = params["password"]
    open_when_done = params.get("open_when_done", False)

    stamp   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    t_start = time.time()

    # ── 1. Discover 1250s from OSPF Stability state ───────────────────────
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

    # Exclude the schema-version sentinel key (starts with "_")
    rids = sorted(k for k in ospf_state if not k.startswith("_"))
    if not rids:
        progress("[!] OSPF Stability state is empty.")
        progress("    Run 'OSPF & LDP Stability' at least once first.")
        return None

    progress("  {} site(s) found.".format(len(rids)))

    # ── 2. Build hostname map ─────────────────────────────────────────────
    progress("[Hostnames] Building loopback->hostname map...")
    lb_map = ospf_stability.build_loopback_hostname_map()
    progress("  {} mapping(s) found.".format(len(lb_map)))

    # ── 3. Load IXC stability state ───────────────────────────────────────
    progress("[State] Loading IXC Stability state...")
    state = load_state()
    progress("  {} site(s) in state file.".format(len(state)))

    # ── 4. Fleet sweep ────────────────────────────────────────────────────
    progress("\n[Sweep] {} site(s) at {} concurrent workers...".format(
        len(rids), SWEEP_WORKERS))

    stats = {
        "ok": 0, "unreachable": 0, "errors": 0,
        "transitions": 0, "sites_with_changes": 0,
    }
    lock = threading.Lock()
    cred_error = threading.Event()

    def _worker(rid):
        if cred_error.is_set():
            return
        hostname = lb_map.get(rid, rid)
        try:
            _sweep_site(rid, hostname, username, password,
                        state, lock, stats, progress)
        except CredentialError:
            cred_error.set()
            progress("[!!] Credential error -- halting sweep.  Check password.")

    # Use a semaphore to cap concurrency rather than submitting all at once.
    sem     = threading.Semaphore(SWEEP_WORKERS)
    threads = []

    def _bounded(rid):
        with sem:
            _worker(rid)

    for rid in rids:
        t = threading.Thread(target=_bounded, args=(rid,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    if cred_error.is_set():
        progress("[!] Sweep aborted after credential error.")
        return None

    progress("\n[Sweep] Done -- {} ok  {} unreachable  {} error(s)  "
             "{} transition(s) across {} site(s).".format(
                 stats["ok"], stats["unreachable"], stats["errors"],
                 stats["transitions"], stats["sites_with_changes"]))

    # ── 5. Save state ─────────────────────────────────────────────────────
    save_state(state)

    # ── 6. Write report ───────────────────────────────────────────────────
    elapsed  = time.time() - t_start
    report   = build_report(state, stamp, elapsed)
    ts_file  = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out_path = os.path.join(OUTPUT_DIR, "ixc_stability_{}.txt".format(ts_file))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    progress("\n[Done] Report: {}".format(out_path))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception:
            pass

    return out_path


if __name__ == "__main__":
    p = collect_inputs()
    run(p)
