#!/usr/bin/env python3
"""
ds1_util.py -- shared DS1 list parsing for every tool that takes DS1 input.

WHY A SHARED MODULE: five call sites parsed DS1 input with the same inline
one-liner (autotester_standalone, app.py x3, ds1_gsx_test). Adding card
expansion to each would have been five copies of one rule -- the
drift-by-accretion shape that had LAX circuits reported as DFW for two
months. One function, one place.

CARD EXPANSION (pcapps, 2026-10-01): a bare card name means every DS1 on it.

    "ts1"              -> ts1/1 .. ts1/28
    "ts1, ts3"         -> both cards, 56 DS1s
    "ts1/5"            -> just that one, unchanged
    "ts1, ts3/4"       -> 28 + 1

Every DS1 on the card is returned whether or not it is provisioned -- checking
all of them is the point, and an unprovisioned port is a finding, not
something to pre-filter out of the list.

Built by P. Capps.
"""

import re

# A 1250 TS card carries 28 DS1s. Not configurable: it is the hardware.
DS1_PER_CARD = 28

# "ts1" / "TS12" -- a card with no /port. The port form is "ts1/5".
_CARD_ONLY_RE = re.compile(r'^ts(\d{1,2})$', re.IGNORECASE)
_DS1_RE       = re.compile(r'^ts(\d{1,2})/(\d{1,2})$', re.IGNORECASE)


def expand_ds1_list(raw):
    """
    Parse a user-entered DS1 string into a concrete DS1 list.

    Accepts ',' or '&' separators (both were already in use). Entries are
    lower-cased, de-duplicated, and kept in the order first seen so the report
    follows what the engineer typed.

    Returns (ds1_list, notes). `notes` describes any expansion that happened,
    for echoing back to the operator -- an input that silently became 28
    device logins should say so before it runs.

    Unrecognised entries are PASSED THROUGH unchanged rather than dropped. A
    silently discarded DS1 is a circuit nobody checked; downstream validation
    is better placed to reject it with a useful message.
    """
    out, seen, notes = [], set(), []
    for part in re.split(r'[,&]', raw or ""):
        entry = part.strip().lower()
        if not entry:
            continue

        m = _CARD_ONLY_RE.match(entry)
        if m:
            card = "ts{}".format(int(m.group(1)))
            added = 0
            for p in range(1, DS1_PER_CARD + 1):
                ds1 = "{}/{}".format(card, p)
                if ds1 not in seen:
                    seen.add(ds1)
                    out.append(ds1)
                    added += 1
            notes.append("'{}' expanded to {} DS1s ({}/1-{}/{})".format(
                entry, added, card, card, DS1_PER_CARD))
            continue

        m = _DS1_RE.match(entry)
        if m:
            ds1 = "ts{}/{}".format(int(m.group(1)), int(m.group(2)))
            if ds1 not in seen:
                seen.add(ds1)
                out.append(ds1)
            continue

        # Unknown shape -- keep it; let the tool's own validation complain.
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out, notes


def _self_test():
    fails = []

    def chk(label, got, want):
        if got != want:
            fails.append("{}: got {!r} want {!r}".format(label, got, want))
        else:
            print("  ok  {}".format(label))

    l, n = expand_ds1_list("ts1")
    chk("ts1 -> 28 DS1s", len(l), 28)
    chk("ts1 first/last", (l[0], l[-1]), ("ts1/1", "ts1/28"))
    chk("ts1 note emitted", len(n), 1)

    l, _ = expand_ds1_list("ts1, ts3, ts5, ts8")
    chk("four cards -> 112", len(l), 112)
    chk("no duplicates", len(set(l)), 112)

    l, n = expand_ds1_list("ts1/5")
    chk("single DS1 unchanged", l, ["ts1/5"])
    chk("no expansion note", n, [])

    l, _ = expand_ds1_list("ts1, ts3/4")
    chk("mixed card + port", (len(l), l[-1]), (29, "ts3/4"))

    # A card and one of its own ports must not duplicate that port.
    l, _ = expand_ds1_list("ts1/5, ts1")
    chk("port then card dedupes", len(l), 28)
    chk("typed port stays first", l[0], "ts1/5")

    chk("ampersand separator", expand_ds1_list("ts1/1 & ts1/2")[0],
        ["ts1/1", "ts1/2"])
    chk("case insensitive", expand_ds1_list("TS1/3")[0], ["ts1/3"])
    chk("padded port normalised", expand_ds1_list("ts01/05")[0], ["ts1/5"])
    chk("blank input", expand_ds1_list("")[0], [])
    # Junk is passed through, never silently dropped.
    chk("junk passed through", expand_ds1_list("ts1/5, garbage")[0],
        ["ts1/5", "garbage"])

    if fails:
        print("\nFAILED:")
        for f in fails:
            print("  !! " + f)
    else:
        print("\nAll ds1_util self-tests passed.")
    return not fails


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
