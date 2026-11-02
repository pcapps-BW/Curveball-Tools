#!/usr/bin/env python3
"""
loop_check.py -- detect a CONFIGURED maintenance loopback on a DS1.

WHY THIS MATTERS

A looped DS1 returns CLEAN performance monitors, because it is looping its
own signal back -- so "PMs clean" on a looped circuit is the single most
misleading thing a PM tool can report. Anyone reading PMs needs to know a
loop is in place first. (pcapps, item #3.)

WHAT IT DETECTS, AND WHAT IT DOES NOT

Grounded on the `maintenance loopback` config verb the suite already knows
how to SET (ds1_gsx_test's MOP generator):

    1250 TDM : set chassis slot tsX ces-card <card> port ds1/N
                   maintenance loopback terminal|facility
    1800 OCx : set chassis slot tsX ces-card <card> port ocM/P
                   sts-1 S vt-15 V ds1 maintenance loopback terminal|facility

`release` removes the statement, so any `maintenance loopback terminal` or
`... facility` line present in `show configuration | display-set` is an
ACTIVE loop. That is the whole detection: parse those lines out of the
running config. No extra device command beyond a config grep.

NOT covered here, because the suite has no confirmed READ sample of a loop on
them (would be guessing device output, which this project does not do):
  - MUX CEM hairpin loops (both sides)
  - GSX T1 loops
  - GE / fiber interface loops on the 1250 or 1800
These need the detection command or a looped-interface sample before they can
be added. Tracked in TODO.md.

⚠ UNVERIFIED AGAINST LIVE: built against the set-command line shape, not a
captured looped-config dump. The first real looped circuit confirms the
format; the parser is structured so a near-miss shows up as a missed loop
(safe-ish for PMs) rather than a false one.

Built by P. Capps.
"""

import re

# The config-grep command a tool runs to feed this parser. `| no-more` so the
# output is not paged (Ribbon/NPT rule).
LOOP_CONFIG_CMD = ('show configuration | display-set | '
                   'match "maintenance loopback" | no-more')

# A set statement that establishes a loop. 'release' is deliberately NOT here:
# releasing removes the statement, so it never persists in the config.
_LOOP_RE = re.compile(
    r'\bport\s+(?P<port>\S+)'                # port ds1/5  OR  port oc12/1
    r'(?P<mid>.*?)'                          # (1800: sts-1 S vt-15 V ds1 ...)
    r'\bmaintenance\s+loopback\s+'
    r'(?P<type>terminal|facility)\b',
    re.IGNORECASE)

# slot token, e.g. 'slot ts13'
_SLOT_RE = re.compile(r'\bslot\s+(ts\d+)\b', re.IGNORECASE)
# the ds1 index inside a 1250 'port ds1/N' or an 1800 '... ds1 N'/'ds1/N'
_DS1_PORT_RE = re.compile(r'\bds1/(\d+)\b', re.IGNORECASE)
# 1800 sub-structure: sts-1 S vt-15 V
_STS_VT_RE = re.compile(r'\bsts-1\s+(\d+)\s+vt-15\s+(\d+)\b', re.IGNORECASE)


def parse_loops(config_text):
    """
    Every active maintenance loopback in a display-set config dump.

    Returns a list of dicts:
      {"slot":  'ts13' or None,
       "port":  the port token ('ds1/5', 'oc12/1', ...),
       "ds1":   the DS1 index as a string, or None,
       "sts":   '10' (1800 only) or None,
       "vt":    '1'  (1800 only) or None,
       "type":  'terminal' | 'facility',
       "raw":   the whole config line}

    Empty list when nothing is looped -- which is the normal, healthy case.
    """
    out = []
    for line in (config_text or "").splitlines():
        m = _LOOP_RE.search(line)
        if not m:
            continue
        slot = _SLOT_RE.search(line)
        sts_vt = _STS_VT_RE.search(line)
        # DS1 index: '/N' form on the 1250, or a trailing 'ds1 N' on the 1800.
        ds1 = _DS1_PORT_RE.search(line)
        ds1_num = ds1.group(1) if ds1 else None
        if ds1_num is None:
            m2 = re.search(r'\bds1\s+(\d+)\b', line, re.IGNORECASE)
            ds1_num = m2.group(1) if m2 else None
        out.append({
            "slot": slot.group(1).lower() if slot else None,
            "port": m.group("port"),
            "ds1": ds1_num,
            "sts": sts_vt.group(1) if sts_vt else None,
            "vt": sts_vt.group(2) if sts_vt else None,
            "type": m.group("type").lower(),
            "raw": line.strip(),
        })
    return out


def _ds1_slot_num(ds1):
    """'ts1/26' or 'ds1-ts1/26' -> ('ts1', '26'). (None, None) on junk."""
    m = re.search(r'(ts\d+)/(\d+)', (ds1 or ""), re.IGNORECASE)
    return (m.group(1).lower(), m.group(2)) if m else (None, None)


def loops_for_ds1(config_text, ds1):
    """
    Active loops that apply to ONE DS1 (e.g. 'ts1/26').

    Matches on slot + DS1 index. A 1250 loop line carries both; a loop whose
    line has no parseable DS1 index is returned too (better to over-warn
    about a loop than to miss one -- a loop silently ignored makes clean PMs
    look trustworthy).
    """
    want_slot, want_num = _ds1_slot_num(ds1)
    hits = []
    for lp in parse_loops(config_text):
        if lp["ds1"] is not None and want_num is not None:
            if lp["ds1"] != want_num:
                continue
            if lp["slot"] and want_slot and lp["slot"] != want_slot:
                continue
            hits.append(lp)
        elif lp["ds1"] is None:
            # Loop line we could not pin to a DS1 -- surface it rather than
            # drop it.
            hits.append(lp)
    return hits


def loop_banner(loops, ds1=None):
    """
    One-line warning for a report, or '' when nothing is looped.

    The wording leads with the consequence, because the point is that the PMs
    below cannot be trusted through a loop.
    """
    if not loops:
        return ""
    kinds = sorted({lp["type"] for lp in loops})
    where = " on {}".format(ds1) if ds1 else ""
    faces = {"terminal": "faces BW core", "facility": "faces LEC"}
    detail = "; ".join(
        "{} ({})".format(lp["type"], faces.get(lp["type"], "?"))
        for lp in loops)
    return ("[ !! ] MAINTENANCE LOOP CONFIGURED{} -- {}. PMs read through a "
            "loop show CLEAN regardless of the real circuit; do not trust "
            "them until the loop is released.".format(where, detail or
                                                      ", ".join(kinds)))


# ---- GSX T1 loop -----------------------------------------------------------
# Confirmed from real captures (LAXGSX04, 2026-07-08): `SHOW T1 <id> STATUS`
# carries a `Loopback Status:` line, `NONE` when not looped. Every GSX hop in
# the suite already pulls that command, so detecting a GSX T1 loop needs no
# extra command -- just read the field out of the status text already in hand.
_GSX_LOOP_RE = re.compile(r'^\s*Loopback\s+Status\s*:\s*(\S+)', re.I | re.M)


def gsx_loop_from_t1_status(t1_status_raw):
    """
    The GSX T1 loopback state, or None when not looped / not found.

    Returns the raw value (e.g. 'LINE', 'PAYLOAD', 'LOCAL') when the field is
    present and not NONE. 'NONE' and a missing field both return None -- a
    loop that is not there is not a warning.
    """
    m = _GSX_LOOP_RE.search(t1_status_raw or "")
    if not m:
        return None
    val = m.group(1).strip()
    return None if val.upper() in ("NONE", "NOLOOP", "") else val


# ---- MUX controller loopback (standard Cisco) ------------------------------
# Confirmed from the Cisco NCS 4200 48-port T1/E1 CEM config guide (IOS XE
# Fuji 16.7.x), "Configuring Loopback": a loopback is a controller statement,
#
#     controller t1 0/0/1
#      loopback local line        (loops the line signal back -- toward LEC)
#      loopback network line      (loops toward the packet/PSN side -- E1)
#      loopback diag
#
# removed with `no loopback`. The 4216 is channelized T3->T1, so the statement
# sits under the T1 (or T3) controller. The suite already reads the MUX
# controller config (`show run | b controller T3`), so this is a config grep,
# no new command -- and it is standard documented Cisco, not the suite's
# bespoke hairpin. This is the primary MUX loop check.
_MUX_CTRL_HDR_RE = re.compile(r'^\s*controller\s+(T[13]\s+\S+)', re.I)
_MUX_LOOP_RE = re.compile(r'^\s*loopback\s+(local|network|diag)\b', re.I)


def mux_controller_loops(config_text):
    """
    Active controller loopbacks in MUX running-config, with their controller.

    Returns a list of {"controller", "mode", "raw"}. `mode` is 'local' (faces
    the LEC/line side), 'network' (faces the packet/PSN side) or 'diag'.
    `no loopback` is never a statement that persists, so any `loopback ...`
    line present is active.
    """
    out, current = [], None
    for line in (config_text or "").splitlines():
        h = _MUX_CTRL_HDR_RE.match(line)
        if h:
            current = h.group(1).strip()
            continue
        if line.strip() == "!":
            current = None
            continue
        # A 'no loopback' line would start with 'no', which _MUX_LOOP_RE's
        # start-anchored \s*loopback will not match -- but guard anyway.
        if re.match(r'^\s*no\s+loopback\b', line, re.I):
            continue
        m = _MUX_LOOP_RE.match(line)
        if m:
            out.append({"controller": current, "mode": m.group(1).lower(),
                        "raw": line.strip()})
    return out


# ---- MUX CEM hairpin loop --------------------------------------------------
# A production CEM connection joins two DIFFERENT legs (the ACR/1250 side and
# the GSX side). A hairpin loop points one leg back at ITSELF, so both sides
# of the `connect` are identical -- that is the whole fingerprint, taken from
# the MOP mux_loop_script generates:
#
#   production : connect <name> CEM-ACR<acr> <cem>   CEM<gsx_ce> <gsx_id>
#   GSX loop   : connect <name> CEM<gsx_ce> <gsx_id> CEM<gsx_ce> <gsx_id>
#   1250 loop  : connect <name> CEM-ACR<acr> <cem>   CEM-ACR<acr> <cem>
#
# ⚠ UNVERIFIED against a live `show connection name` / `show run` dump -- the
# fingerprint (two equal legs) is from the set-command shape, so the exact
# output format is confirm-live.
_CONNECT_RE = re.compile(
    r'\bconnect\s+(?P<name>\S+)\s+(?P<a>CEM\S*\s+\S+)\s+(?P<b>CEM\S*\s+\S+)',
    re.IGNORECASE)


def mux_hairpin_from_connect(connect_raw):
    """
    Detect a CEM hairpin loop in `show connection name` / `show run` output.

    Returns {"name", "leg", "faces"} when both legs of the connection are
    identical (a hairpin), else None. `faces` reads the leg: a CEM-ACR leg
    loops the ACR/1250 side, a plain CEM leg loops the GSX side.
    """
    for line in (connect_raw or "").splitlines():
        m = _CONNECT_RE.search(line)
        if not m:
            continue
        a = re.sub(r'\s+', ' ', m.group("a").strip()).upper()
        b = re.sub(r'\s+', ' ', m.group("b").strip()).upper()
        if a == b:
            faces = ("1250/LEC side" if a.startswith("CEM-ACR")
                     else "GSX side")
            return {"name": m.group("name"), "leg": m.group("a").strip(),
                    "faces": faces}
    return None


def self_test(verbose=True):
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    # 1250 TDM loop, terminal.
    C1250 = ("set chassis slot ts1 ces-card mse1-32 port ds1/26 "
             "maintenance loopback terminal")
    lp = parse_loops(C1250)
    chk("1250 terminal loop parsed", len(lp), 1)
    chk("1250 slot", lp[0]["slot"], "ts1")
    chk("1250 ds1 index", lp[0]["ds1"], "26")
    chk("1250 type", lp[0]["type"], "terminal")

    # 1800 OCx loop, facility, with sts/vt.
    C1800 = ("set chassis slot ts13 ces-card cesm-10g port oc12/1 "
             "sts-1 10 vt-15 1 ds1 maintenance loopback facility")
    lp = parse_loops(C1800)
    chk("1800 facility loop parsed", len(lp), 1)
    chk("1800 sts", lp[0]["sts"], "10")
    chk("1800 vt", lp[0]["vt"], "1")
    chk("1800 type", lp[0]["type"], "facility")

    # A released / absent loop: nothing to find.
    chk("no loop in a clean config",
        parse_loops("set chassis slot ts1 ces-card mse1-32 port ds1/26 "
                    "framing esf"), [])
    # 'release' must never be read as an active loop (it would not persist,
    # but guard anyway).
    chk("release is not an active loop",
        parse_loops("set ... port ds1/26 maintenance loopback release"), [])

    # Per-DS1 matching.
    cfg = C1250 + "\n" + ("set chassis slot ts1 ces-card mse1-32 port ds1/5 "
                          "maintenance loopback facility")
    chk("loops_for_ds1 matches the right DS1",
        [x["type"] for x in loops_for_ds1(cfg, "ts1/26")], ["terminal"])
    chk("loops_for_ds1 ignores a different DS1",
        loops_for_ds1(cfg, "ts1/27"), [])
    chk("loops_for_ds1 respects the slot",
        loops_for_ds1(cfg, "ts9/26"), [])
    chk("ds1-prefixed form also matches",
        len(loops_for_ds1(cfg, "ds1-ts1/5")), 1)

    # GSX T1 loopback status (confirmed field from real LAXGSX04 capture).
    _t1 = ("T1 Status\nCircuit Name: T1-1-11-2-14\n"
           "Operational Status: UP\nLoopback Status:  NONE\n"
           "Sending Test Pattern: NONE")
    chk("GSX not looped -> None", gsx_loop_from_t1_status(_t1), None)
    chk("GSX looped (LINE) detected",
        gsx_loop_from_t1_status(_t1.replace("Loopback Status:  NONE",
                                            "Loopback Status:  LINE")), "LINE")
    chk("GSX missing field -> None",
        gsx_loop_from_t1_status("T1 Status\nOperational Status: UP"), None)

    # MUX controller loopback (standard Cisco, grounded on the NCS 4200 doc).
    _cfg = ("controller T3 8/1/12\n t1 1 cem-group 101 unframed\n"
            " loopback local line\n!\n"
            "controller T3 8/1/13\n framing c-bit\n!\n")
    _ml = mux_controller_loops(_cfg)
    chk("MUX controller loopback detected", len(_ml), 1)
    chk("MUX loop mode + controller",
        (_ml[0]["mode"], _ml[0]["controller"]), ("local", "T3 8/1/12"))
    chk("MUX network loopback detected",
        mux_controller_loops("controller T1 0/0/1\n loopback network line")[0]["mode"],
        "network")
    chk("no loopback is not an active loop",
        mux_controller_loops("controller T1 0/0/1\n no loopback"), [])
    chk("clean controller config -> no loop",
        mux_controller_loops("controller T3 8/1/12\n framing c-bit\n!"), [])

    # MUX CEM hairpin.
    _prod = "connect MYCONN CEM-ACR26 723  CEM8/2/47 113"
    _gsxl = "connect MYCONN CEM8/2/47 113  CEM8/2/47 113"
    _acrl = "connect MYCONN CEM-ACR26 723  CEM-ACR26 723"
    chk("production connection is not a hairpin",
        mux_hairpin_from_connect(_prod), None)
    chk("GSX-side hairpin detected",
        (mux_hairpin_from_connect(_gsxl) or {}).get("faces"), "GSX side")
    chk("1250-side hairpin detected",
        (mux_hairpin_from_connect(_acrl) or {}).get("faces"), "1250/LEC side")

    # Banner.
    chk("no banner when clean", loop_banner([]), "")
    b = loop_banner(loops_for_ds1(C1250, "ts1/26"), "ts1/26")
    chk("banner warns and names the risk",
        ("MAINTENANCE LOOP CONFIGURED" in b and "ts1/26" in b
         and "do not trust" in b), True)

    print()
    if fails:
        print("FAILED:")
        for f in fails:
            print("  " + f)
        return False
    print("All loop_check self-tests passed.")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if self_test() else 1)
