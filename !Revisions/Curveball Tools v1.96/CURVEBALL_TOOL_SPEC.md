# Curveball Tool Spec — authoring standard

**Purpose:** the exact format a script must follow to plug into the Curveball
launcher. Written to be handed to a person *or* pasted to an AI agent as
instructions. Follow it exactly and the tool drops into the launcher with no
cleanup required.

The launcher auto-discovers tools: it scans its folder at startup, loads every
`.py` that declares a `TOOL_INFO`, and adds it to the menu. Your file just has to
meet the contract below.

---

## Scope & ownership (read this first)

This spec covers **compatibility only** — how to shape a script so it plugs into
the launcher. It does **not** make your tool correct.

- **The tool owner is responsible for their tool working correctly** — that it
  connects to the right thing, runs the right commands, and produces the right
  output. Conforming to this spec does not transfer that responsibility.
- **You are responsible for it being read-only and safe** to run against the
  production network (see the read-only rule below). This is non-negotiable and
  is the one thing that must be true before a tool is shared.
- The launcher maintainer provides the platform (launcher, contract,
  conventions) and integrates conforming tools. The launcher maintainer does not
  debug or guarantee the behavior of individual tools.

In short: **you make it conform and confirm it works; the launcher just runs
it.** If your tool misbehaves, that's owned by you, not the launcher.

---

## Hard contract (all three are required)

A tool module **MUST** define:

1. **`TOOL_INFO`** — a dict with these five keys, used to build the menu entry:
   ```python
   TOOL_INFO = {
       "name": "My Tool",            # menu label
       "ran_from": "1250",           # device/source it runs from (e.g. "1250", "1800", "Local")
       "desc": "One line: what it does.",
       "requires": "1250 IP, creds", # inputs the user must supply
       "order": 70,                  # menu position (existing tools use 10-60; pick an unused number)
   }
   ```

2. **`collect_inputs(session)`** — does ALL prompting; returns a `params` dict
   (or `None` to abort). This is the **only** function allowed to prompt.

3. **`run(params)`** — does the work using `params`. **MUST NOT prompt** — every
   input it needs is already in `params`.

Plus a `main()` + `if __name__ == "__main__"` so the tool also runs standalone.

---

## Hard rules (MUST / MUST NOT)

- **MUST be read-only.** Issue only `show`/`SHOW` commands (and session-only
  paging like `terminal length 0` or `| no-more`). **NEVER** send `set`,
  `configure`, `commit`, `write`, `clear`, `reset`, `delete`, `no ...`, loopback,
  or any command that changes device state. This runs against a live production
  network. A single mutating command is a hard failure of review.
- **`run(params)` MUST NOT call `input()` or `getpass()`.** If it prompts, it
  will hang or double-ask when driven by the launcher. All prompting lives in
  `collect_inputs`.
- **MUST NOT do work at module top level.** No connecting, file I/O, or logic
  that runs on `import`. The launcher *imports* every tool file at startup to
  read its `TOOL_INFO`; anything at module scope executes then. Put all behavior
  inside functions.
- **MUST NOT hardcode credentials or tokens** anywhere in the file.
- **MUST handle errors, not swallow them.** No bare `except:`. Catch specific
  exceptions and print a clear message.
- **SHOULD** write its report to `C:\Claude\<toolname>_<timestamp>_<host>.txt`,
  echo the report to the console, and support the optional JIRA post — matching
  the rest of the suite so output is consistent.

---

## Session keys (for cross-tool reuse)

If `collect_inputs` reads/writes these standard keys, the launcher reuses them
across tools in one session automatically:

| Key | Behavior |
|-----|----------|
| `username`, `password` | infra login — reused **silently** by later tools |
| `gsx_user`, `gsx_pass` | GSX login — reused silently |
| `ip_1250` | remembered; offer as an **editable default** (always shown) |
| `site`, `interface`, `ds1_list` | remembered; offer as editable defaults |

Rule of thumb: **credentials reuse silently; anything that identifies a device is
shown as a default and confirmed** — so a tool never runs against the wrong
device because a value carried over invisibly.

---

## Golden template (copy this, then change the marked parts)

This is a complete, conforming device tool. Clone it and edit the four `# EDIT`
spots. For a non-device tool (e.g. reads a local file), keep the same three-part
structure but drop the client/credentials pieces.

```python
#!/usr/bin/env python3
"""my_tool.py -- one-line description. READ-ONLY. Built by <you>."""

import os
import datetime

try:
    import paramiko  # noqa: F401
    _DEP_ERROR = None
except ImportError as e:
    _DEP_ERROR = "paramiko not installed (pip install paramiko): {}".format(e)

from npt1250_client import NPT1250Client          # EDIT: the client you need
from jira_client import post_report, valid_ticket

# EDIT 1 -- menu metadata
TOOL_INFO = {
    "name": "My Tool",
    "ran_from": "1250",
    "desc": "One line: what it does.",
    "requires": "1250 IP, creds",
    "order": 70,
}


def collect_inputs(session=None):
    """All prompting happens here. Return a params dict, or None to abort."""
    import getpass
    session = session if session is not None else {}

    # device-identifying value -> editable default (always shown)
    default_ip = session.get("ip_1250")
    prompt = "1250 IP" + (" [{}]".format(default_ip) if default_ip else "")
    ip = input(prompt + ": ").strip() or (default_ip or "")
    if not ip:
        print("No IP.")
        return None

    # JIRA (optional) -- keep this block verbatim for consistency
    jira_ticket = None
    if input("Add to JIRA? (y/n) [n]: ").strip().lower() in ("y", "yes"):
        while True:
            jira_ticket = input("JIRA number (NOE-XXXX): ").strip().upper()
            if valid_ticket(jira_ticket):
                break
            if not jira_ticket:
                jira_ticket = None
                break
            print("  Invalid format. Expected NOE-XXXX.")

    open_when_done = input("Open results file when done? (y/n) [y]: ").strip().lower()
    open_when_done = open_when_done in ("", "y", "yes")

    # credentials -> silent reuse if the session already has them
    user = session.get("infra_user")
    pw   = session.get("infra_pass")
    if not (user and pw):
        user = input("Username: ").strip()
        pw   = getpass.getpass("Password: ")
    if not (user and pw):
        print("Missing credentials.")
        return None

    return {
        "ip_1250": ip,
        "username": user,
        "password": pw,
        "jira_ticket": jira_ticket,
        "open_when_done": open_when_done,
    }


def run(params):
    """Does the work. MUST NOT prompt -- everything comes from params."""
    if _DEP_ERROR:
        print("[!] {} -- tool unavailable.".format(_DEP_ERROR))
        return

    ip = params["ip_1250"]
    user = params["username"]
    pw   = params["password"]
    jira_ticket    = params.get("jira_ticket")
    open_when_done = params.get("open_when_done", True)

    c = NPT1250Client(ip, user, pw)
    print("\n[connecting] {}".format(ip))
    try:
        c.connect()
    except Exception as e:
        print("[!] connect failed: {}: {}".format(type(e).__name__, e))
        return
    host = c.hostname
    print("  host: {}".format(host))

    lines = ["MY TOOL", "Host: {}  IP: {}".format(host, ip), "=" * 64]

    # EDIT 2 -- your READ-ONLY commands here
    for title, cmd in [
        ("Example Section", "show version"),
    ]:
        print("  collecting: {}".format(title))
        raw = c.run(cmd + " | no-more")     # EDIT 3: paging suffix if the platform needs it
        lines += ["", "=" * 64, "  " + title, "  CMD: " + cmd, "=" * 64]
        body = [ln.rstrip() for ln in raw.splitlines() if ln.rstrip()]
        lines += ["  " + ln for ln in body] if body else ["  (no output)"]

    c.close()

    # echo to console + write file
    print("")
    print("\n".join(lines))
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = r"C:\Claude\my_tool_{}_{}.txt".format(ts, host)   # EDIT 4: filename prefix
    try:
        with open(out_path, "w") as f:
            f.write("\n".join(lines))
        print("\n[written] {}".format(out_path))
    except Exception as e:
        print("\n[!] write failed: {}".format(e))
        return

    if jira_ticket:
        intro = "My tool -- {} ({}), run {}.".format(
            host, ip, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        ok, msg = post_report(jira_ticket, intro, "My Tool -- {}".format(host),
                              "\n".join(lines))
        print("[JIRA] {}".format(msg) if ok else "[JIRA] FAILED: {}".format(msg))

    if open_when_done:
        try:
            os.startfile(out_path)
        except Exception as e:
            print("[!] could not auto-open: {}".format(e))


def main():
    print("=" * 64)
    print("  MY TOOL (standalone)")
    print("=" * 64)
    params = collect_inputs()
    if params:
        run(params)


if __name__ == "__main__":
    main()
```

---

## Conformance checklist (before handing the file in)

Compatibility — the tool won't slide in without these:
- [ ] File is a single `.py`, dropped in the launcher folder.
- [ ] Has `TOOL_INFO` with all five keys; `order` doesn't collide with existing tools.
- [ ] Has `collect_inputs(session)` (returns dict or None) and `run(params)`.
- [ ] `run(params)` contains **no** `input()`/`getpass()` calls.
- [ ] **No code runs at module top level** (only defs + `TOOL_INFO` + the
      `if __name__` guard).
- [ ] No hardcoded credentials/tokens; no bare `except:`.

Safety — required before sharing (non-negotiable):
- [ ] **Read-only**: every device command starts with `show`/`SHOW`
      (grep the file to be sure). No `set/configure/commit/write/clear/no/...`.

Function — the tool owner's responsibility:
- [ ] It actually does what it's supposed to. Runs standalone first
      (`python my_tool.py`) and produces correct output.
- [ ] Appears in `python launcher.py` and runs end to end.

If the launcher can't load a tool it prints `[skip] <name>: <reason>` and keeps
going — so a malformed tool never breaks the menu for everyone else, but it also
won't appear until it conforms.

**Bottom line:** conforming to this spec gets your tool *into* the launcher.
Whether it works correctly is on you, the tool owner. The launcher runs what it's
given; it doesn't validate your logic.
