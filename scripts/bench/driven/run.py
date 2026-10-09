"""Run Bench 2 turns for one transport in bench.json: local Gemma (fresh llama-server) or a hosted model on OpenRouter.

Every model call goes through the recorder. Director on and off are interleaved per context. `--resume` continues a stopped
run in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx

from scripts.bench.cache.blocks import command, launch, ready, record_missing_turn, sha256, stop
from scripts.bench.cache.orb_driver import save
from scripts.bench.driven.corpus import contexts

TURN_SECONDS = 900
ORB_COMMIT = "8821c07125be648135f7c980ce44d3a3f0d2a4ea"
SNAPSHOT = json.loads(Path(__file__).with_name("bench.json").read_text())
SOURCE = Path.home() / "lmg/Anonymous/Orb"
LLAMA = Path.home() / "lmg/llama-cpp-webui/data/llama.cpp/build/bin/llama-server"
MODEL = Path.home() / "lmg/llama-cpp-webui/data/models/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf"
SUBJECT_MODELS = {
    "subjects-68m-v1-q8_0.gguf": "8ddc36932a1356b5d5c2dfa156f03cb2c82bc13ca1b66432e5d99b061689326e",
    "subject-pairs-68m-v1-q8_0.gguf": "c51aedb3c79865c8db76e30b135fdd497e4159cd45b8e7bfccbcb4babdc0ac42",
}
# A hosted provider's rate limit or outage is retried after a pause; every other failure is an attempt that counts.
PROVIDER_ERRORS = {408, 429, 500, 502, 503, 504, 529}
BACKOFF_SECONDS = [60, 300, 1200]


def port_busy(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def build_plan(count: int, repeats: int, pilot: bool, transport: str = "gemma") -> dict:
    """A, B then B, A: the arm that goes first alternates by context and repeat, so drift never lines up with an arm."""
    chosen = contexts()[:count]
    if len(chosen) < count:
        raise ValueError(f"the corpus has {len(chosen)} contexts, not {count}")
    turns = {}
    for repeat in range(repeats):
        for index, context in enumerate(chosen):
            order = [True, False] if (index + repeat) % 2 == 0 else [False, True]
            for director in order:
                key = f"r{repeat + 1}-{context['id']}-{'on' if director else 'off'}"
                turns[key] = {
                    "context": context["id"],
                    "repeat": repeat + 1,
                    "director": director,
                    "arm": key.rsplit("-", 1)[1],
                }
    return {"pilot": pilot, "transport": transport, "contexts": {context["id"]: context for context in chosen}, "turns": turns}


def provenance(harness: Path, transport: str, model_sha256: str | None) -> dict:
    config = SNAPSHOT["transports"][transport]
    identity = {
        "transport": transport,
        "upstream": config["upstream"],
        "provider": config.get("provider"),
        "harness_commit": command(["git", "rev-parse", "HEAD"], harness),
        "harness_dirty": bool(command(["git", "status", "--porcelain", "--", "scripts/bench"], harness)),
        # Bench 3 scores with the harness checkout's backend.analysis, so it must be the Orb that ran.
        "harness_backend_matches_orb": not command(["git", "diff", "--stat", ORB_COMMIT, "HEAD", "--", "backend"], harness),
        "orb_commit": ORB_COMMIT,
        "ram": command(["free", "-b"]),
        "harness_files_sha256": {
            str(path.relative_to(harness)): sha256(path)
            for path in sorted((harness / "scripts/bench").rglob("*"))
            if path.is_file() and path.suffix in {".py", ".js", ".json", ".sh"} and "results" not in path.parts
        },
    }
    if "provider" in config:
        identity["model_name"] = config["model_config"]["model_name"]
    else:
        if model_sha256 is None:
            raise ValueError("a local model run needs --model-sha256")
        version = subprocess.run([LLAMA, "--version"], capture_output=True, text=True)
        identity.update(
            model=str(MODEL),
            model_name=MODEL.name,
            model_sha256=model_sha256,
            llama_server_version=(version.stdout + version.stderr).strip().splitlines()[-2:],
            gpu=command(["nvidia-smi", "--query-gpu=name,driver_version,power.limit,memory.total", "--format=csv"]),
        )
    return identity


def api_key(upstream: str) -> str:
    """OPENROUTER_API_KEY, else the main checkout's saved endpoint for the same host. Never printed or saved in the run."""
    if key := os.environ.get("OPENROUTER_API_KEY"):
        return key
    host = upstream.split("//", 1)[1].split("/", 1)[0]
    row = (
        sqlite3.connect(SOURCE / "backend/data/app.db")
        .execute("select api_key from endpoints where kind = 'chat' and url like ? and api_key != '' limit 1", (f"%{host}%",))
        .fetchone()
    )
    if row is None:
        raise RuntimeError(f"no API key for {host}: set OPENROUTER_API_KEY")
    return row[0]


def start_model(harness: Path, run: Path):
    gpu = command(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])
    if gpu:
        raise RuntimeError(f"another compute process is using the GPU: {gpu}")
    env = {**os.environ, "LLAMA_BIN": str(LLAMA), "MODEL": str(MODEL)}
    child = launch(
        ["bash", harness / "scripts/bench/serve_gemma.sh"], harness, run / "llama-server.log", run / "llama-server.pid", env
    )
    ready("http://127.0.0.1:5000/health", child)
    save(
        run / f"server-{time.time_ns()}.json",
        {
            "pid": child.pid,
            "cmdline": Path(f"/proc/{child.pid}/cmdline").read_bytes().decode().split("\x00"),
            "utc_ns": time.time_ns(),
        },
    )
    (run / "metrics-before.txt").write_text(httpx.get("http://127.0.0.1:5000/metrics", trust_env=False).text)


def start_recorder(harness: Path, run: Path, python: Path, upstream: str):
    args = [python, "-m", "scripts.bench.recorder", "--upstream", upstream, "--output", run / "requests"]
    child = launch([*args, "--binding", run / "recorder-binding.json"], harness, run / "recorder.log", run / "recorder.pid")
    deadline = time.monotonic() + 30
    while not port_busy(5001):
        if child.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("the recorder did not start")
        time.sleep(0.2)


def provider_failed(run: Path, key: str, since_utc_ns: int) -> bool:
    """Whether any of this attempt's calls met a provider rate limit or outage, from the recorder's own copy."""
    for call in (run / "requests").iterdir():
        metadata = json.loads((call / "metadata.json").read_text())
        if metadata.get("binding", {}).get("key") != key or metadata["utc_unix_ns"] < since_utc_ns:
            continue
        if metadata.get("status") in PROVIDER_ERRORS:
            return True
        error = metadata.get("error")
        if isinstance(error, dict) and error.get("code") in PROVIDER_ERRORS:
            return True
    return False


def set_aside(output: Path) -> Path:
    attempt = 1
    while output.with_name(f"{output.name}.attempt-{attempt}").exists():
        attempt += 1
    target = output.with_name(f"{output.name}.attempt-{attempt}")
    output.rename(target)
    return target


def worktree(harness: Path, run: Path) -> Path:
    tree = run / "orb"
    command(["git", "worktree", "add", "--detach", tree, ORB_COMMIT], SOURCE)
    shutil.copytree(
        harness / "scripts/bench",
        tree / "scripts/bench",
        ignore=shutil.ignore_patterns("evidence", "results", "__pycache__"),
        dirs_exist_ok=True,
    )
    # The subject_fixation step runs only with Local ML's subject analyzer on disk; copy the catalog-pinned files.
    models = tree / "backend/data/models"
    models.mkdir(parents=True, exist_ok=True)
    for name, digest in SUBJECT_MODELS.items():
        shutil.copyfile(SOURCE / "backend/data/models" / name, models / name)
        if sha256(models / name) != digest:
            raise ValueError(f"{name} does not match its pinned sha256")
    return tree


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transport", choices=sorted(SNAPSHOT["transports"]), default="gemma")
    parser.add_argument("--model-sha256", type=Path, help="file holding the verified local model hash")
    parser.add_argument("--contexts", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--resume", action="store_true", help="continue a stopped run in place; the saved plan holds")
    args = parser.parse_args()
    harness, run = Path.cwd(), args.output.resolve()
    python = SOURCE / ".venv/bin/python"
    manifest: dict = {}
    if args.resume:
        plan = json.loads((run / "plan.json").read_text())
        manifest = json.loads((run / "manifest.json").read_text())
        transport, pilot = plan["transport"], plan["pilot"]
        # A crashed runner leaves its own services up; stop them by their pidfiles, never by port.
        for name, expected in (("orb", "uvicorn"), ("recorder", "scripts.bench.recorder"), ("llama-server", "llama-server")):
            stop(run / f"{name}.pid", expected)
    else:
        transport, pilot = args.transport, args.pilot
        plan = build_plan(args.contexts, args.repeats, pilot, transport)
    config = SNAPSHOT["transports"][transport]
    hosted = "provider" in config
    model_sha256 = args.model_sha256.read_text().split()[0] if args.model_sha256 else manifest.get("model_sha256")
    identity = provenance(harness, transport, model_sha256)
    if not pilot and (identity["harness_dirty"] or not identity["harness_backend_matches_orb"]):
        raise ValueError(f"a reportable run needs a clean, committed harness whose backend is Orb {ORB_COMMIT}")
    for port in (5001, 18899) if hosted else (5000, 5001, 18899):
        if port_busy(port):
            raise RuntimeError(f"port {port} is in use; stop the previous run's services first")
    if args.resume:
        changed = identity["harness_files_sha256"] != manifest["harness_files_sha256"]
        if changed and not pilot:
            raise ValueError("the harness changed since the run started; finish it with the harness it began with")
        manifest.setdefault("resumes", []).append({"utc_ns": time.time_ns(), "harness_changed": changed})
    else:
        run.mkdir(parents=True, exist_ok=False)
        save(run / "plan.json", plan)
        corpus = (json.dumps(plan["contexts"], sort_keys=True, ensure_ascii=False) + "\n").encode()
        manifest = {**identity, "corpus_sha256": hashlib.sha256(corpus).hexdigest(), "started_utc_ns": time.time_ns()}
    save(run / "manifest.json", manifest)
    save(run / "recorder-binding.json", {"key": "setup"})
    tree = run / "orb" if args.resume else worktree(harness, run)
    if command(["git", "status", "--porcelain", "--", "backend", "frontend"], tree):
        raise ValueError("Orb application source differs from the pinned commit")
    if not hosted:
        start_model(harness, run)
    start_recorder(harness, run, python, config["upstream"])
    pidfile = run / "orb.pid"
    uvicorn = [python, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "18899"]
    driver = [python, "-m", "scripts.bench.driven.orb_turns"]
    common = ["--plan", run / "plan.json", "--run", run]
    try:
        if not args.resume:
            ready("http://127.0.0.1:18899/api/settings", launch(uvicorn, tree, run / "orb.log", pidfile))
            secret = {**os.environ, "BENCH_API_KEY": api_key(config["upstream"])} if hosted else None
            command([*driver, "prepare", *common], tree, env=secret)
            stop(pidfile, "uvicorn")
            command([*driver, "seed", *common], tree)
        ready("http://127.0.0.1:18899/api/settings", launch(uvicorn, tree, run / "orb.log", pidfile))
        if not args.resume:
            command([*driver, "inventory", *common], tree)
        for key, turn in plan["turns"].items():
            output = run / "turns" / key
            if (output / "summary.json").exists():
                continue
            if output.exists():
                # Interrupted mid-turn: keep the partial attempt as evidence and start the turn over from its seeded state.
                set_aside(output)
                command([*driver, "reset", *common, "--key", key], tree)
            summary: dict = {}
            for backoff in [*BACKOFF_SECONDS, None]:
                started_ns, started_utc_ns = time.monotonic_ns(), time.time_ns()
                label = {**turn, "key": key, "pilot": pilot}
                try:
                    subprocess.run(
                        [str(arg) for arg in [*driver, "turn", *common, "--key", key]],
                        cwd=tree,
                        check=True,
                        timeout=TURN_SECONDS,
                    )
                except subprocess.CalledProcessError as exc:
                    record_missing_turn(output, label, started_ns, f"driver exited {exc.returncode}")
                except subprocess.TimeoutExpired:
                    record_missing_turn(output, label, started_ns, f"turn exceeded the {TURN_SECONDS}-second limit")
                summary = json.loads((output / "summary.json").read_text())
                if summary["complete"] or not hosted or not provider_failed(run, key, started_utc_ns):
                    break
                set_aside(output)
                if backoff is None:
                    raise RuntimeError(f"{key}: the provider keeps failing; rerun with --resume once it recovers")
                print(json.dumps({"turn": key, "provider_error": True, "sleep": backoff}), flush=True)
                time.sleep(backoff)
                command([*driver, "reset", *common, "--key", key], tree)
            print(json.dumps({"turn": key, "complete": summary["complete"]}), flush=True)
    finally:
        stop(pidfile, "uvicorn")
        stop(run / "recorder.pid", "scripts.bench.recorder")
        if not hosted and port_busy(5000):
            (run / "metrics-after.txt").write_text(httpx.get("http://127.0.0.1:5000/metrics", trust_env=False).text)
        stop(run / "llama-server.pid", "llama-server")
    save(run / "run-complete.json", {"finished_utc_ns": time.time_ns()})


if __name__ == "__main__":
    main()
