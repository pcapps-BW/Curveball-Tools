# Curveball Tools -- Web Front End

A Flask app that runs the whole suite from a browser instead of a terminal.
It is now the primary way the tools are used, though it still runs on Flask's
development server bound to localhost -- see section 6 before it leaves your
machine.

> This file described "a proof of concept covering one tool end to end" for
> about two months after every tool was wired up. Section 4's "one tool wired
> up" limitation was the specific line that misled. Corrected 2026-10-01.

---

## 1. The layout

```
app.py                  <- the Flask app (run this)
templates/              <- base.html + home.html + one page per tool
static/                 <- job.js (polling + form wiring), style.css, icon
```

Every tool is routed. The **home page builds itself** from each module's
`TOOL_INFO`, iterating `_TOOL_MODULES` and looking up `TOOL_URLS` for the
link — so adding a tool to those two structures is what makes it appear.

Three structures in `app.py` decide what exists:

| Structure | Effect of being absent from it |
|---|---|
| `_TOOL_MODULES` | no card on the home page |
| `TOOL_URLS` | `KeyError` at home-page render if it *is* in `_TOOL_MODULES` — they must agree |
| a `@app.route` + template | nothing to click through to |

**Two tools are deliberately not in all three.** `reverse_autotester.py` has a
route and lives as a second card inside `templates/autotester.html`, but is
absent from `_TOOL_MODULES`/`TOOL_URLS` so it does not render its own card.
`circuit_audit.py` has none of the three and is CLI-only. See
`ADDING_A_TOOL.md` for both patterns — an unexplained absence here reads
exactly like a registration bug.

`requirements.txt` lists `paramiko`, `requests`, `flask` and `keyring`.

---

## 2. Run it locally

From inside the `Curveball Tools` folder:

```
pip install -r requirements.txt
python app.py
```

You'll see:

```
Curveball Tools -- Web (proof of concept)
Open http://127.0.0.1:5000 in a browser
```

Open that URL. The home page lists every tool; each page's form asks the same
fields that tool's `collect_inputs()` would have prompted for.

What happens:
1. The page submits the form and gets back a job id immediately.
2. It polls the server every ~2 seconds and shows an elapsed timer while the
   job runs in the background.
3. If the job is still running past a threshold (5 minutes, or longer for
   multi-DS1 runs) it shows a "this may be hung" warning banner -- it keeps
   polling, it doesn't stop the job, it's just a heads-up so you're not
   staring at a spinner wondering if something broke.
4. When it finishes, the full report renders on the page and a **Download
   .txt** link appears (same file the CLI would have written).

Credentials are submitted fresh with the form every run and only ever live
in memory for the life of that one background job -- never written to disk,
never put in a cookie/session.

---

## 3. How it fits together (so you're not just running a black box)

- **`collect_inputs()` is never called.** Your existing tools ask questions
  with `input()`/`getpass()`, which would just hang forever with no
  terminal attached to a web request. `app.py` has its own form that asks
  for the same fields, builds the exact same `params` dict `collect_inputs()`
  would have built, and calls that tool's `run(params)` directly. Nothing
  about `run()`'s logic changed -- the SSH clients, Netbox lookups, MUX
  resolution, GSX pull, report building are all the exact same code path as
  the CLI.

- **Each run happens in a background thread, not the web request.** A
  multi-minute SSH job can't reasonably hold an HTTP connection open --
  browsers and proxies time those out, and you'd have no visibility into
  progress. So submitting the form just starts a background thread and
  immediately hands back a job id; the browser polls a small status endpoint
  (`/api/job/<id>`) to find out when it's done. This is also what makes the
  elapsed-timer/hang-warning possible, and it's the natural foundation if you
  ever want real line-by-line progress streaming later (that would be a
  layer on top of this same job-tracking, not a rewrite).

- **Job state lives in memory** (a plain Python dict in `app.py`, guarded by
  a lock since the background thread and the web requests touch it from
  different threads). Restarting the app loses job history. Fine for a POC;
  worth revisiting before this runs unattended for a long time (see below).

---

## 4. Known limitations (by design, for now)

- **"Hung" is a warning, not a kill switch.** Python threads can't be force-
  stopped from the outside. If a job genuinely hangs, the warning banner
  will show, but the underlying SSH session keeps running until its own
  timeout trips (same timeouts the CLI already has). A real "Cancel" button
  that actually kills mid-flight work would need a process-per-job model
  (`multiprocessing` instead of `threading`) -- doable later if this becomes
  a real problem, not needed for the POC.
- **In-memory job store, no cleanup.** Every run adds an entry to a dict that
  nothing ever removes. Not a problem for a POC session; would want a real
  store (or at least a periodic sweep) before this runs unattended for days.
- **The live progress log is captured by swapping `sys.stdout`.** Tools
  already `print()` their progress; `_JobStdoutTee` routes each write to
  whichever background thread's job log is writing, via a thread-id map. That
  is why no tool module needed logging calls added — but it also means
  anything that writes to stdout during a run lands in the user's log.
- **Flask's built-in dev server, with `debug=True`.** Still true as of
  v2.35 (`app.py` binds `127.0.0.1` with `debug=True`). Fine on localhost.
  See the migration checklist below before this is reachable from anywhere
  else.

---

## 5. The CLI-side changes

**Two**, both additive, both no-ops for the CLI.

**`run()` returns its output path.** It previously wrote the report and
returned nothing — fine for a console tool, but the web app needs the path to
read the report and offer a download. Every code path returns it now,
including early-exit failures. `main()` still ignores the return value.

**`autotester_standalone.run()` has an optional structured handback.** Pass an
empty dict as `params["_extra_out"]` and it fills in `jira_summary`,
`route_diag`, `mux_results`, `host_1250`, `ds1_map` and `run_summary`. Callers
that don't set the key are completely unaffected, which is why `run()`'s return
type never had to change.

Two callers use it: `morning_check.py`, and — since v2.35 —
`reverse_autotester.py`, which reads `mux_results` to compare the GSX
assignment the forward walk derived against the one its own trace started
from. **So `run()` now has consumers beyond the CLI and the web route**; check
them before changing its params or what it hands back.

---

## 6. When you're ready to move this onto the network

This list is deliberately NOT done yet -- it's what to work through before
this leaves your machine, not a POC requirement:

- **Turn off `debug=True`.** Flask's debugger allows arbitrary code
  execution if it's ever reachable by anyone but you. Fine on `127.0.0.1`;
  not fine anywhere else.
- **Swap the dev server for a real one.** `app.run()` prints its own
  warning about this. On Windows, **waitress** is the natural choice (pure
  Python, no compiler needed, unlike gunicorn which is Unix-only):
  ```
  pip install waitress
  waitress-serve --host=0.0.0.0 --port=5000 app:app
  ```
- **Use HTTPS once this is off localhost.** The form POSTs credentials in
  the request body -- fine over loopback, not fine in cleartext over a LAN.
  A reverse proxy (IIS, nginx, Caddy) terminating TLS in front of the
  waitress process is the usual pattern.
- **Decide who else can reach it and from where** -- same shared-account
  question already flagged in `TODO.md` for the CLI tools. A web front end
  makes it easier for more people to run these tools, which is the whole
  point, but also raises the same "who ran this and why" attribution
  question a bit sooner than the CLI did.
- **Decide on job persistence** if this will run continuously rather than
  being started fresh each session (a real database or at least a
  SQLite file, plus a cleanup job for old entries).

None of this blocks the local proof of concept -- it's the punch list for
"finally destination," not for today.
