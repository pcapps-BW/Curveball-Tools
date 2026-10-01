# Adding a tool

Two separate jobs, and **doing only the first is the common mistake**:

1. **The tool contract** — makes it work and appear in the CLI launcher.
   Auto-discovered: drop your `.py` file next to `launcher.py` and it shows up
   in the menu with no launcher edit. Sections 1–2 below.
2. **Web wiring** — makes it appear in the Flask app. **Not automatic.**
   Section 3 below.

`circuit_audit.py` is the live demonstration of the gap: it has a valid
`TOOL_INFO`, a `--self-test`, and has never once appeared in the web UI,
because nobody did step 2.

---

## 1. A `TOOL_INFO` dict

Put this near the top of your file. It's what the launcher reads to build the
menu entry:

```python
TOOL_INFO = {
    "name": "My Tool",                       # shown in the menu
    "ran_from": "1250",                      # which device it runs from
    "desc": "One-line description of what it does.",
    "requires": "1250 IP, creds",            # inputs the user must supply
    "order": 70,                             # menu sort position (lower = higher up)
}
```

Pick an `order` that doesn't collide. **In use as of v2.35:** 5, 7, 8, 9, 10,
11, 20, 30, 40, 45, 50, 55, 60, 65, 70. Gaps are deliberate — leave room.
Rather than trusting this list, read the current values:

```
python -c "import importlib,glob,re;[print(getattr(importlib.import_module(f[:-3]),'TOOL_INFO',{}).get('order'),f) for f in sorted(glob.glob('*.py'))]" 2>NUL | findstr /r "^[0-9]"
```

**A tool with no `order` key sorts last and is easy to lose.** `circuit_audit.py`
has none.

**A file without a `TOOL_INFO` is ignored** by the launcher — so shared/helper
modules are safe to keep in the folder; they won't show up as tools.

---

## 2. `collect_inputs(session)` and `run(params)`

Split your script so prompting and doing-the-work are separate:

```python
def collect_inputs(session=None):
    """
    Prompt for this tool's inputs. Return a params dict, or None to abort.
    `session` is an optional dict of values remembered across tools in one
    launcher run -- use it so the user doesn't re-type shared things.
    """
    session = session if session is not None else {}
    import getpass

    # Device-identifying values: offer the remembered value as a default,
    # but always show it so the user confirms which device they're hitting.
    default_ip = session.get("ip_1250")
    prompt = "1250 IP" + (" [{}]".format(default_ip) if default_ip else "")
    ip = input(prompt + ": ").strip() or (default_ip or "")
    if not ip:
        print("No IP."); return None

    # Credentials: reuse silently if the session already has them.
    user = session.get("infra_user")
    pw   = session.get("infra_pass")
    if not (user and pw):
        user = input("Username: ").strip()
        pw   = getpass.getpass("Password: ")
    if not (user and pw):
        print("Missing credentials."); return None

    return {"ip_1250": ip, "username": user, "password": pw}


def run(params):
    """Do the work using params. MUST NOT prompt -- all input is in params."""
    ip   = params["ip_1250"]
    user = params["username"]
    pw   = params["password"]
    # ... connect, run read-only commands, write the report ...


def main():
    # Lets the script also run on its own, outside the launcher.
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
```

### The rules that matter

- **`run(params)` must never prompt.** All input comes from the `params` dict.
  (The launcher has already collected it; prompting inside `run` would hang or
  double-ask.) `collect_inputs` is the *only* place that prompts.
- **`collect_inputs` returns a dict** whose keys are whatever `run` needs.
- **Use the shared `session` keys** so cross-tool reuse works:
  - `username` / `password` -> harvested into the shared infra login (reused
    silently by later tools).
  - `ip_1250`, `site`, `interface`, `ds1_list` -> remembered and offered as
    editable defaults.
- **Read-only.** Tools *execute* only `show`/`SHOW` (plus session-only paging
  like `terminal length 0` / `| no-more`). No config-changing commands.

  **One shipped exception exists** and is the bar for adding another:
  `ds1_gsx_test.run_gsx_monitor()` sends a GSX PM counter reset, but only when
  an opt-in parameter is set, defaulted off, with a UI checkbox the operator
  must tick, no service impact, and the irreversibility stated. If you think
  your tool needs a write, it must clear that bar and get discussed first —
  and it goes in `INTRUSIVE_COMMANDS.md` as a numbered exception in the same
  commit.

  The other carve-out: a tool may **print** an intrusive command as copy/paste
  runbook text for a human to execute and reverse by hand — three places do
  (`ds1_gsx_test.py`, `mux_loop_script.py`, and `build_ces_enable_mop()` in
  `output_builders.py`). If you add a fourth, generate the back-out step
  alongside it so a test is never left half-applied, **and update
  `INTRUSIVE_COMMANDS.md` in the same commit** — that file is the audit
  anyone relies on when asked whether this suite is safe, and it is only
  trustworthy if it is complete.

---

## 3. Wiring it into the web app

The launcher auto-discovers. **`app.py` does not.** Four edits, and the first
two must agree or the home page raises `KeyError` on render:

1. **`import my_tool`** at the top of `app.py`.
2. **Add to both `_TOOL_MODULES` and `TOOL_URLS`.** The home page iterates
   `_TOOL_MODULES` and looks up `TOOL_URLS[mod.__name__]` for the link — a
   module in one and not the other is a crash, not a missing card.
3. **Add a route** that validates the form, builds the same params dict
   `collect_inputs()` would have, and launches it:

   ```python
   @app.route("/api/my-tool/run", methods=["POST"])
   def api_my_tool_run():
       f = request.form
       ip = (f.get("ip_1250") or "").strip()
       if not _valid_ip(ip):
           return jsonify(error="Enter a valid 1250 IP."), 400
       user, pw = _creds(f)
       if not (user and pw):
           return jsonify(error="Credentials are required."), 400
       params = {"ip_1250": ip, "username": user, "password": pw}
       job_id = _launch_job(ip, _warn_after(), lambda: my_tool.run(params))
       return jsonify(job_id=job_id)
   ```

   Five tools share the identical 1250-IP-plus-creds shape via
   `_simple_1250_route()` (discovery, fiber, ospf_events, health, site_sweep)
   — use it if yours fits.

4. **Add `templates/my_tool.html`**, extending `base.html`, with a form whose
   `data-run-url` is your route, `{% include "_job_panels.html" %}`, and
   `initJobForm('myForm')` in the scripts block.

**Validate server-side, in the route.** Browser validation is a convenience,
not a control — the route is what talks to production devices.

### Variant: sharing an existing page

`reverse_autotester.py` renders as a *second card* on the Autotester page
rather than getting its own. To do that:

- **Omit the module from `_TOOL_MODULES` and `TOOL_URLS`** — that is what stops
  it rendering its own home-page card. Keep `TOOL_INFO` for the CLI launcher.
- Add a second `<form>` to the host template with its own `data-run-url` and
  its own `.field-error` div, so validation stays local to that card.
- Call `initJobForm()` once per form. It supports several forms per page (see
  its `panelSuffix` argument); both cards share the one job-output area,
  since only one run happens at a time.
- Register a normal standalone route.

**Write the omission down in the module docstring.** An unexplained absence
from `TOOL_URLS` reads exactly like a registration bug to the next person
auditing `app.py` — and anything that enumerates `TOOL_URLS` to build another
front end (the NOC's ServiceNow UI, for instance) will silently drop the tool.

### Form field names across two forms on one page

Element **IDs** must be unique per page; field **names** need not be, because
only the submitted form's fields are posted. The reverse card reuses the names
`add_jira` / `jira_ticket` with `rev`-prefixed IDs, so the existing
`_parse_jira()` reads the right values with no second set of keys.

---

## Session reuse keys (reference)

If your tool uses these standard param keys, cross-tool reuse "just works":

| Key | Reused how |
|-----|-----------|
| `username`, `password` | infra login — reused **silently** for later tools |
| `gsx_user`, `gsx_pass` | GSX login — reused silently |
| `ip_1250` | remembered, offered as an editable **default** |
| `site` | remembered, offered as a default |
| `interface` | remembered, offered as a default |
| `ds1_list` | remembered, offered as a default |

---

## Checklist for a new tool

**Contract:**

- [ ] File is in the same folder as `launcher.py`
- [ ] Has a `TOOL_INFO` dict (name, ran_from, desc, requires, order)
- [ ] `order` is present and doesn't collide
- [ ] Has `collect_inputs(session)` returning a params dict (or None)
- [ ] Has `run(params)` that does **not** prompt, and **returns its output path**
- [ ] Output path comes from `platform_paths.path(...)`, never a hardcoded
      `C:\Claude\` — the suite runs on Linux too
- [ ] Has a `main()` + `if __name__ == "__main__"` so it runs standalone too
- [ ] Executes only `show`/`SHOW` / session-only commands
- [ ] Wraps the device session in `try/finally` so a parse error cannot leak it
- [ ] Runs cleanly on its own first (`python my_tool.py`)

**Web (skip only if the tool is deliberately CLI-only — and say so in the
docstring):**

- [ ] Imported in `app.py`
- [ ] In **both** `_TOOL_MODULES` and `TOOL_URLS` (or deliberately in neither,
      for the shared-page pattern)
- [ ] Route validates every field server-side before launching
- [ ] Template extends `base.html`, includes `_job_panels.html`, calls
      `initJobForm`
- [ ] Clicked through in a browser, not just unit-tested

**If it parses device output:**

- [ ] Parsed against a **real paste**, not an assumed format
- [ ] A `self_test()` with the captured sample embedded, and the fixture
      actually triggers the code under test — a fixture that silently
      exercises nothing has passed while testing the wrong branch here before
- [ ] Ambiguity prints `NONE FOUND` / `refusing to pick` rather than picking
- [ ] Never reports "clean" when the command failed or returned nothing

If the launcher can't load your tool it will print a `[skip] ...` line saying
why (import error, or missing `collect_inputs`/`run`) and continue — it won't
break the menu for everyone else.

---

## Cutting a release

Four steps. Miss any one and the version in the UI stops matching what is on
disk or in the repo — which has happened before (v1.81 showed in the UI with
no zip on disk).

- [ ] Bump `APP_VERSION` in `app.py`
- [ ] Add a `## vX.YZ — YYYY-MM-DD` entry to **`!Revisions/CHANGELOG.md`**
      (that is the canonical one; the root `CHANGELOG.md` is a stub pointer)
- [ ] Build `!Revisions/Curveball Tools vX.YZ.zip`
- [ ] **Edit `.gitignore`**: the three most recent zips are tracked by explicit
      negation. Add the new version, drop the oldest. Gitignore cannot express
      "keep the newest three", so this is manual — and if you forget, the new
      release is silently untracked, which looks identical to a release that
      was never cut.

Then verify before committing — **all seven self-tests**, not a subset:

```
python ds1_util.py
python chassis_config.py
python assurance.py
python gsx_client.py --self-test
python gsx_perfmon.py --self-test
python circuit_audit.py --self-test
python reverse_autotester.py --self-test

git check-ignore -v "!Revisions/Curveball Tools vX.YZ.zip"   # must print nothing
```

`../../RUNBOOK.md` is the owner of verification and covers the live checks
that these do not: the self-tests exercise parsers against captured output,
and every failure in v2.27–v2.33 was in a *lookup* that the self-tests passed
straight through.

The `check-ignore` command is the one that catches the `.gitignore` mistake.
`!` starts a negation pattern in gitignore, so the literal `!` in `!Revisions`
has to be escaped as `\!Revisions`. An unescaped rule is a silent no-op: on
2026-10-01 that pushed 25 release zips (14.34 MiB, 161 files) to the remote.

**The `.gitignore` window only governs what gets added.** Files already
tracked stay tracked regardless, so the repo accumulated v2.14–v2.24 until
they were explicitly dropped with `git rm --cached` (2026-10-01). If the three
most recent zips are genuinely all you want in the repo, check what is
actually there rather than assuming:

```
git ls-files "!Revisions/*.zip"
```
