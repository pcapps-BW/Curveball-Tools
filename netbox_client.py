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


def _first_label(name):
    """
    'dfw1-mux-04.dfw1' -> 'DFW1-MUX-04'. The hostname, upper-cased for
    comparison: the part before the first dot, with any site/domain suffix
    dropped.

    NetBox stores site-suffixed names while the hardcoded maps and device
    prompts use bare hostnames, so almost every name comparison in here is
    really a comparison of first labels.
    """
    return (name or "").strip().split(".")[0].upper()


class NetboxClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.session = requests.Session()
        if token:
            self.session.headers.update({
                "Authorization": "Token {}".format(token),
                "Accept": "application/json",
            })

    # ---- request plumbing --------------------------------------------------
    #
    # A bare raise_for_status() gives "400 Client Error: Bad Request for url:
    # ...", which says nothing about WHY. NetBox always explains itself in the
    # response body:
    #
    #   {"device": ["Select a valid choice. ATL1-MUX-02 is not one of the
    #    available choices."]}
    #
    # That body is the whole answer and it was being thrown away. Observed
    # 2026-10-01: the reverse autotester reported a flat 400 on both ATL and
    # DFW and there was no way to tell a bad filter from a device NetBox does
    # not have under that name.

    def _get_json(self, url, timeout=20):
        """(data, error_string). Never raises."""
        try:
            r = self.session.get(url, timeout=timeout)
        except Exception as e:
            return None, "{}: {}".format(type(e).__name__, e)
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                if isinstance(body, dict):
                    detail = "; ".join(
                        "{}: {}".format(k, v[0] if isinstance(v, list) and v
                                        else v)
                        for k, v in body.items())
                else:
                    detail = str(body)[:300]
            except Exception:
                detail = (r.text or "")[:300]
            return None, "HTTP {}{}".format(
                r.status_code, " -- {}".format(detail) if detail else "")
        try:
            return r.json(), None
        except Exception as e:
            return None, "response was not JSON: {}: {}".format(
                type(e).__name__, e)

    def _paged(self, url, limit_pages=20):
        """(results, error_string). Follows 'next'."""
        results = []
        pages = 0
        while url and pages < limit_pages:
            data, err = self._get_json(url)
            if err:
                return results, err
            results.extend(data.get("results") or [])
            url = data.get("next")
            pages += 1
        return results, None

    def ipam_for_device(self, device_name, limit=500):
        """
        (ip_records, error) for every IP NetBox has assigned to a device.

        ⚠ Resolves the device to an ID FIRST, like every other lookup in this
        module since v2.32 -- because `/api/ipam/ip-addresses/?device=<name>`
        has the same trap as the interfaces endpoint: NetBox VALIDATES the
        name and answers **HTTP 400** on any mismatch rather than returning
        an empty list.

        This was missed in the v2.32 sweep. That release fixed every
        `interfaces/?device=` query and the verification grep used that
        literal string -- so four `ip-addresses/?device=` queries kept the
        bug, and two of them swallowed the 400 in a bare `except` and
        returned `{}`. The caller then could not tell "this device has no
        management IPs" from "NetBox rejected the name".

        Found 2026-10-02 while pcapps was fixing a `TEMP_GSX1` placeholder in
        a MUX description: `gsx_mgmt_ips()` was the function that would run
        next, and it would have reported `IP : ?` for a name mismatch,
        looking exactly like the description fix had not worked.

        Returns the error as a STRING rather than raising or swallowing, so
        callers can surface it.
        """
        if not self.ok():
            return [], "NetBox not configured"
        if not device_name:
            return [], "no device name"
        dev_id, dev_name, err = self.device_id_for(device_name)
        if not dev_id:
            return [], (err or "device '{}' not in NetBox".format(device_name))
        rows, err = self._paged(
            "{}/api/ipam/ip-addresses/?device_id={}&limit={}".format(
                self.base_url, dev_id, limit))
        if err:
            return [], "IPAM query failed for {}: {}".format(
                dev_name or device_name, err)
        return rows, None

    def device_id_for(self, name):
        """
        (device_id, canonical_name, error) for a device name.

        Filtering interfaces with `?device=<name>` makes NetBox validate the
        name against its device list and return **HTTP 400** when it does not
        match exactly -- not an empty result. So resolve the device first and
        query by `device_id`, which cannot 400 on a name mismatch, and which
        lets a name that simply is not in NetBox be reported as that instead
        of as a protocol error.

        Tries case-insensitive EXACT first, then case-insensitive contains,
        so 'ATL1-MUX-02' still finds 'atl1-mux-02' without matching
        'ATL1-MUX-02-SPARE' when an exact row exists.
        """
        import urllib.parse
        if not self.ok() or not name:
            return None, None, "NetBox not configured"
        q = urllib.parse.quote(name, safe="")

        # Tier 1: case-insensitive EXACT.
        rows, err = self._paged(
            "{}/api/dcim/devices/?name__ie={}&limit=500".format(
                self.base_url, q))
        if err:
            return None, None, "device lookup failed (name__ie): {}".format(err)
        if len(rows) == 1:
            d = rows[0]
            return d.get("id"), (d.get("name") or d.get("display")), None
        if len(rows) > 1:
            names = [r.get("name") or r.get("display") or "?" for r in rows]
            return None, None, (
                "{} devices in NetBox are named '{}': {}".format(
                    len(rows), name, ", ".join(names[:8])))

        # Tier 2: case-insensitive CONTAINS, then keep only rows whose
        # HOSTNAME -- the part before the first dot -- is the name we want.
        #
        # NetBox stores these devices site-suffixed: LOC_TO_MUX says
        # 'DFW1-MUX-04' and NetBox has 'dfw1-mux-04.dfw1'. A contains search
        # therefore also returns every device whose name merely STARTS with
        # it, which at DFW is three patch panels:
        #
        #   dfw1-mux-04-Panel-01.dfw1     <- panel, hostname differs
        #   dfw1-mux-04-Panel-02.dfw1     <- panel
        #   dfw1-mux-04-Panel-03.dfw1     <- panel
        #   dfw1-mux-04.dfw1              <- THE MUX
        #
        # Comparing the first dot-label separates them exactly: the MUX's is
        # 'dfw1-mux-04', a panel's is 'dfw1-mux-04-panel-01'. This is the same
        # hostname-vs-FQDN rule used for GSX node names, and it does not
        # depend on the word "Panel" or on device-role naming -- anything
        # whose hostname is not the one asked for is simply not it.
        rows, err = self._paged(
            "{}/api/dcim/devices/?name__ic={}&limit=500".format(
                self.base_url, q))
        if err:
            return None, None, "device lookup failed (name__ic): {}".format(err)
        if not rows:
            return None, None, "no device named '{}' in NetBox".format(name)

        want_host = _first_label(name)
        hostname_matches = [r for r in rows
                            if _first_label(r.get("name") or r.get("display"))
                            == want_host]
        all_names = [r.get("name") or r.get("display") or "?" for r in rows]

        if len(hostname_matches) == 1:
            d = hostname_matches[0]
            dname = d.get("name") or d.get("display")
            return d.get("id"), dname, None
        if len(hostname_matches) > 1:
            hn = [r.get("name") or r.get("display") or "?"
                  for r in hostname_matches]
            return None, None, (
                "{} devices in NetBox have hostname '{}': {}".format(
                    len(hostname_matches), want_host, ", ".join(hn[:8])))
        return None, None, (
            "no device in NetBox has hostname '{}' -- {} name(s) contain it "
            "but none matches: {}".format(
                want_host, len(rows), ", ".join(all_names[:8])))

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
        # device_id, not device=<name>: a name mismatch makes NetBox return
        # HTTP 400 rather than an empty list (see device_id_for).
        dev_id, _dev_name, _err = self.device_id_for(hostname_1800)
        if not dev_id:
            return None, None
        url = ("{}/api/dcim/interfaces/?device_id={}&name__ic={}&limit=500"
               .format(self.base_url, dev_id, slot_port))
        results, err = self._paged(url)   # 20-page ceiling (10k ifaces)
        if err:
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

        # Same fallback chain as the reverse direction (_far_end_detail):
        # a cable recorded in NetBox whose PATH is unresolved used to read
        # here as "no cable at all".
        return self._far_end_of(matches[0])

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
        d = self.sonet_to_oc_detail(mux_name, sonet_iface)
        if d.get("ambiguous"):
            return "__AMBIGUOUS__", None
        return d.get("oc_name"), d.get("hub_hostname")

    def sonet_to_oc_detail(self, mux_name, sonet_iface):
        """
        sonet_to_oc() with the reasoning attached:

          {"oc_name", "hub_hostname", "source", "reason", "ambiguous",
           "matched_name", "candidates"}

        The two-value form collapses every failure to (None, None), which in
        the reverse autotester printed as "NetBox has no cabled OC" even when
        NetBox plainly had the cable and only the PATH was unresolved. The
        operator needs to know which of those it was: one is a NetBox data-
        entry job, the other is a cable-path/patch-panel job.
        """
        out = {"oc_name": None, "hub_hostname": None, "source": None,
               "reason": None, "ambiguous": False, "matched_name": None,
               "candidates": []}
        if not self.ok():
            out["reason"] = "NetBox is not configured or not reachable"
            return out
        if not mux_name:
            out["reason"] = "no MUX name to look up"
            return out

        # Accept 'SONET 0/13/1', 'sonet0/13/1' or a bare '0/13/1'.
        m = re.search(r'(\d+/\d+/\d+)', sonet_iface or "")
        if not m:
            out["reason"] = ("'{}' does not contain a slot/port like 0/13/1"
                             .format(sonet_iface))
            return out
        slot_port = m.group(1)

        import urllib.parse
        # Resolve the device to an id first: `?device=<name>` 400s on a name
        # mismatch instead of returning nothing (see device_id_for).
        dev_id, dev_name, dev_err = self.device_id_for(mux_name)
        if not dev_id:
            out["reason"] = dev_err or "device '{}' not found".format(mux_name)
            return out
        results, err = self._paged(
            "{}/api/dcim/interfaces/?device_id={}&name__ic={}&limit=500"
            .format(self.base_url, dev_id,
                    urllib.parse.quote(slot_port, safe="")))
        if err:
            # The name filter is only an optimisation -- it keeps the result
            # set small. If NetBox rejects it (unsupported lookup on this
            # version, or the slashes upset it), fetch the device's full
            # interface list and do the matching here, which is where the
            # anchored match happens anyway.
            out["source_note"] = (
                "name filter rejected ({}), fell back to the full interface "
                "list".format(err))
            results, err = self._paged(
                "{}/api/dcim/interfaces/?device_id={}&limit=500".format(
                    self.base_url, dev_id))
            if err:
                out["reason"] = (
                    "NetBox interface query failed for {}: {}".format(
                        dev_name or mux_name, err))
                return out

        # Anchor to end-of-string so '0/13/1' cannot also match '0/13/10'.
        # Same class of mistake as the unanchored CE match that produced the
        # LAXGSX04 wrong-circuit report.
        exact = re.compile(r'(?:^|[^\d])' + re.escape(slot_port) + r'$')
        matches = [i for i in results if exact.search((i.get("name") or ""))]
        out["candidates"] = [i.get("name") for i in results if i.get("name")]
        if not matches:
            out["reason"] = (
                "no interface named '{}' on {} in NetBox{}".format(
                    slot_port, mux_name,
                    " (device has: {})".format(
                        ", ".join(out["candidates"][:12]))
                    if out["candidates"] else
                    " -- the device has no interface matching that at all"))
            return out
        if len(matches) > 1:
            out["ambiguous"] = True
            out["reason"] = ("{} interfaces on {} match '{}': {}".format(
                len(matches), mux_name, slot_port,
                ", ".join(i.get("name") or "?" for i in matches)))
            return out

        iface = matches[0]
        out["matched_name"] = iface.get("name")
        far = self._far_end_detail(iface)
        out["oc_name"] = far["name"]
        out["hub_hostname"] = far["device"]
        out["source"] = far["source"]
        out["reason"] = far["reason"]
        return out

    # ---- reading the far end of a cable -----------------------------------
    #
    # `connected_endpoints` is NetBox's resolved PATH endpoint. It is only
    # populated when the whole path is traceable and marked reachable, so it
    # comes back empty for an interface that plainly has a cable in the UI --
    # e.g. when the run crosses a patch panel whose front/rear mapping is
    # incomplete, or the link is not in a 'connected' state.
    #
    # That is what produced "NetBox has no cabled OC for DFW1-MUX-04 0/13/1"
    # on 2026-10-01 while NetBox's own interface list showed cable #25727 to
    # ces01.dfw1 oc12-ts1/1.
    #
    # So fall back through every shape NetBox exposes, most-resolved first,
    # and TELL THE CALLER which one answered -- a path endpoint and a raw
    # cable peer are not equally strong evidence, and the report should not
    # present them as if they were.

    def _far_end_of(self, iface):
        d = self._far_end_detail(iface)
        return d["name"], d["device"]

    def _far_end_detail(self, iface):
        """
        {"name","device","source","reason"} for the other end of this
        interface's cable. name/device are None when nothing resolved, and
        `reason` then says what WAS found -- cable id, peer count -- so the
        failure is actionable instead of just negative.
        """
        out = {"name": None, "device": None, "source": None, "reason": None}

        # 1. The resolved path endpoint -- strongest.
        for ep in (iface.get("connected_endpoints") or []):
            dev = ep.get("device") or {}
            name = ep.get("name") or ep.get("display")
            if name:
                out.update(name=name,
                           device=dev.get("name") or dev.get("display"),
                           source="NetBox path endpoint")
                return out

        # 2. The far end of THIS cable. Resolves when the path is incomplete
        #    but the cable itself is recorded -- a direct interface-to-
        #    interface link that NetBox has not marked reachable.
        for peer in (iface.get("link_peers") or []):
            dev = peer.get("device") or {}
            name = peer.get("name") or peer.get("display")
            if name and (dev.get("name") or dev.get("display")):
                out.update(name=name,
                           device=dev.get("name") or dev.get("display"),
                           source="NetBox cable peer (path not resolved)")
                return out

        # 3. Read the cable object itself and take whichever termination is
        #    not this interface.
        cable = iface.get("cable")
        cable_id = cable.get("id") if isinstance(cable, dict) else cable
        if cable_id:
            peer = self._cable_far_termination(cable_id, iface.get("id"))
            if peer:
                name, devname = peer
                out.update(name=name, device=devname,
                           source="NetBox cable #{} terminations".format(
                               cable_id))
                return out
            out["reason"] = (
                "interface '{}' has cable #{} in NetBox but neither a "
                "resolved path endpoint, a cable peer, nor a readable far "
                "termination -- the cable path is probably incomplete "
                "(patch panel front/rear mapping, or link not marked "
                "connected)".format(iface.get("name"), cable_id))
            return out

        out["reason"] = ("interface '{}' exists in NetBox but has no cable "
                         "recorded on it".format(iface.get("name")))
        return out

    def _cable_far_termination(self, cable_id, this_iface_id):
        """(name, device_name) of the cable end that is not this_iface_id."""
        try:
            r = self.session.get("{}/api/dcim/cables/{}/".format(
                self.base_url, cable_id), timeout=20)
            r.raise_for_status()
            cab = r.json()
        except Exception:
            return None

        groups = []
        # NetBox 3.3+ : a_terminations / b_terminations (lists)
        for key in ("a_terminations", "b_terminations"):
            groups.extend(cab.get(key) or [])
        # Older: termination_a / termination_b (single objects)
        for key in ("termination_a", "termination_b"):
            t = cab.get(key)
            if t:
                groups.append({"object": t})

        for t in groups:
            obj = t.get("object") or t
            if not isinstance(obj, dict):
                continue
            if this_iface_id is not None and obj.get("id") == this_iface_id:
                continue      # this is our own end
            dev = obj.get("device") or {}
            name = obj.get("name") or obj.get("display")
            devname = dev.get("name") or dev.get("display")
            if name and devname:
                return name, devname
        return None

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
        # device_id, not device=<name>: see device_id_for -- a name mismatch
        # makes NetBox answer HTTP 400, not an empty list.
        _did, _dname, _derr = self.device_id_for(hostname)
        if not _did:
            return [], (_derr or "device '{}' not in NetBox".format(hostname))
        url = "{}/api/dcim/interfaces/?device_id={}&limit=500".format(
            self.base_url, _did)
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
        # ipam_for_device paginates internally and returns (rows, error), so
        # there is no loop or try/except needed here any more.
        by_name = {}
        _iprows, _iperr = self.ipam_for_device(hostname, limit=500)
        if not _iperr:
            for rec in _iprows:
                ao = rec.get("assigned_object") or {}
                nm = (ao.get("name") or "").strip()
                addr = (rec.get("address") or "").split("/")[0].strip()
                if nm and addr and nm not in by_name:
                    by_name[nm] = addr
        # Descriptions are still usable without IPs -- degrade, don't fail.

        for i in out:
            i["ip"] = by_name.get(i["name"], "")
        return out, None

    # ---- NPT-1800 hub device discovery ----
    def bcr_devices(self, name_contains="bcr01"):
        """
        Return all BCR (backbone core router) device names from NetBox, e.g.
        ["bcr01.atl1", "bcr01.jfk1", ...]. Thin alias for the same name__ic
        discovery hub_1800_devices uses -- the BCRs are regular NetBox devices
        (confirmed 2026-11-05: bcr01.jfk1 under the JFK1 site), so no separate
        query or fallback map is needed, only a different name filter.
        """
        return self.hub_1800_devices(name_contains=name_contains)

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
        rows, err = self.ipam_for_device(hostname_1800, limit=500)
        if err:
            return {}
        slots = {}
        for item in rows:
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
        _rows, _err = self.ipam_for_device(device_name, limit=200)
        if _err:
            warns.append("IPAM lookup for {} failed: {}".format(
                device_name, _err))
        for item in _rows:
            addr = (item.get("address") or "").split("/")[0].strip()
            if not addr or addr in ips:
                continue
            ao = item.get("assigned_object") or {}
            iface = (ao.get("name") or "").strip()
            ips.append(addr)
            if iface:
                iface_by_ip[addr] = iface

        if not ips and not warns:
            warns.append("NetBox has no IP addresses for device {}".format(
                device_name))
        return ips, warns, iface_by_ip

    def gsx_mgmt_ips(self, gsx_device_name, warns=None):
        """
        {"1-1":[ips], "1-2":[ips]} (CIDR stripped), or {} with the reason
        appended to `warns` when one is supplied.

        Routed through ipam_for_device so a name NetBox rejects reports as a
        name problem instead of looking like a GSX with no management IPs --
        see that method for why those were indistinguishable before.
        """
        rows, err = self.ipam_for_device(gsx_device_name, limit=500)
        if err:
            if warns is not None:
                warns.append("GSX mgmt IP lookup for {}: {}".format(
                    gsx_device_name, err))
            return {}
        cards = {}
        for item in rows:
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
        if not cards and warns is not None:
            warns.append("NetBox has no MGMT-<shelf>-<slot>-<port> addresses "
                         "for {}".format(gsx_device_name))
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
