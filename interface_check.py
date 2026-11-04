#!/usr/bin/env python3
"""
interface_check.py -- standalone NPT-1800 NNI interface / reachability check.

Standalone port of the SecureCRT interface_check.py. Connects to an NPT-1800
(paramiko, via the shared NPT1800Client) and verifies an NNI sub-interface:
description, config, protocols, OSPF/LDP adjacency, and ping reachability
(destination = interface IP with last octet - 1).

Instead of an IP, the user picks the 1800 by 3-letter site (LAX/ATL/JFK/DFW);
the site maps to the hub IP below.

Runs standalone (prompts for its own inputs) OR via launcher.py (which passes a
params dict to run()).  READ-ONLY: 'show' commands + ping reachability only.

Built by P. Capps.
"""

import re
import os
import datetime

import platform_paths

try:
    import paramiko  # noqa: F401
    _DEP_ERROR = None
except ImportError as e:
    _DEP_ERROR = "paramiko not installed (pip install paramiko): {}".format(e)

from npt1800_client import NPT1800Client
from device_base import CredentialError, prompt_ip
from jira_client import post_report, valid_ticket

import hub_registry
import nni_check
from output_builders import (
    parse_subint_ip, derive_1250_subint_ip, derive_1250_loopback,
    check_down_history,
    check_ospf_outage_events,
    pair_ospf_outages,
    pair_ospf_outages_by_peer,
    format_ospf_outages,
)

# Site -> 1800 hub IP.
#
# This used to be a hardcoded literal here, and the same fact was ALSO hardcoded
# in npt1800_client.HUB_IPS. This copy was corrected on 2026-07-08; the other was
# not, so LAX circuits were reported as DFW for ~2 months. Both now resolve
# through hub_registry, which reads NetBox and falls back to one verified table.
#
# Kept as a module attribute because app.py validates against
# interface_check.SITE_TO_1800. Prefer hub_registry.sites() / site_to_ip() in new
# code -- this snapshot is taken once at import.
SITE_TO_1800 = hub_registry.build_site_to_ip()


# Launcher menu metadata (read by launcher.py auto-discovery).
TOOL_INFO = {
    'name': 'IXC Check',
    'ran_from': '1800',
    'desc': 'Checks an IXC circuit from the 1800 to the 1250: config, OSPF/LDP, and ping reachability.',
    'requires': '1800 site (ATL/LAX/JFK/DFW/LAB), interface, creds',
    'order': 60,
}


def collect_inputs(session=None):
    """
    Prompt for this tool's inputs. Returns a params dict (or None to abort).
    `session` is an optional dict of remembered values across launcher tools:
    device-identifying values are offered as editable defaults; credentials are
    reused silently if already present.
    """
    import getpass
    session = session if session is not None else {}

    # Site (device-identifying -> confirmable, not silent)
    default_site = session.get("site")
    prompt = "1800 site [ATL/LAX/JFK/DFW/LAB]"
    if default_site:
        prompt += " [{}]".format(default_site)
    site = input(prompt + ": ").strip().upper()
    if not site and default_site:
        site = default_site
    if site not in SITE_TO_1800:
        print("Invalid site '{}'. Valid: {}".format(site, ", ".join(SITE_TO_1800)))
        return None
    hub_ip = SITE_TO_1800[site]
    print("  {} -> {}".format(site, hub_ip))

    # Interface (device-identifying -> confirmable default)
    default_iface = session.get("interface")
    prompt = "Interface (e.g. xe-ts13/3.624)"
    if default_iface:
        prompt += " [{}]".format(default_iface)
    iface = input(prompt + ": ").strip()
    if not iface and default_iface:
        iface = default_iface
    ok, why = validate_iface(iface)
    if not ok:
        print("Invalid interface: {}".format(why))
        return None
    if why:
        print("  [note] {}".format(why))

    # JIRA
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

    # Credentials (reused silently within a launcher session if present)
    user = session.get("infra_user")
    pw   = session.get("infra_pass")
    if not (user and pw):
        user = input("Username: ").strip()
        pw   = getpass.getpass("Password: ")
    if not (user and pw):
        print("Missing credentials.")
        return None

    return {
        "site": site,
        "interface": iface,
        "username": user,
        "password": pw,
        "jira_ticket": jira_ticket,
        "open_when_done": open_when_done,
    }


_IPV4_RE  = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
_IFACE_RE = re.compile(r'^[a-z]+\d*-[a-z]{2}\d+/\d+\.\d+$', re.IGNORECASE)


def validate_iface(iface):
    """
    Validate an NNI.VLAN interface identifier, e.g. 'xe-ts16/2.111'.

    Returns (ok, reason).

    The old check was just `"." in iface`, which an IP ADDRESS satisfies. Passing
    10.111.135.100 produced nni='10', vlan='111' -- the first two octets -- and
    the tool went on to run 'show configuration interfaces 10 unit 111', which the
    device rejected. Every downstream section then filtered on vlan '111', which
    matches the second octet of every 10.111.x.x address on the box, so the OSPF
    and LDP sections dumped ~100 unrelated neighbours instead of one.

    An IP is therefore rejected explicitly, with a message that says what to enter
    instead -- the failure was silent and the report looked superficially plausible.
    """
    iface = (iface or "").strip()
    if not iface:
        return False, "empty"
    if _IPV4_RE.match(iface):
        return False, ("that is an IP address, not an interface. Enter the NNI "
                       "and VLAN, e.g. xe-ts16/2.111")
    if "/" not in iface:
        return False, ("missing the slot/port. Expected <nni>/<port>.<vlan>, "
                       "e.g. xe-ts16/2.111")
    if "." not in iface:
        return False, "missing the .VLAN suffix, e.g. xe-ts16/2.111"
    if not iface.rsplit(".", 1)[1].isdigit():
        return False, "the part after the last dot must be a numeric VLAN"
    if not _IFACE_RE.match(iface):
        # Permissive warning path: shape is close enough to proceed, but say so.
        return True, "unusual format -- proceeding"
    return True, ""


def _cmd_failed(out):
    """
    Detect a command the DEVICE rejected, or output mangled by the pager.

    Without this the tool appended the device's error text as if it were data and
    carried on, then reported 'No IP address found in interface config' -- which
    reads like a clean negative result rather than a command that never ran.
    """
    low = (out or "").lower()
    for marker in ("syntax error", "unknown command", "invalid command"):
        if marker in low:
            return "device rejected the command"
    for marker in ("--more--", "press 'h' for instructions",
                   "press space to continue"):
        if marker in low:
            return "output truncated by pager (missing | no-more)"
    return None


def run(params):
    """Execute the interface check given a params dict (no prompting)."""
    if _DEP_ERROR:
        print("[!] {} -- tool unavailable.".format(_DEP_ERROR))
        return

    site  = params["site"].upper()
    iface = params["interface"]
    user  = params["username"]
    pw    = params["password"]
    jira_ticket    = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", True)

    # Resolve live rather than from the import-time snapshot, and record where the
    # answer came from so a NetBox outage or a drifted fallback is visible in the
    # report instead of silently trusted.
    hub_ip, hub_src, hub_warns = hub_registry.site_to_ip(site)
    if not hub_ip:
        print("[!] invalid site '{}'. Valid: {}".format(
            site, ", ".join(hub_registry.sites())))
        return None
    print("  hub: {} -> {}  ({})".format(site, hub_ip, hub_registry.describe_source()))
    for w in hub_warns:
        print("  [!] {}".format(w))
    ok, why = validate_iface(iface)
    if not ok:
        print("[!] invalid interface '{}': {}".format(iface, why))
        return None
    # Split on the LAST dot: the NNI itself contains no dot, but splitting on the
    # first one silently mangled anything unexpected (see validate_iface).
    nni, vlan = iface.rsplit(".", 1)

    c = NPT1800Client(hub_ip, user, pw)
    print("\n[connecting] {} ({})".format(site, hub_ip))
    try:
        c.connect()
    except CredentialError:
        raise
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return None
    try:
        # try/finally added 2026-10-01: the body below is long, and any
        # exception in it used to return without closing the session.
        # Harmless for a one-shot CLI run (process exits, socket drops) but
        # NOT in the long-lived Flask app, where the transport and its
        # reader thread would survive until the device timed the session out.
        print("  host: {}  loc: {}".format(c.hostname, c.loc_code))

        lines = []
        lines.append("NPT-1800 INTERFACE CHECK")
        lines.append("Site: {} ({})   Host: {}".format(site, hub_ip, c.hostname))
        # Cross-check the site we were ASKED for against the site the device REPORTS.
        # The device's own hostname is the highest authority; if they disagree, the
        # hub map is stale and this report may be about the wrong box entirely.
        dev_site = hub_registry.site_from_hostname(c.hostname)
        if dev_site and dev_site != site:
            lines.append("[!] SITE MISMATCH -- asked for {} ({}) but the device "
                         "identifies as {} ('{}'). The hub map is stale; trust the "
                         "device.".format(site, hub_ip, dev_site, c.hostname))
            print("  [!] SITE MISMATCH: asked {} but device says {}".format(site, dev_site))
        lines.append("Hub map source: {}".format(hub_registry.describe_source()))
        lines.append("Interface: {}".format(iface))
        lines.append("=" * 64)

        # Every command ends in '| no-more'.
        #
        # Without it the device paginates at '---More---[Press space to continue]' and
        # the session then feeds the NEXT command's characters to the PAGER instead of
        # the CLI. That is what produced "Can't open help file", "No previous regular
        # expression" and stray "[Press 'h' for instructions.]" lines in the output --
        # and why the OSPF section's neighbour list ran on into the LDP section, so
        # the LDP heading sat above OSPF data. Sections could not be trusted to match
        # their own headings.
        #
        # Filters use the full interface name, not the bare VLAN. Grepping '111' on a
        # 10.111.x.x network matches the second octet of essentially every address on
        # the box (and uptime strings like '11m:51s'), which is why one interface's
        # check returned about a hundred neighbours.
        commands = [
            ("Interface Description",
             "show interfaces description | grep {} | no-more".format(iface)),
            ("Interface Config",
             "show configuration interfaces {} unit {} | display-set | no-more".format(
                 nni, vlan)),
            ("Protocols (interface {})".format(iface),
             "show configuration protocols | display-set | grep {} | except tunnel "
             "| no-more".format(iface)),
            ("OSPF Neighbor",
             "show ospf neighbor | grep {} | no-more".format(iface)),
            ("LDP Adjacency",
             "show ldp adjacency | grep {} | no-more".format(iface)),
        ]

        captured = {}
        failures = {}
        for title, cmd in commands:
            print("  collecting: {}".format(title))
            out = c.run(cmd, 30)
            captured[cmd] = out
            problem = _cmd_failed(out)
            if problem:
                failures[title] = problem
                print("    [!] {}".format(problem))
            lines.append("")
            lines.append("  >> {}".format(title))
            lines.append("  CMD: {}".format(cmd))
            if problem:
                # Say so in the report. Previously the device's own error text was
                # appended as though it were collected data.
                lines.append("  [!] FAILED -- {}".format(problem))
            body = [ln.rstrip() for ln in out.splitlines() if ln.rstrip()]
            if not body:
                lines.append("    (no output)")
            for ln in body:
                lines.append("    {}".format(ln))

        # ---- NNI / blast radius -------------------------------------------------
        # Is this a circuit problem, or is the whole port down? One extra command
        # (`show ospf neighbor | no-more`) gives every sub-interface on the same
        # physical port and its adjacency state, so a port-level outage is named
        # here rather than discovered after fifty IXC tickets have been raised.
        nni_res = None
        try:
            nni_res = nni_check.check_nni(c, iface, hub_name=c.hostname,
                                          progress=print)
            lines.append("")
            lines.append(nni_check.render_block(nni_res))
        except Exception as e:
            print("  [!] NNI check errored (report still valid): {}".format(e))
            lines.append("")
            lines.append("  [!] NNI / blast-radius check errored: {}".format(e))

        # ---- "does this sub-interface even exist here?" -------------------------
        #
        # Confirmed live 2026-10-01: xe-ts13/3.624 was entered against ces01.jfk1,
        # where that port runs .132 through .978 and has no .624. All five reads
        # correctly returned nothing -- and the report was five "(no output)"
        # blocks with no conclusion drawn. That reads like a broken run, and it is
        # indistinguishable from "the interface exists but every command came back
        # empty", which is an entirely different problem.
        #
        # The two facts together are conclusive, and the tool already had both:
        # every read empty (with NO command failures, so the reads are trustworthy)
        # PLUS a parent port that is present and healthy. Say it outright. "Did I
        # typo the VLAN or is the circuit broken" is the first question a NOC
        # engineer asks, and the report should answer it rather than imply it.
        all_empty = all(not (captured.get(cmd) or "").strip()
                        for _t, cmd in commands)
        parent_ok = bool(nni_res and nni_res.get("total_on_port"))
        if all_empty and not failures:
            lines.append("")
            lines.append("-" * 76)
            lines.append("  VERDICT")
            lines.append("-" * 76)
            if parent_ok:
                lines.append(
                    "  [!] {} is NOT CONFIGURED on {}.".format(iface, c.hostname))
                lines.append(
                    "      All five reads returned nothing and none of them "
                    "failed, while the")
                lines.append(
                    "      parent port {} is present and healthy ({} "
                    "sub-interface(s), {} down).".format(
                        nni, nni_res.get("total_on_port"),
                        nni_res.get("down_on_port", 0)))
                lines.append(
                    "      Check the VLAN -- did you mean a different "
                    "sub-interface, or a different hub?")
            else:
                # Parent not seen either: could be the wrong hub entirely, or a
                # port that does not exist. Do NOT claim which.
                lines.append(
                    "  [!] {} returned no data, and the parent port {} was not "
                    "seen either.".format(iface, nni))
                lines.append(
                    "      Either this interface is on a different hub, or the "
                    "port does not exist")
                lines.append(
                    "      on {}. Not enough information to say which.".format(
                        c.hostname))
        elif all_empty and failures:
            # Empty AND something errored -> absence proves nothing.
            lines.append("")
            lines.append("-" * 76)
            lines.append("  VERDICT")
            lines.append("-" * 76)
            lines.append(
                "  [!] All reads were empty, but {} command(s) FAILED -- "
                "absence of data".format(len(failures)))
            lines.append(
                "      proves nothing here. Fix the failure above and re-run "
                "before concluding")
            lines.append(
                "      anything about {}.".format(iface))

        ping_results = {}   # title -> "4/4 (0% loss)" style string, for the summary
        # Key must match exactly what was stored at capture time (line ~289),
        # including the trailing '| no-more' -- without it this lookup always
        # missed, cfg was always "", and both ping and the LDP check silently
        # skipped on every run reporting "no IP found in interface config" even
        # when the config genuinely had one (confirmed live 2026-09-09).
        cfg = captured.get(
            "show configuration interfaces {} unit {} | display-set | no-more".format(
                nni, vlan), "")
        # Real display-set output is "set interfaces <iface> unit <n> family inet
        # address <ip>/<mask>" -- the interface name + unit number sit between
        # "set" and "family", so requiring them contiguous (old pattern) never
        # matched real output. Confirmed live 2026-09-02: config genuinely had
        # the IP; the regex was just checking for text that doesn't exist in
        # that shape.
        ip = parse_subint_ip(cfg)
        if ip:
            dest = derive_1250_subint_ip(ip)
            for title, cmd in [
                ("Ping (4)", "ping {} count 4".format(dest)),
                ("Ping (4, 8000B DF)",
                 "ping {} count 4 size 8000 do-not-fragment".format(dest)),
            ]:
                print("  collecting: {}".format(title))
                out = c.run(cmd, 30)
                # The device writes "4 packets transmitted, 4 received, 0%
                # packet loss" -- NOT "4 packets received". The old pattern
                # required the word "packets" before "received", so it matched
                # nothing and every ping reported "no ping statistics in
                # output" even when all four replied (pcapps, DFW
                # xe-ts16/2.732, 2026-11-02). "packets" before "received" is
                # now optional.
                pm = re.search(
                    r'(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets\s+)?'
                    r'received,\s*(\d+)%\s*packet loss', out)
                if pm:
                    ping_results[title] = "{}/{} received ({}% loss)".format(
                        pm.group(2), pm.group(1), pm.group(3))
                else:
                    ping_results[title] = "no ping statistics in output"
                lines.append("")
                lines.append("  >> {}".format(title))
                lines.append("  CMD: {}".format(cmd))
                for ln in out.splitlines():
                    clean = ln.rstrip()
                    if clean:
                        lines.append("    {}".format(clean))

            # ---- LDP/OSPF outage history for this 1250, at the bottom of ----
            # this interface's block (direction 2026-09-09, outage-pairing added
            # 2026-09-14, split into separate LDP/OSPF summaries 2026-09-14).
            #
            # 'show ne events' (NOT 'show ne events unreported' -- that buffer
            # gets crowded out by unrelated noise in well under an hour elsewhere
            # in this tool) retains this history for 300+ days. Filtered on the
            # 1250's own loopback IP, derived here (never asked for as a separate
            # input) via the confirmed convention: 1800 sub-int (this interface's
            # own IP) -> 1250 sub-int (4th octet - 1) -> 1250 loopback (same first
            # two octets, 3rd octet forced to 130, 4th octet halved).
            #
            # The grep is on the peer IP only, not '| grep Down' -- the Up/Full
            # clearing events are needed too so each drop can be paired with what
            # cleared it (see check_ospf_outage_events / pair_ospf_outages). One
            # device call, then split client-side into two separate summaries:
            # LDP session state and OSPF adjacency state are two different
            # protocols riding the same link, and traffic can keep routing on an
            # intact LDP session even while OSPF reconverges -- so an OSPF-only
            # view can overstate an outage that never actually affected traffic.
            # LDP is the better proxy for an actual IXC outage; both are shown.
            loopback = derive_1250_loopback(dest)
            ldp_outages = None
            ospf_outages = None
            lines.append("")
            if not loopback:
                lines.append("  LDP/OSPF Outages: could not derive 1250 loopback "
                             "from {} -- unexpected addressing (4th octet not "
                             "evenly divisible by 2). Skipping -- not "
                             "guessing.".format(dest))
            else:
                cmd = "show ne events | grep {} | no-more".format(loopback)
                try:
                    ne_raw = c.run(cmd, 30)
                except Exception as e:
                    ne_raw = ""
                    lines.append("  LDP/OSPF Outages: command failed -- {}: {}".format(
                        type(e).__name__, e))
                # peer_ip is passed explicitly here (this loopback), so every
                # matched line already belongs to this one site/interface --
                # pair_ospf_outages_by_peer is a no-op vs pair_ospf_outages in
                # this single-session context, used anyway for consistency with
                # the multi-session-aware call sites in ospf_stability.py.
                ldp_outages = pair_ospf_outages_by_peer(
                    check_ospf_outage_events(ne_raw, loopback, protocol="ldp"))
                ospf_outages = pair_ospf_outages_by_peer(
                    check_ospf_outage_events(ne_raw, loopback, protocol="ospf"))
                # format_ospf_outages' own header line always reads "OSPF
                # Outages: N" -- skip it ([1:]) and print our own header so the
                # LDP block is labeled correctly.
                lines.append("  LDP Outages: {}".format(len(ldp_outages)))
                for ln in format_ospf_outages(ldp_outages).splitlines()[1:]:
                    lines.append("  {}".format(ln))
                lines.append("")
                for ln in format_ospf_outages(ospf_outages).splitlines():
                    lines.append("  {}".format(ln))
                # Always-show LDP SESSION state for this circuit's peer -- the
                # outage history above is the bounce record; this is the live
                # session (state + uptime). Read on the 1800 (c), filtered to the
                # 1250's loopback; bounce annotated from ne_raw already in hand.
                import ldp_check
                lines.append("")
                lines.append("  LDP STATUS:")
                try:
                    _ldp_sess = c.run(ldp_check.LDP_SESSION_CMD, 30)
                except Exception as _le:
                    _ldp_sess = ""
                    lines.append("  [ -- ] LDP session read failed -- {}".format(
                        str(_le)[:60]))
                else:
                    for _ln in ldp_check.status_lines(
                            _ldp_sess, event_raw=ne_raw, peer_filter=loopback):
                        lines.append(_ln)
        else:
            # Distinguish "the config genuinely has no inet address" from "the config
            # command never ran". The old wording claimed the former in both cases.
            cfg_title = "Interface Config"
            lines.append("")
            if cfg_title in failures:
                lines.append("  [!] SKIPPING PING -- could not read the interface "
                             "config ({}).".format(failures[cfg_title]))
                lines.append("      No conclusion should be drawn about this "
                             "interface from this run.")
            else:
                lines.append("  NOTE: interface config read OK but contains no "
                             "'family inet address' -- skipping ping.")

        # Surface any failures at the top level so they are not buried mid-report.
        if failures:
            lines.append("")
            lines.append("=" * 64)
            lines.append("  COMMAND FAILURES ({})".format(len(failures)))
            lines.append("=" * 64)
            for t, why in failures.items():
                lines.append("  {:<28} {}".format(t, why))
            lines.append("  Sections above marked FAILED contain device error text,")
            lines.append("  not interface data.")

    finally:
        c.close()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = platform_paths.path("interface_check_{}_{}.txt".format(ts, c.hostname))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return None

    if jira_ticket:
        print("\n[JIRA] Posting to {} ...".format(jira_ticket))
        intro = "Interface check -- {} on {} ({}), run {}.".format(
            iface, c.hostname, site,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Findings summary -- config/OSPF/LDP/ping at a glance.
        def _found(title_cmd_key, label):
            raw = ""
            for cmd, out in captured.items():
                if title_cmd_key in cmd:
                    raw = out
                    break
            hit = next((l.strip() for l in raw.splitlines() if l.strip()), "")
            return "{}: {}".format(label, hit if hit else "NONE FOUND")
        sum_lines = [
            "Interface {} on {} ({})".format(iface, c.hostname, site),
            _found("show ospf neighbor", "OSPF neighbor"),
            _found("show ldp adjacency", "LDP adjacency"),
        ]
        if ping_results:
            for title, res in ping_results.items():
                sum_lines.append("{}: {}".format(title, res))
        else:
            sum_lines.append("Ping: skipped -- no IP found in interface config")
        if ldp_outages is None:
            sum_lines.append("LDP/OSPF Outages: skipped -- no IP found "
                             "in interface config")
        else:
            sum_lines.append("LDP Outages: {}".format(len(ldp_outages)))
            sum_lines.extend(format_ospf_outages(ldp_outages).splitlines()[1:])
            sum_lines.append("")
            sum_lines.extend(format_ospf_outages(ospf_outages).splitlines())
        ok, msg = post_report(
            jira_ticket, intro,
            "Interface Check -- {}".format(iface), "\n".join(lines),
            attach_path=out_path, summary_text="\n".join(sum_lines))
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open: {}".format(e))

    return out_path


def main():
    print("=" * 64)
    print("  NPT-1800 INTERFACE CHECK (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
