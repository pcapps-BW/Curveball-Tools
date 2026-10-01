#!/usr/bin/env python3
"""
assurance.py -- shared machinery for making tools fail LOUDLY on production gear.

WHY THIS EXISTS
---------------
Every serious bug found in this suite so far has had the same shape: it failed
SILENTLY and produced output that looked structurally valid. None threw an
exception. None looked broken. Examples, all real:

  * A paginated device read returned page 1 of N and reported it as the whole
    log -- correctly naming the OLDEST event while hiding every recent one.
  * A command the device REJECTED had its error text appended as data, then the
    report said "No IP address found in interface config", which reads like a
    clean negative result.
  * Drop history was inferred from adjacency uptime, which is structurally
    incapable of seeing anything older than the current uptime.
  * The same hub map was hardcoded twice; one copy was corrected, the other
    rotted for two months.
  * An xconnect lookup matched a neighbouring circuit and returned a real GSX,
    a real slot and a real T1 -- for the wrong circuit.

What caught nearly all of them was not diligence. It was that two independent
numbers had to agree and something compared them. That is the cheapest reliable
assurance available, because unlike a delivery receipt or an external oracle it
needs nothing outside the tool itself.

This module exists so that pattern is the path of least resistance rather than
something re-invented per tool. Before it there were ~120 ad-hoc "[!]" markers,
25 "_conflict" fields, 9 "WARNING:" strings and 7 "_flag" variables spread across
four files, each with its own convention.

FAILURE CATEGORIES
------------------
The CAT_* constants below name the ways monitoring goes quiet. Use them when
raising a Finding so the taxonomy lives in the code rather than in a document
nobody opens.

MECHANISMS
----------
    contradiction()     two sources for one fact, compared          <- start here
    assert_fresh()      freshness as an assertion, not a footnote
    note_coverage()     denominators: N of M, and why K were skipped
    suppression()       every skip carries a reason AND an expiry
    record_delivery()   positive evidence, not assumed success
    require_artifact()  prove the output exists and is well formed
    self_test()         prove the check can FIRE, not just that it passes

Stdlib only, by design -- an assurance layer that can break on a missing
dependency is its own failure mode.

Built by P. Capps.
"""

import os
import datetime

# ---- severities -----------------------------------------------------------
INFO     = "INFO"
WARN     = "WARN"
CRITICAL = "CRITICAL"
_ORDER   = {INFO: 0, WARN: 1, CRITICAL: 2}

# ---- failure categories ---------------------------------------------------
# Named so a Finding says WHICH way the monitoring went quiet.
CAT_NEVER_ARMED   = "never-armed"        # built, but not actually switched on
CAT_SELF_RETIRING = "self-retiring"      # works now, lapses on its own later
CAT_VOID          = "reporting-into-void"  # output goes where nobody reads it
CAT_PARTIAL       = "partial-application"  # covers some, presents as all
CAT_FAIL_AS_OK    = "fails-as-success"     # error path returns success shape
CAT_BLIND         = "structurally-blind"   # cannot see the thing, by construction
CAT_UNPROVEN      = "never-proven"         # never observed to fire correctly
CAT_DRIFT         = "drift-by-accretion"   # two copies of one fact diverged
CAT_LOST_TOOLING  = "lost-tooling"         # the tool/artifact itself went missing

ALL_CATEGORIES = (CAT_NEVER_ARMED, CAT_SELF_RETIRING, CAT_VOID, CAT_PARTIAL,
                  CAT_FAIL_AS_OK, CAT_BLIND, CAT_UNPROVEN, CAT_DRIFT,
                  CAT_LOST_TOOLING)

# ---- provenance -----------------------------------------------------------
# Attach to every reported value. A number with no stated source is how
# "fails-as-success" and "structurally-blind" hide.
P_MEASURED    = "measured"       # read directly, this run
P_CACHED      = "cached"         # from store; provably still valid
P_PROVEN_ZERO = "proven-zero"    # exact by argument, no read needed
P_INFERRED    = "inferred"       # derived, NOT observed -- treat as a floor
P_FALLBACK    = "fallback"       # authoritative source unavailable
P_UNREACHABLE = "unreachable"    # not measured; must not render as a value
P_UNKNOWN     = "unknown"

TRUSTED_PROVENANCE = (P_MEASURED, P_CACHED, P_PROVEN_ZERO)


class Finding(object):
    """One thing worth saying out loud, with the evidence to back it."""

    def __init__(self, category, severity, message, evidence=None):
        self.category = category
        self.severity = severity
        self.message  = message
        self.evidence = evidence
        self.at       = datetime.datetime.now()

    def __repr__(self):
        return "<Finding {} {} {}>".format(self.severity, self.category,
                                           self.message[:48])

    def line(self):
        s = "[{}] {}: {}".format(self.severity, self.category, self.message)
        if self.evidence:
            ev = str(self.evidence)
            if len(ev) > 400:
                ev = ev[:400] + " ...(truncated)"
            s += "\n        evidence: {}".format(ev.replace("\n", " | "))
        return s


class Coverage(object):
    """
    Denominators. Counters CAT_PARTIAL, which is the category that hides best.

    The paginated-read bug would have been obvious on day one if the tool had
    reported "read 1 page of 47" instead of just reporting results.
    """

    def __init__(self, label, total):
        self.label    = label
        self.total    = int(total or 0)
        self.examined = 0
        self.skipped  = {}      # reason -> count

    def examine(self, n=1):
        self.examined += n

    def skip(self, reason, n=1):
        self.skipped[reason] = self.skipped.get(reason, 0) + n

    @property
    def accounted(self):
        return self.examined + sum(self.skipped.values())

    def unaccounted(self):
        """Items neither examined nor explicitly skipped -- silently dropped."""
        return max(0, self.total - self.accounted)

    def line(self):
        parts = ["{} of {} {} examined".format(self.examined, self.total, self.label)]
        for reason, n in sorted(self.skipped.items(), key=lambda kv: -kv[1]):
            parts.append("{} skipped ({})".format(n, reason))
        if self.unaccounted():
            parts.append("{} UNACCOUNTED FOR".format(self.unaccounted()))
        return "; ".join(parts)


class Assurance(object):
    """
    Accumulator a tool carries through a run, then renders once at the end.

    Usage sketch:

        a = Assurance("OSPF Stability")
        cov = a.coverage("sites", total=203)
        ...
        cov.examine(); cov.skip("proven zero", 20)
        a.contradiction("BTRG uptime vs log", "uptime", 6*3600, "24h drops", 0,
                        holds=lambda u, d: not (u < 86400 and d == 0))
        a.record_delivery("slack", ok=True, detail="HTTP 200")
        a.require_artifact(out_path, min_bytes=200)
        print(a.footer())
    """

    def __init__(self, tool_name):
        self.tool       = tool_name
        self.findings   = []
        self.coverages  = []
        self.provenance = {}     # source -> count
        self.deliveries = []     # (channel, ok, detail)
        self.started    = datetime.datetime.now()

    # ---- raising findings ------------------------------------------------
    def add(self, category, severity, message, evidence=None):
        f = Finding(category, severity, message, evidence)
        self.findings.append(f)
        return f

    def info(self, category, message, evidence=None):
        return self.add(category, INFO, message, evidence)

    def warn(self, category, message, evidence=None):
        return self.add(category, WARN, message, evidence)

    def critical(self, category, message, evidence=None):
        return self.add(category, CRITICAL, message, evidence)

    # ---- MECHANISM 1: two-source contradiction ---------------------------
    def contradiction(self, label, a_name, a_value, b_name, b_value,
                      holds=None, category=CAT_FAIL_AS_OK):
        """
        Compare two independent sources for one fact. Returns True if consistent.

        This is the highest-value mechanism in the module and the cheapest: it
        needs no external oracle, only two paths to the same answer. It is what
        actually caught the paginated read (uptime said the session restarted 6 h
        ago; the log claimed zero drops in 24 h -- impossible), the wrong-circuit
        GSX lookup, and the two-copies hub map.

        `holds` is an optional predicate (a_value, b_value) -> bool for cases
        where the two are related but not equal. Default is equality.
        """
        ok = (a_value == b_value) if holds is None else bool(holds(a_value, b_value))
        if not ok:
            self.critical(
                category,
                "{}: {} says {!r} but {} says {!r} -- these cannot both be "
                "right, so one source is wrong".format(
                    label, a_name, a_value, b_name, b_value),
                evidence={a_name: a_value, b_name: b_value})
        return ok

    # ---- MECHANISM 2: freshness as an assertion --------------------------
    def assert_fresh(self, label, timestamp, max_age_s,
                     category=CAT_SELF_RETIRING):
        """
        Assert a datum is recent enough to act on. Returns True if fresh.

        Stale data that renders identically to fresh data is how self-retiring
        monitoring goes unnoticed -- the dashboard still looks alive.
        `timestamp` may be a datetime or an ISO 'YYYY-MM-DDTHH:MM:SS' string.
        """
        ts = timestamp
        if isinstance(ts, str):
            try:
                ts = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            except Exception:
                self.warn(category, "{}: unparseable timestamp {!r} -- freshness "
                                    "cannot be asserted".format(label, timestamp))
                return False
        if ts is None:
            self.warn(category, "{}: no timestamp -- freshness cannot be "
                                "asserted".format(label))
            return False
        age = (datetime.datetime.now() - ts).total_seconds()
        if age > max_age_s:
            self.warn(category,
                      "{} is STALE: {:.0f} min old, limit {:.0f} min".format(
                          label, age / 60.0, max_age_s / 60.0),
                      evidence="as of {}".format(ts.strftime("%Y-%m-%d %H:%M")))
            return False
        return True

    # ---- MECHANISM 3: coverage denominators ------------------------------
    def coverage(self, label, total):
        c = Coverage(label, total)
        self.coverages.append(c)
        return c

    # ---- MECHANISM 4: expiry on every suppression ------------------------
    def suppression(self, what, reason, expires_on, category=CAT_NEVER_ARMED):
        """
        Register a deliberate skip/mute/exclusion. Returns True if still valid.

        Every suppression must carry an expiry. A permanent mute is
        indistinguishable from a broken check, and "temporarily excluded" is how
        coverage quietly shrinks. `expires_on` is a date or 'YYYY-MM-DD'.
        """
        exp = expires_on
        if isinstance(exp, str):
            try:
                exp = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
            except Exception:
                self.warn(category, "suppression of {} has an unparseable expiry "
                                    "{!r} -- treat as expired".format(what, expires_on))
                return False
        if isinstance(exp, datetime.datetime):
            exp = exp.date()
        today = datetime.date.today()
        if exp < today:
            self.critical(category,
                          "suppression of {} EXPIRED {} ({} days ago) -- reason was "
                          "'{}'. Re-justify or remove it.".format(
                              what, exp, (today - exp).days, reason))
            return False
        days = (exp - today).days
        sev = self.warn if days <= 7 else self.info
        sev(category, "{} is suppressed until {} ({} days left) -- '{}'".format(
            what, exp, days, reason))
        return True

    # ---- MECHANISM 5: positive delivery evidence -------------------------
    def record_delivery(self, channel, ok, detail="", expected=True,
                        category=CAT_VOID):
        """
        Record proof a report actually went somewhere. Returns ok.

        Absence of evidence is treated as failure, not success. A Slack alert
        that silently no-ops because no webhook is configured is armed in the UI
        and dead in practice.
        """
        self.deliveries.append((channel, bool(ok), detail))
        if not expected:
            self.info(category, "{}: delivery not requested".format(channel))
            return ok
        if ok:
            self.info(category, "{}: delivered ({})".format(channel,
                                                            detail or "confirmed"))
        else:
            self.critical(category,
                          "{}: NOT delivered -- {}. This report reached nobody "
                          "via that channel.".format(channel, detail or "no acknowledgement"))
        return bool(ok)

    # ---- MECHANISM 6: prove the artifact ---------------------------------
    def require_artifact(self, path, min_bytes=1, must_contain=None,
                         category=CAT_LOST_TOOLING):
        """
        Prove the output exists, is non-trivial, and contains what it should.

        "The function returned" is not evidence a report was produced. A tool
        version showing in the UI with no artifact on disk is the same failure
        one layer up.
        """
        if not path:
            self.critical(category, "no output path was produced")
            return False
        if not os.path.exists(path):
            self.critical(category, "output missing: {}".format(path))
            return False
        size = os.path.getsize(path)
        if size < min_bytes:
            self.critical(category, "output suspiciously small: {} is {} bytes "
                                    "(expected >= {})".format(path, size, min_bytes))
            return False
        if must_contain:
            try:
                with open(path, "r", errors="ignore") as fh:
                    body = fh.read()
            except Exception as e:
                self.critical(category, "output unreadable: {}: {}".format(path, e))
                return False
            missing = [m for m in must_contain if m not in body]
            if missing:
                self.critical(category, "output is malformed -- missing {}".format(
                    ", ".join(repr(m) for m in missing)), evidence=path)
                return False
        self.info(category, "artifact verified: {} ({:,} bytes)".format(path, size))
        return True

    # ---- provenance tracking ---------------------------------------------
    def note_provenance(self, source, n=1):
        """Count a reported value by where it came from."""
        if source not in (P_MEASURED, P_CACHED, P_PROVEN_ZERO, P_INFERRED,
                          P_FALLBACK, P_UNREACHABLE, P_UNKNOWN):
            source = P_UNKNOWN
        self.provenance[source] = self.provenance.get(source, 0) + n

    def untrusted_share(self):
        """Fraction of reported values that are not measured/cached/proven."""
        total = sum(self.provenance.values())
        if not total:
            return 0.0
        bad = sum(n for s, n in self.provenance.items()
                  if s not in TRUSTED_PROVENANCE)
        return bad / float(total)

    # ---- verdict + rendering ---------------------------------------------
    def worst(self):
        if not self.findings:
            return INFO
        return max((f.severity for f in self.findings), key=lambda s: _ORDER[s])

    def ok(self):
        """True when nothing CRITICAL was raised."""
        return self.worst() != CRITICAL

    def by_category(self):
        out = {}
        for f in self.findings:
            out.setdefault(f.category, []).append(f)
        return out

    def footer(self, width=76):
        """
        One standard block for the end of any report. Replaces the four
        hand-rolled versions previously scattered across the suite.
        """
        sep = "=" * width
        sub = "-" * width
        L = [sep, "  ASSURANCE -- {}".format(self.tool), sep]

        for c in self.coverages:
            L.append("  COVERAGE: {}".format(c.line()))
            if c.unaccounted():
                L.append("    [!] {} item(s) neither examined nor explicitly "
                         "skipped -- silently dropped.".format(c.unaccounted()))

        if self.provenance:
            bits = ["{} {}".format(n, s) for s, n in
                    sorted(self.provenance.items(), key=lambda kv: -kv[1])]
            L.append("  PROVENANCE: {}".format("; ".join(bits)))
            share = self.untrusted_share()
            if share > 0:
                L.append("    {:.0%} of values are inferred/fallback/unreachable "
                         "-- treat those as a floor, not a total.".format(share))

        if self.deliveries:
            L.append("  DELIVERY: " + "; ".join(
                "{}={}".format(ch, "ok" if ok else "FAILED")
                for ch, ok, _ in self.deliveries))

        crit = [f for f in self.findings if f.severity == CRITICAL]
        warns = [f for f in self.findings if f.severity == WARN]
        if crit or warns:
            L.append(sub)
            for f in crit + warns:
                L.append("  " + f.line())
        else:
            L.append("  No warnings. All checks consistent.")

        L.append(sub)
        L.append("  VERDICT: {}   ({} critical, {} warning, {} info)".format(
            "OK" if self.ok() else "ATTENTION REQUIRED",
            len(crit), len(warns),
            len(self.findings) - len(crit) - len(warns)))
        L.append(sep)
        return "\n".join(L)


# ---- MECHANISM 7: prove the check can FIRE --------------------------------
def self_test(name, check, should_pass, should_fail):
    """
    Prove a check both passes on good input AND fires on bad input.

    A check that has never fired is indistinguishable from a broken check. This
    is the complement to require_artifact(): that proves the output, this proves
    the ALARM. Counters CAT_NEVER_ARMED and CAT_UNPROVEN.

        check        callable(sample) -> True when consistent
        should_pass  iterable of samples that MUST return True
        should_fail  iterable of samples that MUST return False

    Returns (ok, [problems]).
    """
    problems = []
    for s in should_pass:
        try:
            if not check(s):
                problems.append("{}: false alarm on good input {!r}".format(name, s))
        except Exception as e:
            problems.append("{}: raised on good input {!r}: {}".format(name, s, e))
    fired = False
    for s in should_fail:
        try:
            if check(s):
                problems.append("{}: MISSED bad input {!r} -- the check cannot "
                                "detect what it exists to detect".format(name, s))
            else:
                fired = True
        except Exception as e:
            problems.append("{}: raised on bad input {!r}: {}".format(name, s, e))
    if should_fail and not fired:
        problems.append("{}: never fired on any bad input -- unproven".format(name))
    return (not problems), problems


if __name__ == "__main__":
    # Worked example: the OSPF Stability run, expressed through this module.
    # Deliberately seeded with the real defects found on 2026-08/09 so the
    # footer shows what each mechanism actually catches.
    a = Assurance("OSPF Stability (worked example)")

    cov = a.coverage("sites", total=203)
    cov.examine(23); cov.skip("cached, counters unchanged", 160)
    cov.skip("proven zero, all legs up >30d", 20)

    for src, n in ((P_MEASURED, 23), (P_CACHED, 160), (P_PROVEN_ZERO, 20)):
        a.note_provenance(src, n)

    # the contradiction that exposed the paginated read
    a.contradiction("AKRNOH25325 uptime vs drop count",
                    "adjacency uptime (s)", 5 * 3600 + 55 * 60,
                    "24h drop count", 0,
                    holds=lambda up, drops: not (up < 86400 and drops == 0),
                    category=CAT_PARTIAL)
    # the two-copies hub map
    a.contradiction("LAX hub IP", "NetBox", "10.111.131.254",
                    "hardcoded fallback", "10.111.135.254",
                    category=CAT_DRIFT)
    a.assert_fresh("hub map", datetime.datetime.now() -
                   datetime.timedelta(hours=30), max_age_s=24 * 3600)
    a.suppression("ces01.old1 polling", "decommissioned, pending NetBox cleanup",
                  expires_on="2026-08-01")
    a.record_delivery("slack", ok=False, detail="no webhook configured")
    a.require_artifact(__file__, min_bytes=500, must_contain=["Assurance"])

    print(a.footer())
    print()
    ok, probs = self_test(
        "count_trust",
        lambda s: not (s["uptime"] < 86400 and s["d24"] == 0),
        should_pass=[{"uptime": 68 * 86400, "d24": 0},
                     {"uptime": 6 * 3600, "d24": 1}],
        should_fail=[{"uptime": 6 * 3600, "d24": 0}])
    print("self_test(count_trust) ->", "OK" if ok else probs)
    print("run verdict ok? ->", a.ok())
