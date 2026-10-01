#!/usr/bin/env python3
"""
npt1250_client.py -- NPT-1250 client (paramiko), subclass of BaseDeviceClient.

Ports the 1250 hop from Autotester_w_JIRA_v2.py. DATA COLLECTION ONLY --
returns the same structures the SecureCRT version produces.

Confirmed: NPT-1250 takes plain paramiko (no legacy flags), prompt ">",
paging "| no-more". Uses shared machinery from device_base.

Built by P. Capps.
"""

import re
import sys

import platform_paths

try:
    import paramiko  # noqa: F401  (imported by device_base; ensures clear error)
except ImportError:
    print("ERROR: paramiko not installed. Run:  pip install paramiko")
    sys.exit(1)

from device_base import BaseDeviceClient


class NPT1250Client(BaseDeviceClient):
    PROMPT_RE = re.compile(r'[\w.\-]+>\s*$')
    CONN_TIMEOUT = 30

    # ---- STEP 1: batch query (MUSE + hub + loopback) ----
    def query_batch(self, ds1_list):
        raw = self.run('show configuration | display-set | match "peer-address" | no-more')
        config_map = {}
        for line in raw.splitlines():
            m_iface = re.search(r'(ds1-ts\d+/\d+) ', line)
            m_muse  = re.search(r'(MUSE_\d+)', line)
            m_peer  = re.search(r'peer-address\s+([\d.]+)', line)
            if m_iface and m_muse and m_peer:
                config_map[m_iface.group(1)] = {
                    "muse_id": m_muse.group(1),
                    "hub_ip":  m_peer.group(1),
                }

        raw2 = self.run('show configuration | display-set | match "lo0" | no-more')
        loopback_ip = "unknown"
        for line in raw2.splitlines():
            m = re.search(r'lo0.*inet.*address\s+([\d.]+)', line, re.IGNORECASE)
            if m:
                loopback_ip = m.group(1)
                break

        ds1_map = {}
        errors  = {}
        for ds1 in ds1_list:
            key = "ds1-{}".format(ds1)
            if key in config_map:
                ds1_map[ds1] = {
                    "muse_id":     config_map[key]["muse_id"],
                    "hub_ip":      config_map[key]["hub_ip"],
                    "loopback_ip": loopback_ip,
                }
            else:
                errors[ds1] = "No MUSE assignment found for {}".format(ds1)
        return ds1_map, errors

    # ---- STEP 2: per-DS1 PMs ----
    def query_ds1_pms(self, ds1_list):
        results = {}
        for ds1 in ds1_list:
            full_ds1 = "ds1-{}".format(ds1)
            slot     = ds1.split("/")[0]
            ch_num   = ds1.split("/")[1]
            out = []
            out.append("  DS1: {}".format(full_ds1))
            out.append("")

            commands = [
                ("Physical Status",
                 'show interfaces physical status slot {} | grep "ds1/{} " | no-more'.format(slot, ch_num)),
                ("NE Alarms",
                 "show ne alarms | grep {} | no-more".format(full_ds1)),
                ("Physical PM 24hr",
                 "show interfaces physical pm 24hour {} | no-more".format(full_ds1)),
                ("CES PM 15min",
                 "show interfaces pm 15min {} ces | no-more".format(full_ds1)),
                ("CES PM 24hr",
                 "show interfaces pm 24hour {} ces | no-more".format(full_ds1)),
            ]

            for label, cmd in commands:
                raw = self.run(cmd)
                out.append("  >> {}".format(label))
                out.append("  CMD: {}".format(cmd))
                if "switch of this interface object is off" in raw:
                    out.append("    NOTE: CES PMs not enabled on this DS1.")
                    # Was a single `set ... pm-collection-enable` line, which
                    # is HALF the sequence -- pm-monitor-enable is also
                    # required, and nothing commits. Someone following it got
                    # no PMs and no error. Point at the full MOP instead of
                    # duplicating it here (2026-10-01).
                    out.append("    To enable: see the CES PMs DISABLED MOP "
                               "at the end of the report (2 set commands "
                               "+ commit confirm)")
                else:
                    for line in raw.splitlines():
                        clean = line.rstrip()
                        if clean:
                            out.append("    {}".format(clean))
                out.append("")
            results[ds1] = "\n".join(out)
        return results


# ============================================================
# STANDALONE TEST
# ============================================================
def main():
    import getpass, datetime
    print("=" * 60)
    print("  NPT-1250 CLIENT -- data-collection port test (base-class)")
    print("=" * 60)

    ip = input("1250 IP [10.111.131.67]: ").strip() or "10.111.131.67"
    ds1_raw = input("DS1(s) comma-sep [ts3/8]: ").strip() or "ts3/8"
    ds1_list = [d.strip() for d in ds1_raw.split(",") if d.strip()]

    user = input("1250 username: ").strip()
    pw   = getpass.getpass("1250 password: ")
    if not user or not pw:
        print("No credentials. Exiting.")
        return

    c = NPT1250Client(ip, user, pw)
    print("\n[connecting]")
    try:
        c.connect()
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return
    print("  host: {}".format(c.hostname))

    print("\n[STEP 1 -- batch query]")
    ds1_map, errors = c.query_batch(ds1_list)
    for ds1, info in ds1_map.items():
        print("  {} -> MUSE {} | hub {} | loopback {}".format(
            ds1, info["muse_id"], info["hub_ip"], info["loopback_ip"]))
    for ds1, err in errors.items():
        print("  {} -> ERROR: {}".format(ds1, err))

    print("\n[STEP 2 -- per-DS1 PMs]")
    pms = c.query_ds1_pms(ds1_list)
    for ds1, text in pms.items():
        print("-" * 50)
        print(text)

    c.close()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("NPT1250_porttest_{}_{}.txt".format(ts, c.hostname))
    lines = ["1250 PORT TEST -- {} ({})".format(c.hostname, ip), ""]
    for ds1, info in ds1_map.items():
        lines.append("{}: MUSE {} | hub {} | loopback {}".format(
            ds1, info["muse_id"], info["hub_ip"], info["loopback_ip"]))
    lines.append("")
    for ds1, text in pms.items():
        lines.append("=" * 50)
        lines.append(text)
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))


if __name__ == "__main__":
    main()
