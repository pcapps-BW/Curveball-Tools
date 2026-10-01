# Adding a tool to the launcher

The launcher **auto-discovers** tools. To add one, drop your `.py` file into the
same folder as `launcher.py`. There is **no launcher edit** — the next time
someone runs `python launcher.py`, your tool appears in the menu.

Your script needs exactly **two things**.

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

Pick an `order` that doesn't collide with the existing tools (they use 10, 20,
30, 40, 50, 60). Anything works; the menu sorts by it.

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
- **Read-only.** Tools in this suite issue only `show`/`SHOW` (and session-only
  paging like `terminal length 0` / `| no-more`). No config-changing commands.

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

- [ ] File is in the same folder as `launcher.py`
- [ ] Has a `TOOL_INFO` dict (name, ran_from, desc, requires, order)
- [ ] `order` doesn't collide with existing tools
- [ ] Has `collect_inputs(session)` returning a params dict (or None)
- [ ] Has `run(params)` that does **not** prompt
- [ ] Has a `main()` + `if __name__ == "__main__"` so it runs standalone too
- [ ] Read-only (only `show`/`SHOW` commands)
- [ ] Runs cleanly on its own first (`python my_tool.py`) before relying on the launcher

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

Then verify before committing:

```
python ds1_util.py
python gsx_perfmon.py --self-test
python circuit_audit.py --self-test
git check-ignore -v "!Revisions/Curveball Tools vX.YZ.zip"   # must print nothing
```

The last command is the one that catches the `.gitignore` mistake. `!` starts a
negation pattern in gitignore, so the literal `!` in `!Revisions` has to be
escaped as `\!Revisions` — an unescaped rule is a silent no-op, which on
2026-10-01 pushed every release zip from v1.0 to v2.14 (14.34 MiB) to the
remote.
