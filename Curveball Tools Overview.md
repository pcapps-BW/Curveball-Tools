# Curveball Tools — Overview

## What it is

Curveball Tools is a personal automation toolkit that traces and diagnoses T1/DS1 circuits end to end across the full transport path — NPT-1250 → NPT-1800 → Cisco NCS 4216 MUX → GSX 9000 — without hand-typing commands into four separate device sessions. It runs as a small local web app (Flask) with a menu of tools, built and iterated over the last ~2 months against real NOC tickets.

## Why it exists

Troubleshooting a circuit today means logging into each device in the path separately, running the same handful of commands, and manually piecing together which hop is actually at fault. That's slow, repetitive, and error-prone — especially under ticket-volume pressure. Curveball Tools automates that trace: one run pulls PMs and alarms from every hop, figures out which segment is actually dirty, and writes a plain-English summary instead of raw device output.

## How it works

- **Web front end** — a locally-run Flask app presents each tool as a simple form (device IP, circuit ID, credentials). No SecureCRT, no manual command entry.
- **Shared route logic** — each hop hands the next one what it needs automatically (MUSE ID → hub IP → OC12 assignment → GSX target), so the user only ever enters the circuit's starting point.
- **Automatic verdict, not just raw output** — the tool walks the route bottom-up, identifies the first dirty hop as the likely root cause, and writes a human-readable "Findings Summary" citing the actual flagged values (real alarm text, real light levels, real PM counters) rather than a bare pass/fail.
- **JIRA + Slack integration** — results can post directly to a JIRA ticket (summary + full diagnostic attachment) and/or a Slack channel, so there's a documented trail without extra copy/paste.
- **Batch automation ("Morning Check")** — scans all open NOC tickets assigned to the user, runs the same trace automatically against each one based on ticket description triggers, and delivers a daily digest — turning a manual per-ticket routine into one click.

## The tools

**Circuit tracing**
- **Autotester** — full end-to-end T1 trace across all four hops; the core tool everything else builds on.
- **Reverse Autotester** — the same trace walked backwards: start at a GSX T1 and resolve inward to the customer DS1 on the 1250. For when a GSX alarm is all you have and nobody knows which circuit it is. Shares the Autotester page rather than having its own.
- **Site Sweep** — fast triage: checks every DS1 on a 1250 at once (no route trace) to see what's clean vs. worth a full Autotester run.
- **Morning Check** — batch-runs Autotester (plus fiber/OSPF/LDP checks) across every open ticket assigned to the user; posts to JIRA and Slack automatically.

**Health & diagnostics**
- **1250 Health Check** — high-level device health: version, interfaces, transceivers, LDP/OSPF, DS1 status, alarms.
- **NPT-1250 Discovery** — full read-only inventory dump for reference/documentation.
- **DS1 PMs** — physical status, alarms, and PM counters for specific DS1s.
- **Fiber Utilization** — real per-port IN/OUT bandwidth (max/avg) on the GE uplinks.
- **OSPF Events + Fiber Levels** — OSPF up/down history, adjacency uptime, and light-level checks.
- **IXC Check** — validates an IXC handoff circuit from the 1800 (config, OSPF/LDP, reachability).

**GSX testing**
- **GSX Perfmon (96 Intervals)** — full 24-hour interval history on a single T1, for stress-test/capacity planning.
- **DS1 Test Builder (AIS / Loops to GSX)** — resolves the route, runs live prechecks, and generates ready-to-run AIS/loop test commands.
- **MUX CEM Loop Script** — precheck-gated MOP generator for hairpin loop test scenarios on the MUX.

## Value

- Turns a multi-device, multi-command manual trace into a single form submission.
- Reduces misdiagnosis: cites the actual flagged value/alarm instead of asking the reader to log in and check.
- Every run is documented on the ticket automatically — no manual copy/paste, consistent format every time.
- Morning Check turns "check my ticket queue" into a one-click daily routine.

## Where it stands

- Actively used and iterated against live tickets for ~2 months; several real bugs (misleading summaries, collapsed PM values, formatting issues) have been caught and fixed through that use.
- Currently a personal proof of concept — runs locally, single user, not yet an IT-sanctioned or team-wide tool.
- Natural next conversation for the demo: whether this is worth investing in as a shared/team tool, and what that would take (hosting, multi-user access, security review).
