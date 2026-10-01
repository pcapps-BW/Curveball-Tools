#!/usr/bin/env python3
"""
device_base.py -- shared paramiko machinery for all NOC device clients.

BaseDeviceClient holds everything common across the 1250 / 1800 / MUX:
  - paramiko connect (overridable for legacy-algo devices like the MUX)
  - background byte-reader thread + locked buffer (no readline deadlock on
    newline-less prompts)
  - prompt-detection, run(), hostname capture, ANSI stripping
  - echo stripping (positional: the first output line is the command echo)

Per-device subclasses override:
  - PROMPT_RE          (NPT uses ">", MUX uses "#")
  - _build_transport() (MUX re-enables legacy kex/hostkey)
  - _post_login()      (MUX sends "terminal length 0"; NPT is a no-op)
  - their own query_* methods

Built by P. Capps.
"""

import re
import time
import threading

import paramiko


def _is_valid_ip(s):
    """Return True if s is a valid IPv4 address (4 octets, each 0-255)."""
    parts = s.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        if not (0 <= int(p) <= 255):
            return False
    return True


def prompt_ip(label, default=None):
    """Prompt for an IPv4 address, re-prompting until valid.
    Returns the entered IP string, or None if the user enters nothing and
    there is no default.
    """
    while True:
        hint = " [{}]".format(default) if default else ""
        raw = input("{}{}: ".format(label, hint)).strip()
        if not raw:
            if default:
                return default
            print("  IP address required.")
            continue
        if _is_valid_ip(raw):
            return raw
        print("  '{}' is not a valid IP address (expected 4 octets, e.g. 10.111.131.77).".format(raw))


class CredentialError(Exception):
    """Raised when SSH authentication is rejected by the device.

    cred_type: 'infra' (1250 / 1800 / MUX) or 'gsx'.
    Caught by launcher.dispatch() to clear the bad cred pair from the session
    so the user is re-prompted on the next tool run.
    """
    def __init__(self, message, cred_type='infra'):
        super().__init__(message)
        self.cred_type = cred_type

ANSI_ESCAPE = re.compile(
    r'(\x1b|\x9b)[\[\]()#;?]*(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-ORZcf-nqry=><~]'
    r'|\x1b\[K'
)

def strip_ansi(text):
    return ANSI_ESCAPE.sub('', text)


class BaseDeviceClient:
    # Default NPT-style prompt; MUX overrides to end with "#".
    PROMPT_RE = re.compile(r'[\w.\-]+[>#]\s*$')
    CONN_TIMEOUT = 30

    def __init__(self, ip, username, password):
        self.ip = ip
        self.username = username
        self.password = password
        self.client = None
        self.chan = None
        self.hostname = "device"
        self._buf = ""
        self._lock = threading.Lock()
        self._stop = False
        self._reader = None

    # ---- overridable transport (MUX re-enables legacy algos) ----
    def _connect_transport(self, conn_timeout):
        """
        Default: plain paramiko connect (works for 1250/1800).
        MUX subclass overrides this to enable legacy kex + ssh-rsa hostkey.
        """
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(
                self.ip,
                username=self.username,
                password=self.password,
                look_for_keys=False,
                allow_agent=False,
                timeout=min(conn_timeout, 30),
            )
        except paramiko.AuthenticationException:
            raise CredentialError(
                "Authentication failed for '{}' on {} -- wrong username or password.".format(
                    self.username, self.ip),
                cred_type='infra')

    def _post_login(self):
        """Hook run once after reaching the prompt. NPT: no-op. MUX: paging off."""
        pass

    def _open_shell(self):
        """Open an interactive shell channel. Overridable for manual-transport
        subclasses (e.g. MUX) that don't go through SSHClient.invoke_shell."""
        return self.client.invoke_shell()

    def connect(self, conn_timeout=None):
        timeout = conn_timeout or self.CONN_TIMEOUT
        self._connect_transport(timeout)
        self.chan = self._open_shell()
        self.chan.settimeout(1.0)

        def _pump():
            while not self._stop:
                try:
                    b = self.chan.recv(1)
                    if not b:
                        return
                    with self._lock:
                        self._buf += strip_ansi(b.decode("ascii", errors="ignore"))
                except Exception:
                    time.sleep(0.05)

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()

        self._wait_for_prompt(timeout)
        self._capture_hostname()
        self._consume()
        self._post_login()
        self._consume()

    # ---- buffer helpers ----
    def _snapshot(self):
        with self._lock:
            return self._buf

    def _consume(self):
        with self._lock:
            data = self._buf
            self._buf = ""
            return data

    def _wait_for_prompt(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self._snapshot()
            tail = snap.rstrip().splitlines()[-1] if snap.strip() else ""
            if self.PROMPT_RE.search(tail):
                return snap
            if self.chan is not None and self.chan.closed:
                return snap
            time.sleep(0.2)
        return self._snapshot()

    def _capture_hostname(self):
        snap = self._snapshot()
        for line in reversed(snap.splitlines()):
            m = re.search(r'@?([\w.\-]+)[>#]', line)
            if m:
                self.hostname = m.group(1)
                return

    # ---- command execution with positional echo strip ----
    def run(self, cmd, timeout=None):
        """
        Send a command, return output with the command echo and trailing
        prompt removed. Echo strip is POSITIONAL: the first non-empty output
        line is the echoed command (handles wrapped/space-varied echoes that
        exact-match filtering missed).
        """
        t = timeout or self.CONN_TIMEOUT
        self._consume()
        self.chan.send(cmd + "\n")
        time.sleep(0.3)
        raw = self._wait_for_prompt(t)
        return self._clean(raw, cmd)

    def _clean(self, raw, cmd):
        # Strip ANSI sequences over the WHOLE assembled output. The reader
        # thread pumps 1 byte at a time, so its per-byte strip_ansi() never
        # sees a complete multi-byte sequence (e.g. \x1b[K erase-line) --
        # those survived into reports, where browsers dropped the invisible
        # ESC and displayed a bare "[K" artifact. Stripping here, after
        # assembly, catches them.
        raw = strip_ansi(raw)
        lines = raw.splitlines()
        cleaned = []
        echo_dropped = False
        for ln in lines:
            s = ln.rstrip()
            if not s:
                continue
            # Option 1 (positional): the first non-empty output line is the
            # command echo -- drop it regardless of exact string match, since
            # wrapped/space-varied echoes broke exact-match filtering.
            if not echo_dropped:
                echo_dropped = True
                continue
            # Drop the trailing prompt line.
            if self.PROMPT_RE.search(s):
                continue
            cleaned.append(s)
        return "\n".join(cleaned)

    def close(self):
        self._stop = True
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
