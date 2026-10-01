#!/usr/bin/env python3
"""
platform_paths.py -- single source of truth for where Curveball Tools reads
and writes its config, state, and report files.

Every tool used to hardcode r"C:\\Claude\\..." directly, in 18 separate
files. That was fine while everything ran on pcapps's own Windows machine,
but broke outright the first time the suite actually ran on real Linux
hardware (noe-util-01a.lab1.bwnet.us, confirmed live 2026-09-11): NetBox and
JIRA both failed because their config file, and every tool's report output
path, is a Windows path that simply does not exist on Linux.

BASE_DIR resolution, in order:
  1. CURVEBALL_HOME environment variable, if set -- explicit override, works
     identically on either OS. Lets a future multi-host deployment point
     everything at a shared location without touching code.
  2. Windows default: C:\\Claude -- UNCHANGED. pcapps's own machine keeps
     working exactly as it always has; nothing to migrate there.
  3. Non-Windows default: ~/.claude -- a per-user directory. Doesn't need
     root, doesn't collide with anything system-owned, and works for
     whatever service account eventually runs this unattended.

Every module that used to write r"C:\\Claude\\..." directly should instead
call platform_paths.path(...) or read platform_paths.BASE_DIR.

Built by P. Capps.
"""

import os

_ENV_VAR = "CURVEBALL_HOME"


def _resolve_base_dir():
    override = os.environ.get(_ENV_VAR)
    if override:
        return override
    if os.name == "nt":
        return r"C:\Claude"
    return os.path.expanduser("~/.claude")


BASE_DIR = _resolve_base_dir()

# Created eagerly, once, at import time -- every module assumes BASE_DIR
# already exists rather than each one separately remembering to mkdir it.
# Silently a no-op if it's already there; never raises here on a read-only
# or permission-denied path -- the caller will find out soon enough when an
# actual file write fails, with a clearer error at the point of use than an
# import-time crash that's harder to trace back to this module.
try:
    os.makedirs(BASE_DIR, exist_ok=True)
except Exception:
    pass


def path(*parts):
    """
    Build a full path under BASE_DIR.

    platform_paths.path("autotester_config.txt")
        -> C:\\Claude\\autotester_config.txt on Windows
        -> ~/.claude/autotester_config.txt on Linux (or CURVEBALL_HOME, if set)
    """
    return os.path.join(BASE_DIR, *parts)
