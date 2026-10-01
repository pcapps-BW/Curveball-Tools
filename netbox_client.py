#!/usr/bin/env python3
"""
netbox_client.py -- Netbox API client (requests) for the standalone tool.

Replaces the SecureCRT PowerShell/Invoke-RestMethod workaround with direct
requests calls. Two lookups:
  - oc_to_sonet(hostname_1800, oc_interface) -> (sonet_path, mux_name)
      the ts3-bug fix: true OC->SONET mapping, source of truth for MUX ACR match
  - gsx_mgmt_ips(gsx_netbox_name) -> {"1-1":[ips], "1-2":[ips]}
      for the GSX hop (device name = short node + 1800 site suffix)

Config (NETBOX_URL / NETBOX_TOKEN) read from autotester_config.txt.

Built by P. Capps.
"""

import re
import sys

import platform_paths

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run:  pip install requests")
    sys.exit(1)

CONFIG_PATH = platform_paths.path("autotester_config.txt")


def load_netbox_config(path=CONFIG_PATH):
    url = token = ""
    try:
        with open(path, "r") as cfg:
            for line in cfg:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key == "NETBOX_URL":
                    url = val.strip().rstrip("/")
                elif key == "NETBOX_TOKEN":
                    token = val.strip()
    except FileNotFoundError:
        pass
    return url, token


class NetboxClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.session = requests.Session()
        if token:
            self.session.headers.update({
                "Authorization": "Token {}".format(token),
                "Accept": "application/json",
            })

    def ok(self):
        return bool(self.base_url and self.session.headers.get("Authorization"))

    # ---- OC12 -> SONET (ts3-bug fix) ----
    def oc_to_sonet(self, hostname_1800, oc_interface):
        """
        Returns (sonet_path, mux_name), or (None, None) on no/failed match, or
        ("__AMBIGUOUS__", None) if more than one interface matches the slot/port.

        Matches on the ts<slot>/<port> portion of the OC name rather than the
        exact name, because the 1800 may report an OC with a different rate
        prefix (oc-, oc12-, oc48-) than Netbox stores. The stable identifier is
        the tsX/Y slot/port, so we pull the device's interfaces and match that.
        """
        if not self.ok():
            return None, None

        # Extract the tsX/Y slot/port from whatever the OC name is.
        m = re.search(r'(ts\d+/\d+)', oc_interface or "")
        if not m:
            return None, None
        slot_port = m.group(1)   # e.g. "ts3/1"

        # Query the device's interfaces, narrowed to those whose name contains
        # the slot/port (name__ic = case-insensitive contains). This keeps the
        # result set tiny (just this OC's facility + its sub-channel children)
        # instead of the device's full interface list, which can exceed 500 and
        # get truncated. We ALSO paginate (follow 'next') so that even if the
        # name filter is unsupported/ignored and the full list comes back, we
        # still retrieve every page rather than silently stopping at 500.
        url = "{}/api/dcim/interfaces/?device={}&name__ic={}&limit=500".format(
            self.base_url, hostname_1800, slot_port)
        results = []
        try:
            pages = 0
            while url and pages < 20:   # 20-page safety ceiling (10k ifaces)
                r = self.session.get(url, timeout=20)
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("results", []))
                url = data.get("next")
                pages += 1
        except Exception:
            return None, None

        # Match ONLY the base OC facility interface: name is 'oc<rate>-tsX/Y'
        # with NOTHING after the slot/port. The device also exposes many
        # sub-channel children (oc12-ts1/1:1:1, :1:1.0, ...) that share the tsX/Y
        # token but are NOT the facility we want -- anchoring to end-of-string
        # excludes every ':X:Y' child. The base facility is also the one that
        # carries connected_endpoints.
        base_re = re.compile(r'^oc\d*-' + re.escape(slot_port) + r'$')
        matches = []
        for iface in results:
            if base_re.match(iface.get("name") or ""):
                matches.append(iface)

        if not matches:
            return None, None
        if len(matches) > 1:
            # Can't tell which OC facility is the right one -- don't guess.
            return "__AMBIGUOUS__", None

        iface = matches[0]
        endpoints = iface.get("connected_endpoints") or []
        if not endpoints:
            return None, None
        ep = endpoints[0]
        sonet_path = ep.get("name") or ep.get("display")
        dev = ep.get("device") or {}
        mux_name = dev.get("name") or dev.get("display")
        return sonet_path, mux_name

    def sonet_to_oc(self, mux_name, sonet_iface):
        """
        The reverse of oc_to_sonet(): MUX SONET interface -> (oc_name, hub_hostname).

        Reads the same cable from the other end. Given 'SONET 0/13/1' (or
        '0/13/1') on a MUX, returns the 1800's OC facility it lands on and that
        1800's hostname.

        Returns (None, None) on no match, or ("__AMBIGUOUS__", None) when more
        than one interface matches -- the same contract oc_to_sonet() uses, so
        callers can treat "cannot tell" distinctly from "not found". Picking one
        of several would produce a complete, plausible, wrong route.
        """
        if not self.ok() or not mux_name:
            return None, None

        # Accept 'SONET 0/13/1', 'sonet0/13/1' or a bare '0/13/1'.
        m = re.search(r'(\d+/\d+/\d+)', sonet_iface or "")
        if not m:
            return None, None
        slot_port = m.group(1)

        import urllib.parse
        url = ("{}/api/dcim/interfaces/?device={}&name__ic={}&limit=500"
               .format(self.base_url,
                       urllib.parse.quote(mux_name, safe=""),
                       urllib.parse.quote(slot_port, safe="")))
        results = []
        try:
            pages = 0
            while url and pages < 20:
                r = self.session.get(url, timeout=20)
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("results", []))
                url = data.get("next")
                pages += 1
        except Exception:
            return None, None

        # Anchor to end-of-string so '0/13/1' cannot also match '0/13/10'.
        # Same class of mistake as the unanchored CE match that produced the
        # LAXGSX04 wrong-circuit report.
        exact = re.compile(r'(?:^|[^\d])' + re.escape(slot_port) + r'$')
        matches = [i for i in results if exact.search((i.get("name") or ""))]
        if not matches:
            return None, None
        if len(matches) > 1:
            return "__AMBIGUOUS__", None

        eps = matches[0].get("connected_endpoints") or []
        if not eps:
            return None, None
        ep = eps[0]
        oc_name = ep.get("name") or ep.get("display")
        dev = ep.get("device") or {}
        return oc_name, (dev.get("name") or dev.get("display"))

    def device_interface_descriptions(self, hostname):
        """
        All interfaces for a device: [{name, type, description}, ...].

        Returns ([], reason) on failure -- NEVER an empty list presented as
        "this device has no interfaces". A NetBox outage and a device with
        nothing configured must not look the same; that ambiguity is what
        made a dropped VPN read as "no MUX found" (2026-09-18).

        Paginated, same 20-page ceiling as oc_to_sonet.
        """
        if not self.ok():
            return [], "NetBox not configured"
        if not hostname:
            return [], "no hostname"

        import urllib.parse
        url = "{}/api/dcim/interfaces/?device={}&limit=500".format(
            self.base_url, urllib.parse.quote(hostname, safe=""))
        out, pages = [], 0
        try:
            while url and pages < 20:
                r = self.session.get(url, timeout=20)
                r.raise_for_status()
                data = r.json()
                for i in data.get("results", []):
                    t = i.get("type") or {}
                    out.append({
                        "name": (i.get("name") or "").strip(),
                        "type": (t.get("label") or t.get("value") or ""),
                        "description": (i.get("description") or "").strip(),
                        "ip": "",
                    })
                url = data.get("next")
                pages += 1
        except Exception as e:
            return [], "{}: {}".format(type(e).__name__, e)

        # Interface IPs come from IPAM, not the interface serializer.
        # They are what lets a hub's OSPF neighbour address be matched to a
        # specific 1250 interface -- and therefore to that interface's
        # circuit description -- WITHOUT needing to log into the device.
        # Confirmed on FTMYFLXA939: ge-ts2/1 = 10.111.133.212/31 (JFK range),
        # ge-ts5/1 = 10.111.132.212/31 (ATL range).
        by_name = {}
        ipurl = "{}/api/ipam/ip-addresses/?device={}&limit=500".format(
            self.base_url, urllib.parse.quote(hostname, safe=""))
        try:
            pages = 0
            while ipurl and pages < 20:
                r = self.session.get(ipurl, timeout=20)
                r.raise_for_status()
                data = r.json()
                for rec in data.get("results", []):
                    ao = rec.get("assigned_object") or {}
                    nm = (ao.get("name") or "").strip()
                    addr = (rec.get("address") or "").split("/")[0].strip()
                    if nm and addr and nm not in by_name:
                        by_name[nm] = addr
                ipurl = data.get("next")
                pages += 1
        except Exception:
            # Descriptions are still usable without IPs -- degrade, don't fail.
            by_name = {}

        for i in out:
            i["ip"] = by_name.get(i["name"], "")
        return out, None

    # ---- NPT-1800 hub device discovery ----
    def hub_1800_devices(self, name_contains="ces01"):
        """
        Return all NPT-1800 hub device names from NetBox.
        Filters on name__ic (case-insensitive contains) so no hardcoded list.
        Returns list of hostnames (strings), e.g. ["ces01.atl1", "ces01.lax1"].
        Follows pagination so works even if there are >500 matches.
        """
        if not self.ok():
            return []
        url = "{}/api/dcim/devices/?name__ic={}&limit=500".format(
            self.base_url, name_contains)
        names = []
        try:
            pages = 0
            while url and pages < 10:
                r = self.session.get(url, timeout=15)
                r.raise_for_status()
                data = r.json()
                for dev in data.get("results", []):
                    name = dev.get("name") or dev.get("display") or ""
                    if name:
                        names.append(name)
                url = data.get("next")
                pages += 1
        except Exception:
            return []
        return names

    def hub_1800_mgmt_ips(self, hostname_1800):
        """
        Return management IPs for an NPT-1800 hub, keyed by slot.
        Queries IPAM for IPs assigned to interfaces named 'MGMT MCP SLOT_MSA'
        or 'MGMT MCP SLOT_MSB' on the given device.

        Returns {"MSA": ["x.x.x.x", ...], "MSB": ["x.x.x.x", ...]} (CIDR stripped).
        Either key may be absent if no IPs found for that slot.

        Mirrors gsx_mgmt_ips() -- same IPAM query, different interface name pattern.
        """
        if not self.ok():
            return {}
        url = "{}/api/ipam/ip-addresses/?device={}&limit=500".format(
            self.base_url, hostname_1800)
        try:
            r = self.session.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return {}
        slots = {}
        for item in data.get("results", []):
            assigned = item.get("assigned_object") or {}
            iface_name = (assigned.get("name") or "").strip()
            # Match "MGMT MCP SLOT_MSA" or "MGMT MCP SLOT_MSB" (case-insensitive)
            m = re.match(r'MGMT\s+MCP\s+SLOT_(MS[AB])', iface_name, re.IGNORECASE)
            if not m:
                continue
            slot_key = m.group(1).upper()   # "MSA" or "MSB"
            addr = item.get("address", "")
            ip = addr.split("/")[0] if addr else ""
            if ip:
                slots.setdefault(slot_key, []).append(ip)
        return slots

    # ---- Hostname by IP (OSPF stability / loopback lookup) ----
    def hostname_by_ip(self, ip):
        """
        Look up the device hostname assigned to an IP address in NetBox IPAM.

        Queries /api/ipam/ip-addresses/ filtered on the address.  Tries both
        the bare IP and the /32 CIDR form since NetBox stores addresses with
        a prefix length.  Returns the assigned device name string, or None.

        Used by ospf_stability.py to resolve 1250 loopback IPs -> hostnames
        by querying the OSPF router-ID against NetBox every run.
        """
        if not self.ok():
            return None
        import urllib.parse
        # NetBox stores addresses as "10.x.x.x/32" -- try both forms
        for addr in (ip, "{}/32".format(ip)):
            url = "{}/api/ipam/ip-addresses/?address={}&limit=5".format(
                self.base_url, urllib.parse.quote(addr, safe=""))
            try:
                r = self.session.get(url, timeout=10)
                r.raise_for_status()
                data = r.json()
                for item in data.get("results", []):
                    assigned = item.get("assigned_object") or {}
                    # Physical interface -> has 'device' key
                    device = assigned.get("device") or {}
                    name = device.get("name") or device.get("display")
                    if name:
                        return name
                    # VM interface -> has 'virtual_machine' key
                    vm = assigned.get("virtual_machine") or {}
                    name = vm.get("name") or vm.get("display")
                    if name:
                        return name
            except Exception:
                pass
        return None

    # ---- GSX mgmt IPs ----
    @staticmethod
    def derive_gsx_device_name(gsx_node, hostname_1800):
        """LAXGSX04 + (ces01.lax1 -> lax1) = LAXGSX04.lax1"""
        if "." in hostname_1800:
            suffix = hostname_1800.split(".", 1)[1]
            return "{}.{}".format(gsx_node, suffix)
        return gsx_node

    def device_mgmt_ips(self, device_name):
        """
        Candidate management IPs for any device, in preference order.

        Returns (ips, warnings, iface_by_ip). primary_ip4 first -- that is
        the address NetBox itself considers canonical -- then every other IP
        assigned to the device, de-duplicated, order preserved.
        iface_by_ip maps address -> NetBox interface name where known, so a
        caller can report that e.g. a Ge port answered when the loopback did
        not.

        Generic on purpose. gsx_mgmt_ips() below is GSX-specific because it
        groups by MGMT-<shelf>-<slot>-<port> interface naming; a MUX has no
        such convention, so this returns a flat ordered list and lets the
        caller try them in turn.

        ([], [reason]) on failure -- never an empty list presented as "this
        device has no addresses". A NetBox outage and a device with no IPs
        must not look the same.
        """
        if not self.ok():
            return [], ["NetBox not configured"], {}
        if not device_name:
            return [], ["no device name"], {}

        import urllib.parse
        q = urllib.parse.quote(device_name, safe="")
        ips, warns = [], []
        iface_by_ip = {}   # ip -> NetBox interface name, for the report

        # primary_ip4 first.
        try:
            r = self.session.get(
                "{}/api/dcim/devices/?name={}".format(self.base_url, q),
                timeout=15)
            r.raise_for_status()
            for dev in r.json().get("results", []):
                pip = (dev.get("primary_ip4") or dev.get("primary_ip") or {}) or {}
                addr = (pip.get("address") or "").split("/")[0].strip()
                if addr and addr not in ips:
                    ips.append(addr)
        except Exception as e:
            warns.append("device lookup for {} failed: {}: {}".format(
                device_name, type(e).__name__, e))

        # Then every other address assigned to the device -- loopback,
        # management, AND physical ports. A MUX whose management address is
        # unreachable can still be reachable on a Ge port that NetBox knows
        # about (pcapps, 2026-10-01: dfw1-mux-04.dfw1), so those are candidates
        # too, tried after the primary.
        #
        # The interface each address belongs to is carried alongside it, so the
        # report can say WHICH port worked rather than just an IP. A Ge port
        # answering when the loopback does not is a finding about the device,
        # not a detail to swallow.
        try:
            r = self.session.get(
                "{}/api/ipam/ip-addresses/?device={}&limit=200".format(
                    self.base_url, q),
                timeout=15)
            r.raise_for_status()
            for item in r.json().get("results", []):
                addr = (item.get("address") or "").split("/")[0].strip()
                if not addr or addr in ips:
                    continue
                ao = item.get("assigned_object") or {}
                iface = (ao.get("name") or "").strip()
                ips.append(addr)
                if iface:
                    iface_by_ip[addr] = iface
        except Exception as e:
            warns.append("IPAM lookup for {} failed: {}: {}".format(
                device_name, type(e).__name__, e))

        if not ips and not warns:
            warns.append("NetBox has no IP addresses for device {}".format(
                device_name))
        return ips, warns, iface_by_ip

    def gsx_mgmt_ips(self, gsx_device_name):
        """Returns {"1-1":[ips], "1-2":[ips]} (CIDR stripped) or {}."""
        if not self.ok():
            return {}
        url = "{}/api/ipam/ip-addresses/?device={}".format(self.base_url, gsx_device_name)
        try:
            r = self.session.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return {}
        cards = {}
        for item in data.get("results", []):
            assigned = item.get("assigned_object") or {}
            iface_name = assigned.get("name", "")
            m = re.match(r'MGMT-(\d+)-(\d+)-(\d+)', iface_name)
            if not m:
                continue
            card_key = "{}-{}".format(m.group(1), m.group(2))
            addr = item.get("address", "")
            ip = addr.split("/")[0] if addr else ""
            if ip:
                cards.setdefault(card_key, []).append(ip)
        return cards


if __name__ == "__main__":
    # quick standalone check
    url, token = load_netbox_config()
    nb = NetboxClient(url, token)
    print("Netbox config loaded:", nb.ok())
    if nb.ok():
        s, m = nb.oc_to_sonet("ces01.lax1", "oc12-ts1/1")
        print("oc_to_sonet(ces01.lax1, oc12-ts1/1) ->", s, m)
        ips = nb.gsx_mgmt_ips("LAXGSX04.lax1")
        print("gsx_mgmt_ips(LAXGSX04.lax1) ->", ips)
