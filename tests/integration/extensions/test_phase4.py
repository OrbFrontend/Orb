"""Phase 4: network, secrets, artifacts, and Git installation, end to end.

These drive real sockets. A throwaway HTTP server answers on loopback, a real
Dulwich-served Git repository is fetched over the wire, and the artifacts land
in the actual ``workflow_attachments`` cache -- because the parts of Phase 4
most worth testing are exactly the ones a mocked transport would skip: the
pinned address, the ``Host`` header, the redirect revalidation, the eviction
metadata, and the recovery flow's binding to the *currently active* revision.

Loopback is reachable here because the granted origin is itself a loopback
origin, which is the design's rule rather than a test convenience: consent to
``http://127.0.0.1:8188`` is consent to a server on your own machine, and the
same client refuses a public-looking name that resolves there.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import backend.database as dbmod
from backend.features.extensions.errors import FlowError
from backend.features.extensions.runtime import current_state
from backend.pipeline import handle_turn
from tests.extension_packages import (
    PNG_BYTES,
    api_artifact_hook_flow,
    api_artifact_manifest,
    api_artifact_recovery_flow,
    orbext,
)

from .conftest import install

# ── a throwaway origin ──────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Answers from ``server.script``: one entry per path."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # noqa: D102 - keep the test output readable
        pass

    def _respond(self):
        status, headers, body = self.server.script.get(self.path.split("?")[0], (404, {}, b"missing"))
        self.server.seen.append(
            {
                "path": self.path,
                "method": self.command,
                "headers": dict(self.headers),
                "body": self.rfile.read(int(self.headers.get("Content-Length") or 0)),
            }
        )
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond


@pytest.fixture
def origin():
    """A loopback HTTP origin whose answers each test scripts."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.script = {}
    server.seen = []
    server.origin = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def http_service(
    origin_url: str,
    secrets: dict[str, str] | None = None,
    *,
    is_cancelled=lambda: False,
):
    from backend.features.extensions.network import HttpService

    async def load():
        return dict(secrets or {})

    return HttpService(
        "api-artifact",
        origins=lambda: frozenset({origin_url}),
        secrets=load,
        is_cancelled=is_cancelled,
    )


async def test_cancellation_abandons_an_outstanding_request(origin):
    """Cancellation stops work that has *started*, not only work between steps.

    The interpreter's own check runs between steps, so without this a cancelled
    turn would hold a socket for the full request budget after the user already
    walked away.
    """
    from backend.features.extensions.errors import FlowCancelled

    origin.script["/v1/echo"] = (
        200,
        {"Content-Type": "application/json"},
        b'{"ok":true}',
    )
    with pytest.raises(FlowCancelled):
        await http_service(origin.origin, is_cancelled=lambda: True).request(
            method="GET",
            url=f"{origin.origin}/v1/echo",
            headers={},
            body=None,
            response_kind="json",
        )


# ── the client on the wire ──────────────────────────────────────────────────


async def test_a_granted_loopback_origin_is_reachable_and_carries_its_host_header(
    origin,
):
    """The pinned address goes in the socket; the name stays in ``Host``.

    Pinning would be worthless if it also changed which virtual host the server
    saw -- and it is the difference between "we validated an address" and "we
    connected to the address we validated".
    """
    origin.script["/v1/echo"] = (
        200,
        {"Content-Type": "application/json"},
        b'{"ok":true}',
    )
    result = await http_service(origin.origin).request(
        method="GET",
        url=f"{origin.origin}/v1/echo",
        headers={"X-Test": "1"},
        body=None,
        response_kind="json",
    )
    assert result == {"status": 200, "body": {"ok": True}}
    assert origin.seen[-1]["headers"]["Host"] == origin.origin.removeprefix("http://")
    assert origin.seen[-1]["headers"]["X-Test"] == "1"


async def test_a_secret_reaches_the_origin_and_never_the_flow(origin):
    origin.script["/v1/render"] = (200, {"Content-Type": "image/png"}, PNG_BYTES)
    result = await http_service(origin.origin, {"api_key": "s3cr3t"}).request(
        method="POST",
        url=f"{origin.origin}/v1/render",
        headers={"Authorization": ["Bearer ", {"$secret": "api_key"}]},
        body={"prompt": "hello"},
        response_kind="bytes",
    )
    assert origin.seen[-1]["headers"]["Authorization"] == "Bearer s3cr3t"
    assert json.loads(origin.seen[-1]["body"]) == {"prompt": "hello"}
    # What comes back is an opaque handle, not a value a flow can read.
    assert result["body"].data == PNG_BYTES
    assert "s3cr3t" not in repr(result["body"])


async def test_a_response_that_echoes_the_secret_is_discarded(origin):
    """Ordinary reflection, refused before the response becomes a flow value.

    Not a claim that a granted origin cannot retain what it was legitimately
    sent -- nothing Orb does prevents that, and the consent copy says so. This
    closes the narrower hole: an endpoint that hands the token back where the
    package could store it.
    """
    origin.script["/v1/echo"] = (
        200,
        {"Content-Type": "application/json"},
        b'{"token":"s3cr3t"}',
    )
    with pytest.raises(FlowError, match="reflected the secret"):
        await http_service(origin.origin, {"api_key": "s3cr3t"}).request(
            method="GET",
            url=f"{origin.origin}/v1/echo",
            headers={},
            body=None,
            response_kind="json",
        )


async def test_a_redirect_to_an_ungranted_origin_is_refused(origin):
    """Every hop is revalidated against the grant, not just the first one."""
    origin.script["/v1/go"] = (302, {"Location": "https://evil.example/x"}, b"")
    with pytest.raises(FlowError, match="not been granted network access"):
        await http_service(origin.origin).request(
            method="GET",
            url=f"{origin.origin}/v1/go",
            headers={},
            body=None,
            response_kind="json",
        )


async def test_a_same_origin_redirect_is_followed_and_keeps_its_headers(origin):
    origin.script["/v1/go"] = (302, {"Location": "/v1/final"}, b"")
    origin.script["/v1/final"] = (
        200,
        {"Content-Type": "application/json"},
        b'{"ok":1}',
    )
    result = await http_service(origin.origin).request(
        method="GET",
        url=f"{origin.origin}/v1/go",
        headers={"X-Test": "keep"},
        body=None,
        response_kind="json",
    )
    assert result["body"] == {"ok": 1}
    assert origin.seen[-1]["headers"]["X-Test"] == "keep"


async def test_a_redirect_loop_is_bounded(origin):
    origin.script["/v1/loop"] = (302, {"Location": "/v1/loop"}, b"")
    with pytest.raises(FlowError, match="more than 3 redirects"):
        await http_service(origin.origin).request(
            method="GET",
            url=f"{origin.origin}/v1/loop",
            headers={},
            body=None,
            response_kind="json",
        )


async def test_a_non_success_status_fails_the_step(origin):
    origin.script["/v1/boom"] = (500, {}, b"nope")
    with pytest.raises(FlowError, match="HTTP 500"):
        await http_service(origin.origin).request(
            method="GET",
            url=f"{origin.origin}/v1/boom",
            headers={},
            body=None,
            response_kind="json",
        )


async def test_an_oversized_response_is_refused_at_the_streaming_boundary(origin, monkeypatch):
    from backend.features.extensions import network

    monkeypatch.setattr(network, "MAX_HTTP_RESPONSE_BYTES", 64)
    origin.script["/v1/big"] = (200, {"Content-Type": "text/plain"}, b"x" * 4096)
    with pytest.raises(FlowError, match="exceeds the 64 byte limit"):
        await http_service(origin.origin).request(
            method="GET",
            url=f"{origin.origin}/v1/big",
            headers={},
            body=None,
            response_kind="text",
        )


# ── secrets through the route ───────────────────────────────────────────────


def _api_artifact(origin_url: str, **overrides) -> bytes:
    """The reference package, retargeted at the throwaway origin.

    Retargeted rather than mocked: the origin in the manifest, the origin in the
    flow's URL, and the origin the socket reaches are the same string, so the
    grant check under test is the production one rather than a stub that always
    agrees.
    """
    manifest = api_artifact_manifest(**overrides)
    manifest["permissions"] = [
        {"capability": "network.request", "origin": origin_url} if p.get("origin") else p for p in manifest["permissions"]
    ]

    def retarget(flow: dict) -> dict:
        for step in flow["steps"]:
            if step.get("op") == "http.request":
                step["url"] = f"{origin_url}/v1/render"
        return flow

    return orbext(
        {
            "orb-extension.json": manifest,
            "flows/render.json": retarget(api_artifact_hook_flow()),
            "flows/recover.json": retarget(api_artifact_recovery_flow()),
        }
    )


async def test_secrets_are_written_by_name_and_read_back_only_as_presence(client, origin):
    await install(client, _api_artifact(origin.origin))

    response = await client.put("/api/extensions/api-artifact/secrets", json={"values": {"api_key": "s3cr3t"}})
    assert response.status_code == 200, response.text
    written = response.json()["data"]["secrets"]
    assert [row["name"] for row in written] == ["api_key"]
    assert written[0]["updated_at"]

    detail = (await client.get("/api/extensions/api-artifact")).json()
    assert [s["name"] for s in detail["secrets"]] == ["api_key"]
    assert all(s["configured"] for s in detail["secrets"])
    # The whole response, serialized. There is no read path that could put the
    # value in it, and this is the assertion that stays true if one is added.
    assert "s3cr3t" not in json.dumps(detail)
    assert "s3cr3t" not in json.dumps((await client.get("/api/extensions")).json())


async def test_consent_explicitly_says_secrets_may_reach_every_origin(client, origin):
    inspection = (
        await client.post(
            "/api/extensions/inspect-file",
            files={"file": ("api.orbext", _api_artifact(origin.origin))},
        )
    ).json()
    warning = inspection["secret_transmission_warning"]
    assert "api_key" in warning
    assert origin.origin in warning
    assert "every approved network origin" in warning


async def test_an_undeclared_secret_is_refused(client, origin):
    await install(client, _api_artifact(origin.origin))
    response = await client.put("/api/extensions/api-artifact/secrets", json={"values": {"other": "x"}})
    assert response.status_code == 400
    assert "declares no secret" in response.json()["detail"]


async def test_a_secret_batch_is_all_or_nothing(client, origin):
    package = _api_artifact(
        origin.origin,
        secrets=[
            {"name": "api_key", "label": "API key"},
            {"name": "second", "label": "Second key"},
        ],
    )
    await install(client, package)
    response = await client.put(
        "/api/extensions/api-artifact/secrets",
        json={"values": {"api_key": "must-not-land", "second": "x" * 4097}},
    )
    assert response.status_code == 400
    assert await dbmod.list_extension_secret_names("api-artifact") == []


async def test_clearing_a_secret_removes_it(client, origin):
    await install(client, _api_artifact(origin.origin))
    await client.put("/api/extensions/api-artifact/secrets", json={"values": {"api_key": "s3cr3t"}})
    await client.put("/api/extensions/api-artifact/secrets", json={"values": {"api_key": None}})
    detail = (await client.get("/api/extensions/api-artifact")).json()
    assert detail["secrets"] == [
        {
            "name": "api_key",
            "label": "API key",
            "description": detail["secrets"][0]["description"],
            "configured": False,
            "updated_at": "",
        }
    ]


async def test_update_removes_secret_rows_the_new_manifest_no_longer_declares(client, origin):
    await install(client, _api_artifact(origin.origin))
    await client.put(
        "/api/extensions/api-artifact/secrets",
        json={"values": {"api_key": "old-value"}},
    )
    replacement = orbext(
        {
            "orb-extension.json": {
                "extension_api": 1,
                "id": "api-artifact",
                "name": "API Artifact",
                "version": "2.0.0",
            }
        }
    )
    inspection = (
        await client.post(
            "/api/extensions/api-artifact/inspect-update",
            files={"file": ("replacement.orbext", replacement)},
        )
    ).json()
    response = await client.post(
        "/api/extensions/api-artifact/update",
        json={"token": inspection["token"], "permissions": []},
    )
    assert response.status_code == 200, response.text
    assert await dbmod.list_extension_secret_names("api-artifact") == []


# ── artifacts through a turn ────────────────────────────────────────────────


async def test_a_post_hook_artifact_commits_with_the_assistant_message(client, db, llm_mock, origin):
    """The whole Phase 4 vertical slice in one turn.

    Consent to an origin, a secret in a header, bytes that never become a flow
    value, and an attachment on the message the turn just wrote -- committed
    with that row rather than before it, so a hook failure could not have left
    an attachment pointing at a message that was never persisted.
    """
    origin.script["/v1/render"] = (200, {"Content-Type": "image/png"}, PNG_BYTES)
    await install(client, _api_artifact(origin.origin))
    await client.put("/api/extensions/api-artifact/secrets", json={"values": {"api_key": "s3cr3t"}})
    await client.put("/api/settings", json={"enable_agent": False})

    cid = "conv-artifact"
    await dbmod.create_conversation(cid, "ext", "Bot", "a scenario")
    llm_mock.enqueue_writer("She nods.")
    [ev async for ev in handle_turn(cid, "hello")]

    messages = await dbmod.get_messages(cid)
    attachments = await dbmod.get_workflow_attachments_for_message(messages[-1]["id"])
    assert len(attachments) == 1
    assert attachments[0]["workflow_id"] == "api-artifact"
    assert attachments[0]["filename"] == "render.png"
    assert attachments[0]["mime_type"] == "image/png"

    # Recovery metadata records the producing revision *and* the package's own
    # parameters, so a later regenerate can reproduce the call without Orb ever
    # re-running the revision that made it.
    metadata = json.loads(attachments[0]["generation_metadata"])
    assert metadata["prompt"] == "She nods."
    assert metadata["orb_extension"]["extension_id"] == "api-artifact"
    assert metadata["orb_extension"]["version"] == "1.0.0"
    assert metadata["orb_extension"]["content_digest"] == current_state().get("api-artifact").digest
    assert origin.seen[-1]["headers"]["Authorization"] == "Bearer s3cr3t"


async def test_regenerate_runs_the_current_revision_on_the_stored_parameters(client, db, llm_mock, origin):
    origin.script["/v1/render"] = (200, {"Content-Type": "image/png"}, PNG_BYTES)
    await install(client, _api_artifact(origin.origin))
    await client.put("/api/extensions/api-artifact/secrets", json={"values": {"api_key": "s3cr3t"}})
    await client.put("/api/settings", json={"enable_agent": False})

    cid = "conv-artifact-regen"
    await dbmod.create_conversation(cid, "ext", "Bot", "a scenario")
    llm_mock.enqueue_writer("She nods.")
    [ev async for ev in handle_turn(cid, "hello")]
    messages = await dbmod.get_messages(cid)
    message_id = messages[-1]["id"]
    original = (await dbmod.get_workflow_attachments_for_message(message_id))[0]

    response = await client.post(
        f"/api/conversations/{cid}/messages/{message_id}/workflow-attachments/{original['id']}/regenerate",
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["attachments"], response.text
    assert origin.seen[-1]["path"].startswith("/v1/render")
    assert json.loads(origin.seen[-1]["body"]) == {"prompt": "She nods."}


async def test_reroll_persists_the_recovery_flows_current_revision_metadata(client, db, llm_mock, origin):
    origin.script["/v1/render"] = (200, {"Content-Type": "image/png"}, PNG_BYTES)
    await install(client, _api_artifact(origin.origin))
    await client.put("/api/extensions/api-artifact/secrets", json={"values": {"api_key": "s3cr3t"}})
    await client.put("/api/settings", json={"enable_agent": False})

    cid = "conv-artifact-reroll-metadata"
    await dbmod.create_conversation(cid, "ext", "Bot", "a scenario")
    llm_mock.enqueue_writer("She nods.")
    [ev async for ev in handle_turn(cid, "hello")]
    message_id = (await dbmod.get_messages(cid))[-1]["id"]
    original = (await dbmod.get_workflow_attachments_for_message(message_id))[0]

    update_bytes = _api_artifact(origin.origin, version="2.0.0")
    inspection = (
        await client.post(
            "/api/extensions/api-artifact/inspect-update",
            files={"file": ("v2.orbext", update_bytes)},
        )
    ).json()
    updated = await client.post(
        "/api/extensions/api-artifact/update",
        json={
            "token": inspection["token"],
            "permissions": [p["value"] for p in inspection["permissions"]],
        },
    )
    assert updated.status_code == 200, updated.text
    active = current_state().get("api-artifact")

    response = await client.post(
        f"/api/conversations/{cid}/messages/{message_id}/workflow-attachments/{original['id']}/reroll-gen",
        json={},
    )
    assert response.status_code == 200, response.text
    sibling_id = response.json()["attachment_id"]
    sibling = next(row for row in await dbmod.get_workflow_attachments_for_message(message_id) if row["id"] == sibling_id)
    metadata = json.loads(sibling["generation_metadata"])
    assert metadata["orb_extension"]["version"] == "2.0.0"
    assert metadata["orb_extension"]["content_digest"] == active.digest


async def test_an_incompatible_recovery_input_leaves_the_attachment_untouched(client, db, llm_mock, origin):
    """An update that changed its recovery contract fails loudly.

    Silently regenerating from a shape the current flow was not written for
    would produce plausible bytes from an unrelated contract, which is the one
    failure a user has no way to notice.
    """
    origin.script["/v1/render"] = (200, {"Content-Type": "image/png"}, PNG_BYTES)
    await install(client, _api_artifact(origin.origin))
    await client.put("/api/extensions/api-artifact/secrets", json={"values": {"api_key": "s3cr3t"}})
    await client.put("/api/settings", json={"enable_agent": False})

    cid = "conv-artifact-incompatible"
    await dbmod.create_conversation(cid, "ext", "Bot", "a scenario")
    llm_mock.enqueue_writer("She nods.")
    [ev async for ev in handle_turn(cid, "hello")]
    messages = await dbmod.get_messages(cid)
    message_id = messages[-1]["id"]
    original = (await dbmod.get_workflow_attachments_for_message(message_id))[0]

    # Update to a revision whose recovery schema no longer accepts the stored
    # payload. The artifact stays exactly as it was.
    tightened = _api_artifact(
        origin.origin,
        version="2.0.0",
        artifact_flows={
            "regenerate": "flows/recover.json",
            "reroll_gen": "flows/recover.json",
            "recovery_input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "style": {"type": "string"},
                },
                "required": ["prompt", "style"],
                "additionalProperties": False,
            },
        },
    )
    inspection = (
        await client.post(
            "/api/extensions/api-artifact/inspect-update",
            files={"file": ("pkg.orbext", tightened)},
        )
    ).json()
    updated = await client.post(
        "/api/extensions/api-artifact/update",
        json={
            "token": inspection["token"],
            "permissions": [p["value"] for p in inspection["permissions"]],
        },
    )
    assert updated.status_code == 200, updated.text

    response = await client.post(
        f"/api/conversations/{cid}/messages/{message_id}/workflow-attachments/{original['id']}/regenerate",
        json={},
    )
    assert response.status_code == 409
    assert "incompatible revision" in response.json()["detail"]
    remaining = await dbmod.get_workflow_attachments_for_message(message_id)
    assert len(remaining) == 1
    assert remaining[0]["data_b64"] == original["data_b64"]


async def test_revoking_the_artifact_grant_unpublishes_the_whole_pair(client, origin):
    """All-or-nothing, because the registry's artifact mandate is.

    A record declaring ``produces_artifacts`` without both recovery hooks fails
    the *entire* overlay swap, so one under-granted package would take every
    other extension -- and the built-ins -- down with it at startup.
    """
    await install(client, _api_artifact(origin.origin))
    from backend.workflows.registry import current_snapshot

    assert current_snapshot().get("api-artifact").produces_artifacts is True

    keep = [p["value"] for p in (await client.get("/api/extensions/api-artifact")).json()["permissions"]]
    await client.put(
        "/api/extensions/api-artifact/permissions",
        json={"permissions": [p for p in keep if p["capability"] != "artifact.write"]},
    )
    record = current_snapshot().get("api-artifact")
    assert record.produces_artifacts is False
    assert record.subscriptions == []
    assert "artifact flows" in current_state().get("api-artifact").blocked


# ── Git installation ────────────────────────────────────────────────────────


@pytest.fixture
def git_origin():
    """A real Git repository served over loopback HTTP by Dulwich."""
    pytest.importorskip("dulwich")
    from dulwich import porcelain
    from dulwich.repo import Repo
    from dulwich.server import DictBackend
    from dulwich.web import WSGIRequestHandlerLogger, WSGIServerLogger, make_wsgi_chain

    root = tempfile.mkdtemp()
    repo = Repo.init(root)
    files = {
        "orb-extension.json": json.dumps(
            {
                "extension_api": 1,
                "id": "git-package",
                "name": "Git Package",
                "version": "1.0.0",
            }
        ),
        "README.md": "not referenced by the manifest",
    }
    for name, body in files.items():
        with open(os.path.join(root, name), "w") as handle:
            handle.write(body)
    porcelain.add(repo, [os.path.join(root, name) for name in files])
    commit = porcelain.commit(
        repo,
        message=b"init",
        committer=b"T <t@example.invalid>",
        author=b"T <t@example.invalid>",
    )
    porcelain.tag_create(
        repo,
        b"v1.0.0",
        author=b"T <t@example.invalid>",
        message=b"release",
        annotated=True,
    )

    from wsgiref.simple_server import make_server

    server = make_server(
        "127.0.0.1",
        0,
        make_wsgi_chain(DictBackend({b"/": repo})),
        handler_class=WSGIRequestHandlerLogger,
        server_class=WSGIServerLogger,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {
            "url": f"http://127.0.0.1:{server.server_address[1]}/",
            "commit": commit.decode(),
        }
    finally:
        server.shutdown()


async def test_a_repository_installs_through_the_ordinary_two_phase_flow(client, git_origin):
    """Same inspect/consent/apply path an archive takes, same compiler.

    The only thing Git adds is where the bytes came from -- which is why the
    resolved commit is recorded and the source kind changes, and nothing else
    about the lifecycle does.
    """
    inspection = await client.post("/api/extensions/inspect", json={"url": git_origin["url"], "allow_local": True})
    assert inspection.status_code == 200, inspection.text
    body = inspection.json()
    assert body["id"] == "git-package"

    installed = await client.post(
        "/api/extensions/install",
        json={"token": body["token"], "permissions": [], "enabled": True},
    )
    assert installed.status_code == 200, installed.text

    entry = current_state().get("git-package")
    assert entry.row["source_kind"] == "git"
    assert entry.row["source_url"] == git_origin["url"]
    assert entry.row["active_commit_id"] == git_origin["commit"]
    # Only manifest-referenced files are compiled, persisted, or served -- the
    # README is in the repository and not in the revision.
    assert sorted(entry.compiled.files) == ["orb-extension.json"]

    listed = next(row for row in (await client.get("/api/extensions")).json()["extensions"] if row["id"] == "git-package")
    assert listed["commit_id"] == git_origin["commit"]
    assert listed["source_url"] == git_origin["url"]


async def test_an_annotated_tag_resolves_to_and_records_its_commit(client, git_origin):
    inspection = await client.post(
        "/api/extensions/inspect",
        json={"url": git_origin["url"], "ref": "v1.0.0", "allow_local": True},
    )
    assert inspection.status_code == 200, inspection.text
    body = inspection.json()
    installed = await client.post(
        "/api/extensions/install",
        json={"token": body["token"], "permissions": [], "enabled": True},
    )
    assert installed.status_code == 200, installed.text
    assert current_state().get("git-package").row["active_commit_id"] == git_origin["commit"]


async def test_git_update_records_commit_when_archive_and_git_compile_to_the_same_digest(client, git_origin):
    archive = orbext(
        {
            "orb-extension.json": {
                "extension_api": 1,
                "id": "git-package",
                "name": "Git Package",
                "version": "1.0.0",
            }
        }
    )
    await install(client, archive)
    assert current_state().get("git-package").row["active_commit_id"] is None

    inspection = (
        await client.post(
            "/api/extensions/git-package/inspect-update-git",
            json={"url": git_origin["url"], "allow_local": True},
        )
    ).json()
    response = await client.post(
        "/api/extensions/git-package/update",
        json={"token": inspection["token"], "permissions": []},
    )
    assert response.status_code == 200, response.text
    entry = current_state().get("git-package")
    assert entry.row["active_commit_id"] == git_origin["commit"]
    assert entry.row["source_kind"] == "git"


async def test_a_local_repository_is_refused_without_explicit_confirmation(client, git_origin):
    response = await client.post("/api/extensions/inspect", json={"url": git_origin["url"]})
    assert response.status_code == 400
    assert "loopback" in response.json()["detail"]


@pytest.mark.parametrize(
    "url,reason",
    [
        ("ftp://example.invalid/repo.git", "http or https"),
        ("https://user@example.invalid/repo.git", "userinfo"),
        ("https://example.invalid/repo.git?x=1", "query string"),
    ],
)
async def test_the_installer_applies_the_flow_client_url_policy(client, url, reason):
    response = await client.post("/api/extensions/inspect", json={"url": url})
    assert response.status_code == 400
    assert reason in response.json()["detail"]


async def test_an_unknown_ref_is_refused_rather_than_guessed(client, git_origin):
    response = await client.post(
        "/api/extensions/inspect",
        json={"url": git_origin["url"], "ref": "no-such-branch", "allow_local": True},
    )
    assert response.status_code == 400
    assert "no branch or tag" in response.json()["detail"]
