"""Stateless localhost MCP auditor; the driver binds one native Run at a time.

    python -m scripts.bench.auditor --binding /path/to/binding.json --output /path/to/audits

Binding JSON: {run_id, workspace_root, history: [{role, content}], user_message}.
History is chronological and excludes the current draft. Publish the binding
atomically before the model can call the auditor. Never read the user's Orb DB.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from backend.analysis import build_targets, filter_audit_report_to_text, format_numbered_report, report_to_dict, run_audit
from backend.database.models import PhraseGroup
from backend.database.seeds import DEFAULT_SETTINGS, SEED_PHRASE_BANK
from backend.pipeline.passes.editor.prompts import PATCH_CATEGORY_RULES

DEFAULT_TOGGLES = {**DEFAULT_SETTINGS["editor_audit_toggles"], "subject_fixation": False}
PHRASE_BANK: list[PhraseGroup] = [
    {"kind": "regex", "pattern": item} if isinstance(item, str) else {"kind": "literal", "variants": item}
    for item in SEED_PHRASE_BANK
]
TOOL = {
    "name": "audit_draft",
    "description": "Audit the current output/main.md bytes using the shared benchmark prose detectors. Re-audit after every edit batch.",
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string", "enum": ["output/main.md"]}},
        "required": ["path"],
        "additionalProperties": False,
    },
}


def contextual_audit(draft: str, history: list[dict], user_message: str) -> dict:
    """Mirror the Editor's boundary with public analysis APIs and fresh seeds."""
    previous = [row["content"] for row in reversed(history) if row["role"] == "assistant"][:20]
    text = "\n\n".join([*reversed(previous), draft])
    report = filter_audit_report_to_text(
        run_audit(
            text,
            PHRASE_BANK,
            assistant_messages=previous,
            structural_text=draft,
            user_message=user_message,
            audit_toggles=DEFAULT_TOGGLES,
        ),
        draft,
    )
    targets = build_targets(report, draft)
    return {
        "draft_sha256": hashlib.sha256(draft.encode()).hexdigest(),
        "total_issues": report.total_issues,
        "report": report_to_dict(report, draft),
        "targets": [asdict(target) for target in targets],
        "numbered_report": format_numbered_report(targets),
        "audit_toggles": DEFAULT_TOGGLES,
        "phrase_groups": len(PHRASE_BANK),
    }


def model_report(result: dict) -> str:
    """What the native Profile sees: the Editor's numbered report and its rules for the flagged categories.

    Orb's Editor reads the same report text; its id-patching header line names a tool the native Profile does not
    have, so that line alone is reworded for exact-string patches. Offsets and the full result stay on disk for scoring.
    """
    report = result["numbered_report"].replace(
        "Numbered issues — patch each by its [id].", "Flagged sentences — patch each by its exact text."
    )
    categories = {category for target in result["targets"] for category in target["categories"]}
    rules = [f"- {rule}" for category, rule in PATCH_CATEGORY_RULES.items() if category in categories]
    return "\n\n".join([report, "Fixing rules:\n" + "\n".join(rules)] if rules else [report])


def audit_bound_file(binding_path: Path, output: Path, path: str) -> dict:
    if path != "output/main.md":
        raise ValueError("only output/main.md is bound")
    binding = json.loads(binding_path.read_text())
    root = Path(binding["workspace_root"]).resolve(strict=True)
    target = (root / path).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError("draft path escapes the bound native Run")
    raw = target.read_bytes()
    result = contextual_audit(raw.decode("utf-8"), binding["history"], binding["user_message"])
    result.update({"run_id": binding["run_id"], "path": path, "monotonic_ns": time.monotonic_ns()})
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{result['monotonic_ns']}-{result['draft_sha256']}"
    (output / f"{stem}.md").write_bytes(raw)
    (output / f"{stem}.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def create_app(binding: Path, output: Path) -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        body = await request.json()
        method, request_id = body.get("method"), body.get("id")
        if request_id is None:
            return Response(status_code=202)
        if method == "initialize":
            version = body.get("params", {}).get("protocolVersion", "2025-03-26")
            # Pin the TauriTavern registration to a legacy Streamable HTTP version.
            if version not in {"2025-03-26", "2025-06-18", "2025-11-25"}:
                version = "2025-03-26"
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "orb-benchmark-auditor", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            params = body.get("params", {})
            try:
                arguments = params.get("arguments", {})
                if params.get("name") != TOOL["name"] or set(arguments) != {"path"}:
                    raise ValueError("expected audit_draft with the single path argument")
                audited = await asyncio.to_thread(audit_bound_file, binding, output, arguments["path"])
                result = {"content": [{"type": "text", "text": model_report(audited)}], "isError": False}
            except (ValueError, KeyError, OSError, TypeError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=5002)
    args = parser.parse_args()
    uvicorn.run(create_app(args.binding, args.output), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
