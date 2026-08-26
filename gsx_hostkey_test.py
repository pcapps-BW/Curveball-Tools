#!/usr/bin/env python3
"""
gsx_hostkey_test.py -- test Tier-1 -hostkey pinning on an UNCACHED GSX.

Proves the flow:
  1. capture the fingerprint plink reports (phase-1 throwaway)
  2. store it locally (TOFU)
  3. connect with -hostkey <fp> -- no prompt, no hang, pinned
  4. a second connect uses the STORED fp (fast path)
  5. change-detection: a bogus stored fp makes connect REFUSE (safety)

Run against a GSX whose key is NOT already in the local store.
Usage:  python gsx_hostkey_test.py
"""

import sys
import time
import getpass

try:
    from gsx_client import (GSXClient, find_plink, capture_fingerprint,
                            _load_hostkey_store, _save_hostkey_store, HOSTKEY_STORE)
except ImportError:
    print("ERROR: gsx_client.py must be in the same folder.")
    sys.exit(1)


def main():
    print("=" * 60)
    print("  GSX -hostkey PINNING TEST (Tier 1)")
    print("=" * 60)

    plink_path = find_plink()
    if not plink_path:
        print("[!] plink not found."); return
    print("plink: {}".format(plink_path))
    print("store: {}".format(HOSTKEY_STORE))

    ip   = input("GSX MGMT IP: ").strip()
    user = input("GSX username: ").strip()
    pw   = getpass.getpass("GSX password: ")
    if not (ip and user and pw):
        print("Missing input."); return

    store = _load_hostkey_store()
    print("\nCurrently stored for {}: {}".format(ip, store.get(ip, "(none)")))

    # --- Step 1: capture fingerprint ---
    print("\n[1] Capturing fingerprint (phase-1 throwaway)...")
    t0 = time.time()
    fp = capture_fingerprint(plink_path, ip, user, pw)
    print("    captured in {:.1f}s: {}".format(time.time() - t0, fp or "(FAILED)"))
    if not fp:
        print("    [FAIL] could not capture fingerprint -- stop here.")
        return

    # --- Step 2 & 3: real connect (resolve -> store -> pin) ---
    print("\n[2/3] Connecting with -hostkey pinning (should NOT hang)...")
    g = GSXClient(ip, user, pw, plink_path)
    t0 = time.time()
    try:
        g.connect()
        print("    [PASS] connected in {:.1f}s -- pinned, no prompt.".format(time.time() - t0))
    except Exception as e:
        print("    [FAIL] {}: {}".format(type(e).__name__, e))
        return

    # quick command sanity
    try:
        _ = g.run_command("")
        print("    session I/O ok.")
    except Exception as e:
        print("    [warn] cmd test: {}".format(e))
    g.close()

    # confirm it got stored
    store = _load_hostkey_store()
    print("\n    stored now: {}".format(store.get(ip)))

    # --- Step 4: second connect uses stored fp (fast path) ---
    print("\n[4] Second connect (uses stored fp)...")
    g2 = GSXClient(ip, user, pw, plink_path)
    t0 = time.time()
    try:
        g2.connect()
        print("    [PASS] reconnected in {:.1f}s via stored fp.".format(time.time() - t0))
    except Exception as e:
        print("    [FAIL] {}: {}".format(type(e).__name__, e))
    g2.close()

    # --- Step 5: change-detection safety ---
    print("\n[5] Change-detection test (inject bogus stored fp)...")
    store = _load_hostkey_store()
    real = store.get(ip)
    store[ip] = "ssh-dss 512 SHA256:BOGUSbogusBOGUSbogusBOGUSbogusBOGUS0000000"
    _save_hostkey_store(store)
    g3 = GSXClient(ip, user, pw, plink_path)
    try:
        g3.connect()
        print("    [FAIL] connected despite mismatched stored fp -- change detect BROKEN.")
    except RuntimeError as e:
        if "HOST KEY CHANGED" in str(e):
            print("    [PASS] correctly REFUSED on key mismatch.")
        else:
            print("    [?] refused, but different error: {}".format(e))
    except Exception as e:
        print("    [?] {}: {}".format(type(e).__name__, e))
    finally:
        g3.close()
    # restore the real fp
    store[ip] = real
    _save_hostkey_store(store)
    print("    (restored real fp in store)")

    print("\n[done] Tier-1 pinning tested end to end.")


if __name__ == "__main__":
    main()
