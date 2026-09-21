#!/usr/bin/env python3
"""Two questions left after the stability probe.

1. MONOTONICITY, done properly: one axis varying (the knight's position and the
   wizard's readiness), every other fact held identical. The first probe's
   ladder failed monotonicity, but its rungs differed on more than one axis.

2. TONE CONTAMINATION: padding the state with irrelevant scenery moved p by
   -0.08, reproducibly. Is that dilution (any padding hedges the answer) or
   tone (gloomy prose reads as bad odds)? In a fiction app the state is full of
   atmospheric prose, so this decides whether the template must be terse.
"""

import json
import os
import sqlite3
import statistics
import sys
import urllib.request

URL, MODEL = "https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13"
Q = {
    "success": {
        "type": "noul",
        "instructions": "The knight prevails in this exchange.",
        "criteria": {
            "true": "The knight ends the exchange in control, on his terms.",
            "false": "He is beaten, driven off, or forced to disengage.",
        },
    }
}

# Held identical across every rung. Only POSITION varies.
CAST = "Ser Alric is a knight in mail with a longsword. Maren is a wizard in no armour, carrying a staff. Neither is wounded. "

RUNGS = [
    ("1 unaware, 40 paces", "Maren is 40 paces away and already casting. Alric has not seen her."),
    ("2 aware, 40 paces", "Maren is 40 paces away with her spells prepared. Alric has seen her."),
    ("3 aware, 10 paces", "Maren is 10 paces away with her spells prepared. Alric has seen her."),
    ("4 reach, shield up", "Alric is within sword's reach. Maren's shield spell is active."),
    ("5 reach, shield down", "Alric is within sword's reach. Maren's shield spell is not active."),
    (
        "6 reach, spent",
        "Alric is within sword's reach. Maren's shield spell is not active, "
        "her staff is broken, and her prepared spells are spent.",
    ),
]

BASE = CAST + RUNGS[4][1]
PADS = {
    "none": "",
    "neutral": " The courtyard is paved in grey flagstone. A cart stands by the north gate, "
    "half unloaded. It is the third week of autumn.",
    "ominous": " The rain has not let up since dawn. Somewhere past the treeline a bell tolls, "
    "and the smell of woodsmoke drifts up from the burning village below.",
    "hopeful": " Morning light breaks over the courtyard wall. Larks are up, and from the "
    "village below drifts the smell of baking bread and someone singing.",
}


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
        return json.loads(r.read())["answers"]["success"]["noul"]


def main():
    k = key()
    print("=" * 66)
    print("1. SINGLE-AXIS LADDER (only position/readiness varies)")
    print("=" * 66)
    ps = []
    for name, rung in RUNGS:
        p = ask(k, CAST + rung)
        ps.append(p)
        bar = "#" * int(p * 40)
        print(f"  {name:<22} {p:.2f}  {bar}")
    mono = all(a <= b + 0.01 for a, b in zip(ps, ps[1:]))
    print(f"\n  monotonic: {'YES' if mono else 'NO'}    span {min(ps):.2f}..{max(ps):.2f} (range {max(ps) - min(ps):.2f})")
    if not mono:
        for i, (a, b) in enumerate(zip(ps, ps[1:])):
            if a > b + 0.01:
                print(f"    violation: rung {i + 1} ({a:.2f}) > rung {i + 2} ({b:.2f})")

    print("\n" + "=" * 66)
    print("2. TONE CONTAMINATION (identical facts, different scenery)")
    print("=" * 66)
    res = {}
    for name, pad in PADS.items():
        vals = [ask(k, BASE + pad) for _ in range(3)]
        res[name] = statistics.mean(vals)
        print(f"  {name:<9} {res[name]:.3f}   {[f'{v:.2f}' for v in vals]}")
    base = res["none"]
    print(f"\n  vs unpadded ({base:.3f}):")
    for n in ("neutral", "ominous", "hopeful"):
        print(f"    {n:<9} {res[n] - base:+.3f}")
    spread = max(res[n] for n in ("neutral", "ominous", "hopeful")) - min(res[n] for n in ("neutral", "ominous", "hopeful"))
    neutral_shift = abs(res["neutral"] - base)
    print(f"\n  tone spread among pads : {spread:.3f}")
    print(f"  dilution (neutral pad) : {neutral_shift:.3f}")
    print(
        "  -> "
        + (
            "TONE drives it: atmospheric prose changes the odds."
            if spread > max(0.05, neutral_shift * 1.5)
            else "DILUTION drives it: any padding hedges, tone matters less."
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
