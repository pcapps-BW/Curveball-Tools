# Team Onboarding — using your Claude agent on Curveball Tools

You're a collaborator on the repo. This is the high-level process for getting
your own Claude (Cowork or Claude Code) set up to work on the tools, and the
standing rules to give it so changes land safely.

Curveball Tools runs live against production transport gear. The single most
important thing: **Claude here reads device state and builds code; a human
approves anything that touches a device beyond a `show`/`ping`.**

---

## 1. One-time setup (per person)

1. **Accept the GitHub invite and clone the repo** (GitHub Desktop is fine).
   The repo is the `Curveball Tools` folder.
2. **Install the skill.** Ask the maintainer (pcapps) for
   `curveball-circuit-intel.skill` — it carries the network knowledge (topology,
   platforms, CLLI/market naming, device command quirks, Jira lookup workflow).
   Open it and click **Save skill**. This is NOT in the repo, so you have to get
   it from the maintainer, not from `git clone`.
3. **Point Claude at the repo.** In Cowork, connect the cloned `Curveball Tools`
   folder. In Claude Code, open a session in that directory.
4. **Dependencies:** `pip install -r requirements.txt`. Note the pin
   `paramiko>=3.5,<5` — paramiko 5 removed the SHA-1 algorithms the Cisco MUX
   needs, so don't "upgrade" past it.
5. **First thing, every session:** tell Claude to read the repo's `README.md`
   and `ADDING_A_TOOL.md` before touching anything. (The deeper `CONTEXT_PRIMER.md`
   / `RUNBOOK.md` live in the maintainer's `C:\Claude\` above the repo — ask for
   them if you want the full loader; the skill + README + ADDING_A_TOOL cover
   day-to-day work.)

---

## 2. The standing prompt to give your Claude

Paste something like this at the start of a work session (edit the path to where
you cloned it):

> You're working on Curveball Tools, a NOC tooling suite in this folder. Before
> changing anything, read `README.md` (architecture + the tools) and
> `ADDING_A_TOOL.md` (the tool contract and the release procedure). This runs
> against LIVE production transport gear. Follow these rules:
> - **Read-only on devices.** Only `show`/`SHOW`/`ping` and session-only settings
>   (`terminal length 0`). Never commit anything to a device config. Anything
>   intrusive is printed as copy/paste runbook text for me to run by hand — stop
>   and ask before generating it.
> - **Never guess device output format.** If you need to parse a command you
>   don't have a real sample of, ask me to paste real output first. A
>   plausible-looking wrong answer is the failure mode this suite is built to
>   avoid.
> - **Two sources, compared.** Any fact reachable two ways gets checked both
>   ways and the disagreement surfaced. Name where every value came from.
> - **Never render "clean" when a command failed.** Say what couldn't be read.
> - **Test and release the project's way:** run the off-box `--self-test`s, then
>   follow `ADDING_A_TOOL.md` to cut a release (bump `APP_VERSION`, add a
>   CHANGELOG entry, build the zip, update the `.gitignore` window).
> Tell me the plan before you build, and confirm with me before any git push.

The installed **curveball-circuit-intel** skill triggers on its own for network
questions (a circuit, a CLLI site, a 1250/1800/MUX/GSX hop, an NOE Jira ticket),
so Claude will pull that knowledge in without being told.

---

## 3. The update / change loop (high level)

1. **You describe the change** in plain terms ("the GSX PM line is misreading X",
   "add a check for Y").
2. **Claude reads** the README / ADDING_A_TOOL / the relevant module's docstrings
   (the docstrings are the real documentation — they record why a parser is the
   way it is and the date a format was confirmed live).
3. **Claude proposes a plan**, flags risk, and — if it needs to parse new device
   output — asks you for a real paste first.
4. **Claude implements**, following the existing patterns (new tools copy the
   shape of an existing one; a new device platform gets a thin `device_base`
   client like `bcr_client.py`).
5. **Claude runs the off-box self-tests** (`python <module>.py --self-test`) —
   they need no credentials and touch no devices.
6. **Claude cuts a release** per `ADDING_A_TOOL.md`: `APP_VERSION` bump, CHANGELOG
   entry, zip into `!Revisions/`, `.gitignore` edited to track only the newest
   three zips.
7. **You review, then sync git** (see below).
8. **First live run proves it.** A tool that passes self-tests is "built, not
   proven" — run it live once and feed Claude the output; new device parsing
   almost always needs one real-output pass to confirm.

---

## 4. Git — the sync, and the one gotcha

From the `Curveball Tools` folder, run the three as ONE line so the order can't
flip (pasting them on separate lines in PowerShell has reordered them and pushed
nothing):

```powershell
git add -A ; git commit -m "your message" ; git push
```

Post-checks:

```powershell
git status -sb                      # want: "## main...origin/main", no [ahead N]
git rev-parse HEAD ; git rev-parse '@{u}'   # the two hashes should match
git ls-files "!Revisions/*.zip"     # only the newest three zips
```

If `git status -sb` shows `[ahead 1]`, the push didn't land — run `git push`
again. Coordinate on a branch/PR flow with the maintainer if more than one of
you is changing things at once, so you're not committing over each other.

---

## 5. Non-negotiables (restate these to Claude if it drifts)

- Live production traffic — every device interaction affects a customer.
- No permanent device config changes without explicit human discussion and
  approval. The one opt-in intrusive action (GSX PM counter reset) is default
  off and destroys that T1's PM history — leave it off.
- Credentials are prompted at runtime, never written to scripts or committed.
- When in doubt, Claude reports what it couldn't prove rather than guessing — and
  the human decides.
