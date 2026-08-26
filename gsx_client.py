#!/usr/bin/env python3
"""
gsx_client.py -- GSX9000 client (plink), importable module.

Extracted from the proven gsx_prover.py. Uses PuTTY plink (NOT paramiko) because
the GSX offers only ssh-dss (512-bit DSA), which paramiko/cryptography reject.
Drives an interactive plink session via subprocess pipes with a byte-reader
thread (no readline deadlock on the newline-less prompt).

In the orchestrator, the node/slot/port/t1 come FROM the MUX hop -- never
entered by hand. mgmt IPs come from Netbox; ping picks the live card.

Built by P. Capps.
"""

import re
import os
import time
import json
import subprocess
import threading

PLINK_CANDIDATES = [
    "plink",
    r"C:\Program Files\PuTTY\plink.exe",
    r"C:\Program Files (x86)\PuTTY\plink.exe",
]

PING_TIMEOUT_MS = 1000
SESSION_TIMEOUT = 45
READ_IDLE = 0.4

# Local fingerprint store (Tier 1 TOFU + change detection). Upgradeable to
# Netbox-sourced known-good fingerprints later (Tier 2).
HOSTKEY_STORE = r"C:\Claude\gsx_hostkeys.json"


def find_plink():
    for cand in PLINK_CANDIDATES:
        try:
            r = subprocess.run([cand, "-V"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 or "plink" in (r.stdout + r.stderr).lower():
                return cand
        except Exception:
            continue
    return None


def ping_host(ip):
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip],
            capture_output=True, text=True, timeout=5)
        out = result.stdout
        return ("TTL=" in out) or ("Received = 1" in out)
    except Exception:
        return False


def find_live_ip(cards):
    """cards = {"1-1":[ips], "1-2":[ips]} -> (card_key, ip) or (None, None)."""
    for card_key in sorted(cards.keys()):
        for ip in cards[card_key]:
            if ping_host(ip):
                return card_key, ip
    return None, None


# ---- Tier 1 host-key fingerprint store (TOFU + change detection) ----

def _load_hostkey_store():
    try:
        with open(HOSTKEY_STORE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hostkey_store(store):
    try:
        d = os.path.dirname(HOSTKEY_STORE)
        if d and not os.path.exists(d):
            os.makedirs(d)
        with open(HOSTKEY_STORE, "w") as f:
            json.dump(store, f, indent=2)
    except Exception:
        pass


# plink prints e.g.:  The server's dss key fingerprint is:
#                       ssh-dss 512 SHA256:RuzhN9i5U6oBd62FBklVpI34S1stmmS97Uqq...
_FP_RE = re.compile(r'(ssh-\w+\s+\d+\s+SHA256:[A-Za-z0-9+/=]+)')


def capture_fingerprint(plink_path, ip, username, password, timeout=20):
    """
    Phase-1: throwaway connect that provokes the host-key line, parse and return
    the fingerprint string plink reports (e.g. 'ssh-dss 512 SHA256:...') or None.

    plink 0.82+ ignores piped y/n for the host-key prompt (needs
    -legacy-stdio-prompts), so we do NOT try to answer it. We just read plink's
    output in a BACKGROUND THREAD (so a blocking read at the newline-less prompt
    can't deadlock us), grab the fingerprint, then kill the process.
    """
    try:
        proc = subprocess.Popen(
            [plink_path, "-ssh", "{}@{}".format(username, ip),
             "-pw", password, "-legacy-stdio-prompts", "exit"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0)
    except Exception:
        return None

    buf = {"data": ""}
    lock = threading.Lock()
    stop = {"flag": False}

    def _pump():
        while not stop["flag"]:
            try:
                b = proc.stdout.read(1)
            except Exception:
                return
            if not b:
                return
            with lock:
                buf["data"] += b.decode("ascii", errors="ignore")

    t = threading.Thread(target=_pump, daemon=True)
    t.start()

    fp = None
    answered = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            snap = buf["data"]
        m = _FP_RE.search(snap)
        if m and not fp:
            fp = m.group(1)
        # With -legacy-stdio-prompts, plink 0.82+ reads the y/n from stdin.
        # Answer 'n' (do NOT cache in registry -- we pin via -hostkey instead)
        # once we've captured the fingerprint, so the throwaway exits cleanly.
        if fp and not answered and "Store key in cache" in snap:
            try:
                proc.stdin.write(b"n\n")
                proc.stdin.flush()
            except Exception:
                pass
            answered = True
            break
        if m and proc.poll() is not None:
            break
        if proc.poll() is not None and not m:
            break
        time.sleep(0.1)

    stop["flag"] = True
    try:
        proc.kill()
    except Exception:
        pass
    return fp


class GSXClient:
    # Full pager line: "--- Press <space> for next page, <c>ontinuous, or <q>uit ---"
    # Match the surrounding dashes too, so no "---  ---" remnant is left behind.
    PAGER_RE   = re.compile(r'-*\s*Press <space>.*?<q>uit\s*-*', re.IGNORECASE)
    PROMPT_RE  = re.compile(r'[\w.\-]+>\s*$')
    HOSTKEY_RE = re.compile(r'Store key in cache', re.IGNORECASE)

    def __init__(self, ip, username, password, plink_path=None):
        self.ip = ip
        self.username = username
        self.password = password
        self.plink_path = plink_path or find_plink()
        self.proc = None
        self._buf = ""
        self._lock = threading.Lock()
        self._reader = None
        self._stop = False

    def _spawn(self, hostkey=None):
        args = [self.plink_path, "-ssh", "{}@{}".format(self.username, self.ip),
                "-pw", self.password]
        if hostkey:
            # plink -hostkey wants the bare fingerprint form (e.g. "SHA256:...").
            # We store the full "ssh-dss 512 SHA256:..." string for readability;
            # extract just the SHA256:... token for the flag.
            m = re.search(r'(SHA256:[A-Za-z0-9+/=]+)', hostkey)
            hk_arg = m.group(1) if m else hostkey
            # Pin the exact host key -- plink accepts silently, no prompt, and
            # REFUSES if the device presents a different key (change detection).
            args += ["-hostkey", hk_arg]
        # -batch: never prompt interactively. With -hostkey supplying the
        # accepted key, plink connects silently; if anything's off it FAILS
        # (clean exit) instead of hanging at a prompt (plink 0.82+ ignores
        # piped prompt answers anyway).
        args += ["-batch"]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0)
        self._stop = False

        def _pump():
            while not self._stop:
                b = self.proc.stdout.read(1)
                if not b:
                    return
                with self._lock:
                    self._buf += b.decode("ascii", errors="ignore")

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()

    def _snapshot(self):
        with self._lock:
            return self._buf

    def _consume(self):
        with self._lock:
            data = self._buf
            self._buf = ""
            return data

    def _send(self, s):
        self.proc.stdin.write(s.encode("ascii"))
        self.proc.stdin.flush()

    def _wait_for_prompt(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self._snapshot()
            # With -hostkey pinning we should NOT see the cache prompt. If we
            # somehow do, it means no/mismatched key -- don't blindly accept.
            if self.PAGER_RE.search(snap):
                self._send("c")
                with self._lock:
                    self._buf = self.PAGER_RE.sub("", self._buf)
                time.sleep(0.2)
                deadline = time.time() + timeout
                continue
            tail = snap.rstrip().splitlines()[-1] if snap.strip() else ""
            if self.PROMPT_RE.search(tail):
                return snap
            if self.proc.poll() is not None:
                return snap
            time.sleep(READ_IDLE)
        return self._snapshot()

    def _resolve_hostkey(self):
        """
        Tier 1: return the fingerprint to pin for this GSX.
        - If stored: return it (subsequent runs pin against the known value).
        - If not stored: capture it (TOFU), store it, return it.
        Raises on a key CHANGE (stored != what the device now presents).
        Returns the fingerprint string, or None if capture failed.
        """
        store = _load_hostkey_store()
        stored = store.get(self.ip)

        # Always capture what the device currently presents so we can detect
        # changes even when we have a stored value.
        current = capture_fingerprint(self.plink_path, self.ip,
                                      self.username, self.password)

        if stored and current and stored != current:
            raise RuntimeError(
                "GSX HOST KEY CHANGED for {}!\n"
                "  stored:  {}\n"
                "  current: {}\n"
                "Refusing to connect. If this device was legitimately replaced, "
                "remove its entry from {} and re-run.".format(
                    self.ip, stored, current, HOSTKEY_STORE))

        if stored and not current:
            # Couldn't re-capture (transient); trust the stored value.
            return stored

        if current and not stored:
            # TOFU: first contact -- record it.
            store[self.ip] = current
            _save_hostkey_store(store)
            return current

        # both present and equal
        return stored or current

    def connect(self):
        if not self.plink_path:
            raise RuntimeError("plink not found. Install PuTTY or add plink to PATH.")
        hostkey = self._resolve_hostkey()   # Tier 1: pin/capture + change detect
        self._spawn(hostkey=hostkey)
        banner = self._wait_for_prompt(SESSION_TIMEOUT)
        if self.proc.poll() is not None:
            if re.search(r'access denied|authentication fail|wrong password|'
                         r'permission denied|incorrect password',
                         banner, re.IGNORECASE):
                from device_base import CredentialError
                raise CredentialError(
                    "GSX authentication failed for '{}' on {} -- wrong username or password.".format(
                        self.username, self.ip),
                    cred_type='gsx')
            raise RuntimeError("plink exited during login. Output:\n{}".format(banner))
        self._consume()
        return banner

    def run_command(self, cmd):
        self._consume()
        self._send(cmd + "\n")
        time.sleep(0.3)
        raw = self._wait_for_prompt(SESSION_TIMEOUT)
        cleaned = []
        for ln in raw.splitlines():
            s = ln.rstrip()
            if not s:
                continue
            if cmd in s:
                continue
            if self.PROMPT_RE.search(s):
                continue
            # Defensive: drop any leftover pager fragment (a line that is only
            # dashes/spaces, e.g. a "---  ---" remnant from a split pager line).
            if re.match(r'^[-\s]*$', s) and "-" in s and len(s.strip("- ")) == 0:
                # keep genuine separator lines (long solid dashes) but drop the
                # short split-pager remnants like "---  ---"
                if "  " in s:   # remnant has a gap where the text was removed
                    continue
            cleaned.append(s)
        return "\n".join(cleaned)

    def pull_t1_stats(self, slot, port, t1):
        """
        Pull the standard T1 command set for a given slot/port/t1.
        Returns list of (label, cmd, output). Excludes the destructive reset.
        """
        t1_id = "T1-1-{}-{}-{}".format(slot, port, t1)
        commands = [
            ("STATUS",             "SHOW T1 {} STATUS".format(t1_id)),
            ("STATISTICS",         "SHOW T1 {} STATISTICS".format(t1_id)),
            ("PERFMON 15MIN",      "SHOW T1 {} PERFMON CURRENT FIFTEENMIN".format(t1_id)),
            ("PERFMON DAY",        "SHOW T1 {} PERFMON CURRENT DAY".format(t1_id)),
            ("PERFMON THRESHOLDS", "SHOW T1 {} PERFMON THRESHOLDS".format(t1_id)),
        ]
        out = []
        for label, cmd in commands:
            try:
                res = self.run_command(cmd)
            except Exception as e:
                res = "[command error: {}]".format(e)
            out.append((label, cmd, res))
        return out

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self._send("exit\n")
                time.sleep(0.5)
        except Exception:
            pass
        self._stop = True
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
