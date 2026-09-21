#!/usr/bin/env python3
"""Separate two causes of the 0.48/0.81/0.63 drift seen in the main probe.

A: same EXACT string, repeated      -> isolates model nondeterminism.
B: controlled paraphrases, same facts -> isolates phrasing sensitivity.
C: same string + irrelevant prose    -> isolates verbosity bias.

The fixes are opposite: nondeterminism is cured by sampling N and averaging,
phrasing sensitivity is cured by authoring discipline (or not at all).
"""

import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

Q = {
    "success": {
        "type": "noul",
        "instructions": "The knight prevails in this exchange.",
        "criteria": {
            "true": "The knight ends the exchange in control, on his terms.",
            "false": "He is beaten, driven off, or forced to disengage.",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "How costly this exchange is for the loser.",
        "criteria": [
            "No real cost; a bruise to pride, nothing more.",
            "A setback -- minor wound, lost ground, lost face.",
            "A serious blow -- lasting injury or a real forfeit.",
            "Catastrophic -- crippling, or fatal.",
        ],
    },
}

BASE = (
    "Ser Alric, a knight in mail with a longsword drawn, has closed to sword's "
    "reach of Maren, a wizard in no armour. She has no shield spell up and no "
    "room to retreat. He is unhurt."
)

# Same facts every time: mail, longsword, in reach, no shield spell, cornered, unhurt.
PARAPHRASES = [
    BASE,
    (
        "Maren the wizard, unarmoured, has let Ser Alric -- a knight in mail, longsword "
        "drawn -- get inside sword's reach. Her shield spell is not up and she has "
        "nowhere to fall back to. He has taken no wounds."
    ),
    ("At sword's reach: Ser Alric, mailed and unhurt, longsword drawn. Maren, unarmoured, shield spell down, cornered."),
    (
        "Ser Alric (knight, mail, longsword, uninjured) is within sword's reach of Maren "
        "(wizard, no armour, no shield spell active, no retreat available)."
    ),
    (
        "The unhurt knight Ser Alric, wearing mail and holding a drawn longsword, now "
        "stands close enough to strike the wizard Maren, who wears no armour, has let "
        "her shield spell lapse, and cannot retreat."
    ),
]

PADDED = BASE + (
    " The rain had not let up since dawn. Somewhere beyond the treeline a "
    "bell was ringing the hour, and the smell of woodsmoke carried on the "
    "wind from the village below."
)


def key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    con = sqlite3.connect("backend/data/app.db")
    try:
        return con.execute(
            "SELECT api_key FROM endpoints WHERE url LIKE '%openrouter.ai%' AND api_key != '' ORDER BY id LIMIT 1"
        ).fetchone()[0]
    finally:
        con.close()


def ask(k, state):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"model": MODEL, "state": state, "questions": Q}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {k}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        a = json.loads(r.read())["answers"]
    return a["success"]["noul"], a["severity"]["score"]


def report(name, vals, note):
    ns = [v[0] for v in vals]
    ss = [v[1] for v in vals]
    print(f"\n{name}")
    print(f"  noul     : {[f'{x:.2f}' for x in ns]}")
    print(f"             range {max(ns) - min(ns):.3f}   sd {statistics.pstdev(ns):.3f}")
    print(f"  severity : {[f'{x:.2f}' for x in ss]}")
    print(f"             range {max(ss) - min(ss):.3f}   sd {statistics.pstdev(ss):.3f}")
    print(f"  -> {note}")
    return statistics.pstdev(ns)


def main():
    k = key()
    print("A. IDENTICAL STRING x6  (isolates nondeterminism)")
    a = [ask(k, BASE) for _ in range(6)]
    sd_a = report("A", a, "any spread here is the model, not the prose")

    print("\nB. CONTROLLED PARAPHRASES x5  (isolates phrasing; facts held constant)")
    b = [ask(k, p) for p in PARAPHRASES]
    sd_b = report("B", b, "spread beyond A's is caused by wording alone")

    print("\nC. BASE + IRRELEVANT SCENERY x3  (isolates verbosity bias)")
    c = [ask(k, PADDED) for _ in range(3)]
    sd_c = report("C", c, "shift vs A's mean = padding moved the judgement")

    print("\n" + "=" * 70)
    print("DIAGNOSIS")
    print("=" * 70)
    mean_a = statistics.mean([x[0] for x in a])
    mean_c = statistics.mean([x[0] for x in c])
    print(f"  nondeterminism (A sd)      : {sd_a:.3f}")
    print(f"  phrasing sensitivity (B sd): {sd_b:.3f}")
    print(f"  padding shift (C-A mean)   : {mean_c - mean_a:+.3f}")
    if sd_a < 0.02:
        print("  -> model is ~deterministic; averaging N samples buys nothing.")
    else:
        print(f"  -> nondeterministic; averaging N samples cuts sd by ~sqrt(N).")
    if sd_b > max(sd_a * 2, 0.05):
        print("  -> PHRASING DOMINATES. The state template is a tuning surface,")
        print("     and two authors writing the same scene get different odds.")
    else:
        print("  -> phrasing is not the main driver.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
