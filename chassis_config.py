#!/usr/bin/env python3
"""
chassis_config.py -- readers for NPT-1800 `show configuration` output.

Two jobs, both parsing the same command family:
  * far-end descriptions off a chassis port (MUX/router identity)
  * layer2-transport blocks on an OC sub-channel (the reverse path to a 1250)

WHY THIS EXISTS (pcapps, 2026-10-01): MUX identity currently comes from NetBox
alone, via netbox_client.oc_to_sonet(). That is a single source, and when it is
wrong or ambiguous there is nothing to compare it against -- `oc_to_sonet` can
only return "__AMBIGUOUS__" or (None, None), never "NetBox and the device
disagree". The 1800's own chassis config carries a description naming the far
end, which makes a second, device-sourced opinion available.

FORMAT, from real ces01.atl1 output 2026-10-01:

    pcapps@ces01.atl1> show configuration | display-set | grep 3/1
    set chassis slot ts13 data-card dhxe-4 port xe-ts13/1 description "bcr01.atl1 Te0/0/0/34"

So: `set chassis slot <slot> data-card <card> port <port> description "<text>"`,
and the description is `<far-end device> <far-end interface>`.

⚠ THE COMMAND IS SLOW. `show configuration | display-set` takes roughly 90-120
seconds to render on a full 1800 config (documented in npt1800_client.py), and
the `| grep` does not avoid that -- the device renders the whole config before
filtering. This is therefore NOT something to call per-DS1 in a 28-DS1 run. It
is for a single targeted lookup, or a deliberate cross-check. Nothing in this
module is wired into the autotester's per-DS1 path on purpose.

READ-ONLY: `show` only.

Built by P. Capps.
"""

import re

# `set chassis slot ts13 data-card dhxe-4 port xe-ts13/1 description "..."`
#
# Tolerant of extra tokens between 'slot' and 'port' because the card type
# varies (dhxe-4 here; other card types exist) and is not what we are after.
# Anchored on 'set chassis slot' so an unrelated 'description' line elsewhere
# in the config cannot match.
_CHASSIS_PORT_RE = re.compile(
    r'^\s*set\s+chassis\s+slot\s+(?P<slot>\S+)\s+.*?'
    r'\bport\s+(?P<port>\S+)\s+description\s+"(?P<desc>[^"]*)"',
    re.IGNORECASE)

# A description like `bcr01.atl1 Te0/0/0/34` -- far-end device, far-end iface.
# The interface half is optional: some descriptions carry only a device name.
_FAR_END_RE = re.compile(
    r'^\s*(?P<device>[A-Za-z0-9][A-Za-z0-9._\-]*)'
    r'(?:\s+(?P<iface>\S.*?))?\s*$')


def port_description_cmd(port_or_slotport):
    """
    The command to run for one port.

    Narrowed with `| grep <slot/port>` so the returned text is small, and
    `| no-more` because every show on these devices needs it. The grep does
    NOT make the command fast -- see the module docstring.
    """
    m = re.search(r'(ts\d+/\d+)', port_or_slotport or "")
    token = m.group(1) if m else (port_or_slotport or "").strip()
    return 'show configuration | display-set | grep {} | no-more'.format(token)


def parse_chassis_ports(raw):
    """
    Parse `set chassis ... port <port> description "..."` lines.

    Returns {port_name: description}. Ports with no description line simply do
    not appear -- absent is absent, not blank.
    """
    out = {}
    for ln in (raw or "").splitlines():
        m = _CHASSIS_PORT_RE.match(ln)
        if m:
            out[m.group("port").strip()] = m.group("desc").strip()
    return out


def split_far_end(desc):
    """
    'bcr01.atl1 Te0/0/0/34' -> ('bcr01.atl1', 'Te0/0/0/34').

    Returns (device, iface) with iface None when the description is a bare
    name. Returns (None, None) for text that does not start with something
    name-shaped, rather than inventing a device from free text.
    """
    m = _FAR_END_RE.match(desc or "")
    if not m or not m.group("device"):
        return None, None
    return m.group("device"), (m.group("iface") or None)


def far_end_for_port(raw, port):
    """
    (device, iface, raw_description) for one port out of a config dump.

    Exact port match only. 'xe-ts13/1' and 'xe-ts13/10' are different ports
    and must not collapse into one another -- the unanchored-substring version
    of this mistake is what produced the LAXGSX04 wrong-circuit report.

    Returns (None, None, None) when the port carries no description.
    """
    desc = parse_chassis_ports(raw).get((port or "").strip())
    if desc is None:
        return None, None, None
    dev, iface = split_far_end(desc)
    return dev, iface, desc


AGREE        = "AGREE"
DISAGREE     = "DISAGREE"
DEVICE_ONLY  = "DEVICE ONLY -- NetBox had no answer"
NETBOX_ONLY  = "NETBOX ONLY -- no description on the device"
NEITHER      = "NEITHER -- no source could name the far end"


def compare_with_netbox(device_far_end, netbox_mux):
    """
    Compare the device's configured description against NetBox's answer.

    (verdict, detail). Comparison is case-insensitive and ignores a trailing
    domain, because 'LAX1-MUX-02' and 'lax1-mux-02.bwnet.us' are the same box
    written two ways -- a false DISAGREE would train people to ignore the
    check, which is worse than not having it.

    DISAGREE is NOT resolved here on purpose. Neither source is established as
    authoritative for this field, and picking a winner would hide the one fact
    worth surfacing: that the two records of one thing have diverged.
    """
    d = (device_far_end or "").strip()
    n = (netbox_mux or "").strip()
    if not d and not n:
        return NEITHER, "neither the device config nor NetBox named a far end"
    if d and not n:
        return DEVICE_ONLY, "device config says '{}'".format(d)
    if n and not d:
        return NETBOX_ONLY, "NetBox says '{}'".format(n)

    short = lambda s: s.lower().split(".")[0]
    if short(d) == short(n):
        return AGREE, "both say '{}'".format(d)
    return (DISAGREE,
            "device config says '{}' but NetBox says '{}' -- one record is "
            "wrong and this tool does not guess which".format(d, n))


# ── layer2-transport: OC sub-channel -> the far-end 1250 ─────────────────────
#
# This is the hop that makes a REVERSE trace possible. Forward, the tool starts
# at a 1250 and learns DS1 -> MUSE -> OC from the 1250 itself. In reverse you
# are standing on the 1800 holding an OC sub-channel and need to know which
# 1250 it serves -- and the 1800's own config says so.
#
# Real output, ces01.jfk1 2026-10-01:
#
#   set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 customer Curveball
#   set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 nms-user-label 332_EKHTINXA15T_ACC
#   set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 pw-type t1-satop
#   set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 remote-tpe 10.x.x.x peer-address 10.x.x.x
#   set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 pw-id 127
#
# ⚠ TWO LINE FORMS. The same configuration prints differently depending on how
# it was asked for, and a parser written against one silently matches nothing
# on the other:
#
#   show configuration | display-set | grep <x>
#       -> "set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 ..."
#   show configuration interfaces oc12-ts1/1:1:11 | display-set
#       -> "set unit 0 layer2-transport MUSE_127 ..."        (prefix stripped)
#
# Both confirmed live on the same sub-channel. The interface is therefore
# OPTIONAL in the pattern, and callers that used the scoped command must pass
# the interface name in themselves -- it is not in the output to recover.
#
# remote-tpe IS the 1250's loopback. Verified 2026-10-01: the address appears
# in `show ospf neighbor` as a neighbour router-ID, which is exactly what
# ospf_stability.py already SSHes to. So it is directly usable, no translation
# needed. `peer-address` also carries an address and is NOT assumed to be the
# same thing -- it is captured separately rather than conflated.

_L2T_RE = re.compile(
    r'^\s*set\s+(?:interfaces\s+(?P<iface>\S+)\s+)?'
    r'unit\s+(?P<unit>\d+)\s+layer2-transport\s+(?P<muse>\S+)\s+'
    r'(?P<rest>.*)$', re.IGNORECASE)

_REMOTE_TPE_RE   = re.compile(r'^remote-tpe\s+(\d{1,3}(?:\.\d{1,3}){3})\b', re.I)
_PEER_ADDR_RE    = re.compile(r'\bpeer-address\s+(\d{1,3}(?:\.\d{1,3}){3})\b', re.I)
# Recognised and deliberately not stored: valueless flags, timers, OAM
# tuning, and peer-address (captured by _PEER_ADDR_RE above).
_KNOWN_IGNORED_RE = re.compile(
    r'^(control-word|pw-status-tlv-enable|severity-profile|hold-off-timer|'
    r'oam|peer-address)\b', re.I)

_SIMPLE_KEY_RE   = re.compile(
    r'^(customer|nms-service-id|nms-user-label|l2-service-mtu|pw-type|'
    r'pw-id|l2vpn-id|description)\s*(.*)$', re.I)

# nms-user-label '332_EKHTINXA15T_ACC' -- leading number is the EVC VLAN on the
# Ethernet side to the same site (that 1250 was on xe-ts16/2.332), and the rest
# carries a CLLI. Captured for cross-checking, never used as the identity: the
# label is free text a human typed.
_LABEL_RE = re.compile(r'^(?P<vlan>\d+)_(?P<clli>[A-Za-z0-9]+)')


def parse_layer2_transport(raw, iface_hint=None):
    """
    Parse layer2-transport config into {muse_id: {...}}.

    Handles both line forms (see the note above). `iface_hint` supplies the
    interface when the scoped command stripped it; a value found in the output
    always wins over the hint, since the output is the device speaking.

    Returns (by_muse, unparsed). `unparsed` holds layer2-transport lines whose
    trailing options were not recognised, so a new keyword shows up as
    information rather than vanishing.
    """
    by_muse, unparsed = {}, []
    for ln in (raw or "").splitlines():
        m = _L2T_RE.match(ln)
        if not m:
            continue
        muse = m.group("muse").strip()
        ent = by_muse.setdefault(muse, {
            "muse": muse, "iface": None, "unit": m.group("unit"),
            "remote_tpe": None, "peer_address": None, "label": None,
            "label_vlan": None, "label_clli": None, "pw_type": None,
            "pw_id": None, "l2vpn_id": None, "customer": None,
            "nms_service_id": None, "mtu": None,
        })
        if m.group("iface"):
            ent["iface"] = m.group("iface")
        elif iface_hint and not ent["iface"]:
            ent["iface"] = iface_hint

        rest = (m.group("rest") or "").strip()

        # 'remote-tpe <ip> ...' is a NESTED block: pw-id, pw-type,
        # peer-address, control-word, oam and severity-profile all appear
        # under it as well as (for some) directly under layer2-transport:
        #
        #   ... MUSE_127 pw-type t1-satop
        #   ... MUSE_127 remote-tpe 10.x.x.x pw-type t1-satop
        #   ... MUSE_127 remote-tpe 10.x.x.x pw-id 127
        #
        # The first version captured the address and `continue`d, so every
        # nested option was silently dropped -- pw_id came back None despite
        # being right there in the output. Strip the 'remote-tpe <ip>' prefix
        # and fall through to the same option parser.
        mt = _REMOTE_TPE_RE.match(rest)
        if mt:
            ent["remote_tpe"] = mt.group(1)
            mp = _PEER_ADDR_RE.search(rest)
            if mp:
                ent["peer_address"] = mp.group(1)
            rest = rest[mt.end():].strip()
            if not rest:
                continue

        ms = _SIMPLE_KEY_RE.match(rest)
        if ms:
            key, val = ms.group(1).lower(), ms.group(2).strip()
            if key == "nms-user-label":
                ent["label"] = val
                ml = _LABEL_RE.match(val)
                if ml:
                    ent["label_vlan"] = ml.group("vlan")
                    ent["label_clli"] = ml.group("clli")
            elif key == "pw-type":
                ent["pw_type"] = val
            elif key == "pw-id":
                ent["pw_id"] = val
            elif key == "l2vpn-id":
                ent["l2vpn_id"] = val
            elif key == "customer":
                ent["customer"] = val
            elif key == "nms-service-id":
                ent["nms_service_id"] = val
            elif key == "l2-service-mtu":
                ent["mtu"] = val
            continue

        # Known options with nothing worth storing, or already captured
        # above. `unparsed` exists to surface a keyword this parser has never
        # seen; filling it with ones we deliberately ignore would make it
        # noise, and noise gets ignored.
        if rest and not _KNOWN_IGNORED_RE.match(rest):
            unparsed.append((muse, rest))
    return by_muse, unparsed


def sub_channel_parts(iface):
    """
    'oc12-ts1/1:1:11' -> ('oc12-ts1/1', '1', '11')  == (base, STS, DS1).

    Returns (None, None, None) when the name is not a sub-channel. The CEM
    group forward is STS concatenated with a zero-padded DS1, so these are the
    two numbers that tie an OC sub-channel to a MUX CEM group.
    """
    m = re.match(r'^(?P<base>[a-z0-9\-]+ts\d+/\d+):(?P<sts>\d+):(?P<ds1>\d+)$',
                 (iface or "").strip(), re.IGNORECASE)
    if not m:
        return None, None, None
    return m.group("base"), m.group("sts"), m.group("ds1")


def _self_test():
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        else:
            print("  ok  {} = {!r}".format(label, got))

    # Verbatim ces01.atl1 output, 2026-10-01.
    real = ('set chassis slot ts13 data-card dhxe-4 port xe-ts13/1 '
            'description "bcr01.atl1 Te0/0/0/34"')
    chk("real line parses", parse_chassis_ports(real),
        {"xe-ts13/1": "bcr01.atl1 Te0/0/0/34"})
    chk("far end split", split_far_end("bcr01.atl1 Te0/0/0/34"),
        ("bcr01.atl1", "Te0/0/0/34"))
    chk("bare device name", split_far_end("lax1-mux-02"),
        ("lax1-mux-02", None))

    multi = """
set chassis slot ts13 data-card dhxe-4 port xe-ts13/1 description "bcr01.atl1 Te0/0/0/34"
set chassis slot ts13 data-card dhxe-4 port xe-ts13/10 description "atl1-mux-02 oc12-1"
set chassis slot ts16 data-card dhge-8 port ge-ts16/4 description "lumen handoff"
set interfaces xe-ts13/1 unit 0 description "not a chassis line"
"""
    ports = parse_chassis_ports(multi)
    chk("three chassis ports found", len(ports), 3)
    # ts13/1 must NOT also match ts13/10.
    chk("exact port match", far_end_for_port(multi, "xe-ts13/1")[0],
        "bcr01.atl1")
    chk("ts13/10 is its own port", far_end_for_port(multi, "xe-ts13/10")[0],
        "atl1-mux-02")
    chk("non-chassis description ignored",
        "xe-ts13/1" in ports and ports["xe-ts13/1"].startswith("bcr01"), True)
    chk("absent port", far_end_for_port(multi, "xe-ts99/9"),
        (None, None, None))

    # Command construction
    chk("cmd from port name", port_description_cmd("oc12-ts3/1"),
        "show configuration | display-set | grep ts3/1 | no-more")

    # Cross-check verdicts
    chk("agree", compare_with_netbox("atl1-mux-02", "atl1-mux-02")[0], AGREE)
    chk("agree across domain suffix",
        compare_with_netbox("atl1-mux-02", "atl1-mux-02.bwnet.us")[0], AGREE)
    chk("agree across case",
        compare_with_netbox("ATL1-MUX-02", "atl1-mux-02")[0], AGREE)
    chk("disagree", compare_with_netbox("atl1-mux-02", "lax1-mux-02")[0],
        DISAGREE)
    v, detail = compare_with_netbox("atl1-mux-02", "lax1-mux-02")
    chk("  disagree names both",
        "atl1-mux-02" in detail and "lax1-mux-02" in detail, True)
    chk("device only", compare_with_netbox("atl1-mux-02", None)[0], DEVICE_ONLY)
    chk("netbox only", compare_with_netbox("", "atl1-mux-02")[0], NETBOX_ONLY)
    chk("neither", compare_with_netbox("", "")[0], NEITHER)

    # ---- layer2-transport, BOTH real line forms ----
    # Form A: broad query keeps the interface prefix.
    broad = """set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 customer Curveball
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 description 
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 nms-service-id 127
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 nms-user-label 332_EKHTINXA15T_ACC
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 l2-service-mtu 1500
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 pw-type t1-satop
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 remote-tpe 10.111.130.77 control-word
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 remote-tpe 10.111.130.77 peer-address 10.111.130.78
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 remote-tpe 10.111.130.77 pw-id 127
set interfaces oc12-ts1/1:1:11 unit 0 layer2-transport MUSE_127 l2vpn-id 127
set interfaces oc12-ts1/1:1:11 unit 0 severity-profile default
"""
    bm, unp = parse_layer2_transport(broad)
    chk("broad: one MUSE found", sorted(bm), ["MUSE_127"])
    e = bm["MUSE_127"]
    chk("broad: iface from output", e["iface"], "oc12-ts1/1:1:11")
    chk("broad: remote_tpe", e["remote_tpe"], "10.111.130.77")
    chk("broad: peer_address kept separate", e["peer_address"], "10.111.130.78")
    chk("broad: pw_type", e["pw_type"], "t1-satop")
    chk("broad: pw_id", e["pw_id"], "127")
    chk("broad: l2vpn_id", e["l2vpn_id"], "127")
    chk("broad: customer", e["customer"], "Curveball")
    chk("broad: label", e["label"], "332_EKHTINXA15T_ACC")
    chk("broad: label vlan", e["label_vlan"], "332")
    chk("broad: label clli", e["label_clli"], "EKHTINXA15T")
    chk("broad: nothing unparsed", unp, [])

    # Form B: scoped query STRIPS the interface. Parser must still work, and
    # the caller supplies the name because it is not recoverable from output.
    scoped = """set unit 0 layer2-transport MUSE_127 remote-tpe 10.111.130.77 control-word
set unit 0 layer2-transport MUSE_127 remote-tpe 10.111.130.77 peer-address 10.111.130.78
set unit 0 layer2-transport MUSE_127 remote-tpe 10.111.130.77 pw-id 127
set unit 0 layer2-transport MUSE_127 pw-type t1-satop
"""
    sm, _ = parse_layer2_transport(scoped, iface_hint="oc12-ts1/1:1:11")
    chk("scoped: still parses", sorted(sm), ["MUSE_127"])
    chk("scoped: remote_tpe", sm["MUSE_127"]["remote_tpe"], "10.111.130.77")
    chk("scoped: iface from hint", sm["MUSE_127"]["iface"], "oc12-ts1/1:1:11")
    sm2, _ = parse_layer2_transport(scoped)
    chk("scoped: no hint -> iface None", sm2["MUSE_127"]["iface"], None)

    # Output wins over the hint -- the device is speaking.
    bm2, _ = parse_layer2_transport(broad, iface_hint="WRONG-HINT")
    chk("output beats hint", bm2["MUSE_127"]["iface"], "oc12-ts1/1:1:11")

    # Several MUSE circuits in one dump must not merge.
    multi = broad + broad.replace("oc12-ts1/1:1:11", "oc12-ts1/1:4:11").replace(
        "MUSE_127", "MUSE_990").replace("10.111.130.77", "10.111.130.99")
    mm, _ = parse_layer2_transport(multi)
    chk("two MUSE circuits kept apart", sorted(mm), ["MUSE_127", "MUSE_990"])
    chk("second one's tpe", mm["MUSE_990"]["remote_tpe"], "10.111.130.99")
    chk("second one's iface", mm["MUSE_990"]["iface"], "oc12-ts1/1:4:11")

    # An unrecognised keyword must surface, not vanish.
    _, unp2 = parse_layer2_transport(
        "set unit 0 layer2-transport MUSE_1 some-new-knob 42")
    chk("new keyword surfaces", unp2, [("MUSE_1", "some-new-knob 42")])

    # Sub-channel split -- these two numbers tie an OC to a MUX CEM group.
    chk("sub-channel parts", sub_channel_parts("oc12-ts1/1:1:11"),
        ("oc12-ts1/1", "1", "11"))
    chk("sub-channel parts (4:11)", sub_channel_parts("oc12-ts1/1:4:11"),
        ("oc12-ts1/1", "4", "11"))
    chk("oc- prefix variant", sub_channel_parts("oc-ts3/1:4:11"),
        ("oc-ts3/1", "4", "11"))
    chk("base port is not a sub-channel", sub_channel_parts("oc12-ts1/1"),
        (None, None, None))

    if fails:
        print("\nFAILED:")
        for f in fails:
            print("  !! " + f)
    else:
        print("\nAll chassis_config self-tests passed.")
    return not fails


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
