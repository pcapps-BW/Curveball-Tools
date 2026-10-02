#!/usr/bin/env python3
"""
sor_store.py -- persistence for the circuit inventory cache (the "SoR").

WHAT THIS IS, AND WHAT IT IS NOT

It is a CACHE of what the devices said, with timestamps. It is not an
authority. pcapps, 2026-10-01: "cache, the whole point of this is we cant
trust netbox 100%." So:

  * the device always wins -- nothing here is ever edited by hand
  * disagreements are REPORTED, never resolved
  * there is no approval workflow and no edit path, by design

NetBox holds only 4 of the 11 fields (hostname, MUX name, the OC12 facility
name, and a GSX node name derivation). The other seven -- loopback, DS1,
MUSE, hub IP, ACR, CEM, GSX assignment -- exist NOWHERE but on the devices.
For those this file is the only durable record there is, and the drift it
catches is device-vs-its-own-past: an ACR that moved, a CEM reprovisioned, a
GSX assignment changed without a ticket.

THE ONE DESIGN DECISION THAT MATTERS

**A changed value is an event, not an overwrite.** A cache that keeps only
"latest" can tell you what is true now but not that anything MOVED, and "did
this change?" is the question being asked. So every field keeps its history.
Retrofitting that later would mean throwing away everything collected in the
meantime.

Storage: versioned JSON under platform_paths, following ospf_stability.py --
atomic write, corrupt-tolerant load, explicit schema version with a
migration hook. There is no SQL anywhere in this suite and introducing
SQLite would mean a second storage idiom to maintain; at ~200 devices x ~28
DS1s the dataset is far too small for that to be the constraint.

Built by P. Capps.
"""

import os
import json
import datetime

import platform_paths

STATE_FILE = platform_paths.path("sor_inventory.json")

# Bump ONLY when a stored field can be wrong in a way that cannot be
# detected or self-healed on the next collection. See
# ospf_stability._migrate_state_schema for the distinction that matters:
# a missing field is structurally identifiable and self-heals; a value that
# is wrong but looks identical to a correct one does not, and only that
# justifies a version bump.
SCHEMA_VERSION = 1
_SCHEMA_KEY = "_schema_version"
_META_KEY = "_meta"

# The 11 fields from TODO.md. Order is report order.
FIELDS = (
    "host_1250", "loopback_ip", "ds1", "muse_id", "hub_ip",
    "oc12", "mux_name", "acr_group", "cem_group",
    "gsx_node", "gsx_assignment",
)

# Fields NetBox also claims, so a later NetBox comparison knows where it has
# standing. Recorded here rather than in the collector so the two cannot
# disagree about it.
NETBOX_CLAIMS = ("host_1250", "mux_name", "oc12", "gsx_node")


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row_key(host_1250, ds1):
    """
    One row per DS1 per 1250. Lowercased so a device that reports its
    hostname in a different case does not fork into a second row -- the
    hostname-vs-FQDN lesson from gsx_client.node_names_match applies here
    too.
    """
    return "{}|{}".format((host_1250 or "?").strip().lower().split(".")[0],
                          (ds1 or "?").strip().lower())


def load(path=None):
    """
    Load the cache. Returns {} on missing OR corrupt file, never raises.

    `path=None` resolves STATE_FILE at CALL time, not at import time. A
    default argument of `path=STATE_FILE` binds once when the module is
    imported, so redirecting the store later -- a test, a CURVEBALL_HOME
    change, a second cache -- is silently ignored and the caller reads the
    original file while believing otherwise. Found exactly that way: the web
    page rendered an empty inventory while the collector was writing a
    populated one.
    """
    path = path or STATE_FILE
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(state, path=None):
    """
    Atomically write the cache: temp file then os.replace.

    A half-written inventory that still parses is worse than no inventory,
    because it looks authoritative. Failure prints and returns False rather
    than raising -- losing a collection is recoverable, crashing a tool run
    that has already touched four devices is not.
    """
    path = path or STATE_FILE          # resolved at call time -- see load()
    tmp = path + ".tmp"
    try:
        state[_SCHEMA_KEY] = SCHEMA_VERSION
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("  [!] could not write {}: {}: {}".format(
            path, type(e).__name__, e))
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def migrate(state, progress=print):
    """Schema migration hook. v1 is the first version, so this is a no-op."""
    current = state.get(_SCHEMA_KEY, SCHEMA_VERSION)
    if current >= SCHEMA_VERSION:
        state[_SCHEMA_KEY] = SCHEMA_VERSION
        return state
    # No migrations yet. When one is needed, follow
    # ospf_stability._migrate_state_schema: say what was wrong, why it cannot
    # self-heal, and what is discarded.
    state[_SCHEMA_KEY] = SCHEMA_VERSION
    return state


def observe(state, host_1250, ds1, values, source="device", when=None):
    """
    Record one observation of one circuit. Returns a list of change dicts.

    `values` is {field: value} -- only the fields actually observed. A field
    absent from `values` is NOT touched: a collection that could not reach
    the MUX must leave the MUX-derived fields alone rather than blanking
    them. Blanking on partial collection would destroy exactly the history
    this file exists to keep.

    Each stored field is:
        {"value", "source", "first_seen", "last_seen",
         "history": [{"value", "source", "from", "to"}, ...]}

    Returns [] on an unchanged observation, so a caller can report "nothing
    moved" without diffing the whole row itself.
    """
    when = when or _now()
    key = row_key(host_1250, ds1)
    row = state.setdefault(key, {})
    row.setdefault(_META_KEY, {"host_1250": host_1250, "ds1": ds1,
                               "first_seen": when})
    meta = row[_META_KEY]
    meta["last_seen"] = when
    # An observation is proof of reachability, so it clears any earlier
    # unverified mark.
    meta.pop("unverified_since", None)

    changes = []
    for field, new in values.items():
        if field not in FIELDS:
            continue
        new = "" if new is None else str(new).strip()
        if not new or new == "?":
            # "?" is this suite's marker for "lookup had no answer". Storing
            # it would turn a known-unknown into a recorded fact.
            continue
        cur = row.get(field)
        if cur is None:
            row[field] = {"value": new, "source": source,
                          "first_seen": when, "last_seen": when,
                          "history": []}
            continue
        if cur["value"] == new:
            cur["last_seen"] = when
            cur["source"] = source
            continue
        # CHANGED. Keep the old value as an event rather than overwriting.
        cur.setdefault("history", []).append({
            "value": cur["value"], "source": cur.get("source", "?"),
            "from": cur.get("first_seen"), "to": cur.get("last_seen"),
        })
        changes.append({"key": key, "field": field,
                        "old": cur["value"], "new": new,
                        "old_last_seen": cur.get("last_seen"), "when": when})
        cur.update({"value": new, "source": source,
                    "first_seen": when, "last_seen": when})
    return changes


def mark_unverified(state, host_1250, ds1=None, reason="", when=None):
    """
    Mark rows as unverified because the device could not be reached.

    NEVER deletes. A missing row and an unreachable device look identical if
    rows are deleted, and they mean opposite things -- deprovisioned versus
    "we could not ask". With ~200 devices a partial sweep is the normal case,
    not the exception.

    `ds1=None` marks every row for that 1250. Returns the number marked.
    """
    when = when or _now()
    prefix = row_key(host_1250, ds1 or "")
    if ds1 is None:
        prefix = prefix.rstrip("?").rstrip("|") + "|"
    n = 0
    for key, row in state.items():
        if key.startswith("_") or not isinstance(row, dict):
            continue
        hit = key == prefix if ds1 is not None else key.startswith(prefix)
        if not hit:
            continue
        meta = row.setdefault(_META_KEY, {})
        meta.setdefault("unverified_since", when)
        meta["unverified_reason"] = reason
        n += 1
    return n


def rows(state):
    """Every circuit row as a flat dict, newest-change first."""
    out = []
    for key, row in sorted(state.items()):
        if key.startswith("_") or not isinstance(row, dict):
            continue
        meta = row.get(_META_KEY, {})
        flat = {"key": key,
                "host_1250": meta.get("host_1250", ""),
                "ds1": meta.get("ds1", ""),
                "first_seen": meta.get("first_seen", ""),
                "last_seen": meta.get("last_seen", ""),
                "unverified_since": meta.get("unverified_since"),
                "unverified_reason": meta.get("unverified_reason", "")}
        changed = 0
        for field in FIELDS:
            cell = row.get(field)
            flat[field] = cell["value"] if cell else ""
            flat[field + "__source"] = cell.get("source", "") if cell else ""
            hist = (cell or {}).get("history") or []
            flat[field + "__changes"] = len(hist)
            changed += len(hist)
        flat["total_changes"] = changed
        out.append(flat)
    return out


def changes(state, limit=None):
    """
    Every recorded change across every row, newest first.

    This is the product. The full table is reference; a 200x28 table nobody
    opens is not an answer to "did anything move?".
    """
    out = []
    for key, row in state.items():
        if key.startswith("_") or not isinstance(row, dict):
            continue
        meta = row.get(_META_KEY, {})
        for field in FIELDS:
            cell = row.get(field)
            if not cell:
                continue
            for h in cell.get("history") or []:
                out.append({
                    "key": key,
                    "host_1250": meta.get("host_1250", ""),
                    "ds1": meta.get("ds1", ""),
                    "field": field,
                    "old": h.get("value"), "new": cell["value"],
                    "old_until": h.get("to"),
                    "changed_at": cell.get("first_seen"),
                    "netbox_claims": field in NETBOX_CLAIMS,
                })
    out.sort(key=lambda c: c.get("changed_at") or "", reverse=True)
    return out[:limit] if limit else out


def unverified(state):
    """Rows whose device could not be reached on the last attempt."""
    return [r for r in rows(state) if r.get("unverified_since")]


def summary(state):
    """One-line counts for the page header and the CLI."""
    rs = rows(state)
    return {
        "rows": len(rs),
        "devices": len({r["host_1250"].lower() for r in rs if r["host_1250"]}),
        "changes": len(changes(state)),
        "unverified": len([r for r in rs if r.get("unverified_since")]),
        "schema": state.get(_SCHEMA_KEY, SCHEMA_VERSION),
    }


def self_test(verbose=True):
    """`python sor_store.py` -- no devices, no files written."""
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    FULL = {"host_1250": "MDLDTXMU542", "loopback_ip": "10.9.9.9",
            "ds1": "ts1/27", "muse_id": "MUSE_972", "hub_ip": "10.111.134.255",
            "oc12": "oc12-ts1/1:7:23", "mux_name": "DFW1-MUX-04",
            "acr_group": "26", "cem_group": "723", "gsx_node": "DFWGSX04",
            "gsx_assignment": "6-1-23"}

    st = {}
    ch = observe(st, "MDLDTXMU542", "ts1/27", FULL, when="2026-10-01 10:00:00")
    chk("first observation records no changes", ch, [])
    chk("one row created", summary(st)["rows"], 1)
    chk("all 11 fields stored",
        len([f for f in FIELDS if st[row_key("MDLDTXMU542", "ts1/27")].get(f)]),
        11)

    # Re-observing the same values must not manufacture a change.
    ch = observe(st, "MDLDTXMU542", "ts1/27", FULL, when="2026-10-01 11:00:00")
    chk("unchanged re-collection is not a change", ch, [])
    chk("last_seen advances",
        st[row_key("MDLDTXMU542", "ts1/27")]["acr_group"]["last_seen"],
        "2026-10-01 11:00:00")
    chk("first_seen does not move",
        st[row_key("MDLDTXMU542", "ts1/27")]["acr_group"]["first_seen"],
        "2026-10-01 10:00:00")

    # The thing this exists for: a value moved.
    moved = dict(FULL, acr_group="52")
    ch = observe(st, "MDLDTXMU542", "ts1/27", moved, when="2026-10-02 09:00:00")
    chk("a moved ACR is reported as one change", len(ch), 1)
    chk("the change names old and new",
        (ch[0]["field"], ch[0]["old"], ch[0]["new"]), ("acr_group", "26", "52"))
    cell = st[row_key("MDLDTXMU542", "ts1/27")]["acr_group"]
    chk("the old value is kept, not overwritten",
        [h["value"] for h in cell["history"]], ["26"])
    chk("current value is the new one", cell["value"], "52")
    chk("history records when the old value was last true",
        cell["history"][0]["to"], "2026-10-01 11:00:00")
    chk("changes() surfaces it", len(changes(st)), 1)
    chk("changes() flags whether NetBox also claims the field",
        changes(st)[0]["netbox_claims"], False)

    # A partial collection must not blank what it could not see.
    # From here on the ACR is 52, so fixtures must carry that forward --
    # re-sending FULL (acr 26) would correctly record a flap BACK, which is
    # real behaviour but not what these cases are testing.
    MOVED = dict(FULL, acr_group="52")
    ch = observe(st, "MDLDTXMU542", "ts1/27",
                 {"host_1250": "MDLDTXMU542", "ds1": "ts1/27"},
                 when="2026-10-03 09:00:00")
    chk("partial collection changes nothing", ch, [])
    chk("MUX fields survive a MUX-less collection",
        st[row_key("MDLDTXMU542", "ts1/27")]["mux_name"]["value"],
        "DFW1-MUX-04")

    # "?" is this suite's "no answer" marker and must never be stored.
    ch = observe(st, "MDLDTXMU542", "ts1/27", dict(MOVED, cem_group="?"),
                 when="2026-10-03 10:00:00")
    chk("'?' is not stored as a fact",
        st[row_key("MDLDTXMU542", "ts1/27")]["cem_group"]["value"], "723")
    chk("empty string is not stored either",
        observe(st, "MDLDTXMU542", "ts1/27", {"acr_group": ""},
                when="2026-10-03 10:00:00"), [])

    # Unreachable device: marked, never deleted.
    n = mark_unverified(st, "MDLDTXMU542", "ts1/27", reason="SSH timeout",
                        when="2026-10-04 09:00:00")
    chk("unverified marks the row", n, 1)
    chk("the row still exists", summary(st)["rows"], 1)
    chk("unverified is reported", len(unverified(st)), 1)
    chk("the reason is kept",
        unverified(st)[0]["unverified_reason"], "SSH timeout")
    observe(st, "MDLDTXMU542", "ts1/27", MOVED, when="2026-10-05 09:00:00")
    chk("a successful collection clears unverified", len(unverified(st)), 0)

    # Hostname casing must not fork a row.
    observe(st, "mdldtxmu542", "TS1/27", MOVED, when="2026-10-05 10:00:00")
    chk("case differences do not fork the row", summary(st)["rows"], 1)
    chk("an FQDN does not fork it either",
        row_key("MDLDTXMU542.dfw1", "ts1/27"),
        row_key("mdldtxmu542", "ts1/27"))

    # Second device.
    observe(st, "SLKCUTMA660", "ts1/25", dict(MOVED, host_1250="SLKCUTMA660",
            ds1="ts1/25"), when="2026-10-05 11:00:00")
    s = summary(st)
    chk("second device counted", (s["rows"], s["devices"]), (2, 2))

    # mark_unverified with ds1=None marks every row on that device.
    observe(st, "SLKCUTMA660", "ts1/26", dict(MOVED, host_1250="SLKCUTMA660",
            ds1="ts1/26"), when="2026-10-05 11:00:00")
    n = mark_unverified(st, "SLKCUTMA660", reason="no route")
    chk("whole-device mark hits both its rows", n, 2)
    chk("and leaves the other device alone",
        st[row_key("MDLDTXMU542", "ts1/27")][_META_KEY].get(
            "unverified_since"), None)

    # A value that returns to a previous one is a SECOND change, not an
    # undo. Found by this suite's own fixture doing it accidentally.
    st2 = {}
    observe(st2, "X", "ts1/1", {"acr_group": "26"}, when="2026-01-01 00:00:00")
    observe(st2, "X", "ts1/1", {"acr_group": "52"}, when="2026-01-02 00:00:00")
    observe(st2, "X", "ts1/1", {"acr_group": "26"}, when="2026-01-03 00:00:00")
    chk("a flap back records two changes, not zero", len(changes(st2)), 2)
    chk("and the full sequence is recoverable",
        [h["value"] for h in st2[row_key("X", "ts1/1")]["acr_group"]["history"]],
        ["26", "52"])

    # STATE_FILE must resolve at CALL time. With `path=STATE_FILE` as a
    # default argument it binds at import, so redirecting the store is
    # silently ignored -- which is how the web page rendered an empty
    # inventory while the collector was writing a populated one.
    import tempfile as _tf
    global STATE_FILE
    _orig = STATE_FILE
    try:
        with _tf.TemporaryDirectory() as _d:
            STATE_FILE = os.path.join(_d, "redirected.json")
            save({"x|y": {_META_KEY: {"host_1250": "x", "ds1": "y"}}})
            chk("save() honours a redirected STATE_FILE",
                os.path.exists(STATE_FILE), True)
            chk("load() honours it too", list(load().keys()) != [], True)
    finally:
        STATE_FILE = _orig

    # Corrupt / missing file tolerance.
    chk("missing file loads empty", load("/nonexistent/sor.json"), {})
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{not json")
        bad = f.name
    try:
        chk("corrupt file loads empty", load(bad), {})
    finally:
        os.unlink(bad)

    # Round-trip through disk.
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sor.json")
        chk("save reports success", save(st, p), True)
        back = migrate(load(p))
        chk("round-trip preserves rows", summary(back)["rows"], 3)
        chk("round-trip preserves change history", len(changes(back)), 1)
        chk("schema version is stamped", back[_SCHEMA_KEY], SCHEMA_VERSION)
        chk("no temp file left behind", os.path.exists(p + ".tmp"), False)

    print()
    if fails:
        print("FAILED:")
        for f in fails:
            print("  " + f)
        return False
    print("All sor_store self-tests passed.")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if self_test() else 1)
