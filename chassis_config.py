#!/usr/bin/env python3
"""
chassis_config.py -- read a port's CONFIGURED far-end description off an 1800.

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
