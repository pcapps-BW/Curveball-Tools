#!/usr/bin/env python3
"""
morning_check.py -- Morning circuit health check automation.

Queries JIRA for all open NOE tickets assigned to the current user,
parses the description for circuit triggers, runs the appropriate tools,
posts results back to JIRA, and sends a digest to Slack.

Trigger format (line 1 of JIRA description):
    1250: <IP> | <DS1>        -> Full suite: Autotester + Fiber + OSPF
    1250: <IP>                -> Lite check: Fiber + OSPF only
    (no match)                -> Post "Scanned -- no automation triggers found."

Config read from platform_paths.path("autotester_config.txt")
(C:\\Claude\\autotester_config.txt on Windows, ~/.claude/... on Linux):
    JIRA_EMAIL=...            (non-sensitive, file)
    JIRA_ASSIGNEE_EMAIL=...   (non-sensitive, file)
    SLACK_WEBHOOK=...         (non-sensitive, file)
    INFRA_USER=...            (non-sensitive, file)
    GSX_USER=...              (non-sensitive, file)
    INFRA_PASS                (secure, Credential Manager via keyring)
    GSX_PASS                  (secure, Credential Manager via keyring)
    JIRA_TOKEN                (secure, Credential Manager via keyring)

IXC interface_check is NOT included in the batch suite -- it requires a
specific site + interface that can't be inferred from the 1250 IP alone.
Run it manually from the web UI when needed.

Built by P. Capps.
"""

import re
import sys
import os
import datetime
import getpass
import base64

import platform_paths

try:
    import requests
except ImportError:
    print("ERROR: requests not installed.  pip install requests")
    sys.exit(1)

from device_base import CredentialError, prompt_ip
from jira_client import post_comment, post_report, valid_ticket, JIRA_BASE, post_slack
from secure_config import load_config, save_config_key as _save_key

# ── Launcher contract ─────────────────────────────────────────────────────────
TOOL_INFO = {
    'name':     'Morning Check',
    'ran_from': 'Windows workstation',
    'desc':     'Batch Autotester: scans all open NOE tickets assigned to you and '
                'runs the same Autotester circuit trace (plus Fiber Utilization + '
                'OSPF Events) on each one based on description triggers, posts '
                'results to JIRA, and sends a Slack digest.',
    'requires': 'JIRA creds + infra creds + GSX creds + Slack webhook (config file)',
    'order':    5,
}

CONFIG_PATH   = platform_paths.path("autotester_config.txt")
OUTPUT_DIR    = platform_paths.BASE_DIR
JIRA_BASE_URL = "https://bandwidth-jira.atlassian.net"

# Regex to parse description line 1
# Group 1: IP,  Group 2: DS1(s) (optional)
TRIGGER_RE = re.compile(
    r'^\s*1250\s*:\s*([\d.]+)'       # "1250: <IP>"
    r'(?:\s*[|,]\s*([^\r\n]+))?',    # optional "| <DS1(s)>"
    re.IGNORECASE
)
# Matches ONLY well-formed DS1 tokens (same tsN/M shape _ds1_parts() expects
# elsewhere in the suite) and is used with finditer() to PULL matching
# tokens out of the free-text remainder of the trigger line, rather than
# crude whitespace/comma splitting + a loose startswith("ts") check. The
# old approach fused a DS1 with adjoining note text that had no separator
# (e.g. "ts1/7Telamon Engaged" -> a bogus "ts1/7telamon" token that still
# passed startswith("ts")) and fed it straight to the Autotester as a real
# DS1, which then failed hop 1 with "No MUSE assignment found for
# ts1/7telamon" -- confirmed on a live run 2026-08-06. finditer() only
# ever extracts the well-formed "ts1/7" and silently drops "Telamon" and
# "Engaged" since neither matches the DS1 shape.
DS1_TOKEN_RE = re.compile(r'ts\d+/\d+', re.IGNORECASE)


# ── JIRA REST helpers ─────────────────────────────────────────────────────────
def _jira_headers(email, token):
    cred = base64.b64encode("{}:{}".format(email, token).encode()).decode()
    return {
        "Authorization": "Basic {}".format(cred),
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


class JiraAuthError(Exception):
    """Raised when /myself confirms the email/token pair is flat-out
    rejected (401/403) -- deliberately distinct from 'ran fine, found
    zero tickets' so run() can report something actionable instead of
    the generic no-tickets message."""
    pass


def _whoami(email, token):
    """
    GET /rest/api/3/myself -- returns who Jira thinks is authenticated for
    this email/token pair (accountId, displayName, emailAddress), or an
    error. Returns (who_dict_or_None, err_str_or_None, status_code_or_None).

    This matters because a clean HTTP 200 on the ticket SEARCH endpoint
    does NOT prove the credentials are valid -- confirmed via Atlassian's
    own support KB ("JQL search REST API returns 200 OK without API
    token"): with bad/missing auth, Jira's search silently falls back to
    ANONYMOUS access and returns a normal-shaped 200 response with
    whatever anonymous users can see (usually nothing), instead of
    erroring. /myself has no such fallback -- a bad token gets a real
    401 there, which is what actually happened on the live run this
    guards against (search kept reporting "0 issues" while /myself said
    401 the whole time).
    """
    url = "{}/rest/api/3/myself".format(JIRA_BASE_URL)
    try:
        r = requests.get(url, headers=_jira_headers(email, token), timeout=15)
    except Exception as e:
        return None, "request failed: {}".format(e), None
    if r.status_code != 200:
        return None, "HTTP {} -- {}".format(r.status_code, (r.text or "")[:200]), r.status_code
    me = r.json()
    return {
        "accountId":    me.get("accountId", "?"),
        "displayName":  me.get("displayName", "?"),
        "emailAddress": me.get("emailAddress", "?"),
    }, None, r.status_code


def get_my_open_tickets(email, token, assignee_email):
    """Return [{key, summary, description}] for open NOE In Progress tickets.

    Uses /rest/api/3/search/jql -- Atlassian fully REMOVED the old
    /rest/api/3/search endpoint (HTTP 410, confirmed live 2026-08-06:
    "The requested API has been removed. Please migrate to the
    /rest/api/3/search/jql API."). The new endpoint also drops
    startAt/total in favor of nextPageToken-based pagination, so this
    loops on that instead of assuming one page covers everything.

    Uses assignee = currentUser() rather than matching assignee_email as a
    literal string -- Jira Cloud's "User Email Visibility" privacy setting
    can make assignee = "someone@x.com" silently resolve to ZERO issues
    (200 OK, no error, just nothing matches) if it can't look up that email
    to an account, which is exactly what happened on the live run this
    replaces: HTTP 200, empty issues list, no error to point at. Since the
    JIRA token passed in already IS assignee_email's own account,
    currentUser() sidesteps the lookup entirely and can't have this failure
    mode. assignee_email is still used for display/logging only now.
    """
    jql = (
        'project = NOE AND assignee = currentUser() '
        'AND status = "In Progress" '
        'ORDER BY updated DESC'
    )

    url = "{}/rest/api/3/search/jql".format(JIRA_BASE_URL)
    headers = _jira_headers(email, token)

    who, who_err, who_status = _whoami(email, token)
    if who:
        print("  [JIRA] Authenticated as: {} <{}>  (accountId {})".format(
            who["displayName"], who["emailAddress"], who["accountId"]))
    else:
        print("  [JIRA] Could not confirm identity via /myself: {}".format(who_err))
        if who_status in (401, 403):
            # Confirmed rejected, not just a transient/network hiccup --
            # stop HERE instead of running the search, which would
            # otherwise fall back to anonymous and report a misleadingly
            # benign "0 issues" instead of the real problem.
            raise JiraAuthError(
                "JIRA email/API token was rejected (HTTP {}). Go to "
                "id.atlassian.com -> Security -> API tokens, create a new "
                "token, and make sure the JIRA email field matches the "
                "account you created it under.".format(who_status))

    print("  [JIRA] JQL: {}".format(jql))

    results = []
    next_token = None
    for _ in range(20):   # hard cap -- 20 * 50 = 1000 issues, ample headroom
        body = {"jql": jql, "maxResults": 50, "fields": ["summary", "description"]}
        if next_token:
            body["nextPageToken"] = next_token
        try:
            r = requests.post(url, headers=headers, json=body, timeout=30)
        except Exception as e:
            print("[JIRA] Query failed: {}".format(e))
            return results

        if r.status_code != 200:
            print("[JIRA] Query returned HTTP {} -- {}".format(r.status_code, r.text[:200]))
            return results

        # Defense in depth, in case /myself ever succeeds but the search
        # itself still runs anonymously for some other reason: Jira's
        # response carries this header naming who the request ran as.
        if (r.headers.get("x-ausername") or "").lower() == "anonymous":
            raise JiraAuthError(
                "The ticket search ran as an ANONYMOUS user (x-ausername: "
                "anonymous), not an authenticated one, even though this "
                "request got HTTP 200 -- the JIRA email/API token is being "
                "silently rejected. Regenerate the API token at "
                "id.atlassian.com -> Security -> API tokens.")

        data = r.json()
        page_issues = data.get("issues", [])
        for issue in page_issues:
            key    = issue.get("key", "")
            fields = issue.get("fields", {})
            desc   = _extract_adf_text(fields.get("description") or {})
            results.append({"key": key, "summary": fields.get("summary", ""), "description": desc})

        next_token = data.get("nextPageToken")
        if not next_token:
            break

    # The query returning HTTP 200 with zero issues is NOT an error -- it
    # just means nothing matched the JQL. Log that plainly instead of
    # letting it look identical to a query failure (that ambiguity is what
    # made the last miss hard to diagnose) -- then run a broader diagnostic
    # query (assignee only, no status filter) so the log shows GROUND TRUTH
    # from the API (real status field values) instead of guessing from the
    # UI. This isolates whether currentUser() is matching ANYTHING at all
    # vs. the status clause specifically being the filter that's too narrow.
    if not results:
        print("  [JIRA] Query succeeded (HTTP 200) but matched 0 issues for:")
        print("    {}".format(jql))
        print("  [JIRA] Running a broader diagnostic query (assignee only, "
              "no status filter) to see what's actually assigned...")
        diag_jql = "project = NOE AND assignee = currentUser() ORDER BY updated DESC"
        try:
            dr = requests.post(url, headers=headers, timeout=30, json={
                "jql": diag_jql, "maxResults": 10, "fields": ["summary", "status"],
            })
            if dr.status_code == 200:
                diag_issues = dr.json().get("issues", [])
                if not diag_issues:
                    print("  [JIRA] Diagnostic ALSO found 0 issues for "
                          "\"{}\" -- currentUser() is not matching any NOE "
                          "tickets at all. This points at the account behind "
                          "the JIRA email/token (or its permissions on the "
                          "NOE project), not the status filter.".format(diag_jql))
                else:
                    print("  [JIRA] Diagnostic found {} issue(s) assigned to "
                          "currentUser() (any status) -- so assignee matching "
                          "DOES work. Their real status field values:".format(
                              len(diag_issues)))
                    for issue in diag_issues:
                        st = (issue.get("fields", {}).get("status") or {}).get("name", "?")
                        print("    {} -- status = \"{}\"".format(issue.get("key", "?"), st))
                    print("  [JIRA] Compare those to the 'In Progress' string "
                          "in the JQL above -- an exact mismatch there (extra "
                          "space, different workflow name, etc.) is why the "
                          "main query comes back empty.")
            else:
                print("  [JIRA] Diagnostic query itself failed: HTTP {} -- {}".format(
                    dr.status_code, (dr.text or "")[:200]))
        except Exception as e:
            print("  [JIRA] Diagnostic query failed: {}".format(e))
    return results


def _extract_adf_text(adf):
    """Extract plain text from ADF dict -- only needs line 1 for trigger parse."""
    if not adf or not isinstance(adf, dict):
        return ""
    lines = []
    for block in adf.get("content", []):
        block_text = []
        for node in block.get("content", []):
            if node.get("type") == "text":
                block_text.append(node.get("text", ""))
        if block_text:
            lines.append("".join(block_text))
        if lines:
            break  # only need first paragraph
    return "\n".join(lines)


def post_no_trigger_comment(ticket, email, token):
    """Post a minimal 'scanned, nothing found' comment."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    ok, msg = post_report(
        ticket=ticket,
        intro="Morning Check -- {} -- Scanned, no automation triggers found.".format(stamp),
        panel_title="Morning Check",
        report_text=(
            "No '1250: <IP>' trigger found on line 1 of the JIRA description.\n"
            "No work was performed.\n\n"
            "To enable automated checks, add a trigger as the first line of the description:\n"
            "  Full suite:  1250: <IP> | <DS1>\n"
            "  Lite check:  1250: <IP>"
        ),
        email=email, token=token,
    )
    return ok, msg


# ── Trigger parser ────────────────────────────────────────────────────────────
def parse_trigger(description):
    """
    Returns:
        ("full", ip, [ds1,...])   -- full suite
        ("lite", ip, [])          -- lite check
        ("none", None, [])        -- no trigger
    """
    if not description:
        return "none", None, []
    line1 = description.splitlines()[0] if description.strip() else ""
    m = TRIGGER_RE.match(line1)
    if not m:
        return "none", None, []
    ip      = m.group(1).strip()
    ds1_raw = (m.group(2) or "").strip()
    if not ds1_raw:
        return "lite", ip, []
    # dict.fromkeys(...) dedupes while keeping first-seen order -- real
    # description text sometimes mentions the same DS1 twice (once in a
    # full list, once flagged separately, e.g. "...ts1/1, ts1/5... recheck
    # ts1/1, ts1/5"), which would otherwise run the Autotester against the
    # same DS1 more than once per ticket for no benefit.
    ds1_list = list(dict.fromkeys(
        tok.group(0).lower() for tok in DS1_TOKEN_RE.finditer(ds1_raw)))
    if not ds1_list:
        return "lite", ip, []
    return "full", ip, ds1_list


# ── Slack ─────────────────────────────────────────────────────────────────────
# post_slack() moved to jira_client.py (2026-08-06) -- generic, no Morning-
# Check-specific logic, so standalone Autotester can share it too instead
# of a cross-tool import. Imported at the top of this file.


# Slack payload is a single "text" blob (no Block Kit) -- Slack will choke
# on/truncate very large webhook posts. Stay safely under that with a hard
# cap; if a batch runs long (many tickets, each with a full route/diag
# block), truncate and point at JIRA/the batch .txt rather than post
# something Slack silently mangles.
_SLACK_TEXT_LIMIT = 32000


def build_slack_digest(results):
    """
    Slack gets "Findings Summary" (the human-readable paragraph) AND
    "Diagnostic Summary & Full T1 Route" per ticket -- same two pieces as
    the JIRA comment's two expands, so a glance at Slack is enough to
    sanity-check that a finding is in the right ballpark without opening
    JIRA (2026-08-06 direction). No raw threshold/finding bullet lists
    though -- that's still "a wall of numbers, not useful" per the
    original v1.61 direction; the diagnostic block is the one exception,
    since it's structured (labeled OK/!! per hop), not a bare number dump.
    """
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "*Morning Circuit Check — {}*".format(stamp),
        "_{} ticket(s) scanned_".format(len(results)),
        "",
    ]
    for r in results:
        if r.get("clean") and not r.get("warnings"):
            icon = ":white_check_mark:"
        elif r.get("warnings"):
            icon = ":warning:"
        else:
            icon = ":large_blue_circle:"
        lines.append("{} *<{}/browse/{}|{}>* — {}".format(
            icon, JIRA_BASE_URL, r["key"], r["key"], r.get("summary", "")[:60]))
        paragraph = (r.get("paragraph") or "").strip()
        lines.append(paragraph if paragraph else "  `{}`".format(
            r.get("status_line", "no status")))
        route_diag = (r.get("route_diag") or "").strip()
        if route_diag:
            lines.append("*Diagnostic Summary & Full T1 Route:*")
            lines.append("```{}```".format(route_diag))
        lines.append("")

    text = "\n".join(lines)
    if len(text) > _SLACK_TEXT_LIMIT:
        text = (text[:_SLACK_TEXT_LIMIT] +
                "\n\n... _truncated -- see JIRA comments or the batch "
                ".txt in C:\\Claude for the rest._")
    return text


# ── Human-readable paragraph builder ─────────────────────────────────────────
# Deterministic Python templating -- NOT an LLM call. Per user direction
# 2026-08-06 (budget-conscious): the desired format is a fill-in-the-blank
# narrative built from data the tools already produce, not free-form AI text
# (no new API key/dependency/per-run cost).
_FIBER_PORT_HDR_RE = re.compile(r'--\s*(ge-ts\d+/\d+)\s*--', re.IGNORECASE)
_FIBER_RATE_RE = re.compile(
    r'(IN|OUT)\s+(Maximum|Average)\s+Rate\(bps\)\s+\d+\s+\(([^)]+)\)')


def _fiber_sentence(fiber_text):
    """
    Per-port utilization -- IN/OUT max & average, as actual Mbps figures
    pulled straight from the Fiber Utilization tool's own report -- rather
    than a vague "data collected" placeholder (2026-08-07 direction: cite
    real numbers). Still no pass/fail verdict: that tool has no threshold
    at all (circuit speeds vary, so no %-utilization is computed to judge
    against) -- this only ever reports, never flags. Optical/light levels
    are a separate, unrelated data source (NNI Rx/Tx dBm, per-DS1 via
    Autotester -- see output_builders._light_level_blurb).

    Shows BOTH directions (IN and OUT) rather than collapsing to one
    number -- dropping either could hide a real saturation issue on an
    asymmetric circuit.
    """
    if not fiber_text:
        return "Fiber Utilization did not return a result."

    chunks = _FIBER_PORT_HDR_RE.split(fiber_text)   # [pre, port, body, port, body, ...]
    port_lines = []
    for i in range(1, len(chunks), 2):
        port = chunks[i]
        body = chunks[i + 1] if i + 1 < len(chunks) else ""
        rates = {(d.upper(), s.lower()): mbps
                 for d, s, mbps in _FIBER_RATE_RE.findall(body)}
        if not rates:
            port_lines.append("{}: no bps data".format(port))
            continue
        port_lines.append(
            "{}: IN max {} / avg {} -- OUT max {} / avg {}".format(
                port,
                rates.get(("IN", "maximum"), "?"), rates.get(("IN", "average"), "?"),
                rates.get(("OUT", "maximum"), "?"), rates.get(("OUT", "average"), "?")))

    if not port_lines:
        return "Fiber Utilization did not return a result."
    return "Fiber utilization:\n" + "\n".join(port_lines)


def _ospf_sentence(ospf_text):
    """
    One human sentence on OSPF/link stability, scoped to the last 7 days
    (2026-08-06 direction) using the count ospf_events.py already computes
    from each event's own timestamp -- NOT just every "down" event sitting
    in the device's unreported-event buffer, which can span more or less
    than a week depending on the device.
    """
    if not ospf_text:
        return "OSPF Events did not return a result."
    m = re.search(r'OSPF bounces in the last 7 days:\s*(\d+)', ospf_text)
    if m:
        count = int(m.group(1))
    else:
        # Fallback for output from before this line existed.
        count = (ospf_text.upper().count("OSPF-DOWN") +
                 ospf_text.count("NEIGHBOR DOWN"))
    if not count:
        return "OSPF is stable, no neighbor bounce events in the last 7 days."
    return ("OSPF logged {} neighbor bounce event(s) in the last 7 days -- "
            "link stability on this circuit should be reviewed.".format(count))


def build_paragraph(autotester_paragraph, fiber_text, ospf_text):
    """
    Assemble the single human-readable "Findings Summary" paragraph for a
    ticket: the per-DS1 hop-by-hop review (1250 -> 1800 -> MUX -> GSX, already
    produced by output_builders.build_jira_summary -- reused as-is rather than
    regenerated, since it already IS a plain-English root-cause narrative)
    plus one plain sentence each for Fiber and OSPF. Empty pieces are omitted.
    """
    parts = []
    if autotester_paragraph and autotester_paragraph.strip():
        parts.append(autotester_paragraph.strip())
    # "\n" not " " -- _fiber_sentence is now multi-line (one line per port,
    # 2026-08-07), so a plain space-join would run the OSPF sentence onto
    # the end of the last port line instead of its own line.
    tail = "\n".join(s for s in (_fiber_sentence(fiber_text), _ospf_sentence(ospf_text)) if s)
    if tail:
        parts.append(tail)
    return "\n\n".join(parts) if parts else "No findings to report."


# ── Batch summary (web UI report panel + Download) ───────────────────────────
# run() processes N tickets and writes one file per ticket via run_full_suite/
# run_lite_suite, but never returned a path itself -- the web app's report
# panel and Download link only ever show/serve whatever run() returns. This
# gives it one small consolidated file to point to instead of the generic
# "finished without producing a report file" fallback.
def _write_batch_summary(assignee, stamp, entries, note=None):
    """
    entries: list of dicts with key/summary/status_line/detail_lines/
             paragraph (digest_results shape -- see build_slack_digest).
    note:    optional extra line (e.g. "no tickets found", "DRY RUN").
    Returns the written path, or None if the write failed.

    Same reading flow as the JIRA/Slack output (2026-08-06 direction):
    1) FINDINGS SUMMARY -- each ticket's human-readable paragraph (the same
       one its JIRA comment's "Findings Summary" expand and Slack message
       get) -- the 10-second read across the whole batch.
    2) PER-TICKET STATUS -- the quick pass/fail index that was already
       here, kept for a fast visual scan.
    Full per-DS1 route/diagnostic detail and raw tool output live in each
    ticket's own JIRA comment + attached MorningCheck_<ticket>_*.txt --
    intentionally not duplicated a third time in this roll-up file.
    """
    lines = [
        "MORNING CHECK -- BATCH SUMMARY -- {}".format(stamp),
        "=" * 64,
        "Assignee  : {}".format(assignee),
        "Ticket(s) : {}".format(len(entries)),
        "=" * 64, "",
    ]
    if note:
        lines += [note, ""]

    if entries:
        lines += ["=" * 64, "  FINDINGS SUMMARY", "=" * 64, ""]
        for e in entries:
            lines.append("{}  --  {}".format(e.get("key", "?"), e.get("summary", "")[:70]))
            paragraph = (e.get("paragraph") or "").strip()
            lines.append(paragraph if paragraph else
                         "  {}".format(e.get("status_line", "(no findings paragraph)")))
            lines.append("")
        lines += ["=" * 64, "  PER-TICKET STATUS", "=" * 64, ""]

    for e in entries:
        lines.append("{}  --  {}".format(e.get("key", "?"), e.get("summary", "")[:70]))
        lines.append("  {}".format(e.get("status_line", "")))
        for dl in e.get("detail_lines", []):
            lines.append("    - {}".format(dl))
        lines.append("")
    lines += ["=" * 64, "END OF BATCH SUMMARY -- see JIRA comments / per-ticket "
              "MorningCheck_*.txt files in C:\\Claude for full detail.", "=" * 64]

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUTPUT_DIR, "MorningCheck_Batch_{}.txt".format(ts))
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        return out_path
    except Exception as e:
        print("  [!] Batch summary write failed: {}".format(e))
        return None


# ── Tool runners ──────────────────────────────────────────────────────────────
# NOTE: fiber_util and ospf_events use "username"/"password" keys (not
# "infra_user"/"infra_pass") -- this matches their own collect_inputs() contract.

def _read_tool_output(out_path):
    if out_path and os.path.exists(out_path):
        try:
            with open(out_path, "r", errors="ignore") as f:
                return f.read()
        except Exception:
            pass
    return ""


def run_full_suite(ticket, ip_1250, ds1_list, creds):
    """
    Run Autotester + Fiber + OSPF.
    Returns (report_text, out_path, digest_info). digest_info also carries
    "paragraph" (human-readable Findings Summary) and "route_diag" (condensed
    per-DS1 route + diagnostic text) -- see build_paragraph().
    """
    import autotester_standalone as auto
    import fiber_util
    import ospf_events

    username = creds["username"]
    password = creds["password"]
    gsx_user = creds["gsx_user"]
    gsx_pass = creds["gsx_pass"]

    stamp        = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    report_parts = []
    digest_info  = {"key": ticket, "clean": True, "warnings": False, "detail_lines": []}

    report_parts.append("Morning Check -- {} -- {}".format(ticket, stamp))
    report_parts.append("=" * 64)
    report_parts.append("1250 IP : {}".format(ip_1250))
    report_parts.append("DS1(s)  : {}".format(", ".join(ds1_list)))
    report_parts.append("Mode    : Full Suite (Autotester + Fiber Util + OSPF Events)")
    report_parts.append("=" * 64)
    report_parts.append("")

    # ── Autotester ────────────────────────────────────────────────────────
    print("  [{}] Running Autotester ({} DS1s)...".format(ticket, len(ds1_list)))
    report_parts.append("[ AUTOTESTER ]")
    report_parts.append("-" * 40)
    auto_extra = {}
    try:
        auto_params = {
            "ip_1250":       ip_1250,
            "ds1_list":      ds1_list,
            "infra_user":    username,
            "infra_pass":    password,
            "gsx_user":      gsx_user,
            "gsx_pass":      gsx_pass,
            "jira_ticket":   None,
            "open_when_done": False,
            # Opt-in: ask autotester_standalone.run() to also hand back the
            # already-computed per-DS1 human-readable summary + condensed
            # route/diagnostic text (see autotester_standalone.py) instead of
            # re-deriving them here by re-parsing the raw report text. Every
            # other caller of run() (web Autotester route, its own CLI)
            # doesn't set this key, so this is a no-op for them.
            "_extra_out":    auto_extra,
        }
        auto_text = _read_tool_output(auto.run(auto_params))
        if auto_text:
            report_parts.append(auto_text)
            for line in auto_text.splitlines():
                s = line.strip()
                if s.startswith("FAIL") or "[ !!" in s:
                    digest_info["detail_lines"].append(s[:80])
                    digest_info["clean"]    = False
                    digest_info["warnings"] = True
        else:
            report_parts.append("  [!] Autotester returned no output.")
            digest_info["warnings"] = True
    except CredentialError:
        raise
    except Exception as e:
        report_parts.append("  [!] Autotester failed: {}: {}".format(type(e).__name__, e))
        digest_info["warnings"] = True

    report_parts.append("")

    # ── Fiber Utilization ─────────────────────────────────────────────────
    print("  [{}] Running Fiber Utilization...".format(ticket))
    report_parts.append("[ FIBER UTILIZATION ]")
    report_parts.append("-" * 40)
    fiber_text = ""   # guaranteed to exist even if the call below raises
    try:
        fiber_params = {
            "ip_1250":       ip_1250,
            "username":      username,    # fiber_util uses "username"/"password"
            "password":      password,
            "jira_ticket":   None,
            "open_when_done": False,
        }
        fiber_text = _read_tool_output(fiber_util.run(fiber_params))
        if fiber_text:
            report_parts.append(fiber_text)
            for line in fiber_text.splitlines():
                if "[ !!" in line or "WARN" in line.upper():
                    digest_info["detail_lines"].append("Fiber: " + line.strip()[:70])
                    digest_info["warnings"] = True
        else:
            report_parts.append("  [!] Fiber util returned no output.")
    except CredentialError:
        raise
    except Exception as e:
        report_parts.append("  [!] Fiber util failed: {}".format(e))

    report_parts.append("")

    # ── OSPF Events ───────────────────────────────────────────────────────
    print("  [{}] Running OSPF Events...".format(ticket))
    report_parts.append("[ OSPF EVENTS & FIBER LEVELS ]")
    report_parts.append("-" * 40)
    ospf_text = ""   # guaranteed to exist even if the call below raises
    try:
        ospf_params = {
            "ip_1250":       ip_1250,
            "username":      username,    # ospf_events uses "username"/"password"
            "password":      password,
            "jira_ticket":   None,
            "open_when_done": False,
        }
        ospf_text = _read_tool_output(ospf_events.run(ospf_params))
        if ospf_text:
            report_parts.append(ospf_text)
            down_count = (ospf_text.upper().count("OSPF-DOWN") +
                          ospf_text.count("NEIGHBOR DOWN"))
            if down_count:
                digest_info["detail_lines"].append(
                    "OSPF: {} bounce event(s) in log".format(down_count))
                digest_info["warnings"] = True
        else:
            report_parts.append("  [!] OSPF events returned no output.")
    except CredentialError:
        raise
    except Exception as e:
        report_parts.append("  [!] OSPF events failed: {}".format(e))

    report_parts.append("")

    # ── Write combined file ───────────────────────────────────────────────
    full_text = "\n".join(report_parts)
    ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = os.path.join(OUTPUT_DIR,
        "MorningCheck_{}_{}_{}.txt".format(ticket, ip_1250.replace(".", "_"), ts))
    try:
        with open(out_path, "w") as f:
            f.write(full_text)
        print("  [{}] Written: {}".format(ticket, out_path))
    except Exception as e:
        print("  [!] Write failed: {}".format(e))
        out_path = None

    # ── Human-readable "Findings Summary" (1st JIRA expand + all of Slack) ──
    autotester_paragraph = auto_extra.get("jira_summary", "")
    digest_info["paragraph"]   = build_paragraph(autotester_paragraph, fiber_text, ospf_text)
    # Condensed "Diagnostic Summary & Full T1 Route" (2nd JIRA expand) --
    # per-DS1 route + diagnostic blocks only, NOT the shared infra stats /
    # raw PM dumps (those stay in the attached .txt only).
    digest_info["route_diag"]  = auto_extra.get("route_diag", "")

    digest_info["status_line"] = (
        "Full suite — {} finding(s)".format(len(digest_info["detail_lines"]))
        if digest_info["detail_lines"] else "Full suite — all clean"
    )
    return full_text, out_path, digest_info


def run_lite_suite(ticket, ip_1250, creds):
    """
    Run Fiber + OSPF only (no DS1 specified).
    Returns (report_text, out_path, digest_info).
    """
    import fiber_util
    import ospf_events

    username     = creds["username"]
    password     = creds["password"]
    stamp        = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    report_parts = []
    digest_info  = {"key": ticket, "clean": True, "warnings": False, "detail_lines": []}
    texts        = {}   # label -> raw tool output, so the paragraph builder
                        # can address Fiber/OSPF individually afterward.

    report_parts.append("Morning Check -- {} -- {}".format(ticket, stamp))
    report_parts.append("=" * 64)
    report_parts.append("1250 IP : {}".format(ip_1250))
    report_parts.append("Mode    : Lite Check (Fiber Util + OSPF Events -- no DS1 specified)")
    report_parts.append("=" * 64)
    report_parts.append("")

    for label, module, params_extra in [
        ("FIBER UTILIZATION", fiber_util, {}),
        ("OSPF EVENTS & FIBER LEVELS", ospf_events, {}),
    ]:
        print("  [{}] Running {}...".format(ticket, label))
        report_parts.append("[ {} ]".format(label))
        report_parts.append("-" * 40)
        text = ""
        try:
            p = {"ip_1250": ip_1250, "username": username, "password": password,
                 "jira_ticket": None, "open_when_done": False}
            p.update(params_extra)
            text = _read_tool_output(module.run(p))
            if text:
                report_parts.append(text)
                if label == "OSPF EVENTS & FIBER LEVELS":
                    down_count = (text.upper().count("OSPF-DOWN") +
                                  text.count("NEIGHBOR DOWN"))
                    if down_count:
                        digest_info["detail_lines"].append(
                            "OSPF: {} bounce event(s)".format(down_count))
                        digest_info["warnings"] = True
            else:
                report_parts.append("  [!] {} returned no output.".format(label))
        except CredentialError:
            raise
        except Exception as e:
            report_parts.append("  [!] {} failed: {}".format(label, e))
        report_parts.append("")
        texts[label] = text

    full_text = "\n".join(report_parts)
    ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = os.path.join(OUTPUT_DIR,
        "MorningCheck_{}_{}_{}.txt".format(ticket, ip_1250.replace(".", "_"), ts))
    try:
        with open(out_path, "w") as f:
            f.write(full_text)
        print("  [{}] Written: {}".format(ticket, out_path))
    except Exception as e:
        print("  [!] Write failed: {}".format(e))
        out_path = None

    # No DS1 was tested in lite mode, so there's no Autotester paragraph and
    # no route/diagnostic block -- just the Fiber/OSPF sentences.
    digest_info["paragraph"]  = build_paragraph(
        "", texts.get("FIBER UTILIZATION", ""), texts.get("OSPF EVENTS & FIBER LEVELS", ""))
    digest_info["route_diag"] = ""

    digest_info["status_line"] = (
        "Lite check — {} finding(s)".format(len(digest_info["detail_lines"]))
        if digest_info["detail_lines"] else "Lite check — clean"
    )
    return full_text, out_path, digest_info


# ── Launcher contract ─────────────────────────────────────────────────────────
def collect_inputs(session=None):
    session = session or {}
    cfg     = load_config()

    print("\n-- Morning Check Configuration --")

    email    = cfg.get("JIRA_EMAIL", "")
    token    = cfg.get("JIRA_TOKEN", "")
    if not (email and token):
        if not email:
            email = input("JIRA email: ").strip()
        if not token:
            token = getpass.getpass("JIRA API token: ")
        if not (email and token):
            print("JIRA credentials required.")
            return None

    assignee = cfg.get("JIRA_ASSIGNEE_EMAIL", "")
    if not assignee:
        assignee = input("Your JIRA assignee email [{}]: ".format(email)).strip() or email
        _save_key("JIRA_ASSIGNEE_EMAIL", assignee)

    username = cfg.get("INFRA_USER", "") or session.get("infra_user", "")
    password = cfg.get("INFRA_PASS", "") or session.get("infra_pass", "")
    if not (username and password):
        print("\n-- Infrastructure Credentials (1250 / 1800 / MUX) --")
        if not username:
            username = input("Username: ").strip()
        if not password:
            password = getpass.getpass("Password: ")
        if not (username and password):
            print("Infrastructure credentials required.")
            return None

    gsx_user = cfg.get("GSX_USER", "") or session.get("gsx_user", "")
    gsx_pass = cfg.get("GSX_PASS", "") or session.get("gsx_pass", "")
    if not (gsx_user and gsx_pass):
        print("\n-- GSX Credentials --")
        if not gsx_user:
            gsx_user = input("GSX username: ").strip()
        if not gsx_pass:
            gsx_pass = getpass.getpass("GSX password: ")
        if not (gsx_user and gsx_pass):
            print("GSX credentials required.")
            return None

    slack_webhook = cfg.get("SLACK_WEBHOOK", "")
    if not slack_webhook:
        raw = input("\nSlack webhook URL (leave blank to skip): ").strip()
        if raw:
            slack_webhook = raw
            _save_key("SLACK_WEBHOOK", slack_webhook)

    dry_run = input("\nDry run? (parse tickets only, no execution) (y/n) [n]: ").strip().lower()
    dry_run = dry_run in ("y", "yes")

    open_when_done = input(
        "\nOpen the batch summary file when done? (y/n) [n]: ").strip().lower()
    open_when_done = open_when_done in ("y", "yes")

    return {
        "jira_email":     email,
        "jira_token":     token,
        "assignee":       assignee,
        "username":       username,
        "password":       password,
        "gsx_user":       gsx_user,
        "gsx_pass":       gsx_pass,
        "slack_webhook":  slack_webhook,
        "dry_run":        dry_run,
        "open_when_done": open_when_done,
        # Session harvest keys
        "infra_user":    username,
        "infra_pass":    password,
    }


def run(params):
    """Main execution -- no prompting."""
    email         = params["jira_email"]
    token         = params["jira_token"]
    assignee      = params["assignee"]
    username      = params["username"]
    password      = params["password"]
    gsx_user      = params["gsx_user"]
    gsx_pass      = params["gsx_pass"]
    slack_webhook  = params.get("slack_webhook", "")
    dry_run        = params.get("dry_run", False)
    open_when_done = params.get("open_when_done", False)

    creds = {"username": username, "password": password,
             "gsx_user": gsx_user, "gsx_pass": gsx_pass}

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print("\n" + "=" * 64)
    print("  MORNING CHECK -- {}".format(stamp))
    print("=" * 64)

    def _finish(entries, note=None):
        """Write the batch summary, honor open_when_done, return the path --
        the one exit point for run(), used from every early-return below too
        so 'no tickets'/'dry run' behave the same as a full completed run."""
        out_path = _write_batch_summary(assignee, stamp, entries, note=note)
        if open_when_done and out_path:
            try:
                os.startfile(out_path)
            except Exception as e:
                print("[!] could not auto-open batch summary: {}".format(e))
        return out_path

    # ── Fetch tickets ─────────────────────────────────────────────────────
    print("\nFetching open NOE tickets for {}...".format(assignee))
    try:
        tickets = get_my_open_tickets(email, token, assignee)
    except JiraAuthError as e:
        print("[!] {}".format(e))
        return _finish([], note="JIRA AUTHENTICATION FAILED -- {}".format(e))

    if not tickets:
        print("No open tickets found (or JIRA query failed).")
        return _finish([], note=(
            "No open NOE tickets found for this assignee (or the JIRA "
            "query failed -- check the server console for details)."))

    print("Found {} ticket(s).".format(len(tickets)))

    # ── Parse triggers ────────────────────────────────────────────────────
    parsed = []
    for t in tickets:
        mode, ip, ds1_list = parse_trigger(t["description"])
        parsed.append({"key": t["key"], "summary": t["summary"],
                        "mode": mode, "ip": ip, "ds1_list": ds1_list})
        icon = {"full": "[FULL]", "lite": "[LITE]", "none": "[SKIP]"}[mode]
        detail = "| IP: {} DS1: {}".format(ip, ds1_list) if mode != "none" else ""
        print("  {} {} -- {} {}".format(icon, t["key"], t["summary"][:50], detail))

    if dry_run:
        print("\n[Dry run] Stopping before tool execution.")
        dry_entries = [{
            "key": p["key"], "summary": p["summary"],
            "status_line": "[{}] IP: {}  DS1: {}".format(
                p["mode"].upper(), p["ip"] or "--",
                ", ".join(p["ds1_list"]) if p["ds1_list"] else "--"),
            "detail_lines": [],
        } for p in parsed]
        return _finish(dry_entries, note=(
            "DRY RUN -- tickets were parsed only. No tools were run, no "
            "JIRA comments or Slack digest were posted."))

    # ── Execute per ticket ────────────────────────────────────────────────
    digest_results = []

    for t in parsed:
        key  = t["key"]
        mode = t["mode"]
        print("\n" + "=" * 64)
        print("  Processing {} ({})".format(key, mode.upper()))
        print("=" * 64)

        try:
            if mode == "none":
                print("  No trigger -- posting scan notice...")
                ok, msg = post_no_trigger_comment(key, email, token)
                print("  [JIRA] {}".format(msg if ok else "FAILED: " + msg))
                digest_results.append({
                    "key": key, "summary": t["summary"],
                    "clean": True, "warnings": False,
                    "status_line": "Scanned — no triggers found", "detail_lines": [],
                })
                continue

            elif mode == "lite":
                _report_text, out_path, digest_info = run_lite_suite(
                    key, t["ip"], creds)
            else:
                _report_text, out_path, digest_info = run_full_suite(
                    key, t["ip"], t["ds1_list"], creds)

            digest_info["summary"] = t["summary"]

            # Post to JIRA -- two expands (2026-08-06 redesign, replacing the
            # old single "Full Report" panel that just dumped raw numbers):
            #   1st expand "Findings Summary"   -- human-readable paragraph
            #                                       (also all of what Slack gets)
            #   2nd expand "Diagnostic Summary & Full T1 Route" -- condensed
            #                                       per-DS1 route/diagnostic
            #                                       (not the shared infra
            #                                       stats / raw PM dumps)
            # The full raw combined report is attached as a .txt only
            # (attach_path=out_path) -- never posted inline anymore.
            print("  [{}] Posting to JIRA...".format(key))
            ok, msg = post_report(
                ticket=key,
                intro="Morning Check -- {} -- {} | {}".format(
                    stamp, t["ip"],
                    ", ".join(t["ds1_list"]) if t["ds1_list"] else "lite check"),
                precheck_text=digest_info.get("paragraph", ""),
                precheck_title="Findings Summary",
                panel_title="Diagnostic Summary & Full T1 Route",
                report_text=digest_info.get("route_diag", ""),
                email=email, token=token,
                attach_path=out_path,
            )
            print("  [JIRA] {}".format(msg if ok else "FAILED: " + msg))
            if not ok:
                digest_info["detail_lines"].insert(0, "JIRA post failed: {}".format(msg[:60]))

            digest_results.append(digest_info)

        except CredentialError as e:
            print("\n  [!] Credential error on {}: {}".format(key, e))
            raise
        except Exception as e:
            print("  [!] Error on {}: {}: {}".format(key, type(e).__name__, e))
            digest_results.append({
                "key": key, "summary": t["summary"],
                "clean": False, "warnings": True,
                "status_line": "ERROR: {}".format(str(e)[:60]), "detail_lines": [],
            })

    # ── Slack digest ──────────────────────────────────────────────────────
    if slack_webhook and digest_results:
        print("\nPosting Slack digest...")
        ok, msg = post_slack(slack_webhook, build_slack_digest(digest_results))
        print("[Slack] {}".format(msg if ok else "FAILED: " + msg))
    elif not slack_webhook:
        print("\n(Slack webhook not configured -- skipping digest)")

    print("\n" + "=" * 64)
    print("  MORNING CHECK COMPLETE -- {} ticket(s) processed".format(len(parsed)))
    print("=" * 64)

    return _finish(digest_results)


def main():
    print("=" * 64)
    print("  MORNING CHECK -- NOE Ticket Automation")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
