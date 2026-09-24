#!/usr/bin/env python3
"""Benchmark the ``update_state`` tool shape on a model.

Sends fixed after-reply scenarios through the real request text, tool schema,
alias mapping, parser, and validator -- the same builders a turn uses -- to an
OpenAI-compatible endpoint, forcing the ``update_state`` tool, and measures:

* the valid-call rate: a call arrived, parsed, and nothing in it was malformed or
  named an unknown field or entry;
* whether entries are retired as a list nears and reaches its limit (recall of
  the entries the reply resolved), and whether a full list is retired before
  it is added to;
* how often an entry that still holds is retired by mistake;
* whether a quiet reply is left alone (keep), and whether a changed value is set.

Each scenario runs ``--runs`` times. Paid endpoints are billed per run.

    .venv/bin/python scripts/probe_state_tool.py --endpoint http://127.0.0.1:8080/v1 --model gemma \\
        [--api-key-env OPENROUTER_API_KEY] [--runs 20] [--concurrency 4] [--out results.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core import (  # noqa: E402
    MAX_ACTIVE_ENTRIES,
    StateFragment,
    fold_events,
    plan_state_ops,
)
from backend.pipeline.passes.state.prompts import (  # noqa: E402
    build_state_request,
    entry_aliases,
)
from backend.pipeline.passes.state.step import parse_state_call  # noqa: E402
from backend.prompting.tool_schemas import (  # noqa: E402
    UPDATE_STATE_CHOICE,
    build_state_tool,
)

LOCATION = StateFragment(
    id="location",
    label="Location",
    heading="Location",
    description="Where the scene currently takes place, in a few words.",
    mode="value",
)
THREADS = StateFragment(
    id="open_threads",
    label="Open threads",
    heading="Open threads",
    description=(
        "Promises, questions, and plans that are still open. Add one when the story opens it; "
        "retire one once it is resolved, kept, or broken."
    ),
    mode="entries",
)
FRAGMENTS = (LOCATION, THREADS)

THREAD_TEXTS = (
    "Who hired the masked courier?",
    "Mara owes the smugglers forty silver by the new moon.",
    "The stolen ledger is hidden somewhere in the warehouse.",
    "Captain Voss suspects Mara of informing.",
    "The lighthouse keeper promised to signal if the patrol ships return.",
    "Mara's brother has not written in three weeks.",
    "The user agreed to meet Ilse at the chapel at dusk.",
    "A fever is spreading through the dock quarter.",
    "The harbor master's seal was forged.",
    "Someone has been following Mara since the market.",
    "The user's sword was left at the inn as collateral.",
    "The guild vote on the new tariff is in two days.",
)

SYSTEM = (
    "You are Mara, a smuggler in the port city of Kessel. Write Mara's replies in third person, "
    "past tense, and keep the story moving."
)


@dataclass(frozen=True)
class Scenario:
    name: str
    threads: int  # how many of THREAD_TEXTS are active, from the first
    location: str
    user: str
    reply: str
    resolves: frozenset[int]  # 1-based indexes into THREAD_TEXTS the reply resolves
    new_location: str | None  # a keyword the new value must contain; None = keep
    opens: int  # new threads the reply opens


SCENARIOS = (
    Scenario(
        name="near_limit",
        threads=11,
        location="the harbor warehouse",
        user="We tear the crates apart until we find it. Then we still have time to reach the chapel, right?",
        reply=(
            "Behind the third crate of salt cod, under a loose plank, Mara's fingers closed on oilcloth: the "
            "stolen ledger, dry and whole. She tucked it inside her coat and they ran. The chapel bell was still "
            "ringing dusk when they slipped through its side door, and Ilse was waiting by the font as she had "
            "promised. She turned the ledger's pages by candlelight and went pale. 'These payments,' she said, "
            "'all go to one buyer in the capital. Someone close to the crown.'"
        ),
        resolves=frozenset({3, 7}),
        new_location="chapel",
        opens=1,
    ),
    Scenario(
        name="full",
        threads=12,
        location="the harbor quay",
        user="Keep watching the lighthouse. Tell me the moment anything changes.",
        reply=(
            "Near midnight the lighthouse lamp went dark, once, twice, then burned green: the keeper's signal, "
            "just as he had sworn. Mara swore under her breath. Out past the breakwater, three patrol ships "
            "were sliding into line across the harbor mouth, lanterns hooded. 'They're closing the harbor,' she "
            "said. 'Nothing sails out of Kessel tonight.'"
        ),
        resolves=frozenset({5}),
        new_location=None,
        opens=1,
    ),
    Scenario(
        name="quiet",
        threads=11,
        location="the inn common room",
        user="Let's just eat something first. I'm starving.",
        reply=(
            "Mara waved the innkeeper over and ordered black bread, onion soup, and two mugs of small beer. "
            "She ate like a gull, elbows on the table, and laughed when the user burned their tongue. 'Slow "
            "down,' she said. 'The soup isn't going anywhere, even if the rest of this city is.'"
        ),
        resolves=frozenset(),
        new_location=None,
        opens=0,
    ),
)


def _events(scenario: Scenario) -> list[dict[str, Any]]:
    events = [{"fragment_id": LOCATION.id, "entry_id": "loc", "op": "add", "text": scenario.location}]
    events += [
        {"fragment_id": THREADS.id, "entry_id": f"t{i}", "op": "add", "text": THREAD_TEXTS[i - 1]}
        for i in range(1, scenario.threads + 1)
    ]
    return events


def build(scenario: Scenario) -> tuple[list[dict[str, str]], dict, Any, list]:
    """The request messages, the tool, and the state it was built from."""
    view = fold_events(_events(scenario))
    aliases = entry_aliases(FRAGMENTS, view)
    tool = build_state_tool(FRAGMENTS)
    request = build_state_request(FRAGMENTS, view, aliases, placement="after_reply", tool_schema=tool)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": scenario.user},
        {"role": "assistant", "content": scenario.reply},
        {"role": "user", "content": request},
    ]
    return messages, tool, view, aliases


@dataclass
class Outcome:
    scenario: str
    called: bool = False
    valid: bool = False
    error: str = ""
    retired: list[int] = field(default_factory=list)  # thread indexes
    adds: int = 0
    location_set: str | None = None
    rejected: list[str] = field(default_factory=list)
    raw: Any = None


def _tool_calls(body: dict) -> list[dict[str, Any]]:
    message = ((body.get("choices") or [{}])[0] or {}).get("message") or {}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass  # left as text: the parser reports it malformed
        calls.append({"name": function.get("name"), "arguments": arguments})
    return calls


async def run_one(client: httpx.AsyncClient, args: argparse.Namespace, scenario: Scenario) -> Outcome:
    messages, tool, view, aliases = build(scenario)
    outcome = Outcome(scenario.name)
    payload = {
        "model": args.model,
        "messages": messages,
        "tools": [tool],
        "tool_choice": UPDATE_STATE_CHOICE,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    try:
        response = await client.post("/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        outcome.error = str(error)[:300]
        return outcome
    calls = _tool_calls(body)
    outcome.raw = calls
    outcome.called = any(call["name"] == "update_state" for call in calls)
    ops, parse_rejections = parse_state_call(calls, FRAGMENTS, aliases)
    events, rejections = plan_state_ops(ops, {f.id: f for f in FRAGMENTS}, view, source="agent")
    outcome.rejected = [r.reason for r in (*parse_rejections, *rejections)]
    outcome.valid = outcome.called and not any(
        reason in ("malformed", "unknown_fragment", "unknown_entry") for reason in outcome.rejected
    )
    for event in events:
        if event["fragment_id"] == THREADS.id and event["op"] == "retire":
            outcome.retired.append(int(str(event["entry_id"])[1:]))
        elif event["fragment_id"] == THREADS.id and event["op"] == "add":
            outcome.adds += 1
        elif event["fragment_id"] == LOCATION.id and event["op"] in ("add", "revise"):
            outcome.location_set = event["text"]
    return outcome


def summarize(scenario: Scenario, outcomes: list[Outcome]) -> dict[str, Any]:
    n = len(outcomes)
    if not n:
        return {}
    expected = len(scenario.resolves)
    hits = sum(len(set(o.retired) & scenario.resolves) for o in outcomes)
    mistaken = [len(set(o.retired) - scenario.resolves) for o in outcomes]
    if scenario.new_location is None:
        location_ok = sum(o.location_set is None for o in outcomes)
    else:
        location_ok = sum(bool(o.location_set) and scenario.new_location in o.location_set.lower() for o in outcomes)
    return {
        "runs": n,
        "errors": sum(bool(o.error) for o in outcomes),
        "valid_call_rate": round(sum(o.valid for o in outcomes) / n, 3),
        "active_before": scenario.threads,
        "limit": MAX_ACTIVE_ENTRIES,
        "resolved_retire_recall": round(hits / (expected * n), 3) if expected else None,
        "mistaken_retires_per_run": round(sum(mistaken) / n, 3),
        "runs_with_a_mistaken_retire": sum(bool(m) for m in mistaken),
        "full_rejections": sum(o.rejected.count("full") for o in outcomes),
        "adds_per_run": round(sum(o.adds for o in outcomes) / n, 3),
        "expected_adds": scenario.opens,
        "location_correct_rate": round(location_ok / n, 3),
        "rejection_reasons": {r: sum(o.rejected.count(r) for o in outcomes) for r in {r for o in outcomes for r in o.rejected}},
    }


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    headers = {}
    key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    gate = asyncio.Semaphore(args.concurrency)
    scenarios = [s for s in SCENARIOS if not args.scenario or s.name in args.scenario]
    async with httpx.AsyncClient(base_url=args.endpoint.rstrip("/"), headers=headers, timeout=args.timeout) as client:

        async def guarded(scenario: Scenario) -> Outcome:
            async with gate:
                return await run_one(client, args, scenario)

        outcomes = await asyncio.gather(*(guarded(s) for s in scenarios for _ in range(args.runs)))
    by_name = {s.name: [o for o in outcomes if o.scenario == s.name] for s in scenarios}
    return {
        "model": args.model,
        "endpoint": args.endpoint,
        "summary": {s.name: summarize(s, by_name[s.name]) for s in scenarios},
        "runs": [o.__dict__ for o in outcomes],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8080/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", default="", help="environment variable holding the API key, if any")
    parser.add_argument("--runs", type=int, default=20, help="runs per scenario")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--scenario", action="append", choices=[s.name for s in SCENARIOS], help="run only these")
    parser.add_argument("--out", help="write the summary and every run's parsed call as JSON")
    parser.add_argument("--show-request", action="store_true", help="print each scenario's request and exit")
    args = parser.parse_args()
    if args.show_request:
        for scenario in SCENARIOS:
            messages, tool, _, _ = build(scenario)
            print(f"=== {scenario.name}\n{messages[-1]['content']}\n\n{json.dumps(tool, indent=2)}\n")
        return 0
    result = asyncio.run(main_async(args))
    print(json.dumps({"model": result["model"], "summary": result["summary"]}, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
