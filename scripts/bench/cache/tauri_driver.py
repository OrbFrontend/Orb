"""Drive the real TauriTavern WebView and host ABI over tauri-driver WebDriver.

This does not implement any model/tool loop or prompt/commit bridge.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


class WebView:
    def __init__(self, application: Path, endpoint="http://127.0.0.1:4444"):
        self.client = httpx.Client(base_url=endpoint, timeout=300, trust_env=False)
        self.session = None
        try:
            value = self.call(
                "POST", "/session", {"capabilities": {"alwaysMatch": {"tauri:options": {"application": str(application)}}}}
            )
            self.session = value["sessionId"]
            # Each script is a short bridge call; a turn's own limit is enforced by its polling loop.
            self.call("POST", f"/session/{self.session}/timeouts", {"script": 180000})
            deadline = time.monotonic() + 90
            while not self.evaluate("return document.readyState === 'complete' && Boolean(window.__TAURITAVERN_MAIN_READY__);"):
                if time.monotonic() >= deadline:
                    raise TimeoutError(self.evaluate("return {url: location.href, body: document.body?.innerText};"))
                time.sleep(0.1)
        except BaseException:
            self.close()
            raise

    def call(self, method, path, payload=None):
        response = self.client.request(method, path, json=payload)
        response.raise_for_status()
        value = response.json()["value"]
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(value)
        return value

    def evaluate(self, script: str, *args):
        wrapped = (
            "const done = arguments[arguments.length - 1]; const input = arguments[0]; (async () => {"
            + script
            + "})().then(value => done({ok: true, value: value ?? null}), error => done({ok: false, error: String(error), stack: error.stack}));"
        )
        result = self.call("POST", f"/session/{self.session}/execute/async", {"script": wrapped, "args": list(args) or [None]})
        if not result["ok"]:
            raise RuntimeError(result)
        return result["value"]

    def close(self):
        try:
            if self.session is not None:
                self.call("DELETE", f"/session/{self.session}")
                self.session = None
        finally:
            self.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    view = WebView(args.application)
    try:
        script = (
            args.script.read_text()
            if args.script
            else "await window.__TAURITAVERN_MAIN_READY__; return {url: location.href, title: document.title, api: Object.keys(window.__TAURITAVERN__.api), body: document.body.innerText.slice(0, 2000)};"
        )
        result = view.evaluate(script)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        diagnostic = view.evaluate(
            "return {url: location.href, readyState: document.readyState, body: document.body.innerText.slice(0, 4000), frontendLogs: await window.__TAURITAVERN__.api.dev.frontendLogs.list({limit: 50})};"
        )
        args.output.write_text(json.dumps({"error": str(exc), "diagnostic": diagnostic}, indent=2, ensure_ascii=False) + "\n")
        raise
    finally:
        view.close()
