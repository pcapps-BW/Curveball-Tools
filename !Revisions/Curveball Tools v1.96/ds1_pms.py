#!/usr/bin/env python3
"""
ds1_pms.py -- standalone NPT-1250 per-DS1 PM check.

Standalone port of the SecureCRT "DS1 PMs.py". Connects to a 1250 (paramiko, via
the shared NPT1250Client) and pulls physical status, alarms, and CES PM figures
for a user-supplied set of DS1s.

Runs standalone (prompts for its own inputs) OR via launcher.py (which passes a
params dict to run()).
Validation (from the original): slots ts1/ts3/ts6/ts8, channels 1-28.
Input accepts bare "tsX/Y"; normalized to "ds1-tsX/Y" for device commands.
READ-ONLY: 'show' commands only.

Built by P. Capps.
"""

import os
import datetime

import platform_paths

try:
    import paramiko  # noqa: F401
    _DEP_ERROR = None
except ImportError as e:
    _DEP_ERROR = "paramiko not installed (pip install paramiko): {}".format(e)

from npt1250_client import NPT1250Client
from device_base import CredentialError, prompt_ip
from jira_client import post_report, valid_ticket

VALID_SLOTS = ["ts1", "ts3", "ts6", "ts8"]
CHANNEL_MIN = 1
CHANNEL_MAX = 28


def _parse_and_validate(raw_entries):
    """Return (channels, error). channels = [(full, slot, ch_num), ...]."""
    channels = []
    seen = []
    for entry in raw_entries:
        raw = entry.strip().lower()
        core = raw[len("ds1-"):] if raw.startswith("ds1-") else raw
        if not core.startswith("ts"):
            return None, "Invalid format: '{}'  (expected tsX/Y, e.g. ts1/21)".format(entry.strip())
        try:
            slot, ch_num_str = core.split("/")
            ch_num = int(ch_num_str)
        except (ValueError, IndexError):
            return None, "Could not parse: '{}'  (expected tsX/Y, e.g. ts1/21)".format(entry.strip())
        if slot not in VALID_SLOTS:
            return None, "Invalid slot '{}' in '{}'  (valid: {})".format(
                slot, entry.strip(), ", ".join(VALID_SLOTS))
        if ch_num < CHANNEL_MIN or ch_num > CHANNEL_MAX:
            return None, "Channel {} out of range in '{}'  (valid: {}-{})".format(
                ch_num, entry.strip(), CHANNEL_MIN, CHANNEL_MAX)
        ch = "ds1-{}/{}".format(slot, ch_num)
        if ch in seen:
            return None, "Duplicate channel: '{}'".format(entry.strip())
        seen.append(ch)
        channels.append((ch, slot, ch_num))
    return channels, None


# Launcher menu metadata (read by launcher.py auto-discovery).
TOOL_INFO = {
    'name': 'DS1 PMs',
    'ran_from': '1250',
    'desc': 'Pulls physical status, alarms, and PMs for specific DS1s on a 1250.',
    'requires': '1250 IP, DS1(s), creds',
    'order': 30,
}


def collect_inputs(session=None):
    """Prompt for inputs; return params dict (or None to abort)."""
    import getpass
    session = session if session is not None else {}

    ip = prompt_ip("1250 IP", default=session.get("ip_1250"))
    if not ip:
        return None

    print("Enter DS1(s), comma-separated.  Format: tsX/Y  (e.g. ts1/21, ts3/6)")
    print("  Valid slots: {} | channels {}-{}".format(
        ", ".join(VALID_SLOTS), CHANNEL_MIN, CHANNEL_MAX))
    default_ds1 = session.get("ds1_list")
    prompt = "DS1(s)"
    if default_ds1:
        prompt += " [{}]".format(",".join(default_ds1))
    channels_str = input(prompt + ": ").strip()
    if not channels_str and default_ds1:
        raw_entries = list(default_ds1)
    else:
        # Normalize: accept & as an alternate separator (lowercase handled in _parse_and_validate)
        raw_entries = [e for e in channels_str.replace("&", ",").split(",") if e.strip()]
    if not raw_entries:
        print("No input.")
        return None
    channels, err = _parse_and_validate(raw_entries)
    if err:
        print(err)
        return None
    print("  {} DS1(s): {}".format(len(channels), ", ".join(c[0] for c in channels)))

    jira_ticket = None
    if input("Add to JIRA? (y/n) [n]: ").strip().lower() in ("y", "yes"):
        while True:
            jira_ticket = input("JIRA number (NOE-XXXX): ").strip().upper()
            if valid_ticket(jira_ticket):
                break
            if not jira_ticket:
                print("  (blank -- skipping JIRA)")
                jira_ticket = None
                break
            print("  Invalid format. Expected NOE-XXXX (e.g. NOE-1234).")

    open_when_done = input("Open results file when done? (y/n) [y]: ").strip().lower()
    open_when_done = open_when_done in ("", "y", "yes")

    user = session.get("infra_user")
    pw   = session.get("infra_pass")
    if not (user and pw):
        user = input("Username: ").strip()
        pw   = getpass.getpass("Password: ")
    if not (user and pw):
        print("Missing credentials.")
        return None

    # ds1_list stored as the raw normalized strings for session reuse
    return {
        "ip_1250": ip,
        "ds1_list": [c[0] for c in channels],
        "username": user,
        "password": pw,
        "jira_ticket": jira_ticket,
        "open_when_done": open_when_done,
    }


def run(params):
    """Execute the DS1 PM check given a params dict (no prompting)."""
    if _DEP_ERROR:
        print("[!] {} -- tool unavailable.".format(_DEP_ERROR))
        return

    ip = params["ip_1250"]
    # Accept ds1_list as normalized 'ds1-tsX/Y' strings; re-validate to get parts.
    ds1_list = params["ds1_list"]
    channels, err = _parse_and_validate(ds1_list)
    if err:
        print("[!] {}".format(err))
        return None
    user = params["username"]
    pw   = params["password"]
    jira_ticket    = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", True)

    c = NPT1250Client(ip, user, pw)
    print("\n[connecting] {}".format(ip))
    try:
        c.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return None
    host = c.hostname
    print("  host: {}".format(host))

    lines = []
    lines.append("NPT-1250 DS1 PMs")
    lines.append("Host: {}  IP: {}".format(host, ip))
    lines.append("=" * 64)

    def block(title, cmd):
        raw = c.run(cmd + " | no-more")
        lines.append("")
        lines.append("  >> {}".format(title))
        lines.append("  CMD: {}".format(cmd))
        for ln in raw.splitlines():
            clean = ln.rstrip()
            if clean:
                lines.append("    {}".format(clean))
        return raw

    findings = []   # per-DS1 one-liners for the JIRA summary
    for ch, slot, ch_num in channels:
        print("  collecting: {}".format(ch))
        lines.append("")
        lines.append("=" * 64)
        lines.append("  -- {} --".format(ch))
        lines.append("=" * 64)
        raw_status = block("Physical Status",
              'show interfaces physical status slot {} | grep "ds1/{} "'.format(slot, ch_num))
        raw_alarms = block("NE Alarms", "show ne alarms | grep {}".format(ch))
        block("Physical PM 24hr", "show interfaces physical pm 24hour {}".format(ch))
        block("CES PM 24hr (Valid intervals)",
              "show interfaces pm 24hour {} ces | grep Valid".format(ch))
        block("CES PM 24hr (ES)",
              "show interfaces pm 24hour {} ces | grep ES | except Stat".format(ch))
        block("CES PM 24hr (UAS)",
              "show interfaces pm 24hour {} ces | grep UAS".format(ch))

        # One-liner per DS1: echo the device's own status row + alarm count.
        status_row = next(
            (l.strip() for l in raw_status.splitlines() if "ds1/" in l.lower()), "")
        alarm_rows = [l.strip() for l in raw_alarms.splitlines()
                      if ch in l and l.strip()]
        finding = "{}  {}".format(ch, status_row if status_row
                                  else "(no status row returned)")
        if alarm_rows:
            finding += "  |  {} alarm(s): {}".format(
                len(alarm_rows), alarm_rows[0][:60])
        else:
            finding += "  |  no alarms"
        findings.append(finding)

    c.close()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("DS1_PMs_{}_{}.txt".format(ts, host))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = "DS1 PMs -- {} DS1(s) on {}, run {}.".format(
            len(channels), host, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        ok, msg = post_report(
            jira_ticket, intro, "DS1 PMs -- {}".format(host), "\n".join(lines),
            attach_path=out_path, summary_text="\n".join(findings))
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open: {}".format(e))

    return out_path


def main():
    print("=" * 64)
    print("  NPT-1250 DS1 PMs (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
