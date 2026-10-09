"""Run isolated native Bench 1 blocks; restart llama-server before every block."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import traceback
from pathlib import Path

import httpx

from scripts.bench.cache.orb_driver import save
from scripts.bench.cache.tauri_driver import WebView
from scripts.bench.cache.tt_run import HERE, run

ORB_COMMIT = "e78029f00af528375b117010607b2fcf0e3f1a45"
TT_COMMIT = "a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_missing_turn(output, label, started_ns, error):
    """A driver that died before saving still produced an attempt; keep it in the denominator."""
    if (output / "summary.json").exists():
        return
    output.mkdir(parents=True, exist_ok=True)
    save(
        output / "summary.json",
        {**label, "started_ns": started_ns, "finished_ns": time.monotonic_ns(), "complete": False, "error": error},
    )


def command(args, cwd=None, env=None):
    return subprocess.check_output([str(arg) for arg in args], cwd=cwd, env=env, text=True).strip()


def stop(pidfile, expected):
    if not pidfile.exists():
        return
    pid = int(pidfile.read_text())
    proc = Path(f"/proc/{pid}/cmdline")
    if not proc.exists():
        return
    actual = proc.read_bytes().decode().replace("\x00", " ")
    if expected not in actual:
        raise ValueError(f"refusing to stop PID {pid}: {actual}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while proc.exists() and time.monotonic() < deadline:
        if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
            return
        time.sleep(0.1)
    if proc.exists():
        os.kill(pid, signal.SIGKILL)


def launch(args, cwd, log, pidfile, env=None):
    with log.open("ab") as stream:
        child = subprocess.Popen(
            [str(arg) for arg in args], cwd=cwd, env=env, stdout=stream, stderr=stream, start_new_session=True
        )
    pidfile.write_text(str(child.pid) + "\n")
    return child


def ready(url, child):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"service exited {child.returncode}: {url}")
        try:
            if httpx.get(url, timeout=2, trust_env=False).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise TimeoutError(url)


def restart_model(root, harness, block):
    pidfile = root / "pilot/llama-server.pid"
    previous_pid = pidfile.read_text().strip() if pidfile.exists() else None
    stop(pidfile, "llama-server")
    gpu = command(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])
    deadline = time.monotonic() + 15
    while (
        gpu
        and previous_pid
        and all(line.split(",")[0].strip() == previous_pid for line in gpu.splitlines())
        and time.monotonic() < deadline
    ):
        time.sleep(0.5)
        gpu = command(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])
    if gpu:
        raise RuntimeError(f"another compute process is using the GPU: {gpu}")
    save(
        block / "gpu-before.json",
        {
            "compute_processes": gpu,
            "gpu": command(["nvidia-smi", "--query-gpu=name,driver_version,power.limit,memory.total", "--format=csv"]),
            "ram": command(["free", "-b"]),
        },
    )
    env = {
        **os.environ,
        "LLAMA_BIN": str(Path.home() / "lmg/llama-cpp-webui/data/llama.cpp/build/bin/llama-server"),
        "MODEL": str(Path.home() / "lmg/llama-cpp-webui/data/models/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf"),
    }
    child = launch(["bash", harness / "scripts/bench/serve_gemma.sh"], harness, block / "llama-server.log", pidfile, env)
    ready("http://127.0.0.1:5000/health", child)
    (block / "metrics-before.txt").write_text(httpx.get("http://127.0.0.1:5000/metrics", trust_env=False).text)
    save(
        block / "server.json",
        {
            "pid": child.pid,
            "cmdline": Path(f"/proc/{child.pid}/cmdline").read_bytes().decode().split("\x00"),
            "started_utc_ns": time.time_ns(),
        },
    )


def orb_block(root, harness, block, fixture, turns, pilot):
    python = Path.home() / "lmg/Anonymous/Orb/.venv/bin/python"
    source = Path.home() / "lmg/Anonymous/Orb"
    worktree = block / "orb"
    command(["git", "worktree", "add", "--detach", worktree, ORB_COMMIT], source)
    shutil.copytree(
        harness / "scripts/bench",
        worktree / "scripts/bench",
        ignore=shutil.ignore_patterns("evidence", "results", "__pycache__"),
    )
    if command(["git", "status", "--porcelain", "--", "backend", "frontend"], worktree):
        raise ValueError("Orb application source differs from the pinned commit")
    pidfile = root / "pilot/orb.pid"
    stop(pidfile, "uvicorn")
    args = [python, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "18899"]
    child = launch(args, worktree, block / "orb.log", pidfile)
    ready("http://127.0.0.1:18899/api/settings", child)
    common = [python, "-m", "scripts.bench.cache.orb_driver"]
    cid = command([*common, "prepare", "--fixture", fixture, "--output", block], worktree).splitlines()[-1]
    stop(pidfile, "uvicorn")
    command([*common, "seed", "--conversation", cid, "--fixture", fixture, "--output", block], worktree)
    child = launch(args, worktree, block / "orb.log", pidfile)
    ready("http://127.0.0.1:18899/api/settings", child)
    for index in range(turns):
        output = block / f"turn-{index + 1:02d}"
        started_ns = time.monotonic_ns()
        label = {"arm": "orb", "fixture": fixture.stem, "turn": index + 1, "pilot": pilot, "block": block.name}
        try:
            command(
                [
                    *common,
                    "turn",
                    "--conversation",
                    cid,
                    "--fixture",
                    fixture,
                    "--output",
                    block / f"turn-{index + 1:02d}",
                    "--binding",
                    root / "pilot/recorder-binding.json",
                    "--turn",
                    index,
                    "--block",
                    block.name,
                    *(["--reportable"] if not pilot else []),
                ],
                worktree,
            )
        except subprocess.CalledProcessError as exc:
            record_missing_turn(output, label, started_ns, f"driver exited {exc.returncode}")
        summary = json.loads((output / "summary.json").read_text())
        print(json.dumps({"block": block.name, "turn": index + 1, "complete": summary["complete"]}), flush=True)
    stop(pidfile, "uvicorn")
    return block / "applied.json"


def tt_block(root, block, fixture, applied, arm, turns, pilot):
    application = root / "tauritavern/src-tauri/target/release/tauritavern"
    source = root / "tauritavern"
    if command(["git", "rev-parse", "HEAD"], source) != TT_COMMIT or command(
        ["git", "status", "--porcelain", "--", "src", "src-tauri"], source
    ):
        raise ValueError("TauriTavern application source differs from the pinned commit")
    data = root / "tt-pilot-data/com.tauritavern.client/data"
    profile = "benchmark-director" if arm == "tt-handoff" else "benchmark-single"
    view = WebView(application)
    try:
        view.evaluate(HERE.joinpath("tt_preflight.js").read_text())
        configured = view.evaluate(
            HERE.joinpath("tt_setup.js").read_text(),
            {"fixture": json.loads(fixture.read_text()), "applied": json.loads(applied.read_text())},
        )
        save(block / "configuration.json", configured)
        for index in range(turns):
            run(
                view,
                profile,
                json.loads(fixture.read_text()),
                data,
                block / f"turn-{index + 1:02d}",
                root / "pilot/recorder-binding.json",
                root / "pilot/auditor-binding.json",
                index,
                pilot=pilot,
                block=block.name,
            )
    finally:
        view.close()


def provenance(root, harness):
    """Identities every block manifest records, so a block names exactly what it ran."""
    llama = Path.home() / "lmg/llama-cpp-webui/data/llama.cpp/build/bin/llama-server"
    version = subprocess.run([llama, "--version"], capture_output=True, text=True)
    return {
        "harness_commit": command(["git", "rev-parse", "HEAD"], harness),
        "harness_dirty": bool(command(["git", "status", "--porcelain", "--", "scripts/bench"], harness)),
        "orb_commit": ORB_COMMIT,
        "tauritavern_commit": TT_COMMIT,
        "tauritavern_binary_sha256": sha256(root / "tauritavern/src-tauri/target/release/tauritavern"),
        "model_sha256": (root / "pilot/model.sha256").read_text().split()[0],
        "llama_server_version": (version.stdout + version.stderr).strip().splitlines()[-2:],
        "harness_files_sha256": {
            str(path.relative_to(harness)): sha256(path)
            for path in sorted((harness / "scripts/bench").rglob("*"))
            if path.is_file() and path.suffix in {".py", ".js", ".json", ".sh"} and "results" not in path.parts
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--turns", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sizes", type=int, nargs="+", default=[2000, 8000, 16000, 32000])
    parser.add_argument("--arms", nargs="+", default=["orb", "tt-handoff", "tt-single"])
    args = parser.parse_args()
    root, harness = args.root.resolve(), Path.cwd()
    identity = provenance(root, harness)
    if not args.pilot and identity["harness_dirty"]:
        raise ValueError("a reportable sweep needs a committed, clean benchmark harness")
    args.output.mkdir(parents=True, exist_ok=True)
    applied = root / "pilot/orb-short/applied.json"
    for repeat in range(args.repeats):
        for size in args.sizes:
            fixture = root / f"fixtures/bellwick-{size}.json"
            for arm in args.arms[repeat % len(args.arms) :] + args.arms[: repeat % len(args.arms)]:
                block = args.output / f"r{repeat + 1}-{size}-{arm}"
                if (block / "block-complete.json").exists() or (block / "block-failed.json").exists():
                    continue
                block.mkdir(parents=True, exist_ok=False)
                save(
                    block / "manifest.json",
                    {
                        "arm": arm,
                        "size": size,
                        "repeat": repeat + 1,
                        "pilot": args.pilot,
                        "turns": args.turns,
                        "fixture": str(fixture),
                        "fixture_sha256": sha256(fixture),
                        **identity,
                        "started_utc_ns": time.time_ns(),
                    },
                )
                try:
                    restart_model(root, harness, block)
                    if arm == "orb":
                        applied = orb_block(root, harness, block, fixture, args.turns, args.pilot)
                    else:
                        tt_block(root, block, fixture, applied, arm, args.turns, args.pilot)
                    (block / "metrics-after.txt").write_text(httpx.get("http://127.0.0.1:5000/metrics", trust_env=False).text)
                except Exception as exc:
                    if not any(block.glob("turn-*")):
                        # Setup failed before any turn began (model server, WebView session, app setup): that is the
                        # harness, not either application. Set the block aside and stop, so a resume reruns it cleanly.
                        block.rename(block.with_name(f"{block.name}.setup-failed-{time.time_ns()}"))
                        raise
                    # A failure once turns began keeps the sweep going; every turn the block never reached is
                    # still an attempted turn.
                    for index in range(args.turns):
                        label = {
                            "arm": arm,
                            "fixture": fixture.stem,
                            "turn": index + 1,
                            "pilot": args.pilot,
                            "block": block.name,
                        }
                        record_missing_turn(block / f"turn-{index + 1:02d}", label, time.monotonic_ns(), f"block failed: {exc}")
                    save(block / "block-failed.json", {"error": str(exc), "traceback": traceback.format_exc()})
                    print(json.dumps({"failed": block.name, "error": str(exc)}), flush=True)
                    continue
                save(block / "block-complete.json", {"finished_utc_ns": time.time_ns()})
                print(json.dumps({"finished": block.name}), flush=True)


if __name__ == "__main__":
    main()
