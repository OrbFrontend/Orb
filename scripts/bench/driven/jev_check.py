"""Preflight for Bench 2: confirm Jev still labels the hand-written fixtures correctly before scoring a run.

The fixtures cover pivots, end-of-reply hooks, static description, long replies, and dialogue that asks questions without
changing the situation.
Run it before every scoring pass; a wrong label or a changed returned model means the judge moved and the run waits.

Credentials come from JEV_URL, JEV_API_KEY and JEV_MODEL, or else from the decision endpoint in backend/data/app.db.
The key is never printed.

    PYTHONPATH=. .venv/bin/python scripts/bench/driven/jev_check.py [results.json]
"""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from backend.inference.jev import ChoiceAnswer, DecisionClient, DecisionQuestion, decisions_url

ROOT = Path(__file__).resolve().parents[3]

# The benchmark question. Changing a word here starts a new judge version: rerun the fixtures and the 40 hand labels.
SHAPE = DecisionQuestion(
    "shape",
    "Which describes how the reply moves the scene?",
    {
        "driven": "Partway through, the situation changes (an event, a decision, a discovery or a request with stakes) "
        "and the rest of the reply develops it.",
        "afterthought": "The situation stays the same until the very end, where the reply adds a hook, event or question.",
        "static": "The situation stays the same throughout; the reply only reacts, describes or chats, even if a "
        "character asks questions along the way.",
    },
    "choice",
)

REQUEST = "I take a seat at the end of the bar and ask Mara for whatever's cheapest."

_OPENING = (
    'Mara slid a chipped glass down the bar without looking up. "Cheapest is the house red. Tastes like a penny, but '
    "it's honest.\" She poured, wiped the rim with her thumb, and set the bottle back on the shelf."
)

_LONG_BODY = [
    _OPENING[:-1] + " beside a row of others, each one dustier than the last.",
    "Rain drummed on the shutters in long, uneven gusts. A fiddler in the corner worked through the same three tunes he "
    "always played, missing the same high note each time, and nobody minded. The fire in the hearth popped and settled, "
    "throwing an orange wash across the low beams and the fishing nets someone had hung there years ago and never taken "
    "down.",
    '"Weather\'s been like this all week," Mara said, mostly to herself. "Boats stay in, men drink, I get paid. Can\'t '
    'complain." She dried a mug, held it up to the lamp, frowned at a smudge, and dried it again.',
    "The tavern smelled of wet wool, woodsmoke and old beer soaked into the floorboards. A dog slept under a table by the "
    "door, twitching at whatever it dreamed of. Two fishermen argued quietly over dice, their voices rising and falling "
    "like the rain. An old woman knitted by the fire, her needles clicking in a rhythm that matched nothing else in the "
    "room.",
    "Mara refilled a carafe of water and set it near your elbow without being asked. She leaned on the bar for a while, "
    "chin on her fist, watching the rain run down the window. The lamplight caught the copper rings on her fingers and "
    "the grey just starting at her temples. She hummed along with the fiddler under her breath, a half-beat behind him.",
    '"Glass is chipped on the left," she added. "Drink from the right." Then she went back to her mugs.',
]

# (expected label, paragraphs)
FIXTURES: dict[str, tuple[str, list[str]]] = {
    "pivot_a": (
        "driven",
        [
            _OPENING,
            "Then the bell over the door rang, and her hand stopped halfway to the rag. Two men in harbor-guard grey "
            "stepped in out of the rain. Mara's eyes went to them, then to you, then to the narrow door behind the bar.",
            '"Drink up slow," she murmured, pressing a brass key flat under your glass. "When I drop a tray, you go '
            "through that door and you don't stop until you hit the alley. Someone's been asking for a stranger who "
            'looks a lot like you."',
            "She straightened, smiled at the guards, and reached for the stack of trays.",
        ],
    ),
    "pivot_b": (
        "driven",
        [
            '"Cheapest is the stuff nobody else will drink," Mara said, uncorking a dusty bottle with her teeth. She '
            "poured you a finger of something amber and cloudy.",
            "Halfway through the pour she paused and leaned closer, studying the ring on your hand. Her easy grin "
            "thinned. \"Where'd you get that?\" She didn't wait for an answer; she was already untying her apron.",
            '"Bar\'s closed," she called to the room, loud enough to turn heads. To you, quieter: "The last man who '
            "wore that ring owed my brother a ship. You're coming with me to the docks, and you can explain it to him "
            'yourself."',
        ],
    ),
    "afterthought_a": (
        "afterthought",
        [
            _OPENING,
            "Rain drummed on the shutters. A fiddler in the corner worked through the same three tunes he always "
            "played, and the fire in the hearth popped and settled. Mara hummed along under her breath as she dried a "
            "row of mugs, the lamplight catching the copper rings on her fingers.",
            "The tavern smelled of wet wool and woodsmoke. A dog slept under a table by the door, twitching at whatever "
            "it dreamed of. Mara set another mug on the shelf and finally looked at you.",
            '"So," she said. "What brings you to Saltmarsh?"',
        ],
    ),
    "afterthought_b": (
        "afterthought",
        [
            'Mara poured the house red and pushed it toward you. "Two coppers." She took your coins and dropped them '
            "in the jar by the taps.",
            "The evening crowd was thin. A pair of fishermen argued quietly over dice, and an old woman knitted by the "
            "fire. Mara leaned on the bar and watched the rain run down the window, the lamplight soft on her face. The "
            "fiddler finished his tune and started it again.",
            "Suddenly, the door burst open.",
        ],
    ),
    "static_a": (
        "static",
        [
            _OPENING,
            "Rain drummed on the shutters. A fiddler in the corner worked through the same three tunes he always "
            "played, and the fire in the hearth popped and settled. Mara hummed along under her breath as she dried a "
            "row of mugs.",
            "The tavern smelled of wet wool and woodsmoke. She set another mug on the shelf and went on drying.",
        ],
    ),
    "static_b": (
        "static",
        [
            "You take a seat at the end of the bar. Mara nods and reaches for the cheapest bottle, the house red, and "
            "pours you a glass.",
            '"Here you go," she says, setting it in front of you. "Cheapest we\'ve got." She smiles, takes your '
            "coins, and goes back to wiping down the counter, the tavern warm and quiet around you.",
        ],
    ),
    # About 1,600 characters each, to check that length alone does not sway the label.
    "long_static": ("static", _LONG_BODY),
    "long_afterthought": (
        "afterthought",
        _LONG_BODY
        + ['Then she looked at you properly for the first time. "You\'re not from Saltmarsh. What are you running from?"'],
    ),
    # Dialogue that changes nothing: the earlier wording labeled the first and third `driven`.
    "question_static": (
        "static",
        [
            _OPENING,
            '"You\'ve been on the road a while, haven\'t you?" she asked, not looking up from the mug she was drying. "Boots '
            'say so." She did not wait for an answer. Rain drummed on the shutters, and the fiddler in the corner started the '
            "same tune again.",
            "Mara set the mug on the shelf, took down another, and went on drying. The fire popped and settled. A dog slept "
            "under a table by the door, twitching at whatever it dreamed of.",
        ],
    ),
    "chatty_afterthought": (
        "afterthought",
        [
            _OPENING,
            '"Most folk who sit at that end want to be left alone," Mara said, leaning on the bar. "That\'s fine by me. I '
            "get enough talk from the fishermen. They'll tell you about every fish they ever lost.\" She laughed under her "
            "breath and wiped a ring off the counter.",
            "The fire popped. The fiddler missed his high note, and someone groaned good-naturedly. Mara refilled the water "
            "jug near your elbow without being asked.",
            '"So," she said. "Passing through, or staying the night?"',
        ],
    ),
    "chatty_static": (
        "static",
        [
            _OPENING,
            '"Where are you headed, then? North?" Mara asked. "Roads north are mud this time of year. Mud to your knees." '
            "She shook her head and went back to her mugs, humming along with the fiddler.",
            '"My cousin went north once," she added after a while. "Came back with a cough and a wife. Kept the cough '
            'longer." The rain kept on. A log shifted in the fire and sent up a spray of sparks.',
            "She set the bottle back on the shelf and leaned against the counter, content to watch the room.",
        ],
    ),
    "dialogue_pivot": (
        "driven",
        [
            _OPENING,
            '"Where are you headed, then?" Mara asked, then stopped with the rag still in her hand. "North? You said north?" '
            "She glanced at the door, then leaned across the bar.",
            "\"Then you'll pass the Greywater bridge. My brother keeps the toll there and he hasn't answered a letter in "
            'three weeks." She pulled a folded paper from her apron and pressed it into your hand. "Give him this. And if '
            "the toll house is empty, don't cross. Come back here and tell me.\"",
            "She straightened and turned to the next customer as if nothing had been said, but her hands were not quite steady "
            "on the tap.",
        ],
    ),
}


def state_for(request: str, reply: str) -> str:
    """The judge's state: the same shape as the Editor gate's state in backend/pipeline/passes/editor/gate.py."""
    return f"Current request:\n{request}\n\nReply:\n{reply}"


def judge_client() -> DecisionClient:
    url, key, model = os.environ.get("JEV_URL"), os.environ.get("JEV_API_KEY"), os.environ.get("JEV_MODEL")
    if not (url and key and model):
        conn = sqlite3.connect(ROOT / "backend" / "data" / "app.db")
        endpoint_id, model = conn.execute("select decision_endpoint_id, decision_model from settings").fetchone()
        url, key = conn.execute("select url, api_key from endpoints where id = ?", (endpoint_id,)).fetchone()
    return DecisionClient(decisions_url(url), key or "", model, timeout=60)


async def main() -> int:
    client = judge_client()
    rows, wrong = [], 0
    for name, (expected, paragraphs) in FIXTURES.items():
        response = await client.decide(state_for(REQUEST, "\n\n".join(paragraphs)), [SHAPE])
        answer = response.answers.get("shape")
        if not isinstance(answer, ChoiceAnswer):
            print(f"{name:18} no answer")
            wrong += 1
            continue
        ok = answer.selected == expected
        wrong += not ok
        probs = " ".join(f"{k}={v:.2f}" for k, v in answer.probabilities.items())
        print(f"{name:18} {'ok ' if ok else 'BAD'} {answer.selected:12} {probs}  [{response.returned_model}]")
        rows.append(
            {
                "fixture": name,
                "expected": expected,
                "selected": answer.selected,
                **answer.probabilities,
                "returned_model": response.returned_model,
            }
        )
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(rows, indent=1))
    print(f"{len(FIXTURES) - wrong}/{len(FIXTURES)} fixtures labeled as expected")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
