"""Check that a llama-server honours the controls the benchmarks depend on, before any Orb turn runs.

    python3 scripts/bench/smoke_llama.py [http://127.0.0.1:5000] [history_tokens]

Standard library only, so it runs on the inference host. It checks, on a chat-shaped prompt of about `history_tokens`:

1. A second request that only changes the last user message reuses the history (the checkpoint Orb relies on).
2. `cache_prompt: false` really re-reads the whole prompt, so Bench 1's no-reuse arms measure what they claim.
It prints prefill speed at that size, and the time a request spends before its slot starts (template rendering and
tokenizing), which grows with the number of messages rather than tokens.

The history is shaped like real chats: about 100-character user turns and 1,300-character replies.
"""

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 32000


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.load(resp)


def history(target: int) -> list[dict]:
    """Alternating turns of distinct text, grown until the tokenizer says the target is reached."""
    messages = [{"role": "system", "content": "You are a narrator in a quiet harbor town."}]
    i = 0
    while True:
        for _ in range(20):
            i += 1
            messages.append({"role": "user", "content": f"Turn {i}: I walk down to pier {i} and look for boat {i * 7}."})
            sentences = (
                f"Pier {i} creaks under the tide. Boat {i * 7} rocks against rope number {i * 13}, its hull painted "
                f"the colour of wet slate {i} years ago. A gull lands on post {i * 3}, looks at you, and leaves. "
            )
            messages.append({"role": "assistant", "content": sentences * 6})
        text = "\n".join(m["content"] for m in messages)
        if len(post("/tokenize", {"content": text})["tokens"]) >= target:
            return messages


def chat(messages: list[dict], **extra) -> tuple[dict, float]:
    body = {
        "messages": messages,
        "max_tokens": 24,
        "temperature": 0.0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        **extra,
    }
    started = time.monotonic()
    out = post("/v1/chat/completions", body)
    return out, time.monotonic() - started


def describe(label: str, out: dict, wall: float) -> dict:
    t = out.get("timings", {})
    before_slot = wall - (t.get("prompt_ms", 0) + t.get("predicted_ms", 0)) / 1000
    print(
        f"{label:34} cache_n={t.get('cache_n', '?'):>6} prompt_n={t.get('prompt_n', '?'):>6} "
        f"prefill={t.get('prompt_per_second', 0):7.0f} tok/s  decode={t.get('predicted_per_second', 0):5.1f} tok/s  "
        f"wall={wall:6.1f}s  before_slot={before_slot:4.1f}s"
    )
    return t


def main() -> int:
    base = history(TARGET)
    ask = lambda text: base + [{"role": "user", "content": text}]  # noqa: E731
    failures = []

    describe("cold", *chat(ask("What do I see now?")))
    warm = describe("new last message, cache on", *chat(ask("Where is the harbormaster?")))
    cold = describe("same request, cache_prompt false", *chat(ask("Where is the harbormaster?"), cache_prompt=False))
    total = warm.get("cache_n", 0) + warm.get("prompt_n", 0)
    if warm.get("cache_n", 0) < 0.9 * total:
        failures.append(f"checkpoint reuse: only {warm.get('cache_n')} of {total} prompt tokens reused")
    if cold.get("cache_n", 0) > 0.05 * total:
        failures.append(f"cache_prompt false still reused {cold.get('cache_n')} tokens")

    with urllib.request.urlopen(BASE + "/props") as resp:
        props = json.load(resp)
    print(f"n_ctx={props.get('default_generation_settings', {}).get('n_ctx')}  build={props.get('build_info', '?')}")
    for failure in failures:
        print("FAIL", failure)
    print("ok" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
