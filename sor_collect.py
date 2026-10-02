#!/usr/bin/env python3
"""
sor_collect.py -- collect one site's circuits into the inventory cache.

WHY THIS TAKES NO PARSER

`autotester_standalone.run()` already resolves all 11 schema fields and hands
them back structurally when the caller passes an empty dict as
`params["_extra_out"]` -- `mux_results`, `ds1_map` and `host_1250`. That
handback already has two consumers (`morning_check`, `reverse_autotester`),
so this is an established path, not a new one.

The alternative considered and rejected: parse the 407 archived
`Autotester_*.txt` reports in C:\\Claude. Those span June to October across
roughly a hundred releases and the report format DRIFTED at least three times
(`ACR Group : 16` in June, an inline `SONET 0/5/4  ACR 53  CEM 401` in
August, back to `ACR Group : 16` in October). A parser written against the
current format silently skips or misreads the older files, and silent partial
coverage of an inventory is the exact failure this suite exists to avoid.
Reading the structured handback has no format to drift.

SCOPE: ONE SITE

Deliberately. The expensive problems -- concurrency across ~200 devices,
partial sweeps, retry, runtime -- are all deferred. A working one-site
collector is NOT "nearly done" on a fleet collector; see SOR_DESIGN_NOTE.md.

Cost per run: one site is one 1250 and up to 28 DS1s, about four device
logins (1250, 1800, MUX, GSX) -- the same as one forward Autotester run. It
is read-only throughout, because it IS a forward Autotester run.

Built by P. Capps.
"""

import datetime

import platform_paths
import sor_store
import autotester_standalone
from device_base import CredentialError

TOOL_INFO = {
    "name": "SoR Collect (one site)",
    "ran_from": "1250",
    "desc": ("Collects one 1250's circuits into the inventory cache and "
             "reports anything that changed since last time."),
    "requires": "1250 IP, infra creds, GSX creds",
    "order": 12,
}


def _gsx_assignment(mux):
    """slot-port-t1 as one string, or '' when the GSX hop did not resolve."""
    parts = [mux.get("gsx_slot"), mux.get("gsx_port"), mux.get("gsx_t1")]
    if not all(p not in (None, "", "?") for p in parts):
        return ""
    return "-".join(str(p) for p in parts)


def map_fields(ds1, host_1250, ds1_map, mux_results, oc12_results=None):
    """
    One DS1's observation dict, keyed to sor_store.FIELDS.

    Only resolved fields are included. A field left OUT is untouched by
    sor_store.observe, which is what makes a partial collection safe -- a run
    that could not reach the MUX must not blank the MUX-derived values it
    recorded last week.
    """
    info = (ds1_map or {}).get(ds1) or {}
    mux = (mux_results or {}).get(ds1) or {}
    oc12 = (oc12_results or {}).get(ds1) or ""

    # The GSX node has two sources and the forward run cross-checks them
    # (v2.28). Prefer what the DEVICE called itself; fall back to the MUX
    # port description. On a NAME CONFLICT the collector stores nothing
    # rather than picking a side -- see collect_site.
    gsx_node = (mux.get("gsx_node_device") or mux.get("gsx_node") or "")

    out = {
        "host_1250":      host_1250,
        "ds1":            ds1,
        "loopback_ip":    info.get("loopback_ip"),
        "muse_id":        info.get("muse_id"),
        "hub_ip":         info.get("hub_ip"),
        "oc12":           oc12,
        "mux_name":       mux.get("mux_name"),
        "acr_group":      mux.get("acr_group"),
        "cem_group":      mux.get("cem_group"),
        "gsx_node":       gsx_node,
        "gsx_assignment": _gsx_assignment(mux),
    }
    return {k: v for k, v in out.items() if v not in (None, "", "?")}


def collect_site(ip_1250, infra_user, infra_pass, gsx_user, gsx_pass,
                 state=None, progress=print, _runner=None):
    """
    Run one forward trace and fold the result into the cache.

    Returns (state, report) where report is a dict of what happened. Never
    raises except CredentialError -- a collection failure must not look like
    a crash, and must not look like a successful empty collection either.
    """
    state = sor_store.load() if state is None else state
    sor_store.migrate(state, progress)
    runner = _runner or autotester_standalone.run

    report = {"ip_1250": ip_1250, "host_1250": "", "ds1s": 0,
              "changes": [], "skipped": [], "error": None,
              "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    extra = {}
    try:
        runner({
            "ip_1250": ip_1250,
            # Empty list = every DS1 the 1250 reports, which is what an
            # inventory wants. A named DS1 list would collect a slice and
            # leave the rest looking deprovisioned.
            "ds1_list": [],
            "infra_user": infra_user, "infra_pass": infra_pass,
            "gsx_user": gsx_user, "gsx_pass": gsx_pass,
            "jira_ticket": None, "slack_webhook": None,
            "open_when_done": False,
            "_extra_out": extra,
        })
    except CredentialError:
        raise
    except Exception as e:
        report["error"] = "{}: {}".format(type(e).__name__, e)
        progress("  [!] collection failed: {}".format(report["error"]))
        # Could not reach the device -- mark what we already knew as
        # unverified rather than deleting it or silently keeping it as
        # current. A missing row and an unreachable device mean opposite
        # things.
        host = _host_for_ip(state, ip_1250)
        if host:
            n = sor_store.mark_unverified(state, host, reason=report["error"],
                                          when=report["when"])
            report["unverified"] = n
            progress("  marked {} existing row(s) unverified for {}".format(
                n, host))
        return state, report

    host_1250 = extra.get("host_1250") or ""
    mux_results = extra.get("mux_results") or {}
    ds1_map = extra.get("ds1_map") or {}
    oc12_results = extra.get("oc12_results") or {}
    report["host_1250"] = host_1250

    if not host_1250:
        # Without the device's own hostname there is no safe row key. The
        # 1250's hostname is the authority (v2.27's lesson on the GSX), so
        # storing under an IP would fork the row the moment the name is
        # known.
        report["error"] = ("1250 did not report a hostname -- refusing to "
                           "store rows under an IP")
        progress("  [!] {}".format(report["error"]))
        return state, report

    for ds1 in sorted(ds1_map):
        mux = mux_results.get(ds1) or {}
        # A GSX identity conflict means the counters and the assignment may
        # belong to a different chassis (v2.28). Record the circuit but NOT
        # the GSX fields -- storing a contested value as fact is how a cache
        # becomes worse than no cache.
        values = map_fields(ds1, host_1250, ds1_map, mux_results,
                            oc12_results)
        if mux.get("gsx_name_verdict") == "conflict":
            values.pop("gsx_node", None)
            values.pop("gsx_assignment", None)
            report["skipped"].append(
                "{}: GSX fields withheld -- {}".format(
                    ds1, mux.get("gsx_name_conflict") or "name conflict"))
        changes = sor_store.observe(state, host_1250, ds1, values,
                                    source="device", when=report["when"])
        report["changes"].extend(changes)
        report["ds1s"] += 1

    progress("  {} -- {} DS1(s) recorded, {} change(s)".format(
        host_1250, report["ds1s"], len(report["changes"])))
    for c in report["changes"]:
        progress("  [CHANGED] {} {} : {} -> {}  (was last seen {})".format(
            c["key"], c["field"], c["old"], c["new"], c["old_last_seen"]))
    for s in report["skipped"]:
        progress("  [!] {}".format(s))
    return state, report


def _host_for_ip(state, ip):
    """Last known hostname for a 1250 loopback, so a failed run can mark it."""
    for row in sor_store.rows(state):
        if row.get("loopback_ip") == ip:
            return row.get("host_1250")
    return ""


def render(report, state):
    """The text report for the file and the web log."""
    s = sor_store.summary(state)
    SEP = "=" * 64
    out = [SEP, "  SoR COLLECTION -- {}".format(report["when"]), SEP, ""]
    if report["error"]:
        out += ["  COLLECTION FAILED", "    {}".format(report["error"])]
        if report.get("unverified"):
            out.append("    {} existing row(s) marked unverified (NOT "
                       "deleted)".format(report["unverified"]))
        out += ["", "  The cache is unchanged apart from that mark.", ""]
    else:
        out += ["  Site     : {} ({})".format(report["host_1250"],
                                              report["ip_1250"]),
                "  Recorded : {} DS1(s)".format(report["ds1s"]),
                "  Changed  : {}".format(len(report["changes"])), ""]
        if report["changes"]:
            out += ["-" * 58, "  CHANGES SINCE LAST COLLECTION", "-" * 58]
            for c in report["changes"]:
                out += ["  {} {}".format(c["key"], c["field"]),
                        "      was {!r} (last seen {})".format(
                            c["old"], c["old_last_seen"]),
                        "      now {!r}".format(c["new"])]
            out.append("")
        else:
            out += ["  Nothing moved since the last collection.", ""]
        for sk in report["skipped"]:
            out.append("  [!] {}".format(sk))
        if report["skipped"]:
            out.append("")
    out += ["-" * 58,
            "  CACHE: {} row(s), {} device(s), {} recorded change(s), "
            "{} unverified".format(s["rows"], s["devices"], s["changes"],
                                   s["unverified"]),
            "-" * 58,
            "  This is a CACHE of what the devices said, not an authority.",
            "  NetBox claims only {} of the 11 fields ({}); for the other "
            "seven".format(len(sor_store.NETBOX_CLAIMS),
                           ", ".join(sor_store.NETBOX_CLAIMS)),
            "  this is the only durable record outside the devices.",
            SEP]
    return "\n".join(out)


def collect_inputs(session=None):
    import getpass
    session = session if session is not None else {}
    print()
    print("SoR Collect -- records one 1250's circuits into the inventory "
          "cache.")
    print("Read-only: it runs the forward Autotester and stores the result.")
    print()
    ip = (session.get("ip_1250")
          or input("1250 IP: ").strip())
    if not ip:
        print("No IP.")
        return None
    user = session.get("infra_user") or input("Infrastructure username: ").strip()
    pw = session.get("infra_pass") or getpass.getpass("Infrastructure password: ")
    guser = session.get("gsx_user") or input("GSX username: ").strip()
    gpw = session.get("gsx_pass") or getpass.getpass("GSX password: ")
    if not (user and pw and guser and gpw):
        print("Missing credentials.")
        return None
    return {"ip_1250": ip, "username": user, "password": pw,
            "gsx_user": guser, "gsx_pass": gpw, "open_when_done": True}


def run(params):
    progress = params.get("progress") or (lambda s="": print(s))
    state, report = collect_site(
        params["ip_1250"], params["username"], params["password"],
        params["gsx_user"], params["gsx_pass"], progress=progress)

    # Save even on failure: the unverified marks are part of the record.
    sor_store.save(state)

    body = render(report, state)
    progress("")
    progress(body)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = (report["host_1250"] or params["ip_1250"]).replace("/", "_")
    out_path = platform_paths.path("SoR_Collect_{}_{}.txt".format(stamp, tag))
    try:
        with open(out_path, "w") as f:
            f.write(body)
        progress("  written: {}".format(out_path))
    except Exception as e:
        progress("  [!] could not write {}: {}".format(out_path, e))
        return None
    if params.get("open_when_done"):
        import os
        if os.name == "nt":
            try:
                os.startfile(out_path)      # noqa: S606
            except Exception:
                pass
    return out_path


def self_test(verbose=True):
    """`python sor_collect.py --self-test` -- stubbed, no devices."""
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    DS1_MAP = {
        "ts1/27": {"muse_id": "MUSE_972", "hub_ip": "10.111.134.255",
                   "loopback_ip": "10.9.9.9"},
        "ts1/28": {"muse_id": "MUSE_973", "hub_ip": "10.111.134.255",
                   "loopback_ip": "10.9.9.9"},
    }
    MUX = {
        "ts1/27": {"mux_name": "DFW1-MUX-04", "acr_group": "26",
                   "cem_group": "723", "gsx_node": "DFWGSX04",
                   "gsx_node_device": "DFWGSX04", "gsx_slot": "6",
                   "gsx_port": "1", "gsx_t1": "23",
                   "gsx_name_verdict": "confirmed"},
        "ts1/28": {"mux_name": "DFW1-MUX-04", "acr_group": "26",
                   "cem_group": "724", "gsx_node": "DFWGSX04",
                   "gsx_node_device": "DFWGSX04", "gsx_slot": "6",
                   "gsx_port": "1", "gsx_t1": "24",
                   "gsx_name_verdict": "confirmed"},
    }
    OC12 = {"ts1/27": "oc12-ts1/1:7:23", "ts1/28": "oc12-ts1/1:7:24"}

    def fake_run(mux=None, ds1_map=None, oc12=None, host="MDLDTXMU542",
                 boom=None):
        def _r(params):
            if boom:
                raise boom
            params["_extra_out"].update(
                host_1250=host,
                mux_results=mux if mux is not None else MUX,
                ds1_map=ds1_map if ds1_map is not None else DS1_MAP,
                oc12_results=oc12 if oc12 is not None else OC12)
            return "/tmp/fwd.txt"
        return _r

    # ---- field mapping ----
    vals = map_fields("ts1/27", "MDLDTXMU542", DS1_MAP, MUX, OC12)
    chk("all 11 fields map from the handback", len(vals), 11)
    chk("gsx_assignment is built from slot-port-t1",
        vals["gsx_assignment"], "6-1-23")
    chk("gsx_node prefers the device's own name", vals["gsx_node"], "DFWGSX04")
    # An unresolved GSX hop must omit the assignment, not emit "?-?-?".
    partial = {"ts1/27": {"mux_name": "DFW1-MUX-04", "gsx_slot": "?"}}
    pv = map_fields("ts1/27", "MDLDTXMU542", DS1_MAP, partial, {})
    chk("unresolved GSX omits the assignment", "gsx_assignment" in pv, False)
    chk("and does not invent an oc12", "oc12" in pv, False)
    chk("but still records what it does know", pv["mux_name"], "DFW1-MUX-04")

    # ---- first collection ----
    st, rep = collect_site("10.9.9.9", "u", "p", "g", "p", state={},
                           progress=lambda *a: None,
                           _runner=fake_run())
    chk("both DS1s recorded", rep["ds1s"], 2)
    chk("first collection reports no changes", len(rep["changes"]), 0)
    chk("cache has two rows", sor_store.summary(st)["rows"], 2)

    # ---- unchanged re-collection ----
    st, rep = collect_site("10.9.9.9", "u", "p", "g", "p", state=st,
                           progress=lambda *a: None, _runner=fake_run())
    chk("unchanged re-collection reports nothing moved",
        len(rep["changes"]), 0)

    # ---- a moved ACR ----
    moved = {k: dict(v) for k, v in MUX.items()}
    moved["ts1/27"]["acr_group"] = "52"
    st, rep = collect_site("10.9.9.9", "u", "p", "g", "p", state=st,
                           progress=lambda *a: None,
                           _runner=fake_run(mux=moved))
    chk("a moved ACR is detected", len(rep["changes"]), 1)
    chk("the change is on the right DS1 and field",
        (rep["changes"][0]["field"], rep["changes"][0]["old"],
         rep["changes"][0]["new"]), ("acr_group", "26", "52"))
    chk("the report mentions the change",
        "CHANGES SINCE LAST COLLECTION" in render(rep, st), True)

    # ---- unreachable device ----
    st, rep = collect_site("10.9.9.9", "u", "p", "g", "p", state=st,
                           progress=lambda *a: None,
                           _runner=fake_run(boom=RuntimeError("SSH timeout")))
    chk("failure is reported, not raised", bool(rep["error"]), True)
    chk("rows are NOT deleted", sor_store.summary(st)["rows"], 2)
    chk("rows are marked unverified", rep.get("unverified"), 2)
    chk("the failure report says rows were kept",
        "NOT\n    deleted" in render(rep, st) or
        "marked unverified (NOT" in render(rep, st), True)
    # A later success clears it.
    st, rep = collect_site("10.9.9.9", "u", "p", "g", "p", state=st,
                           progress=lambda *a: None,
                           _runner=fake_run(mux=moved))
    chk("a successful collection clears unverified",
        sor_store.summary(st)["unverified"], 0)

    # ---- credentials must still propagate ----
    try:
        collect_site("10.9.9.9", "u", "p", "g", "p", state={},
                     progress=lambda *a: None,
                     _runner=fake_run(boom=CredentialError("bad", cred_type="gsx")))
        chk("CredentialError propagates", "no raise", "raise")
    except CredentialError:
        chk("CredentialError propagates", True, True)

    # ---- no hostname -> refuse to store ----
    st2, rep2 = collect_site("10.9.9.9", "u", "p", "g", "p", state={},
                             progress=lambda *a: None,
                             _runner=fake_run(host=""))
    chk("no hostname means no rows stored", sor_store.summary(st2)["rows"], 0)
    chk("and the refusal is explained",
        "refusing to store rows under an IP" in (rep2["error"] or ""), True)

    # ---- GSX name conflict -> withhold the contested fields ----
    conflict = {k: dict(v) for k, v in MUX.items()}
    conflict["ts1/27"]["gsx_name_verdict"] = "conflict"
    conflict["ts1/27"]["gsx_name_conflict"] = "MUX says DFWGSX04, device says DFWGSX09"
    st3, rep3 = collect_site("10.9.9.9", "u", "p", "g", "p", state={},
                             progress=lambda *a: None,
                             _runner=fake_run(mux=conflict))
    row = [r for r in sor_store.rows(st3) if r["ds1"] == "ts1/27"][0]
    chk("a contested GSX node is not stored", row["gsx_node"], "")
    chk("nor the contested assignment", row["gsx_assignment"], "")
    chk("but the rest of the circuit is", row["acr_group"], "26")
    chk("and the withholding is reported", len(rep3["skipped"]), 1)
    chk("the other DS1 is unaffected",
        [r for r in sor_store.rows(st3) if r["ds1"] == "ts1/28"][0]["gsx_node"],
        "DFWGSX04")

    # ---- the handback really provides what this module reads -------------
    # A stub supplies whatever it is told to, so it will happily hide a key
    # the real run() never sets. That is exactly what happened: this module
    # read `oc12_results` and autotester_standalone did not hand it back, so
    # the OC12 field would have stayed silently empty on every real
    # collection while every test passed. This check reads the SOURCE for
    # the keys it actually assigns.
    import re as _re
    import os as _os
    _src = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "autotester_standalone.py"),
                encoding="utf-8").read()
    _handback = set(_re.findall(r'extra\["(\w+)"\]', _src))
    for _needed in ("host_1250", "mux_results", "ds1_map", "oc12_results"):
        chk("run() hands back {!r}".format(_needed),
            _needed in _handback, True)

    # ---- the report states what it is ----
    txt = render(rep, st)
    chk("report says it is a cache, not an authority",
        "not an authority" in txt, True)
    chk("report names how many fields NetBox claims",
        "only 4 of the 11 fields" in txt, True)

    print()
    if fails:
        print("FAILED:")
        for f in fails:
            print("  " + f)
        return False
    print("All sor_collect self-tests passed.")
    return True


def main():
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    main()
