#!/usr/bin/env python3
"""
npt1800_client.py -- NPT-1800 hub client (paramiko), subclass of BaseDeviceClient.

Ports query_1800_batch from Autotester_w_JIRA_v2.py. DATA COLLECTION ONLY.
Same NPT platform as the 1250: plain paramiko, ">" prompt, "| no-more" paging.

MUSE config lookup is SLOW -- but the cost is a per-CALL device-side config
render (~90-120s to render+match "show configuration | display-set" on a big
hub), NOT a per-DS1 cost. The original port issued that slow call once PER
DS1 (looping match "{muse_id}" per ds1), so an N-DS1 run paid the ~90-120s
render N times. Fixed (see _muse_lookup_batch): one render per HUB, matched
against a regex alternation of every muse_id needed this run (same pattern
npt1250_client.query_batch already uses for "peer-address" -- match once,
parse many). N DS1s on one hub now cost ~90-120s total instead of N*90-120s.

Built by P. Capps.
"""

import re
import sys

try:
    import paramiko  # noqa: F401
except ImportError:
    print("ERROR: paramiko not installed. Run:  pip install paramiko")
    sys.exit(1)

from device_base import BaseDeviceClient

HUB_IPS = {
    "10.111.132.255": {"name": "ATL-1800", "ip": "10.111.132.255"},
    "10.111.132.254": {"name": "ATL-1800", "ip": "10.111.132.254"},
    "10.111.130.255": {"name": "JFK-1800", "ip": "10.111.130.255"},
    "10.111.130.254": {"name": "ATL-1800", "ip": "10.111.130.254"},
    "10.111.131.255": {"name": "DFW-1800", "ip": "10.111.131.255"},
    "10.111.131.254": {"name": "DFW-1800", "ip": "10.111.131.254"},
    "10.111.135.255": {"name": "LAX-1800", "ip": "10.111.135.255"},
    "10.111.135.254": {"name": "LAX-1800", "ip": "10.111.135.254"},
}


class NPT1800Client(BaseDeviceClient):
    PROMPT_RE = re.compile(r'[\w.\-]+>\s*$')
    CONN_TIMEOUT = 90

    def __init__(self, ip, username, password):
        super().__init__(ip, username, password)
        self.loc_code = ""

    def _capture_hostname(self):
        super()._capture_hostname()
        m2 = re.search(r'(atl|jfk|dfw|lax)', self.hostname, re.IGNORECASE)
        if m2:
            self.loc_code = m2.group(1).lower()

    def _muse_lookup_batch(self, muse_ids, timeout):
        """
        One device-side config render+match for ALL muse_ids in this hub group,
        instead of one slow render per muse_id (see module docstring). Same
        "show configuration | display-set | match ..." source/data as before --
        only the batching changed, not what's being queried.

        Uses a regex alternation of every muse_id (Junos-style 'match' supports
        '|' alternation), each still suffixed with a trailing space to preserve
        the original exact-boundary behavior (so MUSE_100 can't match MUSE_1001).

        Returns {muse_id: matched_lines_text} -- same shape/content each
        muse_id's own per-DS1 call used to return, just computed together.
        """
        muse_ids = list(dict.fromkeys(muse_ids))  # de-dupe, preserve order
        if not muse_ids:
            return {}
        alt = "|".join("{} ".format(m) for m in muse_ids)
        raw = self.run(
            'show configuration | display-set | match "{}" | no-more'.format(alt),
            timeout)
        grouped = {m: [] for m in muse_ids}
        for line in raw.splitlines():
            for m in muse_ids:
                if "{} ".format(m) in line:
                    grouped[m].append(line)
                    break   # a line belongs to exactly one muse_id's group
        return {m: "\n".join(lines) for m, lines in grouped.items()}

    def query_batch(self, hub_ip, ds1_map):
        """Returns identical 7-tuple to SecureCRT query_1800_batch."""
        hub_info = HUB_IPS.get(hub_ip)
        if not hub_info:
            return {}, {}, "?", "?", "?", {
                ds1: "Hub IP {} not in HUB_IPS map".format(hub_ip)
                for ds1 in ds1_map
            }, {}

        # One render for the whole hub group (was: one render PER DS1).
        muse_lookup_timeout = 150
        # OC alarm/PM stat commands below are lighter (not a full-config
        # render) and already deduped per unique OC -- keep a separate,
        # smaller timeout for those.
        hub_timeout   = 60
        oc12_map      = {}
        oc12_stats    = {}
        peerless_cust = {}
        errors        = {}

        if not self.loc_code:
            raise RuntimeError(
                "Could not determine location from 1800 hostname '{}'".format(self.hostname))

        muse_ids = [info["muse_id"] for info in ds1_map.values()]
        muse_raw = self._muse_lookup_batch(muse_ids, muse_lookup_timeout)

        for ds1, info in ds1_map.items():
            muse_id = info["muse_id"]
            raw = muse_raw.get(muse_id, "")

            oc12 = None
            for line in raw.splitlines():
                if muse_id in line:
                    m = re.search(r'(oc\d*-ts\d+/\d+:\d+:\d+)', line)
                    if m:
                        oc12 = m.group(1)
                        break

            if oc12:
                oc12_map[ds1] = oc12
                if "customer Peerless" in raw:
                    mp = re.search(r'oc\d*-ts(\d+)/', oc12)
                    if mp:
                        peerless_cust[ds1] = mp.group(1)
            else:
                errors[ds1] = "Could not find OC interface for {} on {}".format(muse_id, hub_info["name"])
                continue

            oc12_base = oc12.split(":")[0]
            oc12_stats_key = "{}|{}".format(hub_ip, oc12_base)
            if oc12_stats_key not in oc12_stats:
                stat_lines = []
                _oc_m = re.match(r'oc(\d+)', oc12_base, re.IGNORECASE)
                _oc_type = "OC{}".format(_oc_m.group(1)) if _oc_m else "OC"
                stat_lines.append("  {}: {}  ({})".format(_oc_type, oc12_base, hub_info["name"]))
                stat_lines.append("  Hub IP: {}".format(hub_ip))
                stat_lines.append("")

                for label, cmd in [
                    ("OC Alarms",
                     "show ne alarms | match \"{}\" | no-more".format(oc12)),
                    ("OC Physical PM 15min",
                     "show interfaces physical pm 15min {} | no-more".format(oc12_base)),
                    ("OC Physical PM 24hr",
                     "show interfaces physical pm 24hour {} | no-more".format(oc12_base)),
                    ("OC CES PM 15min",
                     "show interfaces pm 15min {} ces | no-more".format(oc12)),
                    ("OC CES PM 24hr",
                     "show interfaces pm 24hour {} ces | no-more".format(oc12)),
                ]:
                    raw_stat = self.run(cmd, hub_timeout)
                    stat_lines.append("  >> {}".format(label))
                    stat_lines.append("  CMD: {}".format(cmd))
                    for line in raw_stat.splitlines():
                        clean = line.rstrip()
                        if clean:
                            stat_lines.append("    {}".format(clean))
                    stat_lines.append("")

                oc12_stats[oc12_stats_key] = "\n".join(stat_lines)

        return (oc12_map, oc12_stats, hub_info["name"], self.hostname,
                self.loc_code, errors, peerless_cust)


# ============================================================
# STANDALONE CONNECTION TEST -- interface_check.py commands
# (Does NOT exercise query_batch; in the pipeline the 1800 is driven by the
#  ds1_map handed over FROM the 1250 -- never entered by hand.)
# ============================================================
def main():
    import getpass
    print("=" * 60)
    print("  NPT-1800 CLIENT -- connection test (interface_check cmds)")
    print("=" * 60)

    hub_ip = input("1800 hub IP [10.111.131.254]: ").strip() or "10.111.131.254"
    iface  = input("Interface (e.g. xe-ts13/3.624): ").strip()
    if not iface or "." not in iface:
        print("Invalid interface -- must include a dot for the VLAN.")
        return
    nni  = iface.split(".")[0]
    vlan = iface.split(".")[1]

    user = input("1800 username: ").strip()
    pw   = getpass.getpass("1800 password: ")
    if not user or not pw:
        print("No credentials. Exiting.")
        return

    c = NPT1800Client(hub_ip, user, pw)
    print("\n[connecting]")
    try:
        c.connect()
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return
    print("  host: {}  loc: {}".format(c.hostname, c.loc_code))

    commands = [
        "show interfaces description | grep {}".format(iface),
        "show configuration interfaces {} unit {} | display-set".format(nni, vlan),
        "show configuration protocols | display-set | grep {} | except tunnel".format(vlan),
        "show ospf neighbor | grep {}".format(vlan),
        "show ldp adjacency | grep {}".format(vlan),
    ]

    captured = {}
    for cmd in commands:
        print("\n[cmd] {}".format(cmd))
        out = c.run(cmd, 30)
        captured[cmd] = out
        for line in out.splitlines():
            if line.rstrip():
                print("    {}".format(line.rstrip()))

    cfg = captured.get(
        "show configuration interfaces {} unit {} | display-set".format(nni, vlan), "")
    m = re.search(r"set family inet address (\d+\.\d+\.\d+\.\d+)/", cfg)
    if m:
        ip = m.group(1)
        octets = ip.split(".")
        octets[-1] = str(int(octets[-1]) - 1)
        dest = ".".join(octets)
        for cmd in [
            "ping {} count 4".format(dest),
            "ping {} count 4 size 8000 do-not-fragment".format(dest),
        ]:
            print("\n[cmd] {}".format(cmd))
            out = c.run(cmd, 30)
            for line in out.splitlines():
                if line.rstrip():
                    print("    {}".format(line.rstrip()))
    else:
        print("\n[note] No IP address found in interface config -- skipping ping.")

    c.close()
    print("\n[done] If commands returned output above, paramiko works on the 1800.")


if __name__ == "__main__":
    main()
