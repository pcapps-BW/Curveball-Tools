#!/usr/bin/env python3
"""
hub_registry.py -- single source of truth for NPT-1800 hub identity.

WHY THIS MODULE EXISTS
----------------------
The same fact -- "which 1800 hub is at which management IP, and what is it
called" -- used to be encoded in TWO hardcoded tables:

    npt1800_client.HUB_IPS      IP   -> {name, ip}
    interface_check.SITE_TO_1800  SITE -> IP

On 2026-07-08 the mapping was verified live and `SITE_TO_1800` was corrected.
`HUB_IPS` was not. It kept `10.111.131.254` labelled "DFW-1800" when that box is
actually LAX (`ces01.lax1`), so for ~2 months every tool reading `HUB_IPS` for a
hub NAME reported LAX circuits as DFW. The IP used to connect was right, only the
label was wrong, so nothing ever failed loudly -- it surfaced on 2026-09-01 only
because a FULL T1 ROUTE showed "DFW-1800 (10.111.131.254)" feeding LAX1-MUX-02.

Two copies of one fact, one updated. This module removes the second copy.

AUTHORITY ORDER
---------------
1. THE DEVICE ITSELF -- once connected, the hub's own hostname (`ces01.lax1`)
   states where it is. Nothing can drift from that, so it always wins. Use
   name_from_hostname().
2. NETBOX -- needed for site -> IP, because we must know the IP *before* we can
   connect and ask. OSPF Stability already discovers hubs this way and has never
   been wrong about hub identity.
3. VERIFIED_HUBS BELOW -- fallback only, so a NetBox outage cannot take these
   tools offline. Used with a visible warning, never silently.

When NetBox and the fallback DISAGREE, that is exactly the drift this module
exists to prevent, so it is reported loudly rather than resolved quietly.

Built by P. Capps.
"""

import re
import time

try:
    from netbox_client import NetboxClient, load_netbox_config
    _NETBOX_IMPORT_OK = True
except Exception:                                   # pragma: no cover
    _NETBOX_IMPORT_OK = False

# ---------------------------------------------------------------------------
# THE ONE TABLE. Fallback only -- NetBox is authoritative.
#
# CORRECTED 2026-10-01. The previous values (10.111.130.254 / .130.255 /
# .131.254 / .131.255) were stale on ALL FOUR hubs and had drifted away from
# NetBox. circuit_audit.py reported the disagreement on every run:
#
#     DRIFT for ATL: NetBox says 10.111.132.255 but hub_registry fallback
#     says 10.111.130.254 -- one is stale, NetBox is authoritative
#
# It was harmless only because NetBox wins; with NetBox unreachable the suite
# would have fallen back to four wrong addresses. Exactly the drift-by-
# accretion failure this module was created to end, quietly reappearing in
# the fallback itself.
#
# The values below are DEVICE-CONFIRMED, not merely NetBox-sourced: the
# 2026-10-01 audit connected to each IP and the box reported the matching
# hostname off its own prompt (ces01.atl1 / .jfk1 / .dfw1 / .lax1), with
# "Name from: device hostname" on all four. That is the strongest evidence
# available -- stronger than NetBox alone.
# ---------------------------------------------------------------------------
VERIFIED_HUBS = {
    "ATL": {"ip": "10.111.132.255", "name": "ATL-1800", "hostname": "ces01.atl1"},
    "JFK": {"ip": "10.111.133.255", "name": "JFK-1800", "hostname": "ces01.jfk1"},
    "DFW": {"ip": "10.111.134.255", "name": "DFW-1800", "hostname": "ces01.dfw1"},
    "LAX": {"ip": "10.111.135.255", "name": "LAX-1800", "hostname": "ces01.lax1"},
    # Lab rig, added 2026-09-11 for testing on noe-util-01a -- not one of the
    # four production hub sites NetBox hub discovery returns, so this entry
    # ONLY ever comes from this hardcoded fallback, never from NetBox. IP is
    # a placeholder pending pcapps confirming the real value.
    "LAB": {"ip": "10.255.41.255", "name": "LAB-1800", "hostname": "ces01.lab3"},
}

# Legacy IPs that appeared in the old HUB_IPS table but are absent from the
# verified map and are NOT returned by NetBox hub discovery. Possibly MSA/MSB
# peers, possibly decommissioned. Retained so an unexpected connection still
# resolves to *something* rather than erroring, but the name is explicitly
# marked so it can never be quoted as fact.
LEGACY_IPS = {
    # The four addresses VERIFIED_HUBS carried until 2026-10-01. Retained, not
    # deleted: they may still be reachable second management interfaces, and
    # anything that resolves one should get a name rather than nothing. The
    # "(stale map)" suffix makes it obvious in a report where the name came
    # from, so one of these appearing is a signal rather than a silent answer.
    "10.111.130.254": "ATL-1800 (stale map -- confirm)",
    "10.111.130.255": "JFK-1800 (stale map -- confirm)",
    "10.111.131.254": "LAX-1800 (stale map -- confirm)",
    "10.111.131.255": "DFW-1800 (stale map -- confirm)",
    # Second addresses in each production range, never device-confirmed.
    "10.111.132.254": "ATL-1800 (unverified)",
    "10.111.133.254": "JFK-1800 (unverified)",
    "10.111.134.254": "DFW-1800 (unverified)",
    "10.111.135.254": "LAX-1800 (unverified)",
}

HUB_NAME_FILTER = "ces01"        # NetBox device-name filter for 1800 hubs
_SITE_RE = re.compile(r'(atl|jfk|dfw|lax|lab)', re.IGNORECASE)
_CACHE_TTL_S = 900              # 15 min; a process rarely lives that long anyway

# module-level cache: {"hubs": {SITE: {...}}, "at": epoch, "source": str}
_cache = {"hubs": None, "at": 0.0, "source": "", "warnings": []}


# ---------------------------------------------------------------------------
# Authority 1 -- the device's own hostname
# ---------------------------------------------------------------------------
def site_from_hostname(hostname):
    """
    'ces01.lax1' -> 'LAX'. Returns None if no site token is present.

    This is the most trustworthy signal available: the box is telling us where
    it is. Prefer it over any map whenever a connection has been established.
    """
    m = _SITE_RE.search(hostname or "")
    return m.group(1).upper() if m else None


def name_from_hostname(hostname):
    """
    'ces01.lax1' -> 'LAX-1800'. Returns None if the site can't be derived.

    Preferred over ip_to_name(): derived from the device itself, so it cannot
    disagree with reality.
    """
    site = site_from_hostname(hostname)
    return "{}-1800".format(site) if site else None


# ---------------------------------------------------------------------------
# Authority 2 -- NetBox
# ---------------------------------------------------------------------------
def _load_from_netbox():
    """
    Build {SITE: {ip, name, hostname}} from NetBox.
    Returns (hubs_dict, error_string). hubs_dict is {} when unavailable.
    """
    if not _NETBOX_IMPORT_OK:
        return {}, "netbox_client import unavailable"
    try:
        url, token = load_netbox_config()
        nb = NetboxClient(url, token)
        if not nb.ok():
            return {}, "NetBox not configured (NETBOX_URL / NETBOX_TOKEN)"
        hostnames = nb.hub_1800_devices(name_contains=HUB_NAME_FILTER)
        if not hostnames:
            return {}, "NetBox returned no devices matching '{}'".format(HUB_NAME_FILTER)
        hubs = {}
        for hn in sorted(hostnames):
            site = site_from_hostname(hn)
            if not site:
                continue
            slots = nb.hub_1800_mgmt_ips(hn) or {}
            ip = None
            for key in ("MSA", "MSB"):
                ips = slots.get(key) or []
                if ips:
                    ip = ips[0]
                    break
            if not ip:
                continue
            # First hostname wins per site (sorted for determinism)
            hubs.setdefault(site, {"ip": ip, "name": "{}-1800".format(site),
                                   "hostname": hn})
        if not hubs:
            return {}, "NetBox devices found but no MGMT MCP SLOT_MS* IPs resolved"
        return hubs, None
    except Exception as e:
        return {}, "{}: {}".format(type(e).__name__, e)


def _drift_warnings(nb_hubs):
    """
    Compare NetBox against VERIFIED_HUBS and report every disagreement.

    This is the check that would have caught the original bug. A mismatch is not
    silently resolved in either direction -- both values are surfaced so a human
    decides which table is stale.
    """
    warns = []
    for site, fb in sorted(VERIFIED_HUBS.items()):
        nb = nb_hubs.get(site)
        if not nb:
            warns.append("NetBox has no hub for site {} (fallback says {} / {})"
                         .format(site, fb["ip"], fb["name"]))
            continue
        if nb["ip"] != fb["ip"]:
            warns.append("DRIFT for {}: NetBox says {} but hub_registry fallback "
                         "says {} -- one is stale, NetBox is authoritative"
                         .format(site, nb["ip"], fb["ip"]))
    for site in sorted(set(nb_hubs) - set(VERIFIED_HUBS)):
        warns.append("NetBox knows site {} ({}) which is absent from the "
                     "hub_registry fallback -- consider adding it"
                     .format(site, nb_hubs[site]["ip"]))
    return warns


def refresh(force=False):
    """
    Populate the cache. Returns (hubs, source, warnings).

    source is 'netbox' or 'fallback'. warnings is a list of human-readable
    strings -- drift between NetBox and the fallback, or the reason NetBox was
    unusable. Callers should surface these; they are how stale data gets noticed.
    """
    now = time.time()
    if not force and _cache["hubs"] is not None and \
            (now - _cache["at"]) < _CACHE_TTL_S:
        return _cache["hubs"], _cache["source"], _cache["warnings"]

    nb_hubs, err = _load_from_netbox()
    if nb_hubs:
        hubs, source = dict(nb_hubs), "netbox"
        warnings = _drift_warnings(nb_hubs)
        # Keep fallback-only sites reachable rather than losing them outright.
        for site, fb in VERIFIED_HUBS.items():
            hubs.setdefault(site, dict(fb))
    else:
        hubs = {s: dict(v) for s, v in VERIFIED_HUBS.items()}
        source = "fallback"
        warnings = ["Using hardcoded hub map -- NetBox unavailable ({}). "
                    "Values were verified 2026-07-08 but may have drifted."
                    .format(err or "unknown reason")]

    _cache.update({"hubs": hubs, "at": now, "source": source,
                   "warnings": warnings})
    return hubs, source, warnings


# ---------------------------------------------------------------------------
# Public lookups
# ---------------------------------------------------------------------------
def sites():
    """Sorted list of valid site codes, e.g. ['ATL','DFW','JFK','LAX']."""
    hubs, _, _ = refresh()
    return sorted(hubs)


def site_to_ip(site):
    """
    'LAX' -> ('10.111.135.255', 'netbox'|'fallback', [warnings]).
    Returns (None, source, warnings) for an unknown site.
    """
    hubs, source, warns = refresh()
    hub = hubs.get((site or "").strip().upper())
    return (hub["ip"] if hub else None), source, warns


def ip_to_name(ip):
    """
    '10.111.135.255' -> ('LAX-1800', 'netbox'|'fallback'|'legacy', [warnings]).

    Prefer name_from_hostname() when a connection exists -- the device is a
    better authority than any table.
    """
    hubs, source, warns = refresh()
    ip = (ip or "").strip()
    for site, hub in hubs.items():
        if hub["ip"] == ip:
            return hub["name"], source, warns
    if ip in LEGACY_IPS:
        return LEGACY_IPS[ip], "legacy", warns + [
            "{} is a legacy IP absent from NetBox and the verified map -- name "
            "is unverified".format(ip)]
    return None, source, warns


def hub_info(site):
    """Full record for a site, or None. {'ip','name','hostname'}."""
    hubs, _, _ = refresh()
    return hubs.get((site or "").strip().upper())


def describe_source():
    """One-line provenance string, for report headers and progress logs."""
    _, source, warns = refresh()
    txt = {"netbox": "hub map from NetBox (authoritative)",
           "fallback": "hub map from hardcoded fallback -- NetBox unavailable"}.get(
               source, "hub map source: {}".format(source))
    if warns:
        txt += "  [{} warning(s)]".format(len(warns))
    return txt


# ---- Backward-compatible views (built from the ONE table above) ------------
def build_hub_ips():
    """
    IP -> {'name','ip'}, the shape npt1800_client.HUB_IPS used to have.
    Includes the legacy IPs so existing lookups don't start returning None.
    """
    hubs, _, _ = refresh()
    out = {h["ip"]: {"name": h["name"], "ip": h["ip"]} for h in hubs.values()}
    for ip, name in LEGACY_IPS.items():
        out.setdefault(ip, {"name": name, "ip": ip})
    return out


def build_site_to_ip():
    """SITE -> IP, the shape interface_check.SITE_TO_1800 used to have."""
    hubs, _, _ = refresh()
    return {s: h["ip"] for s, h in hubs.items()}


if __name__ == "__main__":
    hubs, source, warns = refresh(force=True)
    print("source: {}".format(source))
    for s in sorted(hubs):
        h = hubs[s]
        print("  {:<4} {:<16} {:<22} {}".format(
            s, h["ip"], h["name"], h.get("hostname", "")))
    if warns:
        print("\nwarnings:")
        for w in warns:
            print("  [!] {}".format(w))
    else:
        print("\nno drift between NetBox and the hardcoded fallback.")
    print("\nname_from_hostname('ces01.lax1') ->", name_from_hostname("ces01.lax1"))
    print("ip_to_name('10.111.135.255')      ->", ip_to_name("10.111.135.255")[0])
