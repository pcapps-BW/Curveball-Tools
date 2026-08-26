#!/usr/bin/env python3
"""
jira_client.py -- post Autotester results to a JIRA ticket as a comment.

Port of the SecureCRT JIRA integration, using requests (not PowerShell).
Posts an ADF (Atlassian Document Format) comment via API v3 with two collapsed
expand panels:
    "Test Results -- Run Summary & Diagnostic"  (run summary + diagnostic summary)
    "Per-DS1 Performance Monitors"              (per-DS1 PMs)

Creds (JIRA_EMAIL, JIRA_TOKEN) read from autotester_config.txt; caller may pass
them in instead. Ticket format: NOE-XXXX.

Fallback: if JIRA returns CONTENT_LIMIT_EXCEEDED, the report is uploaded as a
file attachment instead (post_report / post_comment accept attach_path= for this).

Built by P. Capps.
"""

import re
import sys
import base64
import datetime
import threading

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run:  pip install requests")
    sys.exit(1)

CONFIG_PATH = r"C:\Claude\autotester_config.txt"
JIRA_BASE   = "https://bandwidth-jira.atlassian.net"
TICKET_RE   = re.compile(r'^NOE-\d{4}$')

# Slack payload is a single "text" blob (no Block Kit) -- a very large
# webhook post risks Slack mangling or rejecting it silently. Shared here
# so every caller (Morning Check, standalone Autotester) applies the same
# cap instead of each tool picking its own number.
SLACK_TEXT_LIMIT = 32000


def post_slack(webhook_url, message):
    """
    Post a plain-text message to a Slack Incoming Webhook. Moved here from
    morning_check.py (2026-08-06) -- generic, no Morning-Check-specific
    logic -- so standalone Autotester can post the same way without a
    cross-tool import. Truncates to SLACK_TEXT_LIMIT with a note if the
    caller hands over something oversized (callers should already be
    trimming their own digest text, but this is a last-resort backstop).
    """
    if not webhook_url:
        return False, "No Slack webhook configured."
    if len(message) > SLACK_TEXT_LIMIT:
        message = message[:SLACK_TEXT_LIMIT] + "\n\n... _truncated._"
    try:
        r = requests.post(webhook_url, json={"text": message}, timeout=15)
        if r.status_code == 200:
            return True, "Slack notified."
        return False, "Slack returned HTTP {}: {}".format(r.status_code, r.text[:100])
    except Exception as e:
        return False, "Slack post failed: {}".format(e)

# Hard ceiling for the entire JIRA post. requests' timeout= only covers
# waiting for RESPONSE bytes -- it does NOT cover the upload/send phase, so
# a proxy/VPN that accepts the connection but stalls consuming a large
# upload hangs requests.post() forever. The watchdog bounds that.
JIRA_WATCHDOG_SECS = 240

# Jira Cloud rejects comment bodies past ~32k chars of text with
# CONTENT_LIMIT_EXCEEDED. Reports past this are going to be rejected
# anyway, so skip uploading the doomed multi-hundred-KB JSON (the exact
# payload most likely to trigger the send-phase stall above) and go
# straight to stub-comment + file attachment.
COMMENT_TEXT_LIMIT = 30000


def _bounded(fn, timeout=JIRA_WATCHDOG_SECS):
    """Run fn() (returns (ok, msg)) in a watchdog thread. If it doesn't
    finish in `timeout` seconds, report failure instead of hanging the
    caller forever. The stuck thread is daemonized and abandoned."""
    result = {}

    def _run():
        try:
            result["val"] = fn()
        except Exception as e:
            result["val"] = (False, "JIRA post crashed: {}: {}".format(
                type(e).__name__, e))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, ("TIMED OUT after {}s -- network stall talking to "
                       "JIRA (send-phase hang; large upload through a "
                       "proxy is the usual cause). The report file is "
                       "saved locally -- attach it manually.".format(timeout))
    return result.get("val", (False, "JIRA post returned nothing."))


def load_jira_config(path=CONFIG_PATH):
    email = token = ""
    try:
        with open(path, "r") as cfg:
            for line in cfg:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key == "JIRA_EMAIL":
                    email = val.strip()
                elif key == "JIRA_TOKEN":
                    token = val.strip()
    except FileNotFoundError:
        pass
    if not token:
        # BUG (found 2026-08-06, live): secure_config.py -- added for Morning
        # Check -- migrates JIRA_TOKEN out of this plaintext file into
        # Windows Credential Manager the first time it loads config, and
        # DELETES the plaintext line once migrated. This function never knew
        # to check Credential Manager, so every other tool that posts to
        # JIRA through it (Autotester, Fiber, OSPF, Discovery, Health, GSX
        # Monitor, DS1 Test Builder) started failing with "Missing JIRA
        # credentials" the moment that migration ran, even though the token
        # still exists -- it just moved. Fall back to Credential Manager
        # here so the token keeps working no matter which store has it.
        try:
            from secure_config import get_password
            token = get_password("JIRA_TOKEN") or ""
        except Exception:
            pass
    return email, token


def valid_ticket(ticket):
    return bool(TICKET_RE.match((ticket or "").strip().upper()))


def _build_adf(host_1250, ds1_count, results_text, pm_text, summary_text=None):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    content = [
        {
            "type": "paragraph",
            "content": [{
                "type": "text",
                "text": "Autotester results for {} -- {} DS1(s) tested on {}.".format(
                    host_1250, ds1_count, stamp)
            }]
        },
    ]
    # Per-DS1 brief summary. Was previously always-visible (unwrapped
    # codeBlock) by original design ("the glance-read Phil wants on the
    # ticket") -- changed 2026-08-06 per updated direction: wrap it in a
    # real collapsed expand instead, same treatment Morning Check's
    # "Findings Summary" panel gets, so the ticket body stays short by
    # default.
    if summary_text:
        content.append({
            "type": "expand",
            "attrs": {"title": "Findings Summary"},
            "content": [{
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [{"type": "text", "text": summary_text}]
            }]
        })
    # ADF rejects empty text nodes -- a panel with "" would 400 the whole
    # comment (bit the DS1 Test Builder, which posts no PM text). Only
    # include panels that have content.
    for title, text in (("Test Results -- Run Summary & Diagnostic", results_text),
                        ("Per-DS1 Performance Monitors", pm_text)):
        if text and text.strip():
            content.append({
                "type": "expand",
                "attrs": {"title": title},
                "content": [{
                    "type": "codeBlock",
                    "attrs": {"language": "text"},
                    "content": [{"type": "text", "text": text}]
                }]
            })
    return {"version": 1, "type": "doc", "content": content}


def attach_file(ticket, file_path, email=None, token=None):
    """
    Upload file_path as an attachment to ticket.
    Returns (ok, message).
    """
    ticket = (ticket or "").strip().upper()
    if not valid_ticket(ticket):
        return False, "Invalid ticket format '{}'".format(ticket)

    if not (email and token):
        cfg_email, cfg_token = load_jira_config()
        email = email or cfg_email
        token = token or cfg_token
    if not (email and token):
        return False, "Missing JIRA credentials."

    cred = base64.b64encode("{}:{}".format(email, token).encode("ascii")).decode("ascii")
    url  = "{}/rest/api/3/issue/{}/attachments".format(JIRA_BASE, ticket)
    headers = {
        "Authorization":      "Basic {}".format(cred),
        "X-Atlassian-Token":  "no-check",
        "Accept":             "application/json",
    }
    try:
        import os as _os
        fname = _os.path.basename(file_path)
        with open(file_path, "rb") as fh:
            r = requests.post(url, headers=headers,
                              files={"file": (fname, fh, "text/plain")},
                              timeout=60)
    except Exception as e:
        return False, "Attach request failed: {}: {}".format(type(e).__name__, e)

    if r.status_code in (200, 201):
        return True, "File attached to {}".format(ticket)
    return False, "Attach returned HTTP {} -- {}".format(r.status_code, (r.text or "")[:300])


def _fallback_attach(ticket, short_note, file_path, cred,
                     inline_title=None, inline_text=None, summary_text=None):
    """
    Called when the full report is too large for an inline comment.
    Posts a stub comment then uploads the file as an attachment.
    summary_text (the per-DS1 brief summary) goes in the stub inside a
    collapsed "Findings Summary" expand (changed 2026-08-06 -- was
    previously always-visible text; see _build_adf for the same change on
    the normal-size path). If inline_title/inline_text are also provided (a
    fuller block small enough to fit), that rides along in its own
    collapsed panel underneath.
    Returns (ok, message).
    """
    import os as _os
    fname = _os.path.basename(file_path) if file_path else "report.txt"

    stub_content = [{
        "type": "paragraph",
        "content": [{"type": "text",
                     "text": "{} -- Full report attached: {} (too large for an inline comment).".format(
                         short_note, fname)}]
    }]
    if summary_text:
        stub_content.append({
            "type": "expand",
            "attrs": {"title": "Findings Summary"},
            "content": [{
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [{"type": "text", "text": summary_text}]
            }]
        })
    if inline_text and inline_title:
        stub_content.append({
            "type": "expand",
            "attrs": {"title": inline_title},
            "content": [{
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [{"type": "text", "text": inline_text}]
            }]
        })
    stub_adf = {"version": 1, "type": "doc", "content": stub_content}
    url = "{}/rest/api/3/issue/{}/comment".format(JIRA_BASE, ticket)
    headers = {
        "Authorization": "Basic {}".format(cred),
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    try:
        print("[JIRA] posting {} comment ...".format(
            "summary" if inline_text else "stub"))
        requests.post(url, json={"body": stub_adf}, headers=headers, timeout=30)
    except Exception:
        pass  # stub comment failure is non-fatal; attachment is what matters

    if not file_path:
        return False, "Report too large for a comment and no attach_path provided -- cannot attach."

    try:
        import os as _os2
        size_kb = _os2.path.getsize(file_path) // 1024
        print("[JIRA] uploading attachment {} ({} KB) ...".format(fname, size_kb))
    except OSError:
        print("[JIRA] uploading attachment {} ...".format(fname))

    # BUG (found 2026-08-06, first surfaced on a Morning Check run whose
    # combined report was too large for an inline comment): this call used
    # to omit email/token entirely, so attach_file() fell through to its
    # own load_jira_config() -- which reads the plaintext config file only
    # and knows nothing about secure_config.py's keyring-stored JIRA_TOKEN
    # (a separate credential store morning_check.py added). Every caller
    # that reached this fallback with keyring-only creds failed with
    # "Missing JIRA credentials", even though a valid cred was RIGHT THERE
    # -- it's the same `cred` (base64 "email:token") already used two lines
    # up to post the stub comment successfully. Decode it and reuse it
    # instead of re-deriving credentials from a config file that may not
    # have them.
    try:
        _email, _token = base64.b64decode(cred).decode("utf-8").split(":", 1)
    except Exception:
        _email, _token = None, None
    ok, msg = attach_file(ticket, file_path, email=_email, token=_token)
    if ok:
        return True, "Comment too large -- attached {} to {} instead".format(fname, ticket)
    return False, "Report too large for a comment and attach also failed: {}".format(msg)


def post_comment(ticket, host_1250, ds1_count, results_text, pm_text,
                 email=None, token=None, attach_path=None, summary_text=None):
    """
    Post the results as an ADF comment. Returns (ok, message).
    Bounded by a watchdog -- never hangs the caller past JIRA_WATCHDOG_SECS.
    summary_text: optional small summary (e.g. just the Run Summary block).
    When the full report is too large for an inline comment, the largest
    piece that fits (results_text, else summary_text) is posted inline in
    the stub comment alongside the file attachment.
    """
    return _bounded(lambda: _post_comment_impl(
        ticket, host_1250, ds1_count, results_text, pm_text,
        email, token, attach_path, summary_text))


def _post_comment_impl(ticket, host_1250, ds1_count, results_text, pm_text,
                       email=None, token=None, attach_path=None,
                       summary_text=None):
    ticket = (ticket or "").strip().upper()
    if not valid_ticket(ticket):
        return False, "Invalid ticket format '{}' (expected NOE-XXXX)".format(ticket)

    if not (email and token):
        cfg_email, cfg_token = load_jira_config()
        email = email or cfg_email
        token = token or cfg_token
    if not (email and token):
        return False, "Missing JIRA credentials (JIRA_EMAIL / JIRA_TOKEN)."

    cred = base64.b64encode("{}:{}".format(email, token).encode("ascii")).decode("ascii")

    # Size pre-check: reports past the comment limit are guaranteed to be
    # rejected -- don't upload the doomed JSON, go straight to attachment.
    # The stub still carries the largest summary that fits inline: the
    # Run Summary + Diagnostic Summary if possible (it's the per-DS1 PM
    # dumps that blow the limit, not the summaries), else just the Run
    # Summary one-liners.
    total_chars = len(results_text or "") + len(pm_text or "")
    if total_chars > COMMENT_TEXT_LIMIT:
        print("[JIRA] report text ~{} KB -- over the inline comment limit, "
              "posting summary comment + file attachment instead".format(
                  total_chars // 1024))
        note = "Autotester results -- {} ({} DS1s)".format(host_1250, ds1_count)
        inline_title = inline_text = None
        if results_text and len(results_text) <= COMMENT_TEXT_LIMIT:
            inline_title = "Test Results -- Run Summary & Diagnostic (PMs in attachment)"
            inline_text  = results_text
        return _fallback_attach(ticket, note, attach_path, cred,
                                inline_title, inline_text, summary_text)

    adf = _build_adf(host_1250, ds1_count, results_text, pm_text, summary_text)
    url = "{}/rest/api/3/issue/{}/comment".format(JIRA_BASE, ticket)
    headers = {
        "Authorization": "Basic {}".format(cred),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.post(url, json={"body": adf}, headers=headers, timeout=30)
    except Exception as e:
        return False, "JIRA request failed: {}: {}".format(type(e).__name__, e)

    if r.status_code in (200, 201):
        return True, "Comment posted to {}".format(ticket)
    if r.status_code == 400 and "CONTENT_LIMIT_EXCEEDED" in r.text:
        print("[JIRA] comment rejected for size -- falling back to attachment")
        note = "Autotester results -- {} ({} DS1s)".format(host_1250, ds1_count)
        return _fallback_attach(ticket, note, attach_path, cred,
                                None, None, summary_text)
    return False, "JIRA returned HTTP {} -- {}".format(
        r.status_code, (r.text or "")[:300])


def _build_report_adf(intro, panel_title, report_text, summary_text=None,
                       precheck_text=None, precheck_title=None):
    """Intro paragraph + optional ALWAYS-VISIBLE findings summary + one
    collapsed panel holding the full report.

    precheck_text/precheck_title (optional): when a caller has already
    split live precheck output out of report_text (e.g. the DS1 Test
    Builder, mirroring its web UI), this rides in its OWN collapsed panel
    ahead of the main report panel, instead of being folded into it."""
    content = [{
        "type": "paragraph",
        "content": [{"type": "text", "text": intro}]
    }]
    if summary_text:
        content.append({
            "type": "codeBlock",
            "attrs": {"language": "text"},
            "content": [{"type": "text", "text": summary_text}]
        })
    if precheck_text and precheck_text.strip():
        content.append({
            "type": "expand",
            "attrs": {"title": precheck_title or "Prechecks"},
            "content": [{
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [{"type": "text", "text": precheck_text}]
            }]
        })
    if report_text and report_text.strip():   # ADF rejects empty text nodes
        content.append({
            "type": "expand",
            "attrs": {"title": panel_title},
            "content": [{
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [{"type": "text", "text": report_text}]
            }]
        })
    return {"version": 1, "type": "doc", "content": content}


def post_report(ticket, intro, panel_title, report_text, email=None, token=None,
                attach_path=None, summary_text=None, precheck_text=None,
                precheck_title=None):
    """
    General-purpose single-panel post for the auxiliary tools (discovery,
    DS1 PMs, interface check). Returns (ok, message).
      intro        -- one-line summary shown in the comment body
      panel_title  -- title of the collapsed panel
      report_text  -- the full report (goes in the collapsed code block)
      summary_text -- optional findings summary, ALWAYS VISIBLE on the
                      ticket (normal post AND too-large attachment fallback)
      precheck_text/precheck_title -- optional; puts live precheck output
                      in its own collapsed panel ahead of the main one
                      (e.g. the DS1 Test Builder, mirroring its web UI)
                      instead of folding it into report_text.
    Bounded by a watchdog -- never hangs the caller past JIRA_WATCHDOG_SECS.
    """
    return _bounded(lambda: _post_report_impl(
        ticket, intro, panel_title, report_text, email, token, attach_path,
        summary_text, precheck_text, precheck_title))


def _post_report_impl(ticket, intro, panel_title, report_text,
                      email=None, token=None, attach_path=None,
                      summary_text=None, precheck_text=None,
                      precheck_title=None):
    ticket = (ticket or "").strip().upper()
    if not valid_ticket(ticket):
        return False, "Invalid ticket format '{}' (expected NOE-XXXX)".format(ticket)

    if not (email and token):
        cfg_email, cfg_token = load_jira_config()
        email = email or cfg_email
        token = token or cfg_token
    if not (email and token):
        return False, "Missing JIRA credentials (JIRA_EMAIL / JIRA_TOKEN)."

    cred = base64.b64encode("{}:{}".format(email, token).encode("ascii")).decode("ascii")

    # Size pre-check -- same rationale as _post_comment_impl. Count both
    # panels' text since precheck_text (when given) is additional content
    # riding along in the same comment, not a replacement for report_text.
    total_len = len(report_text or "") + len(precheck_text or "")
    if total_len > COMMENT_TEXT_LIMIT:
        print("[JIRA] report text ~{} KB -- over the inline comment limit, "
              "posting stub comment + file attachment instead".format(
                  total_len // 1024))
        return _fallback_attach(ticket, intro, attach_path, cred,
                                None, None, summary_text)

    adf = _build_report_adf(intro, panel_title, report_text, summary_text,
                             precheck_text, precheck_title)
    url = "{}/rest/api/3/issue/{}/comment".format(JIRA_BASE, ticket)
    headers = {
        "Authorization": "Basic {}".format(cred),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        r = requests.post(url, json={"body": adf}, headers=headers, timeout=30)
    except Exception as e:
        return False, "JIRA request failed: {}: {}".format(type(e).__name__, e)

    if r.status_code in (200, 201):
        return True, "Comment posted to {}".format(ticket)
    if r.status_code == 400 and "CONTENT_LIMIT_EXCEEDED" in r.text:
        print("[JIRA] comment rejected for size -- falling back to attachment")
        return _fallback_attach(ticket, intro, attach_path, cred,
                                None, None, summary_text)
    return False, "JIRA returned HTTP {} -- {}".format(
        r.status_code, (r.text or "")[:300])
