#!/usr/bin/env python3
"""
bcr_client.py -- Cisco IOS-XR backbone core router (BCR) client.

The BCR (`bcr01.<site>`, one per hub) sits above the NPT-1800 toward the core
and peers BGP with the 1800 over a BVI/Bundle-Ether/TenGigE stack (see
Neptune_BGP_reference.md, BCR edge section). This is the suite's FIRST IOS-XR
client.

Why it's a thin subclass
------------------------
IOS-XR (ASR9k-class) offers modern SSH algorithms, so unlike the Cisco NCS 4216
MUX it needs NO legacy-algorithm transport -- the default paramiko connect in
BaseDeviceClient works. Only two things differ from the NPT clients:

  * the prompt ends with '#' (NPT ends with '>'), and it carries the
    RP location prefix: `RP/0/RSP0/CPU0:bcr01.jfk1#`
  * paging is disabled with `terminal length 0` (IOS-XR has no `| no-more`)

Read-only. This client runs `show` commands only; nothing here configures the
BCR. IOS-XR echoes a timestamp line (e.g. "Thu Nov  5 16:19:15.174 UTC") after
each command -- harmless, parsers ignore it.

Built by P. Capps.
"""

import re

from device_base import BaseDeviceClient


class BCRClient(BaseDeviceClient):
    # IOS-XR prompt: "RP/0/RSP0/CPU0:bcr01.jfk1#". Match the ":<hostname>#"
    # tail -- the hostname sits after the final colon, before the '#'. Anchored
    # at end so a '#' mid-output (e.g. in a banner) is not mistaken for the prompt.
    PROMPT_RE = re.compile(r':[\w.\-]+#\s*$')
    CONN_TIMEOUT = 45

    def _post_login(self):
        # Disable paging for the session (session-only; IOS-XR has no | no-more).
        self.run("terminal length 0")
