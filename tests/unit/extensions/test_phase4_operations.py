"""``http.request`` and ``artifact.emit`` as the interpreter sees them.

The transport lives in :mod:`tests.unit.extensions.test_network`; these run
against a stub ``HostServices.http``, because what is under test here is the
*operation* -- its grant check, its quota, the shape of what it stages, and the
fact that nothing it produces reaches Orb until the flow returns.

The artifact assertions are mostly about scope and provenance: a post hook
attaches to the message being written and cannot name one, an action must name
one and it is proved to belong to the invocation, and every emitted file
carries the revision that produced it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.extensions.artifacts import (
    RECOVERY_KEY,
    attachment_payload,
    generation_metadata,
    recovery_input,
    safe_filename,
)
from backend.features.extensions.contracts import (
    Flow,
    OpContext,
    check_context,
    parse_schema,
)
from backend.features.extensions.errors import FlowError
from backend.features.extensions.interpreter import (
    FlowResult,
    HostServices,
    Invocation,
    run_flow,
)
from backend.features.extensions.limits import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_INVOCATION,
    MAX_HTTP_REQUESTS_PER_INVOCATION,
)
from backend.features.extensions.network import ResponseBytes

GRANTS = frozenset(
    {
        ("network.request", None),
        ("network.request", "https://api.example.invalid"),
        ("artifact.write", None),
        ("context.read", "draft"),
        ("state.read", None),
        ("state.read", "conversation"),
        ("state.write", None),
        ("state.write", "conversation"),
    }
)

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


class _Http:
    """A stub egress that records what the interpreter handed it."""

    def __init__(self, body=None, kind="json", fail: Exception | None = None):
        self.calls: list[dict] = []
        self._body = body
        self._kind = kind
        self._fail = fail

    async def request(self, *, method, url, headers, body, response_kind):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "kind": response_kind})
        if self._fail is not None:
            raise self._fail
        return {"status": 200, "body": self._body}


def flow(*steps) -> Flow:
    return Flow.model_validate({"flow_version": 1, "steps": list(steps)})


async def run(
    f: Flow,
    *,
    http=None,
    ctx: dict | None = None,
    grants=GRANTS,
    context: OpContext = OpContext.POST_OBSERVE,
    owns_message=None,
    read_asset=None,
    action_input: dict | None = None,
) -> FlowResult:
    async def read_state(_scope):
        return {}

    invocation = Invocation(
        extension_id="api-artifact",
        context=context,
        host=HostServices(
            grants=lambda: grants,
            read_state=read_state,
            http=http,
            owns_message=owns_message,
            read_asset=read_asset,
        ),
        ctx=ctx or {},
        action_input=action_input or {},
        scopes_in_scope=frozenset({"conversation"}),
        seed="test-seed",
    )
    result: FlowResult | None = None
    async for item in run_flow(f, invocation):
        if isinstance(item, FlowResult):
            result = item
    assert result is not None
    return result


# ── http.request ────────────────────────────────────────────────────────────


async def test_a_request_passes_resolved_values_and_keeps_secret_markers_intact():
    """The marker survives resolution; only the client turns it into a value.

    That is the whole non-disclosure story. If ``resolve_value`` substituted, the
    secret would be an ordinary flow value from that point on and every sink
    would need its own check.
    """
    http = _Http(body={"ok": True})
    result = await run(
        flow(
            {
                "id": "call",
                "op": "http.request",
                "method": "POST",
                "url": "https://api.example.invalid/v1/render",
                "headers": {"Authorization": ["Bearer ", {"$secret": "api_key"}]},
                "body": {"prompt": {"$ref": "ctx.draft"}},
                "response": "json",
            },
            {"op": "state.set", "scope": "conversation", "path": "ok", "value": {"$ref": "steps.call.body.ok"}},
        ),
        http=http,
        ctx={"draft": "She nods."},
    )
    assert http.calls[0]["body"] == {"prompt": "She nods."}
    assert http.calls[0]["headers"]["Authorization"] == ["Bearer ", {"$secret": "api_key"}]
    assert result.effects.state["conversation"] == {"ok": True}


async def test_a_request_without_the_capability_is_refused():
    with pytest.raises(FlowError, match="permission network.request is not granted"):
        await run(
            flow({"op": "http.request", "method": "GET", "url": "https://api.example.invalid/x"}),
            http=_Http(body={}),
            grants=frozenset(),
        )


async def test_the_request_quota_is_charged_per_invocation():
    http = _Http(body={})
    steps = [
        {"op": "http.request", "method": "GET", "url": "https://api.example.invalid/x"}
        for _ in range(MAX_HTTP_REQUESTS_PER_INVOCATION + 1)
    ]
    with pytest.raises(FlowError, match=f"budget of {MAX_HTTP_REQUESTS_PER_INVOCATION} HTTP requests"):
        await run(flow(*steps), http=http)
    assert len(http.calls) == MAX_HTTP_REQUESTS_PER_INVOCATION


async def test_a_request_with_no_egress_service_fails_closed():
    """``None`` rather than a client that refuses everything.

    There is then no object in the invocation a later bug could talk into
    connecting.
    """
    with pytest.raises(FlowError, match="not available in this invocation"):
        await run(flow({"op": "http.request", "method": "GET", "url": "https://api.example.invalid/x"}), http=None)


# ── artifact.emit ───────────────────────────────────────────────────────────


def test_an_artifact_step_names_exactly_one_byte_source():
    with pytest.raises(ValidationError):
        flow({"op": "artifact.emit", "filename": "a.png", "mime": "image/png"})
    with pytest.raises(ValidationError):
        flow(
            {
                "op": "artifact.emit",
                "filename": "a.png",
                "mime": "image/png",
                "data": "x",
                "asset": "assets/a.png",
            }
        )


def test_an_artifact_mime_must_be_an_inert_type():
    """Active content is refused where it is *declared*, not where it renders.

    The mime is stored and travels to the frontend, which turns it into a blob
    the user can open. ``text/html`` here is the package-asset allowlist's hole
    one step further downstream.
    """
    for mime in ("text/html", "image/svg+xml", "application/pdf"):
        with pytest.raises(ValidationError):
            flow({"op": "artifact.emit", "filename": "a", "mime": mime, "data": "x"})
    assert flow({"op": "artifact.emit", "filename": "a.png", "mime": "image/png", "data": "x"})


def test_a_post_hook_may_not_name_a_message_and_an_action_must():
    """Two halves of one rule, both checked at compile time.

    A post hook's target is the row being written -- it has no id yet, and
    naming one would let a hook attach somewhere else in the conversation. An
    action has no such binding, so it must name a target the interpreter can
    prove belongs to the invocation.
    """
    named = flow({"op": "artifact.emit", "filename": "a.png", "mime": "image/png", "data": "x", "message_id": 5})
    unnamed = flow({"op": "artifact.emit", "filename": "a.png", "mime": "image/png", "data": "x"})

    assert check_context(named, OpContext.POST_OBSERVE) == [
        "artifact.emit in a post_observe flow attaches to the message being written and takes no 'message_id'"
    ]
    assert check_context(named, OpContext.ACTION) == []
    assert check_context(unnamed, OpContext.ACTION) == ["artifact.emit in an action must name the 'message_id' it attaches to"]
    assert check_context(unnamed, OpContext.POST_OBSERVE) == []
    # A recovery flow's target is the attachment being rebuilt, which the
    # framework already knows.
    assert check_context(unnamed, OpContext.RECOVERY) == []
    assert check_context(named, OpContext.RECOVERY) != []


async def test_response_bytes_reach_an_artifact_and_nothing_else():
    result = await run(
        flow(
            {
                "id": "call",
                "op": "http.request",
                "method": "GET",
                "url": "https://api.example.invalid/x",
                "response": "bytes",
            },
            {
                "op": "artifact.emit",
                "filename": "render.png",
                "mime": "image/png",
                "data": {"$ref": "steps.call.body"},
                "recovery": {"prompt": "hi"},
            },
        ),
        http=_Http(body=ResponseBytes(media_type="image/png", data=PNG), kind="bytes"),
    )
    [staged] = result.effects.artifacts
    assert staged.data == PNG
    assert staged.recovery == {"prompt": "hi"}
    assert staged.message_id is None


async def test_a_handle_cannot_be_stored_or_interpolated():
    """The bounds already in place do the refusing; no sink needs a new check."""
    with pytest.raises(FlowError):
        await run(
            flow(
                {
                    "id": "call",
                    "op": "http.request",
                    "method": "GET",
                    "url": "https://api.example.invalid/x",
                    "response": "bytes",
                },
                {"op": "state.set", "scope": "conversation", "path": "bytes", "value": {"$ref": "steps.call.body"}},
            ),
            http=_Http(body=ResponseBytes(media_type="image/png", data=PNG)),
        )


async def test_text_and_json_become_artifact_bytes():
    result = await run(
        flow(
            {"op": "artifact.emit", "filename": "note.txt", "mime": "text/plain", "data": "hello"},
            {"op": "artifact.emit", "filename": "data.json", "mime": "application/json", "data": {"a": 1}},
        ),
    )
    assert [a.data for a in result.effects.artifacts] == [b"hello", b'{"a":1}']


async def test_an_asset_is_read_from_the_compiled_revision():
    result = await run(
        flow({"op": "artifact.emit", "filename": "icon.png", "mime": "image/png", "asset": "assets/icon.png"}),
        read_asset=lambda path: PNG if path == "assets/icon.png" else (_ for _ in ()).throw(FlowError("no such asset")),
    )
    assert result.effects.artifacts[0].data == PNG


async def test_an_action_artifact_proves_its_target_is_in_scope():
    async def owns(message_id: int) -> bool:
        return message_id == 42

    body = flow(
        {
            "op": "artifact.emit",
            "filename": "a.png",
            "mime": "image/png",
            "data": "x",
            "message_id": {"$ref": "input.message_id"},
        }
    )
    result = await run(body, context=OpContext.ACTION, owns_message=owns, action_input={"message_id": 42})
    assert result.effects.artifacts[0].message_id == 42

    with pytest.raises(FlowError, match="not part of this conversation"):
        await run(body, context=OpContext.ACTION, owns_message=owns, action_input={"message_id": 43})


async def test_the_artifact_quota_and_byte_budget_are_enforced():
    steps = [
        {"op": "artifact.emit", "filename": f"a{n}.txt", "mime": "text/plain", "data": "x"}
        for n in range(MAX_ARTIFACTS_PER_INVOCATION + 1)
    ]
    with pytest.raises(FlowError, match=f"budget of {MAX_ARTIFACTS_PER_INVOCATION} artifacts"):
        await run(flow(*steps))

    with pytest.raises(FlowError, match="over the"):
        await run(
            flow({"op": "artifact.emit", "filename": "big.txt", "mime": "text/plain", "data": "x" * (MAX_ARTIFACT_BYTES + 1)})
        )


async def test_an_empty_artifact_is_refused():
    with pytest.raises(FlowError, match="produced no bytes"):
        await run(flow({"op": "artifact.emit", "filename": "a.txt", "mime": "text/plain", "data": ""}))


async def test_emitting_without_the_grant_is_refused():
    with pytest.raises(FlowError, match="permission artifact.write is not granted"):
        await run(
            flow({"op": "artifact.emit", "filename": "a.txt", "mime": "text/plain", "data": "x"}),
            grants=frozenset(),
        )


# ── recovery metadata ───────────────────────────────────────────────────────


def test_a_filename_is_reduced_to_an_inert_basename():
    assert safe_filename("../../.bashrc", fallback="f") == "bashrc"
    assert safe_filename("dir/name (1).png", fallback="f") == "name__1_.png"
    assert safe_filename("", fallback="fallback") == "fallback"
    assert safe_filename("...", fallback="fallback") == "fallback"


def test_metadata_records_the_producing_revision_beside_the_package_payload():
    metadata = generation_metadata(
        extension_id="api-artifact",
        version="1.2.0",
        content_digest="abc123",
        recovery={"prompt": "hi"},
    )
    assert metadata["prompt"] == "hi"
    assert metadata[RECOVERY_KEY] == {
        "extension_id": "api-artifact",
        "version": "1.2.0",
        "content_digest": "abc123",
    }


def test_recovery_strips_the_host_half_and_validates_the_rest():
    schema = parse_schema(
        {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
            "additionalProperties": False,
        }
    )
    stored = {"prompt": "hi", RECOVERY_KEY: {"extension_id": "x"}}
    assert recovery_input(stored, schema=schema) == {"prompt": "hi"}

    # The design's exact outcome for a revision that changed its contract: a
    # sanitized diagnostic, and the caller never reaches the write.
    with pytest.raises(FlowError, match="incompatible revision"):
        recovery_input({RECOVERY_KEY: {}}, schema=schema)


def test_the_attachment_payload_is_stamped_by_the_host():
    from backend.features.extensions.artifacts import StagedArtifact

    payload = attachment_payload(
        StagedArtifact(filename="a.png", mime="image/png", data=PNG, annotation="note", recovery={"prompt": "hi"}),
        extension_id="api-artifact",
        version="1.0.0",
        content_digest="abc",
        seed="seed-1",
    )
    # A package chooses neither of these: the bridge validates the pairing
    # before it stages anything, and choosing either would be choosing which
    # workflow owns the file.
    assert payload["workflow_id"] == "api-artifact"
    assert payload["source"] == "workflow:api-artifact"
    assert payload["seed"] == "seed-1"
    assert payload["generation_metadata"]["prompt"] == "hi"
