#!/usr/bin/env python3
"""
mux4216_client.py -- Cisco NCS 4216 MUX client (paramiko), subclass of
BaseDeviceClient.

The MUX needs LEGACY algorithms re-enabled (confirmed via ssh -vv):
    kex     diffie-hellman-group14-sha1   (still in paramiko)
    hostkey ssh-rsa                        (still in paramiko -- NOT the removed DSA)
    cipher  aes###-ctr / mac hmac-sha2/sha1 (modern enough, default-compatible)
So unlike the GSX, the MUX works with paramiko -- we just pin the two legacy algos.

Prompt ends "#". Paging disabled with "terminal length 0" (sent post-login).
Has an authorization banner before the prompt (base reader handles it).

This file currently provides the CLIENT + a connection test. The full
query_4216_batch port (ACR/Netbox reconciliation) is the next step once the
transport is validated.

Built by P. Capps.
"""

import re
import sys

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko not installed. Run:  pip install paramiko")
    sys.exit(1)

from device_base import BaseDeviceClient


class MUX4216Client(BaseDeviceClient):
    PROMPT_RE = re.compile(r'[\w.\-]+#\s*$')   # MUX prompt ends with "#"
    CONN_TIMEOUT = 30

    def _connect_transport(self, conn_timeout):
        """
        Cisco NCS 4216 offers only legacy kex (dh-group14-sha1) and ssh-rsa
        hostkey. Build a Transport with disabled_algorithms={} so nothing is
        pre-disabled, then pin the legacy set paramiko still supports.
        """
        import socket
        sock = socket.create_connection((self.ip, 22), timeout=min(conn_timeout, 30))
        transport = paramiko.Transport(sock, disabled_algorithms={})
        try:
            so = transport.get_security_options()
            so.kex = ("diffie-hellman-group14-sha1",)
            so.key_types = ("ssh-rsa",)
            # ciphers/macs left at paramiko defaults (MUX offers aes-ctr +
            # hmac-sha2/sha1, all of which paramiko supports by default).
        except Exception as e:
            transport.close()
            raise RuntimeError(
                "MUX legacy-algo setup failed: {} (paramiko {})".format(
                    e, getattr(paramiko, "__version__", "?")))

        transport.start_client(timeout=min(conn_timeout, 30))
        transport.auth_password(self.username, self.password)

        self._transport = transport
        self.client = paramiko.SSHClient()
        # keep a handle so close() works; attach the live transport
        self.client._transport = transport

    def _open_shell(self):
        # Open the channel directly from the manually-built transport.
        chan = self._transport.open_session(timeout=self.CONN_TIMEOUT)
        chan.get_pty()
        chan.invoke_shell()
        return chan

    def _post_login(self):
        # Disable paging for the session (session-only, non-persistent).
        self.run("terminal length 0")

    # ---- query_4216_batch port (ACR + Netbox reconciliation) ----
    @staticmethod
    def _parse_oc12(oc12):
        m = re.match(r'oc\d*-(ts\d+)/(\d+):(\d+):(\d+)', oc12)
        if not m:
            raise RuntimeError("Cannot parse OC interface: {}".format(oc12))
        return {"ts_slot": m.group(1), "port": m.group(2),
                "sts": m.group(3), "ds1": m.group(4)}

    def get_connection_name(self, acr_num, cem_group):
        """
        Find the xconnect connection name for a CEM-ACR / cem-group pair.
        Runs 'show run | include CEM-ACR{acr}' and parses for the matching
        cem_group on the connect line.
        Returns the connection name string (e.g. 'CB_OC3_STS02_02') or None.
        """
        raw = self.run("show run | include CEM-ACR{}".format(acr_num))
        cem_group_str = str(cem_group)
        for line in raw.splitlines():
            # connect CB_OC3_STS02_02 CEM-ACR53 202 CEM8/0/4 102
            m = re.match(
                r'\s*connect\s+(\S+)\s+CEM-ACR\d+\s+(\d+)',
                line, re.IGNORECASE)
            if m and m.group(2) == cem_group_str:
                return m.group(1)
        return None

    def query_cem_health(self, acr_num, cem_group, conn_name):
        """
        Pull CEM circuit health for a resolved ACR / cem-group (item 14).
        Runs:
          show cem circuit interface cem-acr {acr} {cem_group}
          show xconnect all | include CE{acr}:{cem_group}(
          show connection name {conn_name}
          show acr group {acr} detail cem | include CE{acr}|{cem_group}
        Returns a dict with raw text blobs + parsed health flags.
        """
        result = {}

        # CEM circuit counters (per-circuit health)
        raw_cem = self.run(
            "show cem circuit interface cem-acr {} {}".format(acr_num, cem_group))
        result["cem_circuit_raw"] = raw_cem

        # Parse key health indicators (non-zero = problem)
        cem_flags = []
        for field in ["Pkts Missing", "Failure Counts",
                      "Generated Lbits", "Received Lbits",
                      "Generated Rbits", "Received Rbits"]:
            m = re.search(
                r'{}\s*:\s*([1-9]\d*)'.format(re.escape(field)), raw_cem)
            if m:
                cem_flags.append("{}: {}".format(field, m.group(1)))
        result["cem_flags"] = cem_flags

        m_line = re.search(r'Line:\s*(\S+)', raw_cem, re.IGNORECASE)
        result["cem_line"] = m_line.group(1).rstrip(",") if m_line else "?"
        m_ckt = re.search(r'Ckt:\s*(\S+)', raw_cem, re.IGNORECASE)
        result["cem_ckt"] = m_ckt.group(1).rstrip(",") if m_ckt else "?"

        # Xconnect leg states (literal backslash-paren is the IOS include filter
        # regex to match a literal '(' -- prevents partial matches)
        raw_xc = self.run(
            r'show xconnect all | include CE{}:{}\('.format(acr_num, cem_group))
        result["xconnect_raw"] = raw_xc
        result["xconnect_ok"] = (
            bool(raw_xc.strip()) and "DOWN" not in raw_xc.upper())

        # Connection binding state
        if conn_name:
            raw_conn = self.run("show connection name {}".format(conn_name))
            result["connection_raw"] = raw_conn
            m_state = re.search(r'Current State:\s*(\S+)', raw_conn)
            result["connection_state"] = m_state.group(1) if m_state else "?"
        else:
            result["connection_raw"] = ""
            result["connection_state"] = "?"

        # ACR working / protect state for this circuit
        raw_acr = self.run(
            "show acr group {} detail cem | include CE{}|{}".format(
                acr_num, acr_num, cem_group))
        result["acr_detail_raw"] = raw_acr

        return result

    def find_acr_by_description(self, hub_hostname, slot_port):
        """
        Scan 'show run | section controller' for a SONET-ACR controller whose
        description contains '{hub_hostname} {slot_port}'
        (e.g. 'ces01.lax1 ts3/1').  Comparison is case-insensitive.

        Returns the ACR number string (e.g. '2') if found, else None.
        Used as a Tier-2 fallback when Netbox has no OC->SONET mapping.
        """
        raw = self.run("show run | section controller")
        target = "{} {}".format(hub_hostname, slot_port).lower()
        current_acr = None
        for line in raw.splitlines():
            m_ctrl = re.match(r'\s*controller\s+SONET-ACR\s+(\S+)', line, re.IGNORECASE)
            if m_ctrl:
                current_acr = m_ctrl.group(1).rstrip(",")
                continue
            m_desc = re.match(r'\s*description\s+(.*)', line, re.IGNORECASE)
            if m_desc and current_acr is not None:
                if target in m_desc.group(1).strip().lower():
                    return current_acr
        return None

    def query_batch(self, mux_name, mux_ip, ds1_oc12_map, ds1_hostname_map, netbox):
        """
        Port of query_4216_batch. Netbox-first SONET resolution (ts3-bug fix).
        Returns (mux_results, t3_stats, mux_name, mux_ip, errors).
        `netbox` is a NetboxClient (or None -> legacy ACR-guess w/ UNVERIFIED flag).
        """
        mux_results = {}
        t3_stats    = {}
        errors      = {}
        sonet_pm_cache = {}   # active_sonet iface -> raw "show controllers sonet" text
                              # (dedupe: multiple DS1s often share the same SONET path)

        # Pull ACR groups once
        raw_acr = self.run("show acr group")
        acr_groups = []
        # The "Currently Active" column is OPTIONAL. An ACR that is not
        # currently switched to either leg shows "N/A" there:
        #
        #   16   SONET 0/7/0   SONET 0/12/0   N/A   N/A
        #
        # The old pattern required three SONET interfaces, so a row like that
        # failed to match and the ENTIRE ACR group vanished -- working and
        # protect both invisible, and any circuit on it failing with "not
        # found in any ACR column" despite being plainly present. Its working
        # and protect legs are valid and usable; only the active leg is
        # unknown, and "unknown" is recorded as "" rather than guessed.
        #
        # Rows with no interfaces at all (a deleted group, or one showing only
        # N/A) still do not match, correctly -- there is nothing to record.
        for line in raw_acr.splitlines():
            m = re.match(r'\s*(\d+)\s+SONET\s+([\d/]+)\s+SONET\s+([\d/]+)'
                         r'(?:\s+SONET\s+([\d/]+))?', line)
            if m:
                acr_groups.append({
                    "acr": m.group(1), "working": m.group(2),
                    "protect": m.group(3), "active": m.group(4) or "",
                })
        if not acr_groups:
            raise RuntimeError("Could not parse ACR groups from {}".format(mux_name))

        for ds1, oc12 in ds1_oc12_map.items():
            try:
                parts = self._parse_oc12(oc12)
                sts   = parts["sts"]
                ds1_n = parts["ds1"]
                expected_cem = "{}{}".format(int(sts), str(int(ds1_n)).zfill(2))

                # ---- NETBOX-FIRST SONET RESOLUTION (ts3-bug fix) ----
                hostname_1800 = ds1_hostname_map.get(ds1, "")
                oc_base = oc12.split(":")[0]
                netbox_sonet = netbox_mux = None
                if netbox is not None:
                    netbox_sonet, netbox_mux = netbox.oc_to_sonet(hostname_1800, oc_base)

                netbox_conflict = None
                acr_num = cem_group = vtg_num = t1_num = active_sonet = None
                # Initialised here, not only inside the NetBox branch:
                # the desc-ACR and legacy paths also reach the SONET-PM
                # step below, where an unbound name would be a NameError
                # on exactly the failure paths that matter most.
                matched_acr = nb_leg = None

                # Ambiguous Netbox match -> treat like a failed lookup (legacy
                # guess) but say WHY, so it's distinguishable from "not found".
                nb_ambiguous = (netbox_sonet == "__AMBIGUOUS__")
                if nb_ambiguous:
                    netbox_sonet = None

                if netbox_sonet:
                    # Match on EITHER leg. An ACR group is defined by its
                    # working AND protect interfaces -- both identify the same
                    # group -- and NetBox may legitimately have recorded either.
                    #
                    # Confirmed on DFW1-MUX-04 2026-10-01: NetBox had SONET
                    # 0/13/1 for oc12-ts1/1, which is ACR 26's PROTECT leg
                    # (working 0/7/1). Matching the working column only, the
                    # lookup failed and the whole DS1 was marked FAIL with
                    # "not found in any ACR working column" -- even though the
                    # ACR was right there and the circuit was up.
                    for acr in acr_groups:
                        if acr["working"] == netbox_sonet:
                            matched_acr, nb_leg = acr, "working"
                            break
                        if acr["protect"] == netbox_sonet:
                            matched_acr, nb_leg = acr, "protect"
                            break
                    if not matched_acr:
                        raise RuntimeError(
                            "NETBOX/MUX MISMATCH: Netbox SONET {} not found in any "
                            "ACR working OR protect column on {}. Cannot verify "
                            "path -- marking FAIL (not guessing).".format(
                                netbox_sonet, mux_name))

                    acr_num      = matched_acr["acr"]
                    active_sonet = matched_acr["active"]

                    # "Are we on protection?" is active != WORKING. It is NOT
                    # "active != whatever NetBox recorded" -- that was the old
                    # test, and it gets this exact case backwards: NetBox had
                    # the protect leg and the circuit was running on protect,
                    # so the two matched and no flag was raised. The device's
                    # own working/active columns are the only correct basis.
                    notes = []
                    if active_sonet and matched_acr["working"] and \
                            active_sonet != matched_acr["working"]:
                        notes.append(
                            "ON PROTECT: ACR {} is running on {} (protect); "
                            "working leg {} is not carrying the circuit"
                            .format(acr_num, active_sonet,
                                    matched_acr["working"]))
                    if nb_leg == "protect":
                        # Worth saying: NetBox has the protect interface
                        # recorded for this OC. Not an error -- the ACR is the
                        # same either way -- but it means NetBox is describing
                        # the standby path, which is easy to misread as the
                        # working one.
                        notes.append(
                            "Netbox records SONET {} for this OC, which is "
                            "ACR {}'s PROTECT leg (working {})".format(
                                netbox_sonet, acr_num, matched_acr["working"]))
                    if notes:
                        netbox_conflict = "; ".join(
                            ([netbox_conflict] if netbox_conflict else []) + notes)

                    raw_sts = self.run(
                        "show run controller SONET-ACR {} | section sts-1 {}".format(acr_num, sts))
                    for line in raw_sts.splitlines():
                        if "cem-group" in line:
                            mc = re.search(r'vtg\s+(\d+)\s+t1\s+(\d+)\s+cem-group\s+(\d+)', line)
                            if mc:
                                cg = mc.group(3)
                                if cg == expected_cem or cg == str(int(sts) * 100 + int(ds1_n)):
                                    cem_group = cg
                                    vtg_num   = mc.group(1)
                                    t1_num    = mc.group(2)
                                    break
                    if not cem_group:
                        cem_group = expected_cem
                else:
                    # ---- TIER 2: MUX controller description scan ----
                    # Each SONET-ACR controller has a description like
                    # 'ces01.lax1 ts3/1' that identifies which 1800 OC port
                    # it connects to.  When Netbox has no OC->SONET mapping
                    # (not-yet-populated circuit), we scan those descriptions
                    # to pinpoint the right ACR without Netbox.
                    # Not attempted when Netbox returned AMBIGUOUS (multiple
                    # OC matches on the device -- the description won't help).
                    desc_acr = None
                    if not nb_ambiguous:
                        _m = re.search(r'(ts\d+/\d+)', oc_base)
                        if _m:
                            desc_acr = self.find_acr_by_description(
                                hostname_1800, _m.group(1))

                    if desc_acr:
                        _acr_entry = next(
                            (a for a in acr_groups if a["acr"] == desc_acr), None)
                        if _acr_entry:
                            acr_num      = _acr_entry["acr"]
                            active_sonet = _acr_entry["active"]
                            raw_sts = self.run(
                                "show run controller SONET-ACR {} | section sts-1 {}".format(
                                    acr_num, sts))
                            for line in raw_sts.splitlines():
                                if "cem-group" in line:
                                    mc = re.search(
                                        r'vtg\s+(\d+)\s+t1\s+(\d+)\s+cem-group\s+(\d+)', line)
                                    if mc:
                                        cg = mc.group(3)
                                        if cg == expected_cem or \
                                                cg == str(int(sts) * 100 + int(ds1_n)):
                                            cem_group = cg
                                            vtg_num   = mc.group(1)
                                            t1_num    = mc.group(2)
                                            break
                            if not cem_group:
                                cem_group = expected_cem
                            netbox_conflict = (
                                "WARNING: Netbox not populated -- MUX description "
                                "fallback used for {} (SONET-ACR {}). Route resolved. "
                                "Update Netbox when available.".format(oc_base, acr_num))
                        else:
                            desc_acr = None   # ACR not in acr_groups; fall through

                    if not desc_acr:
                        # ---- TIER 3: legacy ACR-guess (shotgun all ACR groups) ----
                        if nb_ambiguous:
                            netbox_conflict = ("WARNING: Netbox has multiple interfaces "
                                               "matching {} -- path UNVERIFIED (legacy ACR-guess "
                                               "used)".format(oc_base))
                        else:
                            netbox_conflict = ("WARNING: Netbox lookup failed for {} -- "
                                               "path is UNVERIFIED (legacy ACR-guess used)".format(oc_base))
                        for acr in acr_groups:
                            raw_sts = self.run(
                                "show run controller SONET-ACR {} | section sts-1 {}".format(
                                    acr["acr"], sts))
                            for line in raw_sts.splitlines():
                                if "cem-group" in line:
                                    mc = re.search(
                                        r'vtg\s+(\d+)\s+t1\s+(\d+)\s+cem-group\s+(\d+)', line)
                                    if mc:
                                        cg = mc.group(3)
                                        if cg == expected_cem or \
                                                cg == str(int(sts) * 100 + int(ds1_n)):
                                            acr_num      = acr["acr"]
                                            cem_group    = cg
                                            vtg_num      = mc.group(1)
                                            t1_num       = mc.group(2)
                                            active_sonet = acr["active"]
                                            break
                            if cem_group:
                                break
                        if not cem_group:
                            # NOT FOUND -- say so. This used to assign
                            # acr_groups[0], i.e. whichever ACR happened to
                            # parse first, and the report then printed that
                            # ACR number and its active SONET path as if they
                            # had been determined. A confident wrong ACR and
                            # SONET path is the LAXGSX04 failure (see the
                            # comment block below), just in a different field.
                            #
                            # cem_group keeps expected_cem because that is
                            # ARITHMETIC from the OC assignment (STS + DS1),
                            # not a guess -- it is what we were looking for.
                            # acr_num and active_sonet stay None, which the
                            # route block renders as "NONE FOUND".
                            cem_group    = expected_cem
                            acr_num      = None
                            active_sonet = None
                            _nf = ("CEM {} not found in any ACR group on {} "
                                   "-- ACR and active SONET path UNDETERMINED "
                                   "(not guessed)".format(expected_cem, mux_name))
                            netbox_conflict = (
                                "{}; {}".format(netbox_conflict, _nf)
                                if netbox_conflict else "WARNING: " + _nf)

                # ---- xconnect -> GSX CE interface + CEM id ----
                #
                # Three bugs lived here, and together they returned a DIFFERENT
                # circuit's endpoint. Observed 2026-09-01 on SLKCUTMA660 ts1/25:
                # emitted LAXGSX04 / 11-2-28, actual LAXGSX01 / 12-2-12.
                #
                #   1. The old inner `break` exited only the INNER loop, so every
                #      later matching line OVERWROTE the earlier result -- the LAST
                #      match won, not the first. Never intended.
                #   2. `if "CE{acr}:{cem}" in line` is an unanchored substring:
                #      "CE16:1120" contains "CE16:112", so a neighbouring circuit
                #      matches. Same flaw in the device-side `| i {acr}:{cem}`.
                #   3. With (1) and (2) combined, several lines matched and the
                #      last one's endpoint (8/2/10:128) was used. 128 % 100 = 28,
                #      which is exactly the wrong T1 that was reported; the true
                #      CEM id was 112 -> 112 % 100 = 12, the correct T1.
                #
                # Now: match the CE token anchored (no trailing digit), collect
                # every distinct candidate, and refuse to pick silently when more
                # than one survives.
                raw_xc = self.run("show xconnect all | i {}:{}".format(acr_num, cem_group))
                ce_token_re = re.compile(
                    r'CE{}:{}(?!\d)'.format(re.escape(str(acr_num)),
                                            re.escape(str(cem_group))))
                candidates = []          # [(ce_iface, ce_id), ...] distinct, in order
                for line in raw_xc.splitlines():
                    if not ce_token_re.search(line):
                        continue          # line belongs to a different circuit
                    for ce_iface, ce_id in re.findall(r'CE([\d/]+):(\d+)', line):
                        if "/" not in ce_iface:
                            continue      # ACR side (e.g. CE16:112), not the GSX side
                        if (ce_iface, ce_id) not in candidates:
                            candidates.append((ce_iface, ce_id))

                gsx_xc_conflict = None
                if not candidates:
                    raise RuntimeError(
                        "No GSX xconnect for ACR {}:{}. Raw output: {}".format(
                            acr_num, cem_group, raw_xc.strip() or "(empty)"))
                if len(candidates) == 1:
                    gsx_ce_iface, gsx_cem_id = candidates[0]
                else:
                    # Prefer the endpoint whose CEM id equals the ACR-side CEM
                    # group -- that held true in the one case verified against the
                    # GSX. Still flagged, because one confirmed case is not a rule.
                    exact = [c for c in candidates if c[1] == str(cem_group)]
                    gsx_ce_iface, gsx_cem_id = exact[0] if len(exact) == 1 else candidates[0]
                    gsx_xc_conflict = (
                        "WARNING: {} GSX xconnect endpoints matched ACR {}:{} -- {}. "
                        "Using CE{}:{}{}. The other endpoints belong to different "
                        "circuits; verify on the GSX before acting. Raw xconnect: {}"
                        .format(len(candidates), acr_num, cem_group,
                                ", ".join("CE{}:{}".format(a, b) for a, b in candidates),
                                gsx_ce_iface, gsx_cem_id,
                                " (CEM id matches the ACR CEM group)"
                                if len(exact) == 1 else " (first match; none matched"
                                " the CEM group, so this is a guess)",
                                raw_xc.strip().replace("\n", " | ") or "(empty)"))

                # T3 description -> GSX node/slot/port
                raw_t3 = self.run("show controllers t3 {} | inc [Dd]escription".format(gsx_ce_iface))
                gsx_desc = gsx_node = gsx_slot = gsx_port = None
                gsx_t1_desc = None
                gsx_flag = None
                for line in raw_t3.splitlines():
                    if "escription" in line:
                        md = re.search(r'[Dd]escription[:\s]+(\S+)', line)
                        if md:
                            gsx_desc = md.group(1)
                            p = gsx_desc.split("-")
                            if len(p) >= 3:
                                gsx_node = p[0]
                                gsx_slot = p[1]
                                gsx_port = p[2]
                                # The description carries the T1 as its 4th field
                                # (NODE-SLOT-PORT-T1, e.g. LAXGSX04-11-2-14).
                                # It used to be discarded -- see gsx_t1 below.
                                if len(p) >= 4:
                                    gsx_t1_desc = p[3]
                            else:
                                gsx_flag = (
                                    "WARNING: GSX T3 description on {} is '{}' -- doesn't "
                                    "match the expected NODE-SLOT-PORT-T1 format (e.g. "
                                    "LAXGSX04-11-2-14). Node/slot/port could not be "
                                    "derived.".format(gsx_ce_iface, gsx_desc))
                            break
                if gsx_node is None and gsx_flag is None:
                    # No "description" line at all on this T3 controller -- most
                    # likely the interface simply has no description configured
                    # (a provisioning-documentation gap, same family of issue as
                    # the Netbox-not-populated case above).
                    gsx_flag = (
                        "WARNING: GSX T3 controller {} has no description configured "
                        "-- node/slot/port could not be derived. Raw output: {}".format(
                            gsx_ce_iface, raw_t3.strip() or "(empty)"))

                # ---- GSX T1: two independent sources, cross-checked ----
                #
                # There are two ways to get the T1 number and they can disagree:
                #   (a) cem_id % 100      -- derived from the live xconnect CEM id
                #   (b) description p[3]  -- the provisioned NODE-SLOT-PORT-T1 label
                #
                # (b) used to be parsed and then silently thrown away, so when the
                # two disagreed the report showed (a) with no indication anything
                # was ambiguous. Observed 2026-09-01: CE8/2/10:128 gave
                # cem_id % 100 = 28, printing assignment "11-2-28", while the
                # description on that controller ended in a different T1 -- so the
                # emitted "SHOW T1 T1-1-11-2-28" pointed at the wrong circuit.
                #
                # Both are now kept and compared. Neither is assumed correct: on a
                # mismatch the value is still (a) for backward compatibility, but
                # gsx_t1_conflict carries both so the report can say so out loud
                # rather than quietly picking a side.
                gsx_t1_derived = str(int(gsx_cem_id) % 100) if gsx_cem_id else None
                gsx_t1_conflict = None
                if gsx_t1_desc and gsx_t1_derived and \
                        gsx_t1_desc.lstrip("0") != gsx_t1_derived.lstrip("0"):
                    gsx_t1_conflict = (
                        "WARNING: GSX T1 mismatch on CE{}:{} -- controller description "
                        "'{}' says T1 {}, but CEM id {} implies T1 {}. Assignment shown "
                        "as {}-{}-{} using the CEM-derived value; verify on the GSX "
                        "before acting, and note 'SHOW T1 T1-1-{}-{}-{}' may target the "
                        "wrong circuit.".format(
                            gsx_ce_iface, gsx_cem_id, gsx_desc, gsx_t1_desc,
                            gsx_cem_id, gsx_t1_derived,
                            gsx_slot or "?", gsx_port or "?", gsx_t1_derived,
                            gsx_slot or "?", gsx_port or "?", gsx_t1_derived))
                gsx_t1 = gsx_t1_derived or "?"

                # ---- MUX-side SONET PM (facing the 1800) ----
                # Direction 2026-09-02: the tool checked facility alarms, CEM
                # connection state, and working/protect conflict on the MUX,
                # but never actually pulled SONET PM error counters from the
                # MUX's own side of the OC12/SONET-ACR path facing the 1800
                # (the OC12_COUNTERS check elsewhere is sourced entirely from
                # the 1800, not the MUX) -- confirmed gap, not a silent
                # under-report. "| no-more" required even though this client
                # already sends "terminal length 0" post-login -- this
                # specific command's output is long enough that it still
                # needs it (per live confirmation on jfk1-mux-02).
                sonet_pm_raw = ""
                if not active_sonet and acr_num:
                    _na = ("ACR {} reports no Currently Active leg (working {} "
                           "/ protect {}) -- SONET PMs not pulled, because "
                           "there is no active path to pull them from"
                           .format(acr_num,
                                   (matched_acr or {}).get("working") or "?",
                                   (matched_acr or {}).get("protect") or "?")
                           ) if matched_acr else (
                           "ACR {} reports no Currently Active leg -- SONET "
                           "PMs not pulled".format(acr_num))
                    netbox_conflict = ("{}; {}".format(netbox_conflict, _na)
                                       if netbox_conflict else _na)
                if active_sonet:
                    if active_sonet in sonet_pm_cache:
                        sonet_pm_raw = sonet_pm_cache[active_sonet]
                    else:
                        try:
                            sonet_pm_raw = self.run(
                                "show controllers sonet {} | no-more".format(active_sonet))
                        except Exception:
                            sonet_pm_raw = ""
                        sonet_pm_cache[active_sonet] = sonet_pm_raw

                mux_results[ds1] = {
                    "mux_name":        mux_name,
                    "mux_ip":          mux_ip,
                    "active_sonet":    active_sonet,
                    "sonet_pm":        sonet_pm_raw,
                    "netbox_conflict": netbox_conflict,
                    "acr_group":       acr_num,
                    "cem_group":       cem_group,
                    "vtg":             vtg_num,
                    "t1":              t1_num,
                    "gsx_ce_iface":    gsx_ce_iface,
                    "gsx_cem_id":      gsx_cem_id,
                    "gsx_desc":        gsx_desc,
                    "gsx_node":        gsx_node,
                    "gsx_slot":        gsx_slot,
                    "gsx_port":        gsx_port,
                    "gsx_t1":          gsx_t1,
                    "gsx_t1_desc":     gsx_t1_desc,
                    "gsx_t1_conflict": gsx_t1_conflict,
                    "gsx_xc_conflict": gsx_xc_conflict,
                    "gsx_flag":        gsx_flag,
                }

                # Per-DS1 facility alarm -- full path match.
                # The command lists a row ONLY when a facility is in alarm; no
                # match => not in alarm (healthy). Row columns:
                #   Source | Time | Severity | Syslog String | Description [Index]
                fa_path = "{}.{}/{}/{}".format(
                    active_sonet or "?", sts, vtg_num or "?", t1_num or "?")
                cmd_fa = "show facility-alarm status | inc {}".format(fa_path)
                raw_fa = self.run(cmd_fa)
                fa_lines = ["  >> Facility Alarm Status", "  CMD: {}".format(cmd_fa)]
                fa_alarms = []   # parsed (severity, syslog) for the diagnostic summary
                for line in raw_fa.splitlines():
                    clean = line.rstrip()
                    if clean:
                        fa_lines.append("    {}".format(clean))
                    # Parse only real alarm rows: must start with a facility
                    # Source (e.g. "SONET 0/3/0.2 PATH") and contain a severity.
                    m = re.search(
                        r'\b(CRITICAL|MAJOR|MINOR|WARNING)\b\s+(\S+)', clean)
                    if m and clean and clean[0] not in (" ", "-") \
                            and not clean.lower().startswith(("source", "system")):
                        fa_alarms.append((m.group(1), m.group(2)))
                fa_lines.append("")
                mux_results[ds1]["facility_alarm"] = "\n".join(fa_lines)
                mux_results[ds1]["facility_alarms"] = fa_alarms

                # CEM circuit health (item 14) -- connection name + circuit counters
                try:
                    conn_name = self.get_connection_name(acr_num, cem_group)
                    cem_health = self.query_cem_health(acr_num, cem_group, conn_name)
                    mux_results[ds1]["conn_name"]  = conn_name
                    mux_results[ds1]["cem_health"] = cem_health
                except Exception as _ce:
                    mux_results[ds1]["conn_name"]  = None
                    mux_results[ds1]["cem_health"] = {}

                # T3 stats once per unique T3 interface (DS3 level only)
                if gsx_ce_iface and gsx_ce_iface not in t3_stats:
                    stat_lines = []
                    stat_lines.append("  T3: {}  ({})".format(gsx_ce_iface, mux_name))
                    stat_lines.append("  MUX IP: {}".format(mux_ip))
                    stat_lines.append("")
                    cmd = "show controllers t3 {}".format(gsx_ce_iface)
                    raw_ctrl = self.run(cmd)
                    stat_lines.append("  >> T3 Controller Errors (DS3 level)")
                    stat_lines.append("  CMD: {}".format(cmd))
                    for line in raw_ctrl.splitlines():
                        clean = line.rstrip()
                        if not clean:
                            continue
                        if re.match(r'\s*T3\s+[\d/]+\.\d+\s+T1', clean):
                            break
                        stat_lines.append("    {}".format(clean))
                    stat_lines.append("")
                    t3_stats[gsx_ce_iface] = "\n".join(stat_lines)

            except Exception as e:
                errors[ds1] = str(e)

        return mux_results, t3_stats, mux_name, mux_ip, errors


# ============================================================
# STANDALONE TEST -- exercise query_batch (ACR + Netbox reconciliation)
# Feeds the Maryville values the pipeline would produce:
#   ds1 ts3/8 -> oc12-ts1/1:11:14, 1800 host ces01.lax1
# ============================================================
def main():
    import getpass
    from netbox_client import NetboxClient, load_netbox_config

    print("=" * 60)
    print("  MUX (NCS 4216) CLIENT -- query_batch test")
    print("=" * 60)

    ip       = input("MUX IP [10.243.65.4]: ").strip() or "10.243.65.4"
    mux_name = input("MUX name [LAX1-MUX-02]: ").strip() or "LAX1-MUX-02"
    ds1      = input("DS1 [ts3/8]: ").strip() or "ts3/8"
    oc12     = input("OC12 [oc12-ts1/1:11:14]: ").strip() or "oc12-ts1/1:11:14"
    host1800 = input("1800 host [ces01.lax1]: ").strip() or "ces01.lax1"

    user = input("MUX username: ").strip()
    pw   = getpass.getpass("MUX password: ")
    if not user or not pw:
        print("No credentials. Exiting.")
        return

    url, token = load_netbox_config()
    netbox = NetboxClient(url, token)
    print("\nNetbox config loaded: {}".format(netbox.ok()))

    c = MUX4216Client(ip, user, pw)
    print("[connecting -- legacy algos enabled]")
    try:
        c.connect()
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return
    print("  host: {}".format(c.hostname))

    ds1_oc12_map     = {ds1: oc12}
    ds1_hostname_map = {ds1: host1800}

    print("\n[query_batch -- ACR/Netbox reconciliation]")
    mux_results, t3_stats, mn, mip, errors = c.query_batch(
        mux_name, ip, ds1_oc12_map, ds1_hostname_map, netbox)

    for d, r in mux_results.items():
        print("-" * 50)
        print("  DS1: {}".format(d))
        print("  Active SONET: {}  ACR {}  CEM {} (VTG {}, T1 {})".format(
            r["active_sonet"], r["acr_group"], r["cem_group"], r["vtg"], r["t1"]))
        print("  GSX CE: {}  ->  node {} slot {} port {} t1 {}".format(
            r["gsx_ce_iface"], r["gsx_node"], r["gsx_slot"], r["gsx_port"], r["gsx_t1"]))
        if r["netbox_conflict"]:
            print("  FLAG: {}".format(r["netbox_conflict"]))
        print()
        print(r.get("facility_alarm", ""))
    for d, err in errors.items():
        print("  {} -> ERROR: {}".format(d, err))

    print("\n[T3 stats]")
    for iface, text in t3_stats.items():
        print(text)

    c.close()
    print("\n[done] query_batch reconciliation tested.")


if __name__ == "__main__":
    main()
