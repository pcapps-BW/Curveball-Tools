#!/usr/bin/env python3
"""
app.py -- Curveball Tools web front end (proof of concept).

Wraps all 9 launcher tools in a small Flask app so they can be run from a
browser instead of a terminal. Thin wrapper -- no tool/client module logic
changed except one thing: every tool's run(params) now returns its output
file path (previously returned nothing), so this layer can retrieve it.

Architecture (same for every tool, see WEB_SETUP.md for the full writeup):
  1. collect_inputs() is never called -- it uses input()/getpass(), which
     would hang with no terminal attached. Each tool has its own HTML form
     (templates/<tool>.html) matching the same fields; the Flask route
     builds the same params dict collect_inputs() would have built, then
     calls <module>.run(params) directly.
  2. Each run happens in a background thread (see _launch_job/_run_job,
     tool-agnostic). The browser polls /api/job/<id> for status, an
     elapsed-time counter, a "may be hung" warning past a threshold, and a
     live step-by-step log. That log is just each tool's existing
     print(...) progress lines (the same ones you'd see running it from a
     terminal) -- captured via a thread-aware stdout tee (_JobStdoutTee)
     instead of any new logging calls in the tool modules themselves.
  3. Credentials are POSTed fresh every run and held only in the params
     dict passed into the background thread -- never written to disk,
     never put in a session/cookie.

Built by P. Capps.
"""

import os
import re
import sys
import time
import uuid
import threading

from flask import Flask, render_template, request, jsonify, send_file, abort

import autotester_standalone
import reverse_autotester
import sor_collect
import sor_store
import npt1250_discovery
import ds1_pms
import fiber_util
import ospf_events
import health_check
import gsx_perfmon
import interface_check
import mux_loop_script
import ds1_gsx_test
import morning_check
import site_sweep
import ospf_stability
import ixc_stability
import card_alarms
import bcr_1800_check
import secure_config

from device_base import CredentialError
from jira_client import valid_ticket
from ds1_util import expand_ds1_list

app = Flask(__name__)

# Shown next to the "Built by P. Capps" watermark on every page.
# Bump this with each release (keep in sync with CHANGELOG.md).
APP_VERSION = "v2.68"


@app.context_processor
def _inject_version():
    return {"app_version": APP_VERSION}

# ---- in-memory job store (shared by every tool) --------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()

# ---- live progress log capture --------------------------------------------
# Every tool already print()s its progress as it runs ("[GSX] resolving...",
# "[MUX] connecting...", etc). To surface that in the web UI without adding
# any logging calls to the tool modules, sys.stdout is swapped once at
# startup for a tee that (a) still writes to the real console and (b) routes
# each write to the job log of whichever background thread is currently
# writing, via a thread-id -> job_id map maintained by _run_job.
_THREAD_JOB = {}
_THREAD_JOB_LOCK = threading.Lock()


class _JobStdoutTee:
    def __init__(self, real):
        self._real = real

    def write(self, s):
        self._real.write(s)
        if not s or not s.strip():
            return
        with _THREAD_JOB_LOCK:
            job_id = _THREAD_JOB.get(threading.get_ident())
        if not job_id:
            return
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job["log"].append(s.rstrip("\n"))

    def flush(self):
        self._real.flush()

    def isatty(self):
        return False


sys.stdout = _JobStdoutTee(sys.stdout)


# ---- startup environment check --------------------------------------------
# Checked once at import, loudly, because the failure it catches is both
# total and misleading: paramiko 5.0.0 removed the SHA-1 kex and host-key
# types the Cisco NCS 4216 requires, so EVERY MUX at EVERY site becomes
# unreachable -- and paramiko reports it as "unknown cipher", which points
# at the wrong thing.
#
# This does not raise. The 1250 and 1800 hops still work fine on paramiko 5,
# so a degraded suite is more useful than one that refuses to start; the
# banner makes sure nobody spends a run working out why the MUX hop died.
# Found live 2026-10-01 after "Start Curveball Tools.bat" pip-installed an
# unpinned paramiko>=3.5 and picked up 5.0.0.

# The check itself now lives in env_check.py and fires from device_base's
# import, so it covers the CLI launcher and standalone tool runs too -- not
# just this front end. Called again here only so the banner lands at the top
# of the server's own startup output, where someone launching the web app
# will actually see it. check_paramiko() prints once per process.
import env_check

env_check.check_all()


def _valid_ip(s):
    parts = (s or "").strip().split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _opt_ip(raw):
    """Blank-or-valid-IP for optional fields. Returns (value, error)."""
    s = (raw or "").strip()
    if not s:
        return "", None
    if _valid_ip(s):
        return s, None
    return None, "'{}' is not a valid IP address.".format(s)


def _parse_jira(f):
    """Returns (jira_ticket_or_None, error_or_None)."""
    if f.get("add_jira") != "on":
        return None, None
    raw = (f.get("jira_ticket") or "").strip().upper()
    if not raw:
        # Box checked but no ticket -- error instead of silently skipping
        # the post (a silent skip looks like "JIRA didn't work").
        return None, "'Post to JIRA' is checked -- enter a ticket (NOE-XXXX)."
    if not valid_ticket(raw):
        return None, "JIRA ticket must look like NOE-1234."
    return raw, None


def _creds(f, user_key="infra_user", pass_key="infra_pass"):
    u = (f.get(user_key) or "").strip()
    p = f.get(pass_key) or ""
    return u, p


def _resolve_gsx(raw):
    """
    Accept a GSX IP *or* a hostname, return (ip, note, error).

    An IP passes straight through -- it is the escape hatch for when NetBox
    is wrong, which on this project's recent form is not paranoia (four
    consecutive NetBox lookup bugs on 2026-10-01). A hostname is resolved
    through NetBox: device_id_for already turns a bare 'LAXGSX01' into the
    right device (v2.33's hostname match), and gsx_mgmt_ips returns its
    management IPs. A pingable candidate is preferred; the first is the
    fallback.

    `note` is a one-line human record of what the hostname became, surfaced
    in the job log so the user sees which IP was chosen. `error` is a
    user-facing string or None.

    This runs IN THE REQUEST, so it does NetBox (fast) and ICMP ping
    (1-2s for a two-card GSX) only -- no SSH, which belongs in the job
    thread. ping != SSH (ATLGSX02, 2026-10-01), so the chosen IP is
    "reachable", not "logged into"; the tool reports the real connect
    outcome.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None, "Enter a GSX IP or hostname."
    if _valid_ip(raw):
        return raw, None, None

    # Looks like a hostname. It must not look like a half-typed IP, or we
    # would quietly treat '192.168.40' as a device name and search NetBox
    # for it.
    if re.match(r'^[\d.]+$', raw):
        return None, None, ("'{}' is not a complete IP address (need 4 "
                            "octets) and is not a hostname.".format(raw))

    from netbox_client import NetboxClient, load_netbox_config
    from gsx_client import find_live_ip
    nb_url, nb_token = load_netbox_config()
    nb = NetboxClient(nb_url, nb_token)
    if not nb.ok():
        return None, None, ("'{}' is a hostname, but NetBox is not configured "
                            "to resolve it to an IP. Enter the IP instead."
                            .format(raw))

    warns = []
    cards = nb.gsx_mgmt_ips(raw, warns)
    flat = [ip for key in sorted(cards) for ip in cards[key]]
    if not flat:
        return None, None, ("Could not resolve '{}' to a GSX IP via NetBox: {}"
                            .format(raw, warns[0] if warns
                                    else "no management IPs on that device"))

    _card, live = find_live_ip(cards)
    chosen = live or flat[0]
    if live:
        note = "resolved GSX '{}' -> {} via NetBox (ping-confirmed)".format(
            raw, chosen)
    else:
        note = ("resolved GSX '{}' -> {} via NetBox -- NONE of its {} "
                "address(es) answered ping; trying this one anyway".format(
                    raw, chosen, len(flat)))
    return chosen, note, None


def _warn_after(count=1):
    """Seconds before the UI flags a running job as possibly hung. Scales
    gently for tools that loop per-DS1 (Autotester, DS1 PMs)."""
    return max(300, count * 90)


# ---- overall rollup (currently only Autotester's Run Summary) -----------
# Every per-DS1 Run Summary one-liner autotester_standalone.py writes looks
# like "  TS1/1  --  The test completed successfully  |  1250: TDM Clean /
# CES Dirty (...)  |  ..." or "  TS1/1  --  The test completed with errors
# |  Route incomplete: ...". (Plain "PASS"/"FAIL" tokens replaced 2026-08-17
# per direction -- not intuitive to readers outside the team.) That verdict
# only reflects whether the route resolved -- it says "completed
# successfully" even when a hop is Dirty. This scan adds the rollup verdict
# the report itself doesn't have: clean only if every hop on every DS1 came
# back Clean.
# Tools that don't emit a Run Summary (everything except Autotester, for
# now) have no such line, so this returns None and the UI shows no badge.
_RUN_SUMMARY_LINE_RE = re.compile(
    r'^\s*(\S+)\s+--\s+(The test completed successfully|The test completed with errors)\b(.*)$',
    re.MULTILINE)


def _scan_run_summary(report_text):
    if not report_text or "RUN SUMMARY" not in report_text:
        return None
    matches = _RUN_SUMMARY_LINE_RE.findall(report_text)
    if not matches:
        return None
    flagged = [ds1 for ds1, verdict, rest in matches
               if verdict == "The test completed with errors" or "Dirty" in rest]
    return {"checked": len(matches), "flagged": flagged,
            "verdict": "issues" if flagged else "clean"}


# ---- generic job machinery (identical for every tool) --------------------

# Output types that aren't text -- reading these with open(..., "r") produces
# unreadable garbage in the report panel (found by pcapps 2026-09-14, on
# ospf_stability.py's .xlsx report: the panel showed the raw zip/XML bytes of
# the workbook instead of a report). Extend this set if another tool ever
# returns a different binary format as its out_path.
_BINARY_REPORT_EXTS = {".xlsx", ".xls", ".docx", ".pdf", ".zip"}


def _run_job(job_id, run_callable):
    tid = threading.get_ident()
    with _THREAD_JOB_LOCK:
        _THREAD_JOB[tid] = job_id
    try:
        out_path = run_callable()
        report_text = ""
        ext = os.path.splitext(out_path)[1].lower() if out_path else ""
        if out_path and os.path.exists(out_path) and ext in _BINARY_REPORT_EXTS:
            # Don't decode the binary file as text. The readable report
            # already streamed to the live log via progress()/print() during
            # the run (see _JobStdoutTee below) -- reuse that instead.
            with JOBS_LOCK:
                report_text = "\n".join(JOBS[job_id].get("log", []))
        elif out_path and os.path.exists(out_path):
            with open(out_path, "r", errors="ignore") as f:
                report_text = f.read()
        elif not out_path:
            report_text = ("(Finished without producing a report file -- "
                            "check your inputs, or the server console for details.)")
        verdict = _scan_run_summary(report_text)
        with JOBS_LOCK:
            JOBS[job_id].update(status="done", finished=time.time(),
                                 report=report_text, path=out_path, verdict=verdict,
                                 download_ext=ext)
    except CredentialError as e:
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", finished=time.time(),
                error="Credential error ({}): {}".format(e.cred_type, e))
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", finished=time.time(),
                error="{}: {}".format(type(e).__name__, e))
    finally:
        with _THREAD_JOB_LOCK:
            _THREAD_JOB.pop(tid, None)


def _launch_job(label, warn_seconds, run_callable, initial_log=None):
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "started": time.time(), "finished": None,
            "report": None, "path": None, "error": None,
            # initial_log lets a route seed a line before the job thread
            # starts -- used to show "resolved LAXGSX01 -> 192.168.40.5"
            # so the user sees which IP a hostname became.
            "log": list(initial_log or []),
            "warn_after": warn_seconds, "label": label,
        }
    t = threading.Thread(target=_run_job, args=(job_id, run_callable), daemon=True)
    t.start()
    return job_id


@app.route("/api/job/<job_id>")
def api_job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Unknown job id."), 404
        job = dict(job)

    now = job["finished"] or time.time()
    elapsed = now - job["started"]
    warning = job["status"] == "running" and elapsed > job["warn_after"]

    resp = {"status": job["status"], "elapsed": int(elapsed),
            "warning": warning, "label": job["label"], "log": job.get("log", [])}
    # Mid-run extras published by the tool (e.g. resolved GSX targets for
    # the DS1 Test Builder's monitor auto-fill).
    sink = job.get("sink")
    if sink:
        resp["extras"] = sink
    # Stoppable jobs (e.g. the GSX Monitor) show a Stop button while running.
    resp["stoppable"] = bool(job.get("stop_event")) and job["status"] == "running"
    if job["status"] == "done":
        resp["report"] = job["report"]
        resp["download_url"] = "/download/{}".format(job_id)
        resp["download_ext"] = job.get("download_ext") or ".txt"
        resp["verdict"] = job.get("verdict")
    elif job["status"] == "error":
        resp["error"] = job["error"]
    return jsonify(**resp)


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["path"] or not os.path.exists(job["path"]):
        abort(404)
    return send_file(job["path"], as_attachment=True)


# ---- home page -------------------------------------------------------------

TOOL_URLS = {
    "autotester_standalone": "/tools/autotester",
    "npt1250_discovery":     "/tools/discovery",
    "ds1_pms":               "/tools/ds1-pms",
    "fiber_util":            "/tools/fiber",
    "ospf_events":           "/tools/ospf",
    "health_check":          "/tools/health",
    "gsx_perfmon":           "/tools/gsx-perfmon",
    "interface_check":       "/tools/ixc-check",
    "mux_loop_script":       "/tools/mux-loop",
    "ds1_gsx_test":          "/tools/ds1-gsx-test",
    "morning_check":         "/tools/morning-check",
    "site_sweep":            "/tools/site-sweep",
    "ospf_stability":        "/tools/ospf-stability",
    "ixc_stability":         "/tools/ixc-stability",
    "card_alarms":           "/tools/card-alarms",
    "bcr_1800_check":        "/tools/bcr-1800-check",
    "sor_collect":           "/tools/sor",
}
_TOOL_MODULES = [
    autotester_standalone, npt1250_discovery, ds1_pms, fiber_util,
    ospf_events, health_check, gsx_perfmon, interface_check, mux_loop_script,
    ds1_gsx_test, morning_check, site_sweep, ospf_stability, ixc_stability,
    card_alarms, bcr_1800_check, sor_collect,
]


# Home-page grouping. Keyed by module name so it lives in one place (not spread
# across 17 TOOL_INFO dicts). A module not listed here still appears -- it falls
# into "Other" at the bottom -- so a newly added tool is never silently dropped.
TOOL_CATEGORIES = [
    ("Circuit Tracing & Triage",
        ["autotester_standalone", "site_sweep", "interface_check"]),
    ("Fleet Monitoring",
        ["morning_check", "ospf_stability", "ixc_stability", "card_alarms",
         "bcr_1800_check"]),
    ("1250 Diagnostics",
        ["npt1250_discovery", "ds1_pms", "fiber_util", "ospf_events",
         "health_check"]),
    ("GSX & Test Builders",
        ["gsx_perfmon", "mux_loop_script", "ds1_gsx_test"]),
    ("Inventory",
        ["sor_collect"]),
]


@app.route("/")
def home():
    by_name = {}
    for mod in _TOOL_MODULES:
        info = mod.TOOL_INFO
        by_name[mod.__name__] = {
            "name": info["name"], "ran_from": info["ran_from"],
            "desc": info["desc"], "requires": info["requires"],
            "order": info["order"], "url": TOOL_URLS[mod.__name__],
        }
    groups, placed = [], set()
    for label, names in TOOL_CATEGORIES:
        items = [by_name[n] for n in names if n in by_name]
        items.sort(key=lambda t: t["order"])
        placed.update(names)
        if items:
            groups.append({"category": label, "tools": items})
    # Anything not categorised -> "Other", so nothing disappears.
    leftover = sorted((t for n, t in by_name.items() if n not in placed),
                      key=lambda t: t["order"])
    if leftover:
        groups.append({"category": "Other", "tools": leftover})
    return render_template("home.html", groups=groups)


# ---- Autotester -------------------------------------------------------------

@app.route("/tools/autotester")
def page_autotester():
    cfg = secure_config.load_config_silent()
    return render_template("autotester.html",
                           saved_slack_webhook=cfg.get("SLACK_WEBHOOK", ""))


@app.route("/api/autotester/run", methods=["POST"])
def api_autotester_run():
    f = request.form
    cfg = secure_config.load_config_silent()
    ip_1250 = (f.get("ip_1250") or "").strip()
    if not _valid_ip(ip_1250):
        return jsonify(error="Enter a valid 1250 IP (4 octets, e.g. 10.111.131.77)."), 400
    ds1_raw = (f.get("ds1_list") or "").strip()
    ds1_list, _ds1_notes = expand_ds1_list(ds1_raw)
    if not ds1_list:
        return jsonify(error="Enter at least one DS1 (e.g. ts3/8, ts3/9)."), 400
    infra_user, infra_pass = _creds(f)
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    if not (infra_user and infra_pass and gsx_user and gsx_pass):
        return jsonify(error="Infrastructure and GSX credentials are both required."), 400
    jira_ticket, err = _parse_jira(f)
    if err:
        return jsonify(error=err), 400

    slack_webhook = (f.get("slack_webhook") or "").strip() or cfg.get("SLACK_WEBHOOK", "")
    if (f.get("slack_webhook") or "").strip():
        secure_config.save_config_key("SLACK_WEBHOOK", slack_webhook)

    params = {
        "ip_1250": ip_1250, "ds1_list": ds1_list,
        "infra_user": infra_user, "infra_pass": infra_pass,
        "gsx_user": gsx_user, "gsx_pass": gsx_pass,
        "jira_ticket": jira_ticket,
        "open_when_done": f.get("open_when_done") == "on",
        "slack_webhook": slack_webhook,
    }
    job_id = _launch_job(ip_1250, _warn_after(len(ds1_list)),
                          lambda: autotester_standalone.run(params))
    return jsonify(job_id=job_id)


@app.route("/api/reverse-autotester/run", methods=["POST"])
def api_reverse_autotester_run():
    """
    Reverse trace: GSX -> MUX -> 1800 -> 1250.

    Lives on the autotester page as a second card rather than its own tool
    page, so it is deliberately absent from TOOL_URLS / _TOOL_MODULES (those
    drive the home-page tool list).
    """
    f = request.form
    gsx_ip, gsx_note, gsx_err = _resolve_gsx(f.get("gsx_ip"))
    if gsx_err:
        return jsonify(error=gsx_err), 400
    t1_id = (f.get("t1_id") or "").strip()
    if not reverse_autotester.parse_t1_id(t1_id):
        return jsonify(
            error="Enter the GSX T1 as shelf-slot-port-t1, e.g. T1-1-11-1-11."), 400
    infra_user, infra_pass = _creds(f)
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    if not (infra_user and infra_pass and gsx_user and gsx_pass):
        return jsonify(
            error="Infrastructure and GSX credentials are both required."), 400
    # The reverse form reuses the names 'add_jira'/'jira_ticket' (its element
    # IDs are prefixed 'rev' so they stay unique on the page). Only the
    # submitted form's own fields are posted, so _parse_jira reads the right
    # ones without a second set of keys.
    jira_ticket, err = _parse_jira(f)
    if err:
        return jsonify(error=err), 400

    # Same saved-webhook behaviour as the forward run: a value typed here is
    # saved and becomes the default for both cards.
    cfg = secure_config.load_config_silent()
    slack_webhook = (f.get("slack_webhook") or "").strip() or cfg.get("SLACK_WEBHOOK", "")
    if (f.get("slack_webhook") or "").strip():
        secure_config.save_config_key("SLACK_WEBHOOK", slack_webhook)

    params = {
        "gsx_ip": gsx_ip, "t1_id": t1_id,
        "username": infra_user, "password": infra_pass,
        "gsx_user": gsx_user, "gsx_pass": gsx_pass,
        "jira_ticket": jira_ticket,
        "slack_webhook": slack_webhook,
        "open_when_done": f.get("rev_open_when_done") == "on",
        # Checked by default in the form, so an absent key means the user
        # actively unticked it.
        "analyze": f.get("rev_analyze") == "on",
    }
    # Nine hops across four device types; give it the same headroom a
    # multi-DS1 forward run gets rather than the single-DS1 default. With
    # analysis on it then runs a whole forward autotester as well, walking
    # every device a second time -- so roughly double again.
    job_id = _launch_job(gsx_ip, _warn_after(8 if params["analyze"] else 4),
                         lambda: reverse_autotester.run(params),
                         initial_log=["[{}]".format(gsx_note)] if gsx_note else None)
    return jsonify(job_id=job_id)


# ---- SoR inventory cache ---------------------------------------------------

@app.route("/tools/sor")
def page_sor():
    # Read the cache fresh on every view: a collection run in another tab,
    # or from the CLI, must show up here without restarting the server.
    state = sor_store.migrate(sor_store.load(), lambda *a: None)
    return render_template(
        "sor.html",
        s=sor_store.summary(state),
        rows=sor_store.rows(state),
        changes=sor_store.changes(state, limit=200),
        unverified=sor_store.unverified(state),
        fields=sor_store.FIELDS,
        netbox_claims=sor_store.NETBOX_CLAIMS)


@app.route("/api/sor/run", methods=["POST"])
def api_sor_run():
    f = request.form
    ip_1250 = (f.get("ip_1250") or "").strip()
    if not _valid_ip(ip_1250):
        return jsonify(error="Enter a valid 1250 IP (4 octets, e.g. 10.111.131.77)."), 400
    infra_user, infra_pass = _creds(f)
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    if not (infra_user and infra_pass and gsx_user and gsx_pass):
        return jsonify(
            error="Infrastructure and GSX credentials are both required."), 400

    params = {
        "ip_1250": ip_1250,
        "username": infra_user, "password": infra_pass,
        "gsx_user": gsx_user, "gsx_pass": gsx_pass,
        "open_when_done": f.get("open_when_done") == "on",
    }
    # Collects EVERY DS1 on the 1250, so give it the same headroom a
    # multi-DS1 forward run gets -- it is one, internally.
    job_id = _launch_job(ip_1250, _warn_after(28),
                         lambda: sor_collect.run(params))
    return jsonify(job_id=job_id)


# ---- shared shape: Discovery / Fiber / OSPF / Health ------------------------
# These 4 tools take identical inputs (1250 IP + infra creds + JIRA + open),
# and their run(params) all expect the same keys ("username"/"password", not
# "infra_user"/"infra_pass" -- matches each tool's own collect_inputs()).

def _simple_1250_route(module, run_url):
    f = request.form
    ip_1250 = (f.get("ip_1250") or "").strip()
    if not _valid_ip(ip_1250):
        return jsonify(error="Enter a valid 1250 IP (4 octets, e.g. 10.111.131.77)."), 400
    user, pw = _creds(f)
    if not (user and pw):
        return jsonify(error="Infrastructure username and password are required."), 400
    jira_ticket, err = _parse_jira(f)
    if err:
        return jsonify(error=err), 400

    params = {
        "ip_1250": ip_1250, "username": user, "password": pw,
        "jira_ticket": jira_ticket, "open_when_done": f.get("open_when_done") == "on",
    }
    job_id = _launch_job(ip_1250, _warn_after(), lambda: module.run(params))
    return jsonify(job_id=job_id)


@app.route("/tools/discovery")
def page_discovery():
    info = npt1250_discovery.TOOL_INFO
    return render_template("simple_1250.html", tool_title=info["name"],
                            tool_desc=info["desc"], run_url="/api/discovery/run")


@app.route("/api/discovery/run", methods=["POST"])
def api_discovery_run():
    return _simple_1250_route(npt1250_discovery, "/api/discovery/run")


@app.route("/tools/fiber")
def page_fiber():
    info = fiber_util.TOOL_INFO
    return render_template("simple_1250.html", tool_title=info["name"],
                            tool_desc=info["desc"], run_url="/api/fiber/run")


@app.route("/api/fiber/run", methods=["POST"])
def api_fiber_run():
    return _simple_1250_route(fiber_util, "/api/fiber/run")


@app.route("/tools/ospf")
def page_ospf():
    info = ospf_events.TOOL_INFO
    return render_template("simple_1250.html", tool_title=info["name"],
                            tool_desc=info["desc"], run_url="/api/ospf/run")


@app.route("/api/ospf/run", methods=["POST"])
def api_ospf_run():
    return _simple_1250_route(ospf_events, "/api/ospf/run")


@app.route("/tools/health")
def page_health():
    info = health_check.TOOL_INFO
    return render_template("simple_1250.html", tool_title=info["name"],
                            tool_desc=info["desc"], run_url="/api/health/run")


@app.route("/api/health/run", methods=["POST"])
def api_health_run():
    return _simple_1250_route(health_check, "/api/health/run")


# ---- DS1 PMs -----------------------------------------------------------

@app.route("/tools/ds1-pms")
def page_ds1_pms():
    return render_template("ds1_pms.html")


@app.route("/api/ds1-pms/run", methods=["POST"])
def api_ds1_pms_run():
    f = request.form
    ip_1250 = (f.get("ip_1250") or "").strip()
    if not _valid_ip(ip_1250):
        return jsonify(error="Enter a valid 1250 IP (4 octets, e.g. 10.111.131.77)."), 400
    ds1_raw = (f.get("ds1_list") or "").strip()
    # Card expansion first ("ts1" -> all 28), then the tool's own validation.
    raw_entries, _ds1_notes = expand_ds1_list(ds1_raw)
    if not raw_entries:
        return jsonify(error="Enter at least one DS1 (e.g. ts1/21, ts3/6)."), 400
    channels, verr = ds1_pms._parse_and_validate(raw_entries)
    if verr:
        return jsonify(error=verr), 400
    user, pw = _creds(f)
    if not (user and pw):
        return jsonify(error="Infrastructure username and password are required."), 400
    jira_ticket, jerr = _parse_jira(f)
    if jerr:
        return jsonify(error=jerr), 400

    params = {
        "ip_1250": ip_1250, "ds1_list": [c[0] for c in channels],
        "username": user, "password": pw,
        "jira_ticket": jira_ticket, "open_when_done": f.get("open_when_done") == "on",
    }
    job_id = _launch_job(ip_1250, _warn_after(len(channels)), lambda: ds1_pms.run(params))
    return jsonify(job_id=job_id)


# ---- GSX Perfmon --------------------------------------------------------

@app.route("/tools/gsx-perfmon")
def page_gsx_perfmon():
    return render_template("gsx_perfmon.html")


@app.route("/api/gsx-perfmon/run", methods=["POST"])
def api_gsx_perfmon_run():
    f = request.form
    gsx_ip, gsx_note, gsx_err = _resolve_gsx(f.get("gsx_ip"))
    if gsx_err:
        return jsonify(error=gsx_err), 400
    t1_id = gsx_perfmon._parse_t1_id((f.get("t1_id") or "").strip())
    if not t1_id:
        return jsonify(error="T1 assignment must look like T1-1-7-2-2 or 7-2-2."), 400
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    if not (gsx_user and gsx_pass):
        return jsonify(error="GSX username and password are required."), 400
    jira_ticket, jerr = _parse_jira(f)
    if jerr:
        return jsonify(error=jerr), 400

    params = {
        "gsx_ip": gsx_ip, "t1_id": t1_id,
        "gsx_user": gsx_user, "gsx_pass": gsx_pass,
        "jira_ticket": jira_ticket, "open_when_done": f.get("open_when_done") == "on",
    }
    job_id = _launch_job(t1_id, _warn_after(), lambda: gsx_perfmon.run(params),
                         initial_log=["[{}]".format(gsx_note)] if gsx_note else None)
    return jsonify(job_id=job_id)


# ---- IXC Check -----------------------------------------------------------

@app.route("/tools/ixc-check")
def page_ixc_check():
    return render_template("ixc_check.html")


@app.route("/api/ixc-check/run", methods=["POST"])
def api_ixc_check_run():
    f = request.form
    site = (f.get("site") or "").strip().upper()
    if site not in interface_check.SITE_TO_1800:
        return jsonify(error="Select a valid site (ATL, LAX, JFK, DFW, LAB)."), 400
    iface = (f.get("interface") or "").strip()
    if not iface or "." not in iface:
        return jsonify(error="Interface must include a dot (VLAN sub-interface), e.g. xe-ts13/3.624."), 400
    user, pw = _creds(f)
    if not (user and pw):
        return jsonify(error="Infrastructure username and password are required."), 400
    jira_ticket, jerr = _parse_jira(f)
    if jerr:
        return jsonify(error=jerr), 400

    params = {
        "site": site, "interface": iface, "username": user, "password": pw,
        "jira_ticket": jira_ticket, "open_when_done": f.get("open_when_done") == "on",
    }
    job_id = _launch_job("{} {}".format(site, iface), _warn_after(),
                          lambda: interface_check.run(params))
    return jsonify(job_id=job_id)


# ---- Morning Check --------------------------------------------------------
# NOTE on the "never write credentials to disk" rule in the module docstring
# above: Morning Check is a deliberate, scoped EXCEPTION, per P. Capps --
# retyping the JIRA token + Slack webhook (+ assignee/JIRA email) every run
# was enough of a pain that he asked for it. Only those four non-device
# fields are persisted (JIRA_TOKEN goes to Windows Credential Manager via
# keyring; the other three are plaintext in autotester_config.txt -- same
# split secure_config.py already uses for the CLI path). Infra/GSX device
# passwords are NEVER saved here; they stay session-only like every other
# tool on this site. Uses load_config_silent(), NOT load_config() -- the
# latter calls getpass() for anything missing, which would hang a web
# request instead of just a terminal.

@app.route("/tools/morning-check")
def page_morning_check():
    cfg = secure_config.load_config_silent()
    return render_template(
        "morning_check.html",
        saved_assignee_email=cfg.get("JIRA_ASSIGNEE_EMAIL", ""),
        saved_jira_email=cfg.get("JIRA_EMAIL", ""),
        saved_slack_webhook=cfg.get("SLACK_WEBHOOK", ""),
    )


@app.route("/api/morning-check/run", methods=["POST"])
def api_morning_check_run():
    f = request.form
    cfg = secure_config.load_config_silent()

    assignee_email = (f.get("assignee_email") or "").strip() or cfg.get("JIRA_ASSIGNEE_EMAIL", "")
    if not assignee_email or "@" not in assignee_email:
        return jsonify(error="Enter your JIRA assignee email."), 400

    jira_email = (f.get("jira_email") or "").strip() or cfg.get("JIRA_EMAIL", "")
    jira_token = (f.get("jira_token") or "").strip() or cfg.get("JIRA_TOKEN", "")
    if not (jira_email and jira_token):
        return jsonify(error="JIRA email and API token are required "
                              "(enter them once and they'll be saved)."), 400

    infra_user, infra_pass = _creds(f)
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    if not (infra_user and infra_pass):
        return jsonify(error="Infrastructure username and password are required."), 400
    if not (gsx_user and gsx_pass):
        return jsonify(error="GSX username and password are required."), 400

    slack_webhook  = (f.get("slack_webhook") or "").strip() or cfg.get("SLACK_WEBHOOK", "")
    dry_run        = f.get("dry_run") == "on"
    open_when_done = f.get("open_when_done") == "on"

    # Persist whatever was actually TYPED this run (blank fields just fall
    # back to what's already stored, above -- nothing to re-save there).
    if (f.get("assignee_email") or "").strip():
        secure_config.save_config_key("JIRA_ASSIGNEE_EMAIL", assignee_email)
    if (f.get("jira_email") or "").strip():
        secure_config.save_config_key("JIRA_EMAIL", jira_email)
    if (f.get("jira_token") or "").strip():
        secure_config.save_config_key("JIRA_TOKEN", jira_token)  # -> keyring
    if (f.get("slack_webhook") or "").strip():
        secure_config.save_config_key("SLACK_WEBHOOK", slack_webhook)

    params = {
        "jira_email":     jira_email,
        "jira_token":     jira_token,
        "assignee":       assignee_email,
        "username":       infra_user,
        "password":       infra_pass,
        "gsx_user":       gsx_user,
        "gsx_pass":       gsx_pass,
        "slack_webhook":  slack_webhook,
        "dry_run":        dry_run,
        "open_when_done": open_when_done,
    }
    # Morning check walks multiple tickets sequentially; each ticket can run
    # 3 tools (Autotester ~6 min, Fiber ~1 min, OSPF ~1 min). Scale the
    # hung warning generously -- 20 min floor.
    job_id = _launch_job(
        "Morning Check ({})".format(assignee_email),
        1200,
        lambda: morning_check.run(params))
    return jsonify(job_id=job_id)


# ---- Site Sweep ------------------------------------------------------------

@app.route("/tools/site-sweep")
def page_site_sweep():
    info = site_sweep.TOOL_INFO
    return render_template("simple_1250.html", tool_title=info["name"],
                            tool_desc=info["desc"], run_url="/api/site-sweep/run")


@app.route("/api/site-sweep/run", methods=["POST"])
def api_site_sweep_run():
    return _simple_1250_route(site_sweep, "/api/site-sweep/run")


# ---- OSPF Stability Monitor -----------------------------------------------

@app.route("/tools/ospf-stability")
def page_ospf_stability():
    info = ospf_stability.TOOL_INFO
    return render_template("ospf_stability.html",
                            tool_title=info["name"], tool_desc=info["desc"])


@app.route("/api/ospf-stability/run", methods=["POST"])
def api_ospf_stability_run():
    f = request.form
    user, pw = _creds(f)
    if not (user and pw):
        return jsonify(error="Infrastructure username and password are required."), 400
    params = {
        "username":       user,
        "password":       pw,
        "slack_alert":    f.get("slack_alert") == "on",
        "open_when_done": f.get("open_when_done") == "on",
    }
    # Warn after 15 min. Hub polling is fast (~15 s), but the per-1250 event-log
    # sweep that establishes true drop counts scales with fleet size:
    # ~10 s/device at 8 workers, so ~5 min for a few hundred sites.
    job_id = _launch_job("OSPF Stability Poll", 900,
                          lambda: ospf_stability.run(params))
    return jsonify(job_id=job_id)


# ---- IXC Stability (BETA) ------------------------------------------------

@app.route("/tools/ixc-stability")
def page_ixc_stability():
    info = ixc_stability.TOOL_INFO
    return render_template("ixc_stability.html",
                            tool_title=info["name"], tool_desc=info["desc"])


@app.route("/api/ixc-stability/run", methods=["POST"])
def api_ixc_stability_run():
    f = request.form
    user, pw = _creds(f)
    if not (user and pw):
        return jsonify(error="Infrastructure username and password are required."), 400
    params = {
        "username":       user,
        "password":       pw,
        "open_when_done": f.get("open_when_done") == "on",
    }
    # Fleet-wide DS1 sweep: ~5-15 s per device at SWEEP_WORKERS concurrency.
    # Warn after 20 min for a large fleet.
    job_id = _launch_job("IXC Stability Sweep", 1200,
                          lambda: ixc_stability.run(params))
    return jsonify(job_id=job_id)


# ---- Card Alarms (Fleet Equipment Sweep) ---------------------------------

@app.route("/tools/card-alarms")
def page_card_alarms():
    info = card_alarms.TOOL_INFO
    return render_template("card_alarms.html",
                            tool_title=info["name"], tool_desc=info["desc"])


@app.route("/api/card-alarms/run", methods=["POST"])
def api_card_alarms_run():
    f = request.form
    user, pw = _creds(f)
    if not (user and pw):
        return jsonify(error="Infrastructure username and password are required."), 400
    # Category checkboxes: a box maps to cat_<name>. If none are checked, fall
    # back to all (an empty selection almost certainly means the user didn't mean
    # "show nothing").
    selected = [c for c in card_alarms.CATEGORIES if f.get("cat_" + c) == "on"]
    params = {
        "username":       user,
        "password":       pw,
        "open_when_done": f.get("open_when_done") == "on",
        "slack_alert":    f.get("slack_alert") == "on",
        "categories":     selected or None,
    }
    # Fleet-wide alarm read: ~seconds per device at SWEEP_WORKERS concurrency.
    # Warn after 20 min for a large fleet.
    job_id = _launch_job("Card Alarms Sweep", 1200,
                          lambda: card_alarms.run(params))
    return jsonify(job_id=job_id)


# ---- BCR to 1800 Check ---------------------------------------------------

@app.route("/tools/bcr-1800-check")
def page_bcr_1800_check():
    info = bcr_1800_check.TOOL_INFO
    return render_template("bcr_1800_check.html",
                            tool_title=info["name"], tool_desc=info["desc"])


@app.route("/api/bcr-1800-check/run", methods=["POST"])
def api_bcr_1800_check_run():
    f = request.form
    user, pw = _creds(f)
    if not (user and pw):
        return jsonify(error="Infrastructure username and password are required."), 400
    params = {
        "username":       user,
        "password":       pw,
        "open_when_done": f.get("open_when_done") == "on",
    }
    # Four BCRs + their 1800s, serial; a couple of show commands each.
    job_id = _launch_job("BCR to 1800 Check", 600,
                          lambda: bcr_1800_check.run(params))
    return jsonify(job_id=job_id)


# ---- MUX CEM Loop Script (two modes) -------------------------------------

@app.route("/tools/mux-loop")
def page_mux_loop():
    return render_template("mux_loop.html")


@app.route("/api/mux-loop/run", methods=["POST"])
def api_mux_loop_run():
    f = request.form
    mode = f.get("mode") or "from_1250"
    infra_user, infra_pass = _creds(f)
    if not (infra_user and infra_pass):
        return jsonify(error="Infrastructure username and password are required."), 400
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    jira_ticket, jerr = _parse_jira(f)
    if jerr:
        return jsonify(error=jerr), 400
    open_when_done = f.get("open_when_done") == "on"

    if mode == "mux_direct":
        mux_ip = (f.get("mux_ip") or "").strip()
        mux_name = (f.get("mux_name") or "").strip()
        acr_num = (f.get("acr_num") or "").strip()
        cem_group = (f.get("cem_group") or "").strip()
        if not _valid_ip(mux_ip):
            return jsonify(error="Enter a valid MUX IP."), 400
        if not (mux_name and acr_num and cem_group):
            return jsonify(error="MUX name, ACR group, and CEM group are required."), 400

        opt_ip_1250, e1 = _opt_ip(f.get("opt_ip_1250"))
        if e1:
            return jsonify(error=e1), 400
        opt_ip_1800, e2 = _opt_ip(f.get("opt_ip_1800"))
        if e2:
            return jsonify(error=e2), 400
        opt_gsx_ip, e3 = _opt_ip(f.get("opt_gsx_ip"))
        if e3:
            return jsonify(error=e3), 400

        opt_ds1 = (f.get("opt_ds1") or "").strip().lower() if opt_ip_1250 else ""
        opt_oc_iface = (f.get("opt_oc_iface") or "").strip().lower() if opt_ip_1800 else ""
        opt_gsx_t1 = (f.get("opt_gsx_t1") or "").strip().upper()

        # NOTE: _run_mux_direct never actually opens a live GSX session --
        # gsx_ip/gsx_t1 are only embedded as "run manually" text in the
        # generated script, so gsx_user/gsx_pass aren't required here.
        params = {
            "mode": "mux_direct",
            "mux_ip": mux_ip, "mux_name": mux_name,
            "acr_num": acr_num, "cem_group": cem_group,
            "ip_1250": opt_ip_1250, "ds1": opt_ds1,
            "ip_1800": opt_ip_1800, "oc_iface": opt_oc_iface,
            "gsx_t1": opt_gsx_t1, "gsx_ip": opt_gsx_ip if opt_gsx_t1 else "",
            "infra_user": infra_user, "infra_pass": infra_pass,
            "gsx_user": gsx_user, "gsx_pass": gsx_pass,
            "jira_ticket": jira_ticket, "open_when_done": open_when_done,
        }
        label = mux_name
    else:
        ip_1250 = (f.get("ip_1250") or "").strip()
        ds1_raw = (f.get("ds1") or "").strip().lower()
        if not _valid_ip(ip_1250):
            return jsonify(error="Enter a valid 1250 IP."), 400
        if not ds1_raw:
            return jsonify(error="Enter a DS1 (e.g. ts1/1)."), 400
        ds1 = ds1_raw.replace("&", ",").split(",")[0].strip()
        if not (gsx_user and gsx_pass):
            return jsonify(error="GSX username and password are required in this mode."), 400

        params = {
            "mode": "from_1250",
            "ip_1250": ip_1250, "ds1": ds1,
            "infra_user": infra_user, "infra_pass": infra_pass,
            "gsx_user": gsx_user, "gsx_pass": gsx_pass,
            "jira_ticket": jira_ticket, "open_when_done": open_when_done,
        }
        label = "{} {}".format(ip_1250, ds1)

    job_id = _launch_job(label, _warn_after(), lambda: mux_loop_script.run(params))
    return jsonify(job_id=job_id)


# ---- DS1 Test Builder (AIS / Loops to GSX) -------------------------------

@app.route("/tools/ds1-gsx-test")
def page_ds1_gsx_test():
    return render_template("ds1_gsx_test.html")


@app.route("/api/ds1-gsx-test/run", methods=["POST"])
def api_ds1_gsx_test_run():
    f = request.form
    ip_1250 = (f.get("ip_1250") or "").strip()
    if not _valid_ip(ip_1250):
        return jsonify(error="Enter a valid 1250 IP."), 400
    ds1_raw = (f.get("ds1_list") or "").strip()
    ds1_list, _ds1_notes = expand_ds1_list(ds1_raw)
    if not ds1_list:
        return jsonify(error="Enter at least one DS1 (e.g. ts1/1)."), 400

    device = f.get("device") or "1250"
    test = f.get("test") or "ais"
    direction = f.get("direction") or ""
    if device not in ds1_gsx_test.DEVICES or test not in ds1_gsx_test.TESTS:
        return jsonify(error="Invalid device/test selection."), 400
    dirs = (ds1_gsx_test.AIS_DIRS_1250 if device == "1250"
            else ds1_gsx_test.AIS_DIRS_1800) if test == "ais" else \
           (ds1_gsx_test.LOOP_DIRS_1250 if device == "1250"
            else ds1_gsx_test.LOOP_DIRS_1800)
    if direction not in dirs:
        return jsonify(error="Pick a direction for this device/test combination."), 400

    infra_user, infra_pass = _creds(f)
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    if not (infra_user and infra_pass):
        return jsonify(error="Infrastructure credentials are required."), 400
    if not (gsx_user and gsx_pass):
        return jsonify(error="GSX credentials are required (prechecks/assignments)."), 400
    jira_ticket, jerr = _parse_jira(f)
    if jerr:
        return jsonify(error=jerr), 400

    params = {
        "ip_1250": ip_1250, "ds1_list": ds1_list,
        "device": device, "test": test, "direction": direction,
        "infra_user": infra_user, "infra_pass": infra_pass,
        "gsx_user": gsx_user, "gsx_pass": gsx_pass,
        "jira_ticket": jira_ticket,
        "open_when_done": f.get("open_when_done") == "on",
    }
    sink = {}   # run() publishes resolved GSX targets here mid-run
    # This tool walks 4 live hops (1250 -> 1800 -> MUX -> GSX) per DS1, each
    # with several commands -- routinely runs 6-7 min even for one DS1, well
    # past the 5 min floor other (single/dual-hop) tools use. Bump the base
    # so a normal run doesn't trip the "may be hung" warning.
    job_id = _launch_job("{} {} {}".format(ip_1250, device, test),
                          max(480, _warn_after(len(ds1_list))),
                          lambda: ds1_gsx_test.run(params, sink))
    with JOBS_LOCK:
        JOBS[job_id]["sink"] = sink
    return jsonify(job_id=job_id)


@app.route("/api/gsx-monitor/run", methods=["POST"])
def api_gsx_monitor_run():
    f = request.form
    gsx_ip, gsx_note, gsx_err = _resolve_gsx(f.get("gsx_ip"))
    if gsx_err:
        return jsonify(error=gsx_err), 400
    t1_id = gsx_perfmon._parse_t1_id((f.get("t1_id") or "").strip())
    if not t1_id:
        return jsonify(error="T1 assignment must look like T1-1-7-2-2 or 7-2-2."), 400
    gsx_user, gsx_pass = _creds(f, "gsx_user", "gsx_pass")
    if not (gsx_user and gsx_pass):
        return jsonify(error="GSX credentials are required."), 400
    try:
        interval = int(f.get("interval") or 15)
        duration = int(f.get("duration") or 600)
    except ValueError:
        return jsonify(error="Interval and duration must be numbers (seconds)."), 400
    if not (5 <= interval <= 300):
        return jsonify(error="Interval must be 5-300 seconds."), 400
    if not (30 <= duration <= 3600):
        return jsonify(error="Duration must be 30-3600 seconds."), 400

    stop_event = threading.Event()
    params = {
        "gsx_ip": gsx_ip, "t1_id": t1_id,
        "gsx_user": gsx_user, "gsx_pass": gsx_pass,
        "interval": interval, "duration": duration,
        "stop_event": stop_event,
        "clear_first": f.get("clear_first") == "on",
    }
    job_id = _launch_job("Monitor {}".format(t1_id), duration + 120,
                          lambda: ds1_gsx_test.run_gsx_monitor(params),
                          initial_log=["[{}]".format(gsx_note)] if gsx_note else None)
    with JOBS_LOCK:
        JOBS[job_id]["stop_event"] = stop_event
    return jsonify(job_id=job_id)


@app.route("/api/job/<job_id>/stop", methods=["POST"])
def api_job_stop(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown job id."), 404
    ev = job.get("stop_event")
    if not ev:
        return jsonify(error="This job type can't be stopped."), 400
    ev.set()
    return jsonify(ok=True)


if __name__ == "__main__":
    print("=" * 64)
    print("  Curveball Tools -- Web (proof of concept)")
    print("  Open http://127.0.0.1:5000 in a browser")
    print("=" * 64)
    # debug=True is fine on localhost only -- see WEB_SETUP.md.
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
