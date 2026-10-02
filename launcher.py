#!/usr/bin/env python3
"""
launcher.py -- front-door menu for the Curveball standalone tools.

Auto-discovers tools: at startup it scans this folder for any *.py that declares
a TOOL_INFO dict, and builds the menu from them. To add a tool, just drop its
.py in this folder -- no edit to the launcher required.

A tool module must provide:
  1. TOOL_INFO = {"name","ran_from","desc","requires","order"}   (menu metadata)
  2. collect_inputs(session) -> params dict (or None to abort)    (prompting)
  3. run(params)             -> does the work (no prompting)

Files without a TOOL_INFO (shared modules, helpers) are ignored automatically.

SESSION REUSE (within one launcher run):
  - Credentials (infra + GSX): reused silently once entered.
  - Device-identifying values (1250 IP, site, interface, DS1 list): remembered
    and offered by the tool as an editable default -- always shown/confirmed,
    never silently injected.

Each tool also works standalone (its __main__ calls collect_inputs() then run()).

Built by P. Capps.
"""

import os
import glob
import importlib

import env_check
from device_base import CredentialError

# device_base's import already runs this; called again so the banner is the
# first thing on screen in the menu, above the tool list.
env_check.check_all()


def discover_tools():
    """
    Scan this launcher's folder for tool modules (files declaring TOOL_INFO).
    Returns a list of dicts: {name, ran_from, desc, requires, order, module}.
    Robust to bad files: a broken tool is reported and skipped, never fatal.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    tools = []
    for path in sorted(glob.glob(os.path.join(here, "*.py"))):
        modname = os.path.splitext(os.path.basename(path))[0]
        if modname == "launcher":
            continue
        # Cheap pre-filter: only files that opt in via a TOOL_INFO marker are
        # treated as tools. Shared modules/helpers are skipped silently.
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                if "TOOL_INFO" not in fh.read():
                    continue
        except Exception:
            continue
        # It wants to be a tool -> import it; if that fails, say so loudly.
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            print("  [skip] {}: failed to import ({}: {})".format(
                modname, type(e).__name__, e))
            continue
        info = getattr(mod, "TOOL_INFO", None)
        if not isinstance(info, dict):
            continue
        if not (callable(getattr(mod, "collect_inputs", None)) and
                callable(getattr(mod, "run", None))):
            print("  [skip] {}: has TOOL_INFO but is missing "
                  "collect_inputs()/run().".format(modname))
            continue
        tools.append({
            "name": info.get("name", modname),
            "ran_from": info.get("ran_from", "?"),
            "desc": info.get("desc", ""),
            "requires": info.get("requires", ""),
            "order": info.get("order", 999),
            "module": mod,
        })
    tools.sort(key=lambda t: (t["order"], t["name"].lower()))
    return tools


def print_menu(tools):
    print("\n" + "=" * 64)
    print("  CURVEBALL TOOLS -- LAUNCHER")
    print("=" * 64)
    for i, tool in enumerate(tools, 1):
        print("\n  {}) {}".format(i, tool["name"]))
        print("      Ran From: {}".format(tool["ran_from"]))
        print("      Function: {}".format(tool["desc"]))
        print("      Requires: {}".format(tool["requires"]))
    print("\n  q) Quit")
    print("=" * 64)


def dispatch(tool, session):
    """Call the tool's collect_inputs(session) then run(params); harvest session."""
    mod = tool["module"]
    params = mod.collect_inputs(session)
    if not params:
        print("  (cancelled)")
        return

    # Harvest reusable values for later tools.
    #   credentials -> silent reuse;  device values -> editable defaults
    if params.get("username"):
        session["infra_user"] = params["username"]
    if params.get("password"):
        session["infra_pass"] = params["password"]
    for k in ("infra_user", "infra_pass", "gsx_user", "gsx_pass",
              "ip_1250", "site", "interface", "ds1_list"):
        if params.get(k):
            session[k] = params[k]

    try:
        mod.run(params)
    except CredentialError as e:
        print("\n  [!] {}".format(e))
        if e.cred_type == 'gsx':
            session.pop("gsx_user", None)
            session.pop("gsx_pass", None)
            print("  GSX credentials cleared -- you will be prompted again on the next run.")
        else:
            session.pop("infra_user", None)
            session.pop("infra_pass", None)
            print("  Infrastructure credentials cleared -- you will be prompted again on the next run.")


def main():
    tools = discover_tools()
    if not tools:
        print("No tools found. Make sure the tool .py files are in this folder "
              "alongside launcher.py.")
        return

    session = {}   # remembered values across tools this launcher session
    while True:
        print_menu(tools)
        choice = input("\nSelect a tool (1-{} or q): ".format(len(tools))).strip().lower()
        if choice in ("q", "quit", "exit"):
            print("Bye.")
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(tools)):
            print("  Invalid selection.")
            continue
        tool = tools[int(choice) - 1]
        print("\n--- {} ---".format(tool["name"]))
        dispatch(tool, session)
        again = input("\nRun another tool? (y/n) [y]: ").strip().lower()
        if again in ("n", "no"):
            print("Bye.")
            return


if __name__ == "__main__":
    main()
