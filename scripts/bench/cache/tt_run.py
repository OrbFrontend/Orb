"""Run native TauriTavern turns with the real prompt assembly and save bridges."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from scripts.bench.cache.orb_driver import save, saved_difference
from scripts.bench.cache.tauri_driver import WebView

HERE = Path(__file__).parent
TERMINAL = {"completed", "partial_success", "failed", "cancelled"}
TURN_SECONDS = 600
# Records each new text the reply's chat message shows. TauriTavern streams workspace
# writes into that message before the first commit, including the direction file, so
# scoring later picks the first snapshot that is the start of the reply itself.
FIRST_PROSE_OBSERVER = """
window.__benchProse = [];
const chatRoot = document.querySelector('#chat');
window.__benchObserver?.disconnect();
window.__benchObserver = new MutationObserver(() => {
    const reply = chatRoot.querySelector(`.mes[mesid="${input.replyIndex}"]`);
    if (!reply || reply.getAttribute('is_user') === 'true') return;
    const text = reply.querySelector('.mes_text')?.innerText?.trim();
    const last = window.__benchProse[window.__benchProse.length - 1];
    // A growing stream extends the last snapshot; only a different text starts a new one.
    if (text && (!last || !text.startsWith(last.text.slice(0, 80)))) {
        window.__benchProse.push({utc_ms: Date.now(), text: text.slice(0, 400)});
        if (window.__benchProse.length >= 20) window.__benchObserver.disconnect();
    }
});
window.__benchObserver.observe(chatRoot, {subtree: true, childList: true, characterData: true});
"""


def run(view, profile, fixture, root, output, recorder_binding, auditor_binding, turn, *, pilot=True, block="ad-hoc-pilot"):
    output.mkdir(parents=True, exist_ok=False)
    label = {
        "arm": "tt-single" if profile == "benchmark-single" else "tt-handoff",
        "fixture": fixture["id"],
        "turn": turn + 1,
        "pilot": pilot,
        "block": block,
    }
    save(recorder_binding, label)
    # Clear the previous Run before launch; a caller cannot audit stale files.
    save(auditor_binding, {"run_id": None, "workspace_root": None})
    history = view.evaluate(
        "const script = await import('/script.js'); return script.chat.map(row => ({role: row.is_user ? 'user' : 'assistant', content: row.mes}));"
    )
    save(output / "history.json", history)
    summary = {**label, "started_ns": time.monotonic_ns(), "started_utc_ns": time.time_ns(), "complete": False}
    deadline = time.monotonic() + TURN_SECONDS
    handle = None
    native_run = None
    try:
        handle = view.evaluate(
            """
            const script = await import('/script.js');
            const history = structuredClone(script.chat);
            const user = {name: 'User', is_user: true, is_system: false, send_date: new Date().toISOString(), mes: input.text};
            script.replaceChatContents([...history, user]);
            script.addOneMessage(user);
            await script.saveChat();
            input.replyIndex = history.length + 1;
            """
            + FIRST_PROSE_OBSERVER
            + """
            const handle = await window.__TAURITAVERN__.api.agent.startRunFromLegacyGenerate({profileId: input.profile, options: {stream: true, presentation: 'foreground', startWithEmptyPersist: true}});
            return {handle, history: history.map(row => ({role: row.is_user ? 'user' : 'assistant', content: row.mes}))};
            """,
            {"profile": profile, "text": fixture["user_script"][turn]},
        )
        history, handle = handle["history"], handle["handle"]
        save(output / "history.json", history)
        summary["handle"] = handle
        native_run = root / "_tauritavern/agent-workspaces/chats" / handle["workspaceId"] / "runs" / handle["runId"]
        save(
            auditor_binding,
            {
                "run_id": handle["runId"],
                "workspace_root": str(native_run),
                "history": history,
                "user_message": fixture["user_script"][turn],
            },
        )
        after = 0
        with (output / "events.jsonl").open("w") as saved_events:
            while time.monotonic() < deadline:
                result = view.evaluate(
                    """
                    const agent = window.__TAURITAVERN__.api.agent;
                    return {events: await agent.readEvents({runId: input.runId, afterSeq: input.after, limit: 500}), runs: await agent.listRuns({stableChatId: input.stableChatId, limit: 20}), prose: window.__benchProse};
                    """,
                    {"runId": handle["runId"], "stableChatId": handle["stableChatId"], "after": after},
                )
                arrival = time.monotonic_ns()
                for event in result["events"]["events"]:
                    saved_events.write(json.dumps({"arrival_ns": arrival, "event": event}, ensure_ascii=False) + "\n")
                    after = max(after, event["seq"])
                    if event["type"] == "run_failed":
                        summary["native_error"] = event["payload"]
                saved_events.flush()
                summary["prose_snapshots"] = result["prose"] or []
                observed = next(row for row in result["runs"]["runs"] if row["runId"] == handle["runId"])
                save(output / "observed-run.json", observed)
                status = observed["status"]
                if status in TERMINAL:
                    summary["terminal_observed_ns"] = arrival
                    summary["terminal_observed_utc_ns"] = time.time_ns()
                    summary["status"] = status
                    completion = view.evaluate(
                        "await window.__TAURITAVERN__.api.agent.settleChatPresentation({runId: input}); return {settled_at_ms: Date.now()};",
                        handle["runId"],
                    )
                    summary["settled_ns"] = time.monotonic_ns()
                    summary["host_completion"] = completion
                    settled = view.evaluate(
                        "const script = await import('/script.js'); return {chat: script.chat, logs: await window.__TAURITAVERN__.api.dev.frontendLogs.list({limit: 100})};"
                    )
                    save(output / "presentation.json", settled)
                    final_path = native_run / "output/main.md"
                    final = final_path.read_text() if final_path.exists() else ""
                    (output / "final.md").write_text(final)
                    summary["prose_snapshots"] = view.evaluate("return window.__benchProse;") or []
                    summary["saved_difference"] = saved_difference(final, settled["chat"][-1]["mes"])
                    summary["complete"] = status == "completed" and bool(final) and summary["saved_difference"] != "content"
                    break
                time.sleep(0.5)
            else:
                summary["error"] = f"native task exceeded the {TURN_SECONDS}-second turn limit"
                view.evaluate("return await window.__TAURITAVERN__.api.agent.cancel(input);", handle["runId"])
    except (Exception, KeyboardInterrupt) as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if handle is not None:
            try:
                view.evaluate("return await window.__TAURITAVERN__.api.agent.cancel(input);", handle["runId"])
            except Exception as cancel_error:
                summary["cancel_error"] = str(cancel_error)
    if native_run is not None:
        if native_run.exists():
            shutil.copytree(native_run, output / "native-run")
    summary["finished_ns"] = time.monotonic_ns()
    save(output / "summary.json", summary)
    print(
        json.dumps(
            {
                "arm": label["arm"],
                "complete": summary["complete"],
                "seconds": (summary["finished_ns"] - summary["started_ns"]) / 1e9,
                "error": summary.get("error"),
                "output": str(output),
            }
        ),
        flush=True,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--applied", type=Path, required=True)
    parser.add_argument("--userdata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recorder-binding", type=Path, required=True)
    parser.add_argument("--auditor-binding", type=Path, required=True)
    parser.add_argument("--profile", choices=["benchmark-director", "benchmark-single"], required=True)
    parser.add_argument("--turns", type=int, default=3)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    view = WebView(args.application)
    try:
        view.evaluate(HERE.joinpath("tt_preflight.js").read_text())
        view.evaluate(
            "window.__benchmarkStatus = fetch('/api/backends/chat-completions/status', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({chat_completion_source: 'custom', custom_api_format: 'openai_compat', custom_url: 'http://127.0.0.1:5001/v1'})}).then(async response => ({status: response.status, body: await response.json()})); return true;"
        )
        time.sleep(20)
        save(args.output / "endpoint-status.json", view.evaluate("return await window.__benchmarkStatus;"))
        configured = view.evaluate(
            HERE.joinpath("tt_setup.js").read_text(), {"fixture": fixture, "applied": json.loads(args.applied.read_text())}
        )
        save(args.output / "configuration.json", configured)
        for index in range(args.turns):
            result = run(
                view,
                args.profile,
                fixture,
                args.userdata,
                args.output / f"turn-{index + 1:02d}",
                args.recorder_binding,
                args.auditor_binding,
                index,
            )
            if not result["complete"]:
                break
    except Exception as exc:
        save(
            args.output / "setup-error.json",
            {
                "error": str(exc),
                "diagnostic": view.evaluate(
                    "return {body: document.body.innerText, logs: await window.__TAURITAVERN__.api.dev.frontendLogs.list({limit: 100})};"
                ),
            },
        )
        raise
    finally:
        view.close()


if __name__ == "__main__":
    main()
