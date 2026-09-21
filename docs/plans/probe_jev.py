#!/usr/bin/env python3
"""Phase 0 probe for the decision-fragments plan.

Question under test: can jev act as an *educated dice check* for an "Outcome"
fragment -- and does it DISCRIMINATE, or does it just answer confidently?

A dice check that returns 0.95 for every scenario is a verdict machine, not a
dice check. So we don't ask "does it respond"; we ask:

  1. direction   -- does advantage move the number the right way?
  2. monotonicity-- does a ladder of escalating advantage produce a ladder of p?
  3. spread      -- is the middle of the range ever used? (narrative variety)
  4. stability   -- does a trivial rewording change the answer? (reliability)
  5. transfer    -- does any of the above survive moving from combat to social?

Routes, in priority order (first one with creds wins):
  TYPESAFE_API_KEY                             -> https://api.typesafe.ai/v1/systemone
  CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID -> Workers AI, model typesafe/jev
  OPENROUTER_API_KEY                           -> attempted, expected to 404
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 60
MODEL = "typesafe/jev-1.13"

# --- the Outcome fragment, as it would actually be authored -------------------
# One request carries all three: this is the "batch" the protocol gives free.
QUESTIONS = {
    # The dice check itself. noul returns P(true), not a boolean -- which is
    # exactly what a weighted roll wants.
    "success": {
        "type": "noul",
        "instructions": "The knight prevails in this exchange.",
        "criteria": {
            "true": "The knight ends the exchange in control, on their terms.",
            "false": "The knight is beaten, driven off, or forced to disengage.",
        },
    },
    # Cost of the outcome. This is the part the Director plans around: not
    # *whether* but *how expensively*.
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
    # The beat shape. A hook the Director can write toward.
    "beat": {
        "type": "choice",
        "instructions": "What kind of story beat does this exchange become?",
        "criteria": {
            "decisive": "It ends cleanly; one side clearly wins.",
            "pyrrhic": "It is won, but at a price that matters.",
            "interrupted": "Something external cuts it short before resolution.",
            "stalemate": "Neither side can finish it; they break apart.",
        },
    },
}

KNIGHT = "Ser Alric, a knight in mail, longsword drawn. Trained, but no magic."
WIZARD = "Maren, a wizard. No armour, a staff, and prepared evocations."

# --- scenario ladders ---------------------------------------------------------
# Each ladder is ordered by *expected* advantage to the knight, ascending.
# If jev is a usable dice check, p(success) should climb roughly with the index.

COMBAT = [
    (
        "ambush-at-range",
        f"{WIZARD} ambushes {KNIGHT} across forty paces of open ground. "
        "The knight has no cover and has not yet seen her. She is already casting.",
    ),
    (
        "open-ground-aware",
        f"{KNIGHT} and {WIZARD} face each other across thirty paces of open ground. "
        "Both saw the other coming. Neither has moved yet.",
    ),
    (
        "knight-wounded-in-melee",
        f"{KNIGHT} -- bleeding from a thigh wound, breathing hard -- has closed to "
        f"sword's reach of {WIZARD}, who is unhurt and still has spells prepared.",
    ),
    (
        "clean-melee",
        f"{KNIGHT} has closed to sword's reach of {WIZARD}. She has no shield spell up and no room to retreat. He is unhurt.",
    ),
    (
        "melee-wizard-spent",
        f"{KNIGHT}, unhurt, has {WIZARD} backed against a wall at sword's reach. "
        "She is wounded, her staff is broken, and her prepared spells are spent.",
    ),
]

SOCIAL = [
    (
        "no-leverage",
        "A penniless beggar, unknown at court, asks the paranoid King Ostred to "
        "pardon a condemned thief. The beggar offers nothing and the king is "
        "mid-purge, executing those he suspects.",
    ),
    (
        "loyal-service",
        f"{KNIGHT}, who has served King Ostred loyally for eleven years, asks him "
        "to pardon a condemned thief. He offers nothing but that service.",
    ),
    (
        "holds-proof",
        f"{KNIGHT} asks King Ostred to pardon a condemned thief. He carries signed "
        "proof that the thief's accuser is the one plotting against the crown, and "
        "the king knows Alric does not lie.",
    ),
]

# Same situation as COMBAT[3], reworded -- and FACT-CONTROLLED.
#
# The first version of this block was a bad test: its variants silently differed
# on whether the wizard still had prepared spells, which is a material change to
# the situation, and it reported sd 0.135 / "UNSTABLE" as a result. jev was
# right; the test was wrong. Every rung below now states the identical facts --
# mail, longsword drawn, within sword's reach, shield spell down, cornered,
# knight unhurt -- and varies only the prose. Properly controlled, drift is
# sd ~0.025. See probe_jev_stability.py, which isolates this more carefully.
STABILITY = [
    (
        "paraphrase/a",
        "Ser Alric, a knight in mail with a longsword drawn, has closed to sword's "
        "reach of Maren, a wizard in no armour. She has no shield spell up and no "
        "room to retreat. He is unhurt.",
    ),
    (
        "paraphrase/b",
        "Maren the wizard, unarmoured, has let Ser Alric -- a knight in mail, "
        "longsword drawn -- get inside sword's reach. Her shield spell is not up "
        "and she has nowhere to fall back to. He has taken no wounds.",
    ),
    (
        "paraphrase/c",
        "Ser Alric (knight, mail, longsword, uninjured) is within sword's reach of "
        "Maren (wizard, no armour, no shield spell active, no retreat available).",
    ),
]

DB = "backend/data/app.db"
# OpenRouter serves jev on the *alpha decisions* surface, not chat completions --
# which is why it is absent from /api/v1/models and why the model page 404s.
# The SDK calls it openrouter.alpha.decisions.create(); these are the plausible
# REST spellings, tried in order until one stops 404ing.
CANDIDATE_PATHS = [
    "https://openrouter.ai/api/v1/alpha/decisions",
    "https://openrouter.ai/api/alpha/decisions",
    "https://openrouter.ai/api/v1/decisions",
]


def load_key():
    """Read the OpenRouter key from Orb's db. Never printed, never logged."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    import sqlite3

    db = os.environ.get("ORB_DB", DB)
    if not os.path.exists(db):
        return None
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT api_key FROM endpoints WHERE url LIKE '%openrouter.ai%' AND api_key != '' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def post(url, key, body):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read()), time.monotonic() - t0, None
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf8", "replace")
        return None, time.monotonic() - t0, {"http": e.code, "body": detail}
    except Exception as e:  # noqa: BLE001 -- probe: any failure is a datum
        return None, time.monotonic() - t0, {"error": repr(e)}


def resolve_path(key):
    """Find which REST spelling the alpha decisions API actually answers on."""
    probe_body = {
        "model": MODEL,
        "state": "ping",
        "questions": {"q": {"type": "noul", "instructions": "This is a test.", "criteria": {"true": "yes", "false": "no"}}},
    }
    for url in CANDIDATE_PATHS:
        raw, dt, err = post(url, key, probe_body)
        if raw is not None:
            print(f"  {url} -> OK ({dt:.2f}s)")
            return url, raw
        code = err.get("http")
        print(f"  {url} -> {code or err.get('error')} {str(err.get('body', ''))[:120]}")
        if code and code not in (404, 405):
            # Reached the right surface; the body tells us what it disliked.
            return url, None
    return None, None


def ask(url, key, state):
    """One request, three questions."""
    raw, dt, err = post(url, key, {"model": MODEL, "state": state, "questions": QUESTIONS})
    if raw is None:
        return None, dt, err
    return raw.get("answers", {}), dt, raw


def run(label, ladder, url, key, dump_first):
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(f"{'scenario':<26} {'p(success)':>11} {'severity':>9} {'conf':>6} {'beat':>12} {'lat':>7}")
    print("-" * 78)
    rows, lats = [], []
    for name, state in ladder:
        ans, dt, raw = ask(url, key, state)
        lats.append(dt)
        if not ans:
            print(f"{name:<26} {'FAILED':>11}  {json.dumps(raw)[:44]}")
            rows.append((name, None, None, None))
            continue
        if dump_first and not dump_first[0]:
            dump_first[0] = True
            print("\n--- raw response (contract pin) ---")
            print(json.dumps(raw, indent=2)[:1800])
            print("--- end raw ---\n")
        p = ans.get("success", {}).get("noul")
        sev = ans.get("severity", {}).get("score")
        conf = ans.get("severity", {}).get("confidence")
        beat = ans.get("beat", {}).get("choice")
        fmt = lambda v, n=3: f"{v:.{n}f}" if isinstance(v, (int, float)) else "-"
        print(f"{name:<26} {fmt(p):>11} {fmt(sev, 2):>9} {fmt(conf, 2):>6} {str(beat):>12} {dt:>6.2f}s")
        rows.append((name, p, sev, beat))
    return rows, lats


def verdict(combat, social, stab, lats):
    print(f"\n{'=' * 78}\nVERDICT\n{'=' * 78}")
    ps = [r[1] for r in combat if r[1] is not None]
    if len(ps) >= 2:
        print(f"combat spread      : {min(ps):.3f} .. {max(ps):.3f}  (range {max(ps) - min(ps):.3f})")
        mono = all(a <= b + 0.02 for a, b in zip(ps, ps[1:]))
        print(f"monotonic ladder   : {'YES' if mono else 'NO -- ordering violated'}")
        mid = sum(1 for p in ps if 0.2 < p < 0.8)
        print(
            f"usable mid-range   : {mid}/{len(ps)} scenarios in 0.2-0.8 "
            f"({'dice check' if mid else 'VERDICT MACHINE -- no narrative variety'})"
        )
    sps = [r[1] for r in social if r[1] is not None]
    if len(sps) >= 2:
        print(f"social spread      : {min(sps):.3f} .. {max(sps):.3f}  (range {max(sps) - min(sps):.3f})")
    stp = [r[1] for r in stab if r[1] is not None]
    if len(stp) >= 2:
        sd = statistics.pstdev(stp)
        print(
            f"rewording drift    : {min(stp):.3f} .. {max(stp):.3f}  "
            f"(sd {sd:.3f})  {'STABLE' if sd < 0.05 else 'UNSTABLE -- reliability risk'}"
        )
    if lats:
        lats = sorted(lats)
        p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))]
        print(f"latency med/p95    : {statistics.median(lats):.2f}s / {p95:.2f}s")
        print(f"  -> runs are serial in the turn path; budget p95 x runs-per-turn.")


def main():
    key = load_key()
    if not key:
        print("No OpenRouter key found (env OPENROUTER_API_KEY or endpoints table).")
        return 2
    print(f"resolving alpha decisions endpoint for {MODEL} ...")
    url, ping = resolve_path(key)
    if not url:
        print(
            "\nNo candidate path answered. The REST spelling differs from all three "
            "guesses -- check the OpenRouter alpha docs or sniff the SDK."
        )
        return 3
    if ping:
        print("\n--- ping response (contract pin) ---")
        print(json.dumps(ping, indent=2)[:900])
        print("--- end ping ---")
    print(f"\nroute: {url}")
    dump = [False]
    c, l1 = run("COMBAT LADDER (knight vs wizard)", COMBAT, url, key, dump)
    s_, l2 = run("SOCIAL LADDER (petition the king)", SOCIAL, url, key, dump)
    st, l3 = run("STABILITY (same scene, reworded)", STABILITY, url, key, dump)
    verdict(c, s_, st, l1 + l2 + l3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
