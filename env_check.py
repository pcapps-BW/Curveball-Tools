#!/usr/bin/env python3
"""
env_check.py -- startup checks for dependency versions that silently break
device access.

WHY THIS EXISTS

On 2026-10-01 a LAX reverse-autotester run failed at the MUX hop with
"unknown cipher (paramiko 5.0.0)". Nothing in the suite had changed. The
cause was that `Start Curveball Tools.bat` runs

    pip install -r requirements.txt

on EVERY launch, and `requirements.txt` pinned `paramiko>=3.5` with no
ceiling -- so launching the app upgraded paramiko to 5.0.0, which REMOVED
the SHA-1 algorithms the Cisco NCS 4216 requires. Not disabled: removed from
`Transport._kex_info` / `_key_info`, so `disabled_algorithms={}` cannot bring
them back and every MUX at every site becomes unreachable.

That is a configuration change with no commit, no release and no changelog
entry -- the hardest kind to attribute. A banner is cheap insurance.

WHY IT LIVES HERE AND NOT IN app.py

It was in `app.py` first (v2.37), which covered exactly one of the four ways
these tools get run. Someone using the CLI launcher, running a tool
standalone, or driving `run(params)` from another front end saw nothing and
just hit the MUX hop error. `device_base.py` imports this at module scope
instead: every device client is built on it, so every path that can touch a
device passes through here.

Checks WARN, they do not raise. paramiko 5 breaks the MUX hop but leaves the
1250 and 1800 hops working, so a degraded suite is more useful than one that
refuses to start -- as long as the degradation is impossible to miss.

Built by P. Capps.
"""

import sys

# Printed once per process no matter how many modules import us.
_WARNED = set()

# The MUX's required algorithms, and the paramiko table each must appear in.
# The NCS 4216 offers ONLY these -- there is no fallback to negotiate down to.
_MUX_REQUIRED = (
    ("diffie-hellman-group14-sha1", "_kex_info", "key exchange"),
    ("ssh-rsa",                     "_key_info", "host key"),
)


def paramiko_legacy_status():
    """
    (ok, missing, version) for the SHA-1 algorithms the NCS 4216 MUX needs.

    `missing` is a list of (algorithm, kind) tuples. Never raises -- an
    import failure or an unrecognisable paramiko reports as "cannot tell"
    (ok=True) rather than crying wolf, because a false alarm here would train
    people to ignore the banner.
    """
    try:
        import paramiko
    except Exception:
        return True, [], None
    version = getattr(paramiko, "__version__", "?")
    try:
        missing = []
        for name, table, kind in _MUX_REQUIRED:
            tbl = getattr(paramiko.Transport, table, None)
            # An ABSENT or empty table means we cannot read this paramiko's
            # algorithm registry -- a future rename, a stub, a mock. Treat
            # that as "cannot tell" and stay silent.
            #
            # The obvious `getattr(..., table, {})` is wrong here: it turns
            # an unreadable paramiko into one where every algorithm looks
            # missing, i.e. the loudest possible false alarm. A banner that
            # fires when nothing is broken is worse than no banner, because
            # it teaches people to scroll past the one that matters. Caught
            # by this module's own self-test.
            if not tbl:
                return True, [], version
            if name not in tbl:
                missing.append((name, kind))
    except Exception:
        return True, [], version
    return (not missing), missing, version


def _banner(lines):
    out = ["", "!" * 72]
    out += ["  " + l for l in lines]
    out += ["!" * 72, ""]
    # stderr: the web layer tees stdout into the per-job log, and this is a
    # startup condition rather than something that happened during a run.
    sys.stderr.write("\n".join(out) + "\n")
    try:
        sys.stderr.flush()
    except Exception:
        pass


def check_paramiko(force=False):
    """Warn if paramiko cannot talk to the MUX. Returns True if usable."""
    if "paramiko" in _WARNED and not force:
        return True
    ok, missing, version = paramiko_legacy_status()
    if ok:
        return True
    _WARNED.add("paramiko")
    _banner([
        "paramiko {} has dropped: {}".format(
            version, ", ".join("{} ({})".format(n, k) for n, k in missing)),
        "",
        "The Cisco NCS 4216 MUX offers ONLY these, so EVERY MUX at EVERY site",
        "is unreachable. The MUX and GSX hops of both autotesters will fail.",
        "The 1250 and 1800 hops are unaffected.",
        "",
        "These were removed from paramiko, not disabled -- no setting re-enables",
        "them. You need paramiko 4.x.",
        "",
        "  FIX:  python -m pip install -r requirements.txt",
        "",
        "Then RESTART the app. A server already running keeps the old paramiko",
        "loaded, so the fix looks like it did nothing.",
    ])
    return False


def check_all(force=False):
    """Every startup check. Returns True when nothing was flagged."""
    return check_paramiko(force=force)


def self_test(verbose=True):
    """`python env_check.py` -- no devices, no credentials."""
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        elif verbose:
            print("  ok  {} = {!r}".format(label, got))

    import paramiko

    real_kex = paramiko.Transport._kex_info
    real_key = paramiko.Transport._key_info
    try:
        # Both present -> usable, no banner.
        paramiko.Transport._kex_info = dict(
            real_kex, **{"diffie-hellman-group14-sha1": object()})
        paramiko.Transport._key_info = dict(
            real_key, **{"ssh-rsa": object()})
        ok, missing, _v = paramiko_legacy_status()
        chk("both algorithms present -> ok", (ok, missing), (True, []))

        # kex gone -> flagged, and named.
        paramiko.Transport._kex_info = {
            k: v for k, v in paramiko.Transport._kex_info.items()
            if k != "diffie-hellman-group14-sha1"}
        ok, missing, _v = paramiko_legacy_status()
        chk("missing kex is detected", ok, False)
        chk("missing kex is named",
            missing, [("diffie-hellman-group14-sha1", "key exchange")])

        # Both gone -> both reported, not just the first.
        paramiko.Transport._key_info = {
            k: v for k, v in paramiko.Transport._key_info.items()
            if k != "ssh-rsa"}
        ok, missing, _v = paramiko_legacy_status()
        chk("both missing are reported", len(missing), 2)

        # The banner prints once per process, not once per caller.
        _WARNED.discard("paramiko")
        import io
        real_err, sys.stderr = sys.stderr, io.StringIO()
        try:
            check_paramiko()
            first = sys.stderr.getvalue()
            check_paramiko()
            second = sys.stderr.getvalue()
        finally:
            sys.stderr = real_err
        chk("banner printed on first call", "has dropped" in first, True)
        chk("banner names the fix command",
            "pip install -r requirements.txt" in first, True)
        chk("banner says to restart", "RESTART" in first, True)
        chk("banner not repeated on second call", first == second, True)
    finally:
        paramiko.Transport._kex_info = real_kex
        paramiko.Transport._key_info = real_key
        _WARNED.discard("paramiko")

    # A paramiko that cannot be read must NOT produce a false alarm.
    class _Fake(object):
        __version__ = "9.9.9"

        class Transport(object):
            pass
    real_mod = sys.modules.get("paramiko")
    try:
        sys.modules["paramiko"] = _Fake
        ok, missing, ver = paramiko_legacy_status()
        chk("unreadable paramiko does not cry wolf", (ok, ver), (True, "9.9.9"))
    finally:
        if real_mod is not None:
            sys.modules["paramiko"] = real_mod

    print()
    if fails:
        print("FAILED:")
        for f in fails:
            print("  " + f)
        return False
    print("All env_check self-tests passed.")
    return True


if __name__ == "__main__":
    sys.exit(0 if self_test() else 1)
