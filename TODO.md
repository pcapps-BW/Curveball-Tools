# Curveball Automation -- TODO / Planned Work

## Scripts

### Batch_Ckt_Layout_w_PMs.py / Any script pulling 1800 PMs
- [ ] Add `show chassis transceiver tca slot ts2` (and ts5 where present)
      to 1800 PM collection -- gives Tx/Rx optical thresholds to compare
      against live power readings already being collected

### All PM Scripts
- [ ] Progress indicator -- terminal output was abandoned due to CLI
      interpreting echo as a command. Revisit with a non-CLI approach
      (e.g. updating the SecureCRT tab title, or a non-blocking status window)

### GSX Integration
- [ ] Add GSX CLI hop to route tracer once access is granted
      GSX commands known: SHOW T1 T1-1-X-X-X STATUS / PERFMON CURRENT FIFTEENMIN
      Currently HOP 4 displays assignment only -- no live stats

### 1800 Discovery Script
- [ ] Build NPT-1800 discovery script (equivalent of 1250_discovery.py)
      Required before building NPT-1800_reference.md
      Planned for next device reference session

### Cisco NCS 4216 Discovery Script  
- [ ] Build 4216 discovery script
      Required before building NCS4216_reference.md

## Reference Docs

- [ ] NPT-1800_reference.md -- pending 1800 discovery script + output
- [ ] NCS4216_reference.md -- pending 4216 discovery script + output
- [ ] context.md -- top-level session loader, build after all device refs done

## Database of Record (separate chat)
- [ ] Design schema for Curveball circuit inventory database
      Fields: device hostname, loopback IP, DS1 interface, MUSE ID,
      hub IP, OC12 assignment, MUX name, ACR group, CEM group,
      GSX node, GSX assignment
- [ ] ~200 NPT-1250 devices to inventory
- [ ] Build scan/parse script to populate DB from discovery output
- [ ] Build PM script that queries DB instead of building route each time
      (skip 1800 MUSE lookup -- use stored OC12 assignment directly)

## Neptune BGP (knowledge captured 2026-11-02)

Reference is in `C:\Claude\Neptune_BGP_reference.md` (prefix-set change
procedure + the standard-vs-non-standard decision rules). Two future items
feed off it, build when the time comes:

- [ ] **Read-only Neptune BGP tool** — the three read commands
      (`show run prefix-set`, `show bgp neighbor <peer> advertised-routes`,
      `... routes`), parsed. Blocked on: a real Neptune paste to confirm
      prompt/paging/output format before writing any parser.
- [ ] **Feed the standard-change rules into #6 (SEARN)** — auto-flag a change
      that touches a private IP or an infrastructure circuit as NON-standard.

**Strategy note:** pcapps is accumulating reference material so either tool
can be built later without re-deriving the knowledge. Keep capturing pasted
reference (screenshots, CLI output) into `C:\Claude\*_reference.md` files —
durable across sessions, which conversation context is not.

## Loop checks (#3) -- remaining coverage

Done 2026-11-02 (v2.49): 1250 TDM `maintenance loopback` and GSX T1
`Loopback Status` -- both grounded on real output, wired into the autotester
and ds1_pms. `loop_check.py` is the shared module.

Remaining, parser built but not wired / not grounded on live output:
- [x] **MUX loopback** -- DONE v2.50. The standard Cisco controller loopback
      (`loopback local|network|diag`) is grounded on the NCS 4200 CEM config
      guide and detected from the controller config the reverse autotester
      already pulls. `mux_controller_loops()`. The bespoke hairpin detector
      (`mux_hairpin_from_connect`) is kept as a secondary check; it is still
      confirm-live and unwired, since the hairpin MOP has never been run.
- [ ] **GSX**: covered (Loopback Status field). pcapps was right it is just a
      field in a command we already pull.
- [ ] **Fiber/GE loops** (1250 ge-tsX/1, 1800 10gig) -- not built. A looped
      fiber uplink is high-impact (could down the site), so detecting one is
      worth it, but the config/status fingerprint is unconfirmed -- needs a
      sample. Lower priority (pcapps: "highly unlikely").

## Known Issues / Watch List
- [ ] **`ge-ts2/1` is reported as a fiber uplink at dual-1250 markets, where
      it is actually the local A-B cross-connect.** Found 2026-10-02 while
      scoping the SoR; deferred by pcapps (3-4 markets affected, fix touches
      three shipped tools, not worth the spend yet).

      Three places hardcode the pair:
        fiber_util.py:38              FIBER_PORTS = ["ge-ts2/1", "ge-ts5/1"]
        site_sweep.py:117             ("ge-ts2/1", "ts2"), ("ge-ts5/1", "ts5")
        autotester_standalone.py:366  ("ge-ts2/1", "ts2"), ("ge-ts5/1", "ts5")

      All three render it under "GE FIBER UPLINK HEALTH", so at a dual site
      "the uplink looks healthy" can be describing a cable between two
      devices in the same rack, while the real transport is only on
      ge-ts5/1.

      NOT uniformly wrong, which is why it survived: circuit_audit.py:181
      records `ge-ts2/1  SFP (1GE)  Nitel: NIT555057` -- a real carrier
      circuit on that port at a single-1250 site. So the port is a genuine
      uplink at some sites and an xcon at others.

      Only available test: the hostname suffix (-A/-B present => xcon).
      There are NO interface descriptions on these ports, so the stronger
      description-based test is not available.

      When fixed: LABEL the port rather than silently reclassifying it, and
      a port whose role cannot be determined should say so rather than
      defaulting to "uplink". Confirm against a real dual site first.
      Blast radius is GE uplink reporting only -- DS1 tracing, the SoR
      fields and everything hub/MUX/GSX-side are unaffected.

- [ ] Rotate the Morning Check Slack webhook URL -- it got pasted into a
      chat session on 2026-08-06, so treat it as loosely exposed even
      though a webhook can only post to that one channel. Low priority --
      regenerate via the Slack API app's Incoming Webhooks page (revoke
      the existing one / reinstall the app to get a fresh URL), then
      update it wherever it's stored (Morning Check form / keyring).
- [ ] NTP server flapping in NE events log on 1250s -- not a script issue,
      escalate to systems team (months outstanding)
- [ ] CES PMs disabled by default on DS1s due to CPU load -- enable only
      for troubleshooting, disable after. Commands documented in
      NPT-1250_reference.md
- [ ] Peerless slot list (ts20, ts21, ts24) -- secondary check via
      "customer Peerless" config attribute now implemented as future-proof
      fallback. Update slot list if new Peerless NNI slots are confirmed.

---

## Autotester -- Major Fixes (from _Claude_To_Do.docx, uploaded by pcapps)

### 1) MUX assignment logic is wrong -- needs Netbox API
Current script incorrectly matches OC12 to SONET interface. Example: 1800 hub
rides oc12-ts2/1, which Netbox shows connects to Cisco 4216 interface 0/3/5 --
not 0/5/7 as the script currently outputs. 0/5/7 is actually associated with
oc12-ts1/1. The script's ACR-group-guessing fallback logic is not reliable
enough -- needs to pull the real MUX-to-1800 interface mapping from Netbox API.
- pcapps has Netbox user ID + API key ready
- Need to update the script's credential handling to also store/use Netbox creds
- Also add MUX IP address to the output (currently missing)

### 2) 1800 hop -- wrong interface shown as 1250-facing
Script currently shows the OC12 interface (e.g. oc12-ts2/1:11:10) as facing
the 1250. This is incorrect. The interface facing the 1250 is always in the
format `xe-tsX/X.xxx` (occasionally 4-digit sub-interface). The sub-interface
number matches the LATA -- the 3-digit number at the end of the 1250 hostname.
- Fix: identify and display the xe-tsX/X.xxx interface as 1250-facing
- Move this interface to the "bottom" of the 1800 block (facing 1250 = bottom,
  facing MUX = top) to match physical direction convention

### 3) MUX hop -- reposition GSX-facing interface
Move the GSX-facing interface line to appear ABOVE the "MUX -- XXX1-MUX-0X"
line, so it's visually on the correct "side" of the MUX matching the physical
signal direction (GSX side = top, 1800 side = bottom).

### 4) GSX CLI integration -- new build
Need Netbox API integration to resolve GSX 9000 management IPs.
- Each GSX has 2 cards (e.g. "MGMT-1-1-X" and "MGMT-1-2-X"), 2 IPs per card
  (4 total), only 2 reachable at a time (active working card)
- Script must: identify GSX name -> query Netbox API for the 4 possible IPs
  -> ping to determine which 2 are active -> SSH into the active one
- Device: Sonus Networks GSX9000HD, software V11.00.02 F001
- Need help identifying correct show commands for PMs and status
  (up/dn, RED/EL, ES/SES/UAS/etc.) -- pcapps has GUI access, zero CLI experience
- Once built: add GSX as a full hop in Diagnostic Summary with the same
  fault-isolation logic as the other hops (1250/1800/MUX)

### 5) Full Route section cleanup (Hops 1-4)
- Hop 1 (1250): OK as-is, no changes
- Hop 2 (1800): Add the 1250-facing xe-x/x.xxx interface, positioned ABOVE
  the "Hub" line (matching physical direction -- see item 2)
- Hop 3 (MUX): Same treatment -- move 1800-facing interface ABOVE the "MUX" line
- Hop 4 (GSX): Remove the "T1 Command" line entirely

---

## Open Question -- Needs Investigation
- [ ] ts3 stats collection reportedly "fails silently" in Autotester --
      no error shown, no output. Need actual output/error capture from a
      failed run to diagnose (which function, which command, what's returned).

---

## Concrete Examples (from screenshots, supplements items 1-5 above)

### Item 1 -- MUX assignment example
- 1800 hub rides oc12-ts2/1
- Netbox "Connection" column for that row shows ACR 3 -> physical MUX
  interface 0/3/5 -- NOT 0/5/7
- 0/5/7 is actually tied to oc12-ts1/1 (different OC12 entirely)
- Device example: jfk1-mux-02 (Netbox device FOX2121PD8Y)
- Confirms: script's current ACR-guessing fallback produces wrong SONET/ACR
  pairing when multiple OC12s exist on one MUX -- must pull real mapping
  from Netbox interface table (Connection column), not guess from show acr group

### Item 2 -- 1800-facing-1250 interface example
- Command to get it: `show interfaces description | grep <LATA>`
  Example: `show interfaces description | grep 328`
  Output: `xe-ts16/3.328   up   up   DYTNOH22`
- Confirms LATA = last 3 digits of 1250 hostname, matches the xe sub-interface
- This xe-tsX/X.LATA interface goes at the BOTTOM of the 1800 block (facing 1250)
- The oc12/ocX interface stays at TOP of 1800 block (facing MUX)

### Item 4 -- GSX Netbox example
- Example device: NYCGSX04.jfk1
- Relevant interfaces from Netbox (Interfaces tab):
  - MGMT-1-1-1 -- 192.168.35.14/24
  - MGMT-1-1-2 -- 192.168.36.14/24
  - MGMT-1-2-1 -- 192.168.35.15/24
  - MGMT-1-2-2 -- 192.168.36.15/24
  - (also present but not relevant to mgmt: BITS1/2-MTA1/2, GENET-1-3/4-1/2,
    GW Sig Port)
- Only one card's pair is reachable at a time (1-1 or 1-2, active/protect) --
  script must ping to determine which pair is live before SSH
- GSX COMMANDS.txt referenced by pcapps but not yet uploaded -- follow up
  needed to get actual show command list for GSX9000HD CLI

### Item 5 -- Full route example (for reference on formatting)
Example full route block (JFK site, ts1/9):
```
HOP 1 -- NPT-1250: Host DYTNOH22328, Loopback 10.111.130.73, DS1 ts1/9, MUSE MUSE_4834
HOP 2 -- NPT-1800 Hub: Hub JFK-1800, IP 10.111.130.255, OC12 oc-ts3/1:8:9
HOP 3 -- Cisco NCS 4216 MUX: MUX JFK1-MUX-02, IP 192.168.246.19, Active Path SONET 0/5/7,
         ACR Group 16, CEM Group 809 (VTG 3, T1 1), GSX CE CE8/1/40:109
HOP 4 -- GSX: Node NYCGSX03, Assignment 15-2-9  [T1 Command line to be REMOVED]
```

### GSX9000 Commands -- confirmed syntax (from GSX_COMMANDS.txt, pcapps)

Format: `SHOW T1 T1-{shelf}-{slot}-{port}-{ds1} <subcommand>`
Example device: NYCGSX03, T1-1-15-2-9 (shelf 1, slot 15, port 2, DS1 9)

1. **`SHOW T1 T1-1-15-2-9 STATUS`**
   Returns: Operational Status (UP/DOWN), Alarm Status (NOALARM/etc.),
   Loopback Status, enabled/allocated channel map.
   -- This is the primary up/down + alarm check, equivalent to physical
      status on the 1250.

2. **`SHOW T1 T1-1-15-2-9 STATISTICS`**
   Returns: cumulative RED/AIS/RAI alarm counts, LOF/AIS/RAI defect counts,
   controlled slips, frame bit errors, CRC errors, valid PM interval counts.
   -- This is lifetime/cumulative counters, good for a quick historical
      trouble check but not interval-based.

3. **`SHOW T1 T1-1-15-2-9 PERFMON CURRENT FIFTEENMIN`**
   Returns: current 15-min interval PMs (path/line level, near/far end):
   Code Violations, Errored Seconds, Severely Errored Seconds, SEF/AIS
   Seconds, Controlled Slip Seconds, Unavailable Seconds, Failure Count.
   -- Equivalent to the 15min PM pulls we already do on 1250/1800.

4. **`SHOW T1 T1-1-15-2-9 PERFMON CURRENT DAY`**
   Same fields as #3 but for the current day interval (24hr equivalent).

5. **`SHOW T1 T1-1-15-2-9 PERFMON THRESHOLDS`**
   Returns configured Minor/Major/Critical alarm thresholds for both 15-min
   and 24-hr intervals, plus TCA (Threshold Crossing Alert) reporting
   enable/disable state. pcapps unsure of exact use -- my read: these are
   the trigger levels that would generate a TCA alarm if PM counters exceed
   them. Useful for context but TCA reporting is DISABLED in this example,
   so likely low priority for Autotester unless TCA reporting is enabled
   fleet-wide.

6. **`CONFIGURE T1 T1-1-15-2-9 PERFMON RESET COUNTERS ALL`**
   Clears counters on the DS1. THIS IS A WRITE COMMAND -- never run
   automatically. Read-only commands (1-5) only for Autotester.

**For Autotester GSX hop, recommend running commands 1, 3, and 4** (Status,
PERFMON 15min, PERFMON current day) as the baseline set -- mirrors the
PM pattern already used for 1250/1800. Command 2 (cumulative Statistics)
optional add if useful for trend context. Command 5 (Thresholds) probably
skip unless TCA becomes relevant later. Command 6 excluded entirely --
destructive, human-only.

---

## GSX Build -- Confirmed Decisions & Scope (session 2026-07-07)

**SSH access:** pcapps now HAS SSH/CLI access to the GSX (previously GUI only).

**Credentials:** GSX uses DIFFERENT login than 1250/1800/MUX. Decision = OPTION 2:
prompt for GSX username + password at RUNTIME (masked password), same security
posture as the existing device-login prompts. NOT stored in config file.
Rationale: GSX creds are direct production device access -- higher-value secret
than an API token -- and pcapps deliberately keeps device logins as prompts.

**Which scripts get GSX stats:** Circuit Layout with PMs + both Autotester scripts
(i.e. any script pulling PMs from 1250/1800/MUX). Route-only script does NOT.
EXCLUDE Peerless/customer circuits entirely (no MUX/GSX exists for those).

**Commands to run (from GSX command sheet):** user wants ALL provided commands
run EXCEPT the destructive reset (#6). Baseline: STATUS (#1), STATISTICS (#2),
PERFMON CURRENT FIFTEENMIN (#3), PERFMON CURRENT DAY (#4), PERFMON THRESHOLDS (#5).
Command #6 (CONFIGURE ... RESET COUNTERS) excluded -- destructive, human-only.

**Build sequence (do NOT bolt in all at once -- step by step):**
1. GSX credential prompt at runtime (option 2)
2. Netbox lookup for 4 mgmt IPs -> ping to find live pair -> SSH in
   (reuse query_netbox pattern; MGMT-1-1-X / MGMT-1-2-X interfaces)
3. ONE command first (STATUS), proven on one circuit
4. Then layer in STATISTICS + PERFMON 15min + PERFMON DAY (+ THRESHOLDS)
5. GSX fault-isolation logic in diagnostic summary (full path GSX->1250 visibility)
6. Exclude Peerless/customer circuits

**Prerequisites before building (pcapps to provide):**
- Actual GSX SSH login prompt/flow (what the prompt looks like)
- Whether GSX needs a paging command like the MUX's "terminal length 0"
- Confirm this comes AFTER Netbox fix is validated on Maryville ts3/8

---

## GSX PROVER -- COMPLETED & PROVEN (session 2026-07-07)

**Status: WORKING END TO END.** Standalone `gsx_prover.py` pulls all 5 commands
(STATUS, STATISTICS, PERFMON 15MIN, PERFMON DAY, THRESHOLDS) from LAXGSX04 for
the Maryville test circuit (T1-1-11-2-14) and writes a timestamped .txt.

**Key discoveries (the hard-won facts -- do NOT re-derive):**
- GSX (Sonus/Ribbon GSX9000HD, "Mocana SSH") offers ONLY legacy crypto:
  kex diffie-hellman-group14-sha1/group1-sha1, hostkey ssh-dss (512-bit DSA),
  ciphers aes###-ctr, mac hmac-sha1.
- paramiko CANNOT do the GSX: paramiko 4.0+ REMOVED DSA entirely; even paramiko
  3.5.1 fails because cryptography 49.0.0 rejects the 512-bit DSA key
  ("p must be exactly 1024/2048/3072/4096 bits").
- SecureCRT CANNOT automate GSX login (keyboard-interactive auth; /PASSWORD
  connect-string uses password auth -> the "Enter Secure Shell Password" dialog
  blocks; Screen.Send can't fill a protocol-layer dialog).
- SOLUTION = PuTTY plink (v0.84). Handles legacy crypto natively AND takes
  -pw on the command line. Drive it INTERACTIVELY via subprocess pipes.
- GSX does NOT support one-shot `plink host "CMD"` -- needs interactive shell.
  Flow: connect -> banner -> "LAXGSX04>" prompt -> send cmds -> "exit".
- HANG BUG (fixed): readline() blocks on the newline-less "LAXGSX04>" prompt.
  FIX = background thread reading ONE BYTE at a time into a locked buffer,
  detect prompt directly. Drop the "Press Return" wait (doesn't come via pipe).
- Pager "--- Press <space>... <c>ontinuous... <q>uit ---" answered with 'c'.
- Minor cosmetic TODO: stray "---  ---" fragments where pager regex partial-
  matched. Harmless; tighten regex at integration.
- Env confirmed: Python 3.14.5, plink 0.84, requests 2.34.2 all present.
  GSX creds prompted at runtime via getpass (option 2, NOT stored).

---

## CONFIRMED SSH MAP -- ALL 4 DEVICES (session 2026-07-07, via ssh -vv)

Ground truth from `ssh -vv` against the real devices (NOT vendor docs):

- **NPT-1250**: OpenSSH connects with DEFAULTS. -> paramiko, NO special handling
  (near-certain; confirm with a paramiko connect-test first).
- **NPT-1800**: OpenSSH connects with DEFAULTS. -> paramiko, NO special handling
  (near-certain; same confirm step).
- **Cisco NCS 4216 MUX**: needs TWO legacy algos re-enabled, both still IN
  paramiko:  kex diffie-hellman-group14-sha1  +  hostkey ssh-rsa.
  Confirmed working via:
    ssh -oKexAlgorithms=+diffie-hellman-group14-sha1 -oHostKeyAlgorithms=+ssh-rsa pcapps@10.243.65.4
  MUX hostkey is ssh-rsa (NOT DSA) -> no GSX-style wall. Prompt = "#".
  Has an authorization banner before the password prompt -- read past it.
- **GSX**: plink (see above). ssh-dss removed from paramiko -> plink is the
  ONLY viable path. ALREADY BUILT & PROVEN.

Caveat: OpenSSH connecting != paramiko guaranteed (slightly different defaults).
Lead the port with a cheap paramiko 1250 connect-test to retire that last bit.

---

## FULL STANDALONE PORT -- ORDERED BUILD CHECKLIST (start here next session)

Goal: ONE launch -> prompt for creds -> walk full route (1250/1800/MUX/GSX)
-> ONE combined .txt. Run ALONGSIDE the working SecureCRT scripts during
transition; do NOT retire SecureCRT until output validated identical.

Credentials note: check whether 1250/1800/MUX SHARE one login (likely -- same
NOC gear). If so, prompt once for "infrastructure" creds + once for GSX = 2
pairs, not 4. Confirm before building the prompt block. getpass, masked.

**[ 1250 -- DO THIS FIRST, AM ]**
1. Cheap paramiko connect-test to a 1250 (byte-diagnostic style, like the GSX
   one). Prove paramiko logs in + runs ONE command. Retires last transport risk.
2. Port the 1250 hop: convert crt.Screen.Send/ReadString/Dialog + ConnectInTab
   to a paramiko client. Prompt "> " prompt, "| no-more" paging, banner read.
3. Prove the ported 1250 hop produces the SAME output as the SecureCRT version
   for the Maryville circuit before moving on.

**[ 1800 ]**
4. Port 1800 hop -- same paramiko pattern as 1250 (likely near-identical).
   Keep the slow MUSE lookup for now (optimization is separate TODO).

**[ MUX ]**
5. Port MUX hop -- paramiko WITH the two legacy lines (dh-group14-sha1 kex +
   ssh-rsa hostkey; disabled_algorithms={} + set kex/key prefs on transport).
   Prompt "#", "terminal length 0" paging, read past authorization banner.

**[ GSX ]**
6. Wire in the already-built plink GSXClient. Node/slot/port/T1 come FROM the
   MUX hop in-memory (T3 description + CEM ID) -- NOT manual entry. 1800 host
   suffix -> Netbox device name -> mgmt IPs -> ping -> plink pull.
7. Exclude Peerless/customer circuits from GSX (no MUX/GSX for those).

**[ ASSEMBLE ]**
8. Combine all hops into single output .txt (diagnostic summary + run summary +
   per-DS1 PMs + full route). Reuse existing output-builder logic; only the I/O
   layer changed.
9. Port Netbox (use requests, drop PowerShell) and JIRA integration.
10. Tighten GSX pager regex (kill stray "---  ---").

Reusable seed components already built in gsx_prover.py: Config (reads
autotester_config.txt), NetboxClient (requests), GSXClient (plink+pager),
ping-to-find-live-card. The port IMPORTS/reuses these.

Est: a few focused sessions. Do device-by-device with a live-gear test after
each -- never debug all four at once. Transport risk is retired; remaining work
is methodical I/O-layer porting, not saga-hunting.

---

## SESSION 2026-07-08 (AM/PM) -- STANDALONE PORT PROGRESS

### DONE & VALIDATED this session
- **Consolidated onto a shared base class.** device_base.py = BaseDeviceClient
  (byte-reader thread, prompt detection, run(), hostname capture, ANSI strip,
  POSITIONAL echo strip -- drops first non-empty output line as the cmd echo).
- **1250 ported + tested 100%** (npt1250_client.py) -- NPT1250Client(BaseDeviceClient).
  paramiko plain connect, ">" prompt, "| no-more". query_batch + query_ds1_pms.
- **1800 ported + tested** (npt1800_client.py) -- same base, adds loc_code capture.
  query_batch = MUSE->OC12 + OC12 stats (7-tuple, identical to SecureCRT). Slow
  MUSE lookup timeout scales max(120, n*120). Connection proven w/ interface_check cmds.
- **MUX ported + tested** (mux4216_client.py) -- MUX4216Client(BaseDeviceClient)
  with LEGACY-ALGO transport override: _connect_transport builds paramiko.Transport
  with disabled_algorithms={}, pins kex diffie-hellman-group14-sha1 + key_types
  ssh-rsa, auth_password, opens shell via _open_shell (get_pty+invoke_shell from
  the manual transport). "#" prompt, "terminal length 0" post-login. query_batch
  = full query_4216_batch port INCLUDING the Netbox reconciliation (ts3-bug fix).
  Validated on Maryville: SONET 0/5/7 ACR 16 CEM 1114 (VTG4 T1 2), GSX LAXGSX04-11-2-14,
  facility alarm 0/5/7.11/4/2 LP_AIS -- EXACT match to known-good.
- **Netbox client** (netbox_client.py) -- requests-based (no PowerShell). oc_to_sonet
  + gsx_mgmt_ips + derive_gsx_device_name + config loader.
- **GSX client** (gsx_client.py) -- plink, extracted + importable. pull_t1_stats.

### GSX HOST-KEY -- SOLVED (Tier 1 -hostkey pinning)
Root cause of all the host-key hangs: **plink 0.82+ ignores piped y/n for the
host-key prompt by default** (the -legacy-stdio-prompts change). NOT a timing issue.
SOLUTION (working, all 5 test steps PASS via gsx_hostkey_test.py):
  - capture_fingerprint(): throwaway plink WITH -legacy-stdio-prompts so it reads
    our answer; background-thread byte reader (no deadlock); parse "SHA256:..."
    from the reported "ssh-dss 512 SHA256:..." line; answer 'n' (don't registry-
    cache; we pin instead); kill.
  - real connect: -hostkey SHA256:... + -batch (silent pin, can't hang).
  - Tier 1 store: C:\Claude\gsx_hostkeys.json (TOFU first contact; CHANGE DETECTION
    -- refuses if device presents a different key than stored). Upgradeable to
    Tier 2 (Netbox-sourced known-good fps) by pre-populating the JSON.
  - -hostkey wants the bare "SHA256:..." token (confirmed vs plink manual), not
    the full "ssh-dss 512 SHA256:..." string; we store full, pass the token.

### ORCHESTRATOR -- Stage A (autotester_standalone.py)
One launch -> prompt creds ONCE (infra shared for 1250/1800/MUX + separate GSX)
-> walk 1250 -> 1800 (grouped by hub) -> MUX (grouped by loc, Netbox reconcile)
-> GSX (non-Peerless, assignment FROM MUX in-memory) -> RAW combined .txt.
- Peerless path PROVEN (ts1/27,28 on FRSNCA01728): 1250->1800 flowed, MUX/GSX
  correctly SKIPPED for customer circuits.
- Full-route path (MUX+GSX handoff) = the run in progress as of this note.
- Credentials confirmed: 1250/1800/MUX SHARE one login; GSX separate. 2 prompt pairs.

### FILE INVENTORY (all in the !Consolidation folder together)
device_base.py, netbox_client.py, npt1250_client.py, npt1800_client.py,
mux4216_client.py, gsx_client.py, autotester_standalone.py (orchestrator),
gsx_hostkey_test.py (host-key test). Plus store: C:\Claude\gsx_hostkeys.json.

### REMAINING
- [x] Stage A final proof: DONE. Maryville ts3/8 on 10.111.131.67 flowed ALL
      four hops in one launch. GSX host-key fix worked in-pipeline (no hang);
      MUX->GSX in-memory handoff worked (GSX ran T1-1-11-2-14 derived from the
      MUX T3 desc, nothing typed). All values matched known-good. STAGE A COMPLETE.
- [ ] STAGE B: port polished output builders from SecureCRT -- build_route_block,
      DIAGNOSTIC SUMMARY (OK/!! indicators, X connectors), RUN SUMMARY one-liners
      + fault-direction logic, per-DS1 PM sections, Peerless blocks. (Raw dump ->
      formatted output on top of proven data flow.)
- [ ] Port JIRA integration (API v3 ADF, 2 expand panels) to standalone.
- [ ] Tighten GSX pager regex (stray "---  ---").
- [ ] MUSE-lookup speed optimization (GitHub config-backup) -- still deferred.

### FUTURE PHASE -- WINDOWS GUI / SCRIPT-PICKER FRONT END
pcapps wants a polished front end: a list of scripts you select and run.
Assessment (do this AFTER the CLI tool is finished + stabilized -- wrapping a
still-changing tool means reworking the GUI repeatedly):
  - The clean module architecture (base class + per-device clients + orchestrator)
    makes a GUI a CALLER of existing modules, not a rewrite. Good foundation.
  - Real lift is NOT layout -- it's (a) threading: run the long multi-SSH job on a
    background thread + stream progress, or the window freezes for minutes; and
    (b) reworking input(): getpass prompts -> form fields / Windows credential store.
  - OPTIONS:
    * LIGHT (days): tkinter or PySimpleGUI wrapper -- fields + Run + scrolling output
      pane, bg thread, package to .exe with PyInstaller. MATCHES the script-picker
      vision (launcher listing tools, each opens its input form + output pane).
      RECOMMENDED first step.
    * MEDIUM (weeks): PySide/Qt native app -- styled, sidebar script list, saved
      settings, Windows credential store, installer. The "proper" front end.
    * HEAVY: local web app (Flask/FastAPI + browser) -- most flexible, best path to
      MULTI-USER (a stated long-term goal), most infrastructure.
  - Recommended sequence: finish CLI (Stage B + JIRA) -> run it real to shake out
    bugs -> THEN GUI, starting with the tkinter picker.

---

## ADDITIONAL TODO ITEMS (session 2026-07-08 cont.)

- [ ] **GSX-optional / skip option.** Not every user will have GSX creds (or need
      the GSX hop). Need a clean way to skip GSX: e.g. leave GSX creds blank ->
      skip Hop 4 gracefully (still do 1250/1800/MUX + note "GSX skipped -- no
      creds"). Fine-tuning; likely AFTER the launcher/GUI. When multi-user,
      pair this with per-user credential handling.

- [ ] **Resilience note (already works):** orchestrator handles a mid-run hop
      failure gracefully -- logs the error, skips to next hop, still writes the
      output file with whatever completed. (Observed when a machine-lock dropped
      a MUX session mid-handshake -> WinError 10054; run continued cleanly.)
      Consider: retry-once on transient socket errors (10054 etc.) before giving
      up on a hop. Low priority.

---

## SESSION 2026-07-08 (cont.) -- STAGE B + FEATURES COMPLETE

### DONE & VALIDATED
- **Stage B output builders** (output_builders.py) -- faithful port of the
  SecureCRT builders: build_route_block, build_peerless_block,
  build_diagnostic_summary, run_summary_line. Validated against Maryville ts3/8
  (matched SecureCRT diagnostic read).
- **Output order matches SecureCRT**: RUN SUMMARY -> DIAGNOSTIC SUMMARY ->
  FULL ROUTE -> SHARED INFRA STATS (NPT-1800 OC12 / CISCO 4216 MUX subheaders)
  -> PER DS1 PMs. Quick snapshot on top, "in the weeds" below.
- **GSX pager artifact fixed** (gsx_client.py): PAGER_RE now consumes the
  surrounding dashes + defensive filter drops "---  ---" remnants.
- **JIRA integration ported** (jira_client.py) -- requests-based (not PowerShell).
  Posts ADF comment via API v3 with two expand panels ("Test Results -- Run
  Summary & Diagnostic" + "Per-DS1 Performance Monitors"). Ticket format NOE-XXXX.
  Creds from autotester_config.txt (JIRA_EMAIL/JIRA_TOKEN). CONFIRMED posting to
  a live ticket (NOE-4385).
- **New up-front prompts**: Add to JIRA? (y/n) -> JIRA number; Open results file
  when done? (y/n). Order: 1250 IP -> DS1s -> JIRA -> open -> credentials. No
  prompts after the run starts.
- **Auto-open**: os.startfile() opens the .txt in the default handler when done.
- **Output to ALL THREE destinations**: full polished report now prints to the
  PowerShell console AND writes to the .txt AND (optionally) posts to JIRA.
  Hop-by-hop progress is console-only (kept out of the file). (emit() = console+file,
  progress() = console-only.)
- **GSX diagnostic counter review added** (Option B: clean=one line, dirty=itemized).
  Flags Operational Status DOWN-DS1ALARM + Alarm Status as the GSX "down" indicator,
  itemizes non-zero PERFMON CURRENT counters (ES/SES/UAS/CV).
- **GSX counter BUG fixed**: initial version falsely matched the PERFMON THRESHOLDS
  config table (limits like 106720/518) as if they were error counts, flagging a
  healthy UP/NOALARM GSX. Fixed: _check_gsx_counters now scans ONLY the PERFMON
  CURRENT region (stops before THRESHOLDS) and anchors to line-start so
  "Code Violations" != "Line/Path Code Violations". Verified: healthy GSX -> clean,
  down GSX -> flags real UAS. Confirmed on live circuit (ts1/9 WOTNOH88324 -> clean).
- **MUX/1800/1250 diagnostic blocks left as-is** (per pcapps -- only GSX changed).
- **INSTALL.md + requirements.txt** created for distribution.

### DECISION LOG
- GSX diagnostic = CURRENT STATE ONLY. Cumulative STATISTICS (Total AIS/LOF/CRC
  counts) are NOT flagged -- a healthy UP/NOALARM circuit with historical hits
  should read clean. History remains visible in the detail section. (Flagging
  cumulative counts would false-alarm, same class as the threshold bug.)

### FULL FILE INVENTORY (!Consolidation folder)
device_base.py, netbox_client.py, npt1250_client.py, npt1800_client.py,
mux4216_client.py, gsx_client.py, output_builders.py, jira_client.py,
autotester_standalone.py (run this), gsx_hostkey_test.py (optional test),
requirements.txt, INSTALL.md.
Runtime deps outside folder: C:\Claude\autotester_config.txt (per-user tokens),
C:\Claude\gsx_hostkeys.json (auto-created).

### TOOL STATUS: FUNCTIONALLY COMPLETE
All four hops, both paths (Peerless + full-route), Netbox reconciliation w/
conflict detection, GSX host-key pinning, JIRA post, auto-open, per-hop counter
review -- all working and validated on live circuits.

### REMAINING (future phases, not core)
- [ ] Windows GUI / script-picker front end (see prior session notes -- do after
      CLI stabilized; tkinter/PySimpleGUI wrapper recommended first step).
- [ ] GSX-optional / skip option (blank GSX creds -> skip Hop 4) for users w/o GSX access.
- [ ] Configurable paths (remove hardcoded C:\Claude\) -- portability; pairs w/ GUI.
- [ ] MUSE-lookup speed optimization (GitHub config-backup) -- deferred.
- [ ] Retry-once on transient socket errors (10054) -- low priority.
- [ ] Tier 2 GSX host-key (Netbox-sourced known-good fingerprints) -- multi-user hardening.
- [ ] Scheduled checker (twice-daily In-Progress ticket auto-check) -- later.

---

## MAYBE PILE (ideas raised but not requested / not committed)

- [ ] **JIRA-triggered runs (attribution).** Boss mentioned in passing; no one is
      demanding it. Idea: a JIRA comment (e.g. `/trace <1250-ip> <ds1>`) OR a new
      ticket in the bucket queue triggers the tool, so there's a named-human
      record of who initiated each run even though the device login is a shared
      read-only account.
      KEY DISTINCTION to resolve BEFORE building: JIRA solves ATTRIBUTION
      ("who asked"), not AUTHENTICATION ("what account logged in" -- still the
      shared account). Confirm which objection security actually has:
        (a) "no accountability for the generic account" -> JIRA trail fixes this.
        (b) "no shared credential on devices at all"     -> JIRA trail does NOT
            fix this; would need per-user or key-based device auth (bigger rethink).
      Architecture = "script polls JIRA" (pattern 2 -- safe, nothing inbound
      crosses the network boundary). Prereqs it would need:
        - Unattended credentials (no getpass) -- Windows credential store /
          encrypted config / secrets manager. Security cares most about WHERE
          this lives and WHO can read it.
        - A trigger convention: comment syntax (idea A, best attribution) or a
          structured custom field for the 1250 IP + DS1 (idea B -- prefer a field
          over regex-scraping free-text descriptions).
      Recommendation if it ever gets picked up: idea A (comment-triggered) for
      cleanest attribution; builds on the existing jira_client.py + the
      scheduled-checker item.

---

## SESSION 2026-07-08 (cont.) -- AUX SCRIPTS + LAUNCHER SHELL

### DONE & VALIDATED ON LIVE GEAR
- **Three SecureCRT aux scripts ported to standalone**, reusing the existing
  client modules (device_base / npt1250_client / npt1800_client):
  - **npt1250_discovery.py** -- full read-only 1250 discovery dump. Prompts 1250
    IP + creds. VALIDATED on PEORILPJ368 (10.111.130.78): CES-slot detection
    (ts1/ts4, excl ts2/ts5), fiber-slot detection (ts2/ts5 via DHXE), and
    active-DS1 detection (full set) all worked against real `show chassis
    hardware` output -- the previously-unverified parsing is now confirmed.
  - **ds1_pms.py** -- per-DS1 PM check. Prompts 1250 IP + DS1 list + creds.
    Kept slot/channel validation (ts1/ts3/ts6/ts8, ch 1-28). VALIDATED on
    PEORILPJ368 (ts1/1, ts1/13).
  - **interface_check.py** -- 1800 NNI check. Prompts SITE (ATL/LAX/JFK/DFW)
    + interface + creds; resolves site->1800 IP internally. VALIDATED on ATL
    (ces01.atl1, xe-ts13/4.368), with and without JIRA.
- **Definitive SITE -> 1800 IP map** (in interface_check.py SITE_TO_1800):
    ATL 10.111.130.254 | LAX 10.111.131.254 | JFK 10.111.130.255 | DFW 10.111.131.255
  (Confirms LAX = 10.111.131.254 is the real ces01.lax1 box; clears the old
  HUB_IPS "DFW-1800" mislabel.)
- **"Add to JIRA" added to all three aux scripts.** New general-purpose
  post_report(ticket, intro, panel_title, report_text) + _build_report_adf in
  jira_client.py -- single intro paragraph + one collapsed panel holding the full
  report (vs the Autotester's 2-panel split, since these are single-purpose).
  Prompt order matches Autotester (tool inputs -> JIRA -> open -> creds last);
  end-of-run sequence write -> JIRA -> open confirmed in all three.
- **ds1_pms UX fixes (per pcapps):** removed the "how many DS1s?" prompt (count
  derived from entries + echoed back); input now accepts bare "tsX/Y" (e.g.
  ts1/1), normalized to full "ds1-tsX/Y" internally for device commands. Legacy
  "ds1-tsX/Y" input still accepted; case-insensitive. Slot/channel guardrails
  kept. VALIDATED.

### DONE -- LAUNCHER SHELL (front-end only; back-end wiring pending)
- **launcher.py** -- menu front door (approach B1). Lists all 4 tools with
  description + "Requires:" blurb, collects+validates inputs via FIELD metadata
  (text/masked/list/choice types), appends shared JIRA + open-when-done tail,
  builds a params dict, calls tool.run(params). Menu + input collection VALIDATED
  on pcapps's machine (site + ticket validation reprompt correctly, masked pw,
  list parse). Dispatch currently hits a graceful "[pending] run() not yet wired"
  path because the tool modules don't expose run(params) YET.
- **PARAMS CONTRACT defined** (documented at top of launcher.py) -- the dict keys
  each tool's run(params) will read. This makes the AM refactor mechanical.

### NEXT SESSION (AM) -- THE run(params) REFACTOR (deferred deliberately)
Deferred until AFTER live validation (now complete) so we refactor known-good
code, not unvalidated code. Plan:
1. Split each of the 5 tool modules' main() into collect_inputs() + run(params).
   __main__ still works standalone (collect then run). run() reads the params
   dict instead of prompting.
2. Wire all 5 to launcher.py; remove the [pending] path.
3. Fix two items surfaced tonight:
   (a) tool modules currently do sys.exit(1) at import if paramiko/requests
       missing -> would kill the launcher. Change to graceful "tool unavailable"
       so one missing dep doesn't take down the menu.
   (b) ds1_pms: the standalone count-prompt is already removed; ensure the
       refactor keeps count derived from the list (launcher passes ds1_list).
4. Re-test each script STANDALONE after its split (confirm no regression), then
   via the launcher.
5. Add launcher + 3 aux scripts to the handoff zip; re-zip; checkpoint.

### HANDOFF PACKAGE STATUS
Autotester_Standalone.zip currently contains the core Autotester + docs but
PREDATES the 3 aux scripts, the launcher, and the JIRA post_report helper.
DO NOT redistribute until the AM refactor is done and the zip is rebuilt with:
launcher.py + npt1250_discovery.py + ds1_pms.py + interface_check.py + updated
jira_client.py, and README/INSTALL updated to mention the launcher as the entry
point.

---

## NEW TOOL IDEA (backlog) -- Fiber Utilization Check (1250)

Ran from: NPT-1250. Checks the fiber (GE) interface utilization -- the ts2/ts5
uplinks. A natural 5th launcher tool (fits "Ran From: 1250").

Commands (read-only), per fiber port:
  show interfaces physical pm 24hour ge-ts5/1 | grep bps
  show interfaces physical pm 24hour ge-ts2/1 | grep bps

Sample output (HRFRCT03920), per port returns 6 lines:
  IN  Maximum/Average/Current Rate(bps)
  OUT Maximum/Average/Current Rate(bps)
  e.g. ge-ts2/1: IN ~18.6 Mbps avg, OUT ~16.1 Mbps avg
       ge-ts5/1: IN ~5.2 Mbps avg,  OUT ~3.96 Mbps avg

Implementation notes / reuse:
  - Uses NPT1250Client (same as discovery/ds1_pms). collect_inputs(session)/
    run(params) contract like the other launcher tools. Prompts: 1250 IP + creds
    (+ JIRA? + open?). Reuse the session cred/IP pattern.
  - Fiber ports are ge-ts2/1 and ge-ts5/1 by convention; npt1250_discovery.py
    already has _get_fiber_slots() (DHXE match) -- could AUTO-DETECT the fiber
    slots instead of hardcoding ts2/ts5, then build ge-<slot>/1. Preferred:
    reuse that detection so it works on non-standard chassis.
  - Parse the 6 bps values per port; present a small utilization summary. Would
    be nice to show % of link capacity -- but need link speed (1G? 10G? the
    "ge" vs "xe" naming hints, but confirm). Raw bps is the safe default;
    %-util needs the port speed confirmed. FLAG: don't assume capacity.

DESIGN DECISIONS (resolved by pcapps 7/9):
  1. HARDCODE ts2 and/or ts5. Check both ge-ts2/1 and ge-ts5/1; it will be one,
     the other, or both present. Never any other slot for the foreseeable future.
     (If a port isn't present the command just returns nothing -- handle empty
     gracefully, don't error.)
  2. Show raw bps (device provides bps), BUT also render a human-readable Mbps
     alongside for readability, e.g. "18620415  (18.62 Mbps)". Just a unit
     conversion for the eye -- bps / 1,000,000. No capacity/threshold math.
  3. NO %-utilization logic -- circuit speed varies. Just present the numbers
     (all 6 lines per port: IN/OUT x Max/Avg/Current) with the Mbps annotation.

Build shape: prompts 1250 IP + creds (+ JIRA? + open?), reuse session pattern;
for slot in (ts2, ts5): run "show interfaces physical pm 24hour ge-<slot>/1 |
grep bps | no-more", parse the 6 bps values, print each as
"<label>: <bps>  (<Mbps> Mbps)". Skip a port cleanly if it returns no bps lines.

---

## SESSION 2026-07-09 (AM) -- run(params) REFACTOR + LAUNCHER LIVE + FIBER TOOL + REPACKAGE

### DONE & VALIDATED ON LIVE GEAR
- **run(params) refactor complete** on all tools. Each tool module now exposes:
    collect_inputs(session) -> params dict (all prompting; session-aware)
    run(params)             -> the work (ZERO prompting; enforce-tested)
    main()                  -> standalone: collect then run
  Import guards changed from sys.exit(1) to graceful _DEP_ERROR so a missing dep
  disables just that tool, not the launcher.
- **launcher.py wired live.** Menu (5 tools) -> calls each module's
  collect_inputs(session)/run(params). Removed the old [pending] path and the
  launcher-side metadata collector (tools own their prompting/validation now).
- **Session reuse (validated live):**
    - Credentials (infra + GSX): entered once, reused SILENTLY for later tools.
    - Device-identifying values (ip_1250, site, interface, ds1_list): remembered,
      offered as an EDITABLE DEFAULT shown in the prompt (never silent) so a tool
      never runs against the wrong device by accident.
  Confirmed: Autotester (Peerless ts21 circuit + JIRA NOE-4175) -> Discovery
  (cred silent-reuse + IP default) -> DS1 PMs -> IXC Check, all via launcher on
  live gear (LNBHCAXP730 / ces01.atl1). Standalone paths still work.
- **NEW TOOL: Fiber Utilization (fiber_util.py).** Reads GE uplink 24hr bps PMs
  for ge-ts2/1 and ge-ts5/1 (hardcoded; one/other/both; absent port skipped
  cleanly). Raw bps + human Mbps annotation e.g. "18620415  (18.62 Mbps)". No
  %-util (circuit speeds vary). Echoes report to console AND .txt (+ optional
  JIRA). Menu item #4. VALIDATED live on LNBHCAXP730 (both ports, real figures).
- **Menu order now:** 1 Autotester, 2 NPT-1250 Discovery, 3 DS1 PMs,
  4 Fiber Utilization, 5 IXC Check.

### HANDOFF PACKAGE REBUILT (Autotester_Standalone.zip)
Extracts to single flat Autotester_Standalone/ folder, 19 files:
  launcher.py (entry point)
  5 tools: autotester_standalone, npt1250_discovery, ds1_pms, fiber_util,
           interface_check
  8 shared: device_base, npt1250_client, npt1800_client, mux4216_client,
            gsx_client, netbox_client, output_builders, jira_client
  gsx_hostkey_test.py (optional), requirements.txt,
  autotester_config.example.txt (placeholders only), README.md, INSTALL.md
Pre-zip gate passed: all compile, all 5 tools satisfy collect_inputs/run
contract, no real tokens, no __pycache__. README + INSTALL updated to name
launcher.py as the entry point (standalone runs still documented).
NOTE: TECHNICAL_REVIEW.md (service-account request doc) is NOT in this zip --
it's a separate submission doc. Its section 4 command inventory should be
updated to include the new tools' commands (discovery/ds1_pms/fiber_util/
interface_check) before it goes to the security board -- currently it only
covers the Autotester's 4 devices. (Fiber util adds:
  show interfaces physical pm 24hour ge-ts2/1|ge-ts5/1 | grep bps -- read-only.)

### STILL OUTSTANDING / NEXT
- [ ] TECHNICAL_REVIEW.md section 4: add the aux tools' command inventories
      (still read-only; no new mutating commands) before board submission.
- [ ] Optional: make Discovery / DS1 PMs / IXC Check also echo their full report
      to console like fiber_util now does (one-line print("\n".join(lines)) add)
      -- pcapps hasn't asked; only fiber was requested. Parked.
- [ ] GUI (future phase) -- launcher is the CLI foundation it would build on.
- [ ] Remaining backlog unchanged (configurable paths, GSX-skip, MUSE speed,
      Tier2 hostkey, Database of Record, scheduled checker, JIRA-trigger maybe).

---

## SESSION 2026-07-09 (AM cont.) -- 6TH TOOL: 1250 HEALTH CHECK + REPACKAGE

- **NEW TOOL: 1250 Health Check (health_check.py).** High-level read-only 1250
  look. Runs 16 fixed 'show' commands in order:
    show version; show interfaces;
    show chassis transceiver information slot ts2 port1 (+ | grep laser);
    show chassis transceiver information slot ts5 port1 (+ | grep laser);
    show ldp session; show ospf neighbor;
    show interfaces physical status slot ts1/ts3/ts6/ts8;
    show ne alarms; show chassis alarms; show ne events; show ne events unreported
  No parsing/logic -- sectioned dump, same format as the rest. Console + .txt +
  optional JIRA. Slots hardcoded (ts2/ts5 fiber, ts1/ts3/ts6/ts8 CES). Both
  transceiver + laser-grep lines kept (quick-find). Read-only audit: all 'show'.
  CONSOLE SUPPRESSION: "show ne events" and "show ne events unreported" are
  omitted from the PowerShell echo (huge output) -- shown as
  "(omitted from console -- see .txt / JIRA)"; FULL logs still in .txt + JIRA.
  VALIDATED live on LNBHCAXP730 (version dual-blade, transceiver optics, LDP/OSPF
  up, ts1 DS1 status, ts3/6/8 "CES cards only" handled, alarms clean, events
  suppressed in console as designed).
  Menu item #5 (1250 group). IXC Check now #6.
- **Corrections applied during build:** "no ne events" -> "show ne events"
  (negation command rejected on a read-only tool); "show ospf neighbo" ->
  "show ospf neighbor".

### MENU ORDER (6 tools)
  1 Autotester | 2 NPT-1250 Discovery | 3 DS1 PMs | 4 Fiber Utilization
  5 1250 Health Check | 6 IXC Check

### HANDOFF PACKAGE REBUILT (Autotester_Standalone.zip -- 20 files)
Adds health_check.py to the prior 19. README + INSTALL updated (tools table,
architecture table, standalone run list, file list). Pre-zip gate passed: all
compile, all 6 tools satisfy collect_inputs/run contract, health_check all-show,
no tokens, no __pycache__.

### TECHNICAL_REVIEW.md reminder (unchanged, still outstanding)
Section 4 command inventory still only covers the Autotester's 4 devices. Before
board submission, add the aux tools' commands (discovery / ds1_pms / fiber_util /
health_check / interface_check). ALL read-only ('show' + session '| no-more' +
ping reachability on the 1800). health_check adds the transceiver/version/ldp/
ospf/events 'show' commands listed above. No mutating commands anywhere.

### RECURRING PATTERN NOTE
Two tools added post-refactor (fiber_util, health_check). Adding a tool currently
touches: the new module + launcher TOOLS list + README (4 spots) + INSTALL (2) +
re-zip. If more tools are likely, consider a small "tools self-register" pass to
cut that friction. pcapps not yet decided; flagged.

---

## ON HOLD (discuss w/ peer in office 7/10) -- Config-Backup Audit tool

Peer-provided script: "1250 Audit Scripts.py" (in uploads). Greps a LOCAL zip of
config backups (cloned from GitHub ipeng-config-backup repo) for a pattern
(currently hardcoded: "set routing-options rib"). Prints matching lines per file.

NOT launcher-compatible as-is. To adapt it needs:
  - Restructure from a flat top-to-bottom script into functions. IMPORTANT: the
    launcher imports every TOOL_INFO file at startup, so its logic MUST be inside
    run()/main(), not at module top level -- otherwise it would execute (open the
    zip, print matches) the moment the launcher starts.
  - Add TOOL_INFO + collect_inputs(session) + run(params) (the contract).
  - Rework hardcoded inputs -> prompt for: zip path + search pattern.
  - It's NOT a device tool: no IP/creds, no SSH. ran_from would be something like
    "Local (config backups)". Device session-reuse keys don't apply.
  - Match suite output pattern (.txt to C:\Claude\ + console echo + optional JIRA).
  - Tidy the bare `except:` / decode fallbacks for shared use.

DECISIONS TO RESOLVE WITH PEER (why it's on hold -- easier in person than Slack):
  1. Search pattern: free-form entry (audit for anything) vs. a fixed menu of
     known audits? (foolproof vs flexible)
  2. Zip source: always a manually-downloaded zip, or eventually pull from the
     GitHub repo directly? (affects whether we just prompt for a path now)
  3. Are there OTHER audits the peer runs this same way? (might be one tool with
     a pattern menu rather than a single-purpose tool)
  4. Confirm intent of the hardcoded "set routing-options rib" audit.

Good candidate for the suite regardless -- an offline config-auditor across all
backups complements the live-device tools. Just needs the wrap + intent clarified.

---

## SESSION 2026-07-09 (cont.) -- LAUNCHER AUTO-DISCOVERY (self-registration)

- **launcher.py rewritten to auto-discover tools.** No hardcoded TOOLS list.
  At startup it scans its own folder (os.path.dirname(__file__)) for *.py files
  containing a TOOL_INFO marker, imports them, validates the contract, and builds
  the menu sorted by TOOL_INFO["order"]. Adding a tool = drop the .py in the
  folder; no launcher edit.
- **All six tools got a TOOL_INFO dict** (name, ran_from, desc, requires, order
  10/20/30/40/50/60) inserted just above collect_inputs().
- **Opt-in + robust:** files without TOOL_INFO are ignored silently (shared
  modules/helpers safe to keep in folder). A file that HAS TOOL_INFO but fails to
  import, or is missing collect_inputs/run, prints "[skip] <name>: <reason>" and
  is skipped -- never fatal. Tested with broken/incomplete/no-marker decoys.
- **ADDING_A_TOOL.md** written -- one-page contributor spec for peers: the two
  things a script needs (TOOL_INFO + collect_inputs/run), session-key reference,
  read-only rule, checklist. Hand this to peers who want to contribute tools.
- VALIDATED live: pcapps ran launcher.py on his machine -> all six tools
  discovered in order 1-6. (First run showed only health_check because the other
  5 files in his folder were pre-TOOL_INFO copies -- file-sync gap, not a bug;
  resolved by re-presenting all 6 updated tool files. Good live proof that the
  opt-in skip is safe.)

### DISTRIBUTION LESSON (recurring)
Loose-file updates have now bitten twice (menu numbering; TOOL_INFO sync). Once
active dev settles, the ZIP is the source of truth to hand people, not individual
files. Longer term the PyInstaller .exe removes "did I copy all the files" risk.

### REPACKAGE STATUS -- HELD
Zip NOT yet rebuilt for auto-discovery. Decision (pcapps leaning hold): wait
until after 7/10 peer discussion in case the config-audit tool joins the suite,
so it's one clean repackage. When repackaged, ADD: ADDING_A_TOOL.md + the new
auto-discovery launcher.py + the 6 TOOL_INFO-updated tool files. Also update
README to mention auto-discovery + point to ADDING_A_TOOL.md. Current zip on disk
(20 files) predates auto-discovery -- do not ship until rebuilt.

---

## REPACKAGE DONE (7/9) -- current full snapshot for a new user

Autotester_Standalone.zip rebuilt -- 21 files, extracts to one flat folder:
  launcher.py (auto-discovery entry point)
  6 tools: autotester_standalone, npt1250_discovery, ds1_pms, fiber_util,
           health_check, interface_check  (all with TOOL_INFO)
  8 shared: device_base, npt1250_client, npt1800_client, mux4216_client,
            gsx_client, netbox_client, output_builders, jira_client
  gsx_hostkey_test.py, requirements.txt, autotester_config.example.txt,
  README.md, INSTALL.md, ADDING_A_TOOL.md
Docs updated: README has an "Adding a tool" section + points to ADDING_A_TOOL.md;
INSTALL lists ADDING_A_TOOL.md + notes auto-discovery. Pre-zip gate passed:
all compile, auto-discovery finds 6 in order, no tokens, no pycache.
Supersedes the earlier 13:45 zip (which predated auto-discovery). THIS is the
current shippable snapshot.

Still outstanding (unchanged): TECHNICAL_REVIEW.md section 4 command inventory
needs the 5 aux tools' commands added before board submission (all read-only).
Config-audit tool on hold pending 7/10 peer discussion -- if it joins, re-zip
(now trivial: add its .py, it self-registers; add one README row).

---

## PACKAGE UPDATE (7/9) -- CURVEBALL_TOOL_SPEC.md added to master zip

Autotester_Standalone.zip now 22 files (added CURVEBALL_TOOL_SPEC.md). STANDING
DECISION: the tool-authoring spec ships in the master zip from now on. README +
INSTALL point contributors to CURVEBALL_TOOL_SPEC.md as the canonical authoring
standard (the doc to hand peers / their agents). Spec includes a scope/ownership
section: owner is responsible for their tool WORKING; read-only/safety is the
shared non-negotiable gate; launcher maintainer owns the platform only.

OPEN (doc tidy, pcapps to decide): ADDING_A_TOOL.md and CURVEBALL_TOOL_SPEC.md
overlap. Both currently shipped (README calls the spec canonical, ADDING_A_TOOL
"longer companion"). To avoid drift, consider dropping ADDING_A_TOOL.md and
keeping only the spec on a future repackage. Not done -- removal not yet
authorized.

---

## END OF 7/9 -- menu label fix (zip intentionally one step behind)

Fixed: the "show tech" tag belongs on the DUMP-EVERYTHING tool. Corrected menu:
  2) NPT-1250 Discovery ("show tech")   <- the exhaustive dump (PMs on every DS1,
                                           chassis hw, loopbacks, ~2000 lines)
  5) 1250 Health Check                  <- high-level snapshot (version, xcvrs,
                                           LDP/OSPF, DS1 status, alarms)
(pcapps originally had them backwards; Discovery is the comprehensive one.)
Changed files: npt1250_discovery.py (name += ' ("show tech")'),
health_check.py (name reverted to plain). Both TOOL_INFO name-only edits.

ZIP STATUS: master zip (22 files) is ONE LABEL-FIX BEHIND -- it has the old
names. Loose files in the folder are current. DO NOT ship the zip as-is; rebuild
next session.

BATCH FOR NEXT REPACKAGE (do all in one pass):
  1. This show-tech label fix (2 files above).
  2. Config-audit tool IF it joins after the 7/10 peer discussion (write to
     CURVEBALL_TOOL_SPEC.md, drop in, self-registers).
  3. ADDING_A_TOOL.md consolidation decision -- recommend dropping it, keeping
     only CURVEBALL_TOOL_SPEC.md (pcapps to confirm; removal not yet authorized).
Then: rebuild zip, re-verify pre-zip gate (compile / discover 6+ / no tokens /
no pycache), update TODO.

---

## END-OF-DAY MASTER ZIP REBUILT (7/9) -- CURRENT & SHIPPABLE

Autotester_Standalone.zip rebuilt with tonight's label fix included. 22 files,
current menu: 1 Autotester | 2 NPT-1250 Discovery ("show tech") | 3 DS1 PMs |
4 Fiber Utilization | 5 1250 Health Check | 6 IXC Check. Pre-zip gate passed
(compile/ASCII, 6 tools discover, read-only confirmed [regex false-positives on
"No..." strings + the ping-IP parse regex are prose, not device commands],
no tokens, no pycache, spec template compiles). Zip is now CURRENT -- matches the
loose files. Shippable snapshot for end of 7/9.

Still open (unchanged, for later): TECHNICAL_REVIEW.md sec4 aux commands;
ADDING_A_TOOL.md vs CURVEBALL_TOOL_SPEC.md consolidation (both still shipped);
config-audit tool pending 7/10 peer discussion.

---

## 1250 DIAGNOSTIC -- NNI/EVC + SUB-INT GATE -- DONE & VERIFIED LIVE (7/10)

The Autotester DIAGNOSTIC SUMMARY 1250 block is reworked and confirmed on gear:
  - Duplicate CES block (was re-printing 1250 PSN data on the "top") REMOVED.
  - "top" of 1250 now shows uplink health:
      [ OK/!! ] NNI PMs clean   <- physical fiber (ge-tsX/1): error counters + Rx laser
                <laser Rx/Tx info line per in-service port>  (TX shown, not flagged)
      [ OK/!! ] EVC PMs clean   <- logical sub-int (ge-tsX/1.0): Rx Discard Pkts/Octets
  - CES/PSN block UNCHANGED below the 1250 header (matches screenshot).
  - Applied to BOTH full-route and Peerless branches.

Flagging logic (shared helpers in output_builders.py, reused by the coming 1800 work):
  - check_fiber_health(): FIBER_ERR_COUNTERS >0 (Rx Error/CRC/Collision/Jabber/
    Fragments/Under+Oversize + PCS-ES/SES/UAS/FEC-UCE when numeric); Rx laser <
    FIBER_RX_DBM_FLOOR (-10.0, exclusive; more-negative=worse). TX info only.
  - check_evc_health(): EVC_ERR_COUNTERS >0 (Rx Discard Pkts, Rx Discard Octets;
    Tx Discard is "NA" -> skipped). Same helper works for 1250 ge-tsX/1.0 AND
    1800 xe-tsX/Y.VLAN (identical field format).

IN-SERVICE GATE (autotester_standalone.py hop 1): a fiber port is only checked if
its .0 sub-interface is configured (found in "show interfaces"). Config lives on
ge-tsX/1.0, so sub-int presence = in service. Regardless of up/down state:
  - configured-but-DOWN port (e.g. WOTNOH ge-ts2/1, -40 dBm) -> CHECKED -> flagged
    (real fault preserved).
  - unused port with no .0 -> SKIPPED (no false "dark port" -40 noise).
  - "show interfaces" pull fails -> degrade to checking both (no silent loss).
Verified live: WOTNOH (down port flagged), and a single-fiber site where ge-ts2/1
is unused -> correctly skipped, ge-ts5/1 checked, EVC discards flagged (321765).

Hop 1 now runs 6-7 extra read-only cmds on the 1250 (show interfaces [gate];
per in-service port: physical pm 24h/15m, transceiver grep laser, pm 24h .0).

Files: output_builders.py (helpers + NNI/EVC rows), autotester_standalone.py
(hop-1 collection + gate). Both offline-verified AND live-verified.
Note: master zip in the working folder is still behind -- repackage pending.

## NEXT: 1800 NNI/EVC mirror (unblocked -- all logic confirmed)
Mirror the 1250 change on the 1800 block: OCx assignment stays "top",
add NNI/EVC health on "bottom". Resolution of the NNI interface:
  1. capture 1250 hostname at hop 1 (have it, e.g. WOTNOH88324).
  2. strip LAST 4 chars (trailing 3-4 = LATA) -> prefix (WOTNOH8 -> ...). NOTE:
     verify strip-count on real hostnames; BRYNTXXA570 -> BRYNTXX worked.
  3. on the 1800: show interfaces description | grep <prefix> -> NNI line.
  4. parse logical sub-int (xe-tsX/Y.VLAN) + physical (strip sub -> xe-tsX/Y).
  5. single clean match -> proceed; MULTI-match -> flag "NNI ambiguous" + skip
     (dual-1250 <hostname>-A/-B case deferred until hit).
Commands (read-only): show interfaces pm 24hour/15min xe-tsX/Y.VLAN (logical/EVC),
show interfaces physical pm 24hour/15min xe-tsX/Y (physical NNI).
Reuse check_evc_health (logical) + FIBER_ERR_COUNTERS (physical). 
OPEN QUESTION for build: apply the -10 dBm Rx floor to the 1800 NNI physical
"MinRxPower" (10G optic, reads ~-3.6)? Or show info-only? -> ask pcapps at build.

---

## TODO (pcapps to pick up later) -- output folder for .txt reports

Problem: tool .txt reports all dump into C:\Claude\ root and flood it. pcapps
made a C:\Claude\!Autotester folder and wants Autotester output to go there.

Decision needed before building:
  1. SCOPE: just the Autotester -> C:\Claude\!Autotester\ ? Or declutter the whole
     root -- give each of the 6 tools its own subfolder (!Autotester, !Discovery,
     !DS1_PMs, !FiberUtil, !HealthCheck, !IXCCheck), or one shared C:\Claude\!Output\?
     (Autotester alone leaves the other 5 tools still cluttering root.)
  2. AUTO-CREATE the folder: strongly recommended -- os.makedirs(folder,
     exist_ok=True) before writing, so it works on a fresh install / other
     machines without a manual mkdir step (avoids a new "did you make the folder?"
     support issue like the config file). Confirm OK to auto-create.

Where the path lives: each tool builds out_path as
  r"C:\Claude\<prefix>_{ts}_{host}.txt"
Autotester: autotester_standalone.py (out_path in run()); the 5 aux tools each
have their own out_path in their run(). A shared helper for "resolve output dir
+ makedirs" would keep it DRY if we do all six. Also consider making the base
path configurable (ties into the existing backlog item: remove hardcoded
C:\Claude\ paths / make configurable).

---

## TODO -- real per-transceiver optical thresholds (replaces -10 dBm rule-of-thumb)

pcapps got the threshold command (from AHVLNCOH420):
  show chassis transceiver tca slot ts5 port1
Output (per port):
  monitored quantity                warning low   warning high   alarm low   alarm high
  Tx Optical Power (dBm)         :  -10.0         -3.0           -11.0       -2.0
  Rx Optical Power (dBm)         :  -20.0         -3.0           -21.0       -2.0
  Tx Laser Bias Current (mA)     :  0.0           80.0           0.0         100.0
  Transceiver Temperature(C)     :  -45.0         105.0          -50.0       110.0
  +3.3V supply (V)               :  3.10          3.50           3.00        3.60

WHY IT MATTERS: current code flags Rx < -10.0 (FIBER_RX_DBM_FLOOR, a rule-of-thumb).
This optic's Rx ALARM low is -21.0 and WARNING low is -20.0 -- so -10 FALSE-FLAGS
a healthy optic (e.g. Rx -15 is fine on this optic but current code flags it).
The rule-of-thumb is too aggressive; real thresholds vary per optic.

PLAN (deliberate, do AFTER 1800 NNI render is confirmed on live gear):
  - Add a parser for the TCA table -> per-port {rx: (wlo,whi,alo,ahi), tx: (...)}.
  - Flag Rx/Tx optical power against the REAL bands:
      < alarm low or > alarm high  -> [ !! ] alarm
      < warning low or > warning high (but within alarm) -> [ !! ] warning (or [ ~~ ])
      within warning band -> OK
  - This finally enables TX flagging too (we deferred TX pending real thresholds).
  - Could also flag bias current / temp / supply against their bands (optional).
  - Applies to BOTH 1250 fiber (ge-tsX/1, transceiver slot tsX) and 1800 NNI
    (xe -- but 1800 NNI optics come from 'show interfaces physical pm' MinRxPower/
    TxPower, NOT a transceiver tca command -- CHECK whether 1800 NNI xcvrs support
    the same 'show chassis transceiver tca' cmd, or if only MinRx/Tx is available).
  - Replace FIBER_RX_DBM_FLOOR usage with per-optic thresholds where available;
    keep the -10 floor only as a fallback when tca data can't be read.
  - Command is read-only ('show chassis transceiver tca ...').
OPEN: confirm the tca command works on the 1800's 10G NNI optic (different platform
than the 1250 1G). If not, 1800 stays on MinRx/Tx with a sensible fallback.

---

## 1800 NNI/EVC + EVC RATIO -- DONE & VERIFIED LIVE (7/10)

1800 block now mirrors the 1250: OCx assignment stays "top", NNI/EVC health on
"bottom". Verified live on ATL full-route circuit (JCSNMSCP482 -> resolved
xe-ts16/2.482 logical / xe-ts16/2 physical):
  1800  --  ATL-1800
  oc12-ts1/1:8:16
  [ OK ] NNI xe-ts16/2 PMs clean          <- physical NNI (errors + MinRxPower)
         NNI optics  Rx -5.2 / Tx -2.4 dBm
  [ OK ] EVC xe-ts16/2.482 PMs clean      <- logical (Rx discards + ratio)
Interface names (xe- vs ge-) disambiguate the 1800 vs 1250 NNI/EVC blocks.

NNI resolution (verified): 1250 hostname -> strip last 4 chars -> grep 1800
"show interfaces description | grep <prefix>" -> parse logical (xe-tsX/Y.VLAN) +
physical (xe-tsX/Y). Single clean match proceeds; multi-match -> "NNI ambiguous
-- not checked"; none/error -> "NNI not resolved". Helpers in output_builders.py:
resolve_nni_from_description(), check_nni_health() (reuses FIBER_ERR_COUNTERS +
FIBER_RX_DBM_FLOOR; optical from MinRxPower/TxPower on the physical PM).

BUG FOUND+FIXED live: resolve_nni_from_description was called in the orchestrator
but NOT imported -> NameError swallowed by try/except -> nni came back empty ->
1800 block silently absent. Fixed the import; added an end-to-end test that drives
the hop-2 NNI path so used-but-not-imported can't recur. (Cost a few test cycles;
I wrongly blamed stale files first -- lesson: instrument, don't assume.)

EVC discard flag now shows RATIO: "Rx Discard Pkts: <n> (<pct>)", packets only
(octets dropped). pct = discard/(pass+discard)*100; "<0.01%" for tiny, "ratio n/a"
if pass count missing. Same treatment on 1250 EVC and 1800 EVC (shared
check_evc_health). Flags on any discard >0 regardless of ratio (ratio is for
human judgment; no auto-suppress threshold -- could add later if wanted).

Files changed today (all live-verified): output_builders.py (fiber/NNI/EVC helpers
+ NNI/EVC rows + EVC ratio), autotester_standalone.py (hop-1 fiber/EVC collection
+ sub-int gate, hop-2 NNI resolution + collection, import fix).

ZIP STATUS: master zip is now WELL behind -- missing everything from today
(fiber block, 1250 NNI/EVC, sub-int gate, EVC ratio, 1800 NNI/EVC) AND the earlier
auto-discovery launcher + CURVEBALL_TOOL_SPEC. Loose files in working folder are
current & live-verified. REPACKAGE NEEDED before any distribution.

Parked for later (in TODO above): output folder for .txt reports; per-transceiver
real optical thresholds (show chassis transceiver tca -- replaces -10 rule, which
false-flags: real Rx alarm floor was -21 on the sampled optic); 1800 MUSE-lookup
speed (no interface descriptions on 1800 -> can't grep; options: render config
once per hub not per-DS1 [safe, code-only], scope the config query to the MUSE's
hierarchy [needs device info], or a service/pseudowire operational show [needs
device exploration]).

---

## REPACKAGE DONE (7/10) -- current master, all of today's work folded in

Autotester_Standalone.zip rebuilt -- 21 files (dropped ADDING_A_TOOL.md; single
contributor doc = CURVEBALL_TOOL_SPEC.md). This supersedes all prior zips.
Contains everything from 7/9-7/10:
  - auto-discovery launcher + TOOL_INFO on all 6 tools
  - CURVEBALL_TOOL_SPEC.md (with scope/ownership section)
  - fiber_util + health_check tools (menu 6 tools total)
  - Diagnostic summary rework (ALL live-verified on ATL full-route + others):
      * 1250 NNI (physical fiber: errors + Rx laser <-10) + EVC (logical .0:
        Rx discards + ratio) with sub-interface in-service gate
      * 1800 NNI (physical xe: errors + MinRxPower) + EVC, resolved via 1250
        hostname strip-4 + "show interfaces description | grep" (single match
        proceeds; multi -> "NNI ambiguous"; none/error -> "not resolved")
      * EVC discards shown as raw + ratio %, packets only
      * MUX facility-alarm: any alarm row -> [ !! ] <SEVERITY> <SYSLOG>
        (was only detecting LP_AIS); empty -> [ OK ] No alarms
  - Diagnostic layout (per pcapps, live-verified final):
      GSX block: node, then [OK] GSX PMs line, then assignment
      MUX block: CE (gsx-facing) above "MUX --", then
        "oc12... | SONET ... ACR ... CEM ..." (combined line) below
        (no more "GSX-facing:/1800-facing:" labels -- position implies direction)
      1800 block: oc12 assignment ABOVE the "1800 -- name (ip)" node line
      xe- (1800) vs ge- (1250) interface names disambiguate the two NNI/EVC blocks

Pre-zip gate passed: 21 files, all compile+ASCII, 6 tools discover w/ correct
labels ("show tech" on Discovery), no DEBUG residue, no tokens, no pycache,
no ADDING_A_TOOL, spec template compiles. Read-only audit clean (only "terminal
length 0" MUX paging flagged -- expected/allowed session cmd).

Loose working files == zip contents. THIS is the shippable snapshot.

## STILL PARKED (unchanged, for later)
- Output folder for .txt reports (scope + auto-create decision).
- Per-transceiver real optical thresholds (show chassis transceiver tca) to
  replace the -10 rule-of-thumb (which false-flags: real Rx alarm floor -21 on
  sampled optic). Enables TX flagging too.
- 1800 MUSE-lookup speed: no interface descriptions on 1800 so can't grep;
  safe code-only win = render config ONCE per hub (not per-DS1); bigger options
  need device info (scope config query to MUSE hierarchy, or service-layer show).
- Config-audit tool (peer's script) -- pending 7/10+ in-office discussion.
- TECHNICAL_REVIEW.md sec4: add aux-tool command inventories before board.

## 2026-08-17 -- parked for later

- NNI/EVC resolution is wrong for Peerless/T1-SAToP-over-OC12 circuits
  (confirmed live on EVVLIN03330/ts1-18, DFW-1800): those circuits have no
  separate physical NNI/EVC Ethernet interface at all -- the MUSE tunnel is
  a T1 SAToP pseudowire bound directly to the OC12 facility itself
  (`layer2-transport ... pw-type t1-satop ... remote-tpe <ip>`), terminating
  at a remote-tpe IP, not a local xe-/ge- handoff interface. The current
  `show interfaces description | grep <hostname-prefix>` check was built for
  GE-fiber-uplink circuits (real Ethernet NNI with descriptions) and simply
  doesn't apply to this circuit type -- it was never going to resolve
  anything here, description-grep or MUSE-grep. Decision still needed:
  (a) show `[ .. ] N/A -- T1-SAToP over OC12, no separate NNI interface`
  instead of the current misleading "not resolved (none)" for this circuit
  type, or (b) look into whether NetBox carries a real far-end
  handoff/interface field worth querying instead. Revisit with pcapps.

- Morning Check finding worth digging into: TS1/2 on 10.111.130.73 --
  CES-side RxPsnUnderrunEvents flagged while TDM and every hop above are
  clean. REVIEW text already calls this out as likely a timing/jitter/buffer
  issue on the 1250<->1800 packet transport (not the physical T1) and notes
  these PM counters are cumulative, so a re-run + compare is needed before
  chasing it (static count = historical event, climbing count = active
  issue). Revisit with pcapps once he's re-checked.
