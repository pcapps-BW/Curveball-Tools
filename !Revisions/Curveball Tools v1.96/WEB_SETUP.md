# Curveball Tools -- Web Front End (Proof of Concept)

A small Flask app that runs the **Autotester** from a browser instead of a
terminal. This is a proof of concept covering one tool end to end; the plan
is to run it locally first, then decide whether/how to move it onto a shared
machine on the network.

---

## 1. What's new

Two new files, added alongside the existing tool files (nothing about the
CLI tools changed except one small addition -- see section 5):

```
app.py                  <- the Flask app (run this)
templates/index.html    <- the single page it serves
```

`requirements.txt` now also lists `flask>=3.0`.

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

Open that URL. Fill in the form (same fields the CLI Autotester asks for --
1250 IP, DS1 list, optional JIRA ticket, infra creds, GSX creds) and click
**Run Autotester**.

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
  would have built, and calls `autotester_standalone.run(params)` directly.
  Nothing about `run()`'s logic changed -- the SSH clients, Netbox lookups,
  MUX resolution, GSX pull, report building are all the exact same code path
  as the CLI.

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
- **One tool wired up.** Only the Autotester has a form right now. The other
  8 tools follow the same pattern (a form matching their `collect_inputs()`
  fields + a call to their `run(params)`) -- mechanical repetition once
  you're happy with this one.
- **Flask's built-in dev server, with `debug=True`.** Fine on localhost.
  See the migration checklist below before this is reachable from anywhere
  else.

---

## 5. The one CLI-side change

`autotester_standalone.run()` previously wrote its report to a file but
returned nothing -- fine for the CLI (it only ever printed to the console),
but the web app needs the file path back so it can read the report and offer
a download link. `run()` now `return`s the output path in every code path
(including early-exit failures). This doesn't change CLI behavior at all --
`main()` still just calls `run(params)` and ignores the return value.

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
