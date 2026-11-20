"""NanoGPT, a proxy whose models each sit behind a different upstream engine."""

from __future__ import annotations

from .base import Dialect, ModelProfile

# No endpoint-wide statement about decoding is true of every model NanoGPT fronts. Its own tool-argument decoding is
# unconstrained (observed: GLM-5.2 TEE mangles hyphenated argument keys under a forced call) while its documented
# response_format json_schema strict mode is honored by the routes that implement it -- so the opt-in here is the *optimistic
# default*, not a claim about the whole catalogue. An upstream that quietly ignores the schema is demoted per model on the first
# reply that proves it (``note_structured_output_ignored``), which is why this stays one endpoint-wide knob instead of a
# hand-kept model list.
_PROFILE = ModelProfile(structured_tool_calls=True)


class NanoGPT(Dialect):
    name = "nanogpt"

    def matches(self, url: str) -> bool:
        return "nano-gpt.com" in url.lower()

    def profile(self, model: str) -> ModelProfile:
        return _PROFILE
