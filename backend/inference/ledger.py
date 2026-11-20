"""What each endpoint and model was observed to accept, learned from its responses.

Providers move under Orb, so their quirks are learned from rejections rather than listed. Every learned fact lives here, keyed by
the configured endpoint URL and model, for the life of the backend process. Changing an endpoint's URL, key or proxy forgets what was
learned about it, because the server behind it, or the key that reaches it, may then differ.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .endpoint_profiles import EndpointRoute


@dataclass(slots=True)
class PairFacts:
    """Facts learned about one (endpoint URL, model) pair. A blank model holds endpoint-wide facts."""

    # The route that last completed, so later calls skip probing.
    route: EndpointRoute | None = None
    # The model answered a forced tool_choice with some other tool.
    forced_choice_ignored: bool = False
    # The model ignored a strict response_format schema it was sent.
    structured_output_ignored: bool = False
    # No tool_choice value is routed for this model; the field is dropped.
    tool_choice_unsupported: bool = False
    # Only tool_choice "auto" is accepted; other values are rewritten to it.
    tool_choice_auto_only: bool = False
    # reasoning_effort levels the model refused.
    effort_refused: set[str] = field(default_factory=set)
    # A level stepped past on a refusal that named nothing, learned only once the stepped call succeeds.
    effort_suspected: str | None = None
    # The stepped call failed the same way, so the suspected level was not the cause.
    effort_acquitted: bool = False
    # The endpoint always reasons and refuses a reasoning-off object.
    reasoning_mandatory: bool = False
    # Reasoning fields refused on replayed assistant messages.
    replay_refused: set[str] = field(default_factory=set)
    # The endpoint refused Orb's prompt-cache markers.
    cache_markers_refused: bool = False
    # Top-level body keys the endpoint refused.
    body_fields_refused: set[str] = field(default_factory=set)
    # Messages API: sampling controls refused.
    sampling_unsupported: bool = False
    # Messages API: adaptive thinking and output_config.effort refused.
    thinking_unsupported: bool = False
    # Judge: the llama.cpp ``/v1/systemone`` sibling that answered for a derived decisions route.
    systemone_url: str | None = None


_DEFAULTS = PairFacts()


def _clean(url: str) -> str:
    return url.strip().rstrip("/")


class CapabilityLedger:
    """Every learned capability fact, with a reset, a per-endpoint forget, and a read-only view."""

    def __init__(self) -> None:
        self._pairs: dict[tuple[str, str], PairFacts] = {}

    def facts(self, endpoint_url: str, model: str = "") -> PairFacts:
        """The pair's facts, opened for recording."""
        key = (endpoint_url, model)
        if key not in self._pairs:
            self._pairs[key] = PairFacts()
        return self._pairs[key]

    def peek(self, endpoint_url: str, model: str = "") -> PairFacts:
        """The pair's facts for reading. An unknown pair reads as nothing learned and is not stored."""
        return self._pairs.get((endpoint_url, model)) or PairFacts()

    def forget(self, *endpoint_urls: str) -> None:
        """Drop everything learned about these endpoint URLs, for every model."""
        gone = {_clean(url) for url in endpoint_urls if url}
        for key in [key for key in self._pairs if _clean(key[0]) in gone]:
            del self._pairs[key]

    def reset(self) -> None:
        self._pairs.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        """What has been learned, as plain data: one entry per pair, listing only the facts that differ from the defaults."""
        out: list[dict[str, Any]] = []
        for (endpoint_url, model), facts in sorted(self._pairs.items()):
            entry: dict[str, Any] = {}
            for spec in fields(PairFacts):
                value = getattr(facts, spec.name)
                if value == getattr(_DEFAULTS, spec.name):
                    continue
                if spec.name == "route":
                    value = {"protocol": value.protocol, "url": value.url, "auth_family": value.auth_family}
                elif isinstance(value, set):
                    value = sorted(value)
                entry[spec.name] = value
            if entry:
                out.append({"endpoint_url": endpoint_url, "model": model, **entry})
        return out


LEDGER = CapabilityLedger()
