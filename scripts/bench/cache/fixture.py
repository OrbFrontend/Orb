"""Freeze SFW chat-shaped histories using the inference host's tokenizer."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx

from scripts.bench.archive import write_text

CARD = {
    "id": "benchmark-mara",
    "name": "Mara Vale",
    "description": "Mara Vale is a meticulous harbor archivist in the seaside town of Bellwick. She is thirty-five, speaks plainly, and repairs old maps. She keeps her promises, challenges doubtful evidence, and cares about the harbor workers. She has no supernatural abilities. The user is a visiting surveyor helping her trace a missing public ferry schedule.",
    "personality": "Practical, observant, dry humor, patient with questions, willing to take initiative.",
    "scenario": "The municipal map room overlooks Bellwick harbor. Mara and the visiting surveyor have sorted old records and now sit at a table beside a window. Keep the story safe for work and grounded in ordinary harbor life.",
    "first_mes": "",
    "mes_example": "",
}
PERSONA = "A visiting adult surveyor who listens carefully, takes notes, and leaves other characters free to decide their own actions."
USER_SCRIPT = [
    "I take a seat beside the map table and set my notebook down. I wait while Mara examines the papers.",
    "I read the note she points to, then look through the window at the harbor and listen to what she says.",
    "I slide my notebook toward the middle of the table. I ask which detail she thinks we should check first.",
    "I walk over to the shelves and look at the labels on the boxes, taking my time to find the right record.",
    "I bring the record back to the table and open it carefully. I follow the entries with a finger as I read.",
    "I put a small pencil mark beside the date and pause. I let Mara finish comparing the two accounts.",
    "I gather the loose papers into a folder and stand by the doorway, ready to follow her to the next place.",
    "I stop beside the harbor railing and watch the ferry workers. I keep my notebook tucked under my arm.",
    "I sit on the bench outside the office and sip the water I brought. I listen to the conversation nearby.",
    "I turn to the last page of my notebook and write down the names we have found. I wait for Mara's view.",
]


def exchange(index: int) -> list[dict]:
    objects = [
        "dock ledger",
        "ferry register",
        "tide chart",
        "parcel book",
        "station map",
        "repair account",
        "ticket folder",
        "cargo list",
    ]
    places = [
        "north pier",
        "map room",
        "ferry office",
        "east quay",
        "storehouse",
        "harbor steps",
        "customs desk",
        "public landing",
    ]
    item, place = objects[index % len(objects)], places[(index * 3) % len(places)]
    user = f"I open record {index + 1} about the {place}, compare the dates in the {item}, and ask Mara what she makes of it."
    prose = (
        f"Mara turns the {item} so the window lights the handwritten entries. Record {index + 1} covers a different week from the one above it. "
        f"She checks the printed date against the harbor stamp, then places a blank slip beside the {place} entry. "
        f'"This clerk counted arrivals, while the other counted departures," she says. "We need to keep those columns separate." '
        f"A cart rattles across the cobbles outside, and a worker lifts a crate onto its bed. Mara waits for the wheels to pass before reading the next line. "
        f"The figures show {index % 9 + 2} deliveries on the first morning and {index % 7 + 1} on the second. She writes both in the margin of her own notebook. "
        f"A folded receipt bears the signature of clerk number {index + 11}; its corner has been clipped to mark a payment already settled. "
        f"She sets that receipt apart from the unpaid accounts and checks the paper underneath. Its address refers to a shop beside the {place}. "
        f'"I can ask the shopkeeper about this after we finish the dates," she says. "For now, this tells us which records belong together." '
        "She closes the folder, ties its cotton ribbon, and puts it on the cleared end of the table. The next folder is already within reach."
    )
    return [{"role": "user", "content": user}, {"role": "assistant", "content": prose}]


def freeze(base: str, output: Path, sizes: list[int]):
    output.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base, timeout=120, trust_env=False) as client:
        for target in sizes:
            rows = []
            for index in range(400):
                rows.extend(exchange(index))
                response = client.post(
                    "/tokenize", json={"content": "\n".join(row["content"] for row in rows), "add_special": False}
                )
                response.raise_for_status()
                measured = len(response.json()["tokens"])
                if measured >= target:
                    break
            else:
                raise ValueError("could not reach requested history size")
            fixture = {
                "id": f"bellwick-{target}",
                "target_tokens": target,
                "history_tokens": measured,
                "tokenizer": base + "/tokenize",
                "card": CARD,
                "persona": PERSONA,
                "history": rows,
                "user_script": USER_SCRIPT,
            }
            payload = json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"
            path = output / f"bellwick-{target}.json.gz"
            write_text(path, payload)
            print(
                json.dumps(
                    {
                        "path": str(path),
                        "history_tokens": measured,
                        "messages": len(rows),
                        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[2000, 8000, 16000, 32000])
    args = parser.parse_args()
    freeze(args.base, args.output, args.sizes)
