"""DeepSeek's API, which runs every thinking-on request with reasoner semantics."""

from __future__ import annotations

from .base import Dialect, ModelProfile, is_forced_tool_choice

# deepseek-reasoner is unconditionally thinking-on, so forced choices are coerced statically. Graceful-skip paths in
# Director/Editor handle any unselected tool calls.
_REASONER = ModelProfile(allow_forced_tool_choice=False)


class DeepSeek(Dialect):
    name = "deepseek"

    def matches(self, url: str) -> bool:
        return "api.deepseek.com" in url.lower()

    def profile(self, model: str) -> ModelProfile | None:
        return _REASONER if model == "deepseek-reasoner" else None

    def prepare(self, body: dict) -> list[str]:
        """Coerce forced ``tool_choice`` to ``"auto"`` when thinking is enabled.

        deepseek-chat supports forced-function tool_choice in chat mode but rejects it (and ``"required"``) whenever the request
        also carries thinking=enabled. Coercing to ``"auto"`` lets the director/editor graceful-skip paths handle any unselected
        tool calls.
        """
        thinking = body.get("thinking")
        if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
            return []
        tc = body.get("tool_choice")
        if is_forced_tool_choice(tc):
            body["tool_choice"] = "auto"
            return [f"tool_choice {tc!r} -> 'auto' (thinking enabled)"]
        return []
