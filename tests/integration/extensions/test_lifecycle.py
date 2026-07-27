"""A metadata-only package traversing its whole lifecycle over HTTP.

Phase 1's exit gate, stated as tests: install, enable, update, rollback,
permission edit, uninstall, and purge all work end to end, and no package file
becomes browser or server code along the way.

The tests deliberately go through the routes rather than the lifecycle module.
The two-request consent shape -- inspect, read a diff, apply with an opaque
token -- is the product contract, and reaching past it would leave the part
that binds a decision to a package untested.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend.features.extensions import content_store, staging
from backend.features.extensions.runtime import current_state
from tests.extension_packages import (
    full_manifest,
    full_package,
    manifest,
    metadata_package,
    orbext,
    scoring_flow,
)

from .conftest import catalog, entry, install

# ── install ─────────────────────────────────────────────────────────────────


async def test_inspect_reports_identity_requirements_and_a_consent_diff(client):
    body = (await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", full_package())})).json()

    assert body["id"] == "scene-meter"
    assert body["version"] == "1.0.0"
    assert body["compatible"] is True
    assert len(body["content_digest"]) == 64
    assert body["operation"] == "install"
    assert sorted(body["files"]) == ["flows/score-scene.json", "orb-extension.json", "ui/inspector.json"]
    # A fresh install has nothing granted yet, so every request is an addition.
    assert body["permission_diff"]["unchanged"] == []
    assert {row["capability"] for row in body["permission_diff"]["added"]} == {
        "context.draft.read",
        "model.call",
        "state.read",
        "state.write",
        "ui.contribute",
    }
    # The token is opaque: it does not encode the id, the digest, or the path.
    assert body["id"] not in body["token"] and body["content_digest"] not in body["token"]


async def test_inspect_marks_a_model_call_as_needing_emphasis(client):
    body = (await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", full_package())})).json()
    model_call = next(row for row in body["permissions"] if row["capability"] == "model.call")
    assert model_call["emphasis"] == "high"
    assert "cost money" in model_call["description"]


async def test_inspect_alone_installs_nothing(client):
    await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package())})
    assert (await catalog(client))["extensions"] == []


async def test_install_registers_grants_enablement_and_a_new_generation(client, db):
    before = (await catalog(client))["runtime_generation"]
    body = await install(client, full_package())

    assert body["runtime_generation"] > before
    assert body["effects"] == [{"resource": "extension.catalog"}]

    row = await (await db.execute("SELECT * FROM extension_packages WHERE id = 'scene-meter'")).fetchone()
    assert row["load_status"] == "available"
    assert row["source_kind"] == "archive"
    assert len(json.loads(row["approved_permissions"])) == 5
    # Content is durable and addressed by the digest the user consented to.
    assert content_store.exists(row["active_digest"])

    revision = await (await db.execute("SELECT * FROM extension_revisions WHERE extension_id = 'scene-meter'")).fetchone()
    assert revision["content_digest"] == row["active_digest"]
    assert json.loads(revision["manifest"])["id"] == "scene-meter"


async def test_install_rolls_back_package_metadata_if_enablement_cannot_commit(client, db):
    inspection = (
        await client.post(
            "/api/extensions/inspect-file",
            files={"file": ("pkg.orbext", metadata_package())},
        )
    ).json()
    await db.execute(
        "CREATE TRIGGER reject_extension_enablement "
        "BEFORE UPDATE OF workflow_enabled ON settings "
        "BEGIN SELECT RAISE(ABORT, 'forced enablement failure'); END"
    )
    await db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced enablement failure"):
        await client.post(
            "/api/extensions/install",
            json={"token": inspection["token"], "permissions": [], "enabled": False},
        )

    package = await (await db.execute("SELECT id FROM extension_packages WHERE id = 'scene-meter'")).fetchone()
    revision = await (
        await db.execute("SELECT extension_id FROM extension_revisions WHERE extension_id = 'scene-meter'")
    ).fetchone()
    assert package is None
    assert revision is None


async def test_installing_with_a_reused_token_fails(client):
    inspection = (await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package())})).json()
    payload = {"token": inspection["token"], "permissions": [], "enabled": True}
    assert (await client.post("/api/extensions/install", json=payload)).status_code == 200
    assert (await client.post("/api/extensions/install", json=payload)).status_code == 409


async def test_install_rejects_a_permission_the_manifest_does_not_request(client):
    inspection = (await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package())})).json()
    response = await client.post(
        "/api/extensions/install",
        json={
            "token": inspection["token"],
            "permissions": [{"capability": "state.write", "scope": "character"}],
            "enabled": True,
        },
    )
    assert response.status_code == 400
    assert "does not request" in response.json()["detail"]


async def test_installing_a_second_time_is_directed_to_the_update_flow(client):
    """A reinstall over an existing package must show a permission diff against
    what is already granted, which the install path does not compute."""
    await install(client, metadata_package())
    inspection = (await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package())})).json()
    response = await client.post(
        "/api/extensions/install", json={"token": inspection["token"], "permissions": [], "enabled": True}
    )
    assert response.status_code == 400
    assert "update flow" in response.json()["detail"]


@pytest.mark.parametrize("reserved", ["macros", "tts"])
async def test_reserved_and_builtin_ids_cannot_be_claimed(client, reserved: str):
    response = await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package(id=reserved))})
    if response.status_code == 200:
        applied = await client.post(
            "/api/extensions/install",
            json={"token": response.json()["token"], "permissions": [], "enabled": True},
        )
        assert applied.status_code == 400
    assert reserved not in {item["id"] for item in (await catalog(client))["extensions"]}


# ── the manifest and the loader gate ────────────────────────────────────────


async def test_an_installed_package_is_declarative_in_the_workflow_manifest(client):
    """The one property that must hold before any package id reaches the
    frontend: a community entry never lands in the band the loader ``import()``s.
    """
    await install(client, metadata_package())
    body = (await client.get("/api/workflows")).json()
    record = next(w for w in body if w["id"] == "scene-meter")
    assert record["source"] == "community"
    assert record["frontend_kind"] == "declarative"
    trusted = {w["id"] for w in body if w["frontend_kind"] == "trusted_module"}
    assert "scene-meter" not in trusted


async def test_an_installed_package_publishes_its_hook_but_never_tools(client):
    """An available, fully granted package binds its hook to a host adapter.

    The adapter is the point: the record gains a callable, but it is a generic
    interpreter closure over compiled JSON, not package code. ``tools`` stays
    empty because enabling an ordinary extension must not change the main model
    tool blob, and ``produces_artifacts`` stays False until the phase that can
    honor the regenerate/reroll contract.
    """
    await install(client, full_package())
    record = current_state().get("scene-meter")
    assert record is not None and record.load_status.value == "available"
    assert record.blocked == ()
    from backend.workflows.registry import current_snapshot

    workflow = current_snapshot().get("scene-meter")
    assert workflow is not None
    assert [(s.hook_type.value, s.stage.value) for s in workflow.subscriptions] == [("post_pipeline", "observe")]
    assert workflow.produces_artifacts is False
    assert workflow.tools == []


# ── enablement ──────────────────────────────────────────────────────────────


async def test_installing_disabled_leaves_the_package_visible_and_off(client):
    await install(client, metadata_package(), enabled=False)
    row = await entry(client, "scene-meter")
    assert row["enabled"] is False
    assert row["load_status"] == "available"


async def test_enablement_toggles_and_advances_the_generation(client):
    await install(client, metadata_package(), enabled=False)
    before = (await catalog(client))["runtime_generation"]
    body = (await client.post("/api/extensions/scene-meter/enabled", json={"enabled": True})).json()
    assert body["data"] == {"enabled": True}
    assert body["runtime_generation"] > before
    assert (await entry(client, "scene-meter"))["enabled"] is True


async def test_enablement_toggle_rolls_back_both_mirrors_on_failure(client, db):
    await install(client, metadata_package(), enabled=False)
    await db.execute(
        "CREATE TRIGGER reject_extension_enablement "
        "BEFORE UPDATE OF workflow_enabled ON settings "
        "BEGIN SELECT RAISE(ABORT, 'forced enablement failure'); END"
    )
    await db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced enablement failure"):
        await client.post("/api/extensions/scene-meter/enabled", json={"enabled": True})

    package = await (await db.execute("SELECT enabled FROM extension_packages WHERE id = 'scene-meter'")).fetchone()
    settings = await (await db.execute("SELECT workflow_enabled FROM settings WHERE id = 1")).fetchone()
    assert package["enabled"] == 0
    assert json.loads(settings["workflow_enabled"])["scene-meter"] is False


async def test_enabling_an_unknown_extension_is_a_404(client):
    assert (await client.post("/api/extensions/nope/enabled", json={"enabled": True})).status_code == 404


# ── update and rollback ─────────────────────────────────────────────────────


async def test_update_swaps_the_revision_and_retains_the_previous_digest(client, db):
    await install(client, metadata_package())
    first = (await entry(client, "scene-meter"))["active_digest"]

    inspection = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="1.1.0"))},
        )
    ).json()
    assert inspection["operation"] == "update"
    assert inspection["installed_version"] == "1.0.0"
    assert inspection["version"] == "1.1.0"

    applied = await client.post("/api/extensions/scene-meter/update", json={"token": inspection["token"], "permissions": []})
    assert applied.status_code == 200

    row = await (await db.execute("SELECT * FROM extension_packages WHERE id = 'scene-meter'")).fetchone()
    assert row["active_digest"] != first
    assert row["previous_digest"] == first
    assert (await entry(client, "scene-meter"))["version"] == "1.1.0"


async def test_identical_update_preserves_the_rollback_pointer(client, db):
    await install(client, metadata_package())
    original = (await entry(client, "scene-meter"))["active_digest"]
    update = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="2.0.0"))},
        )
    ).json()
    await client.post(
        "/api/extensions/scene-meter/update",
        json={"token": update["token"], "permissions": []},
    )
    active = (await entry(client, "scene-meter"))["active_digest"]

    identical = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="2.0.0"))},
        )
    ).json()
    applied = await client.post(
        "/api/extensions/scene-meter/update",
        json={"token": identical["token"], "permissions": []},
    )
    assert applied.status_code == 200

    row = await (
        await db.execute("SELECT active_digest, previous_digest FROM extension_packages WHERE id = 'scene-meter'")
    ).fetchone()
    assert row["active_digest"] == active
    assert row["previous_digest"] == original
    assert (await entry(client, "scene-meter"))["can_rollback"] is True


async def test_revision_history_and_content_are_bounded_to_active_plus_rollback(client, db):
    await install(client, metadata_package(version="1.0.0"))
    first = (await entry(client, "scene-meter"))["active_digest"]

    second_inspection = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="2.0.0"))},
        )
    ).json()
    await client.post(
        "/api/extensions/scene-meter/update",
        json={"token": second_inspection["token"], "permissions": []},
    )
    second = (await entry(client, "scene-meter"))["active_digest"]

    third_inspection = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="3.0.0"))},
        )
    ).json()
    await client.post(
        "/api/extensions/scene-meter/update",
        json={"token": third_inspection["token"], "permissions": []},
    )
    third = (await entry(client, "scene-meter"))["active_digest"]

    revisions = list(
        await db.execute_fetchall("SELECT content_digest FROM extension_revisions WHERE extension_id = 'scene-meter'")
    )
    assert {row["content_digest"] for row in revisions} == {second, third}
    assert not content_store.exists(first)
    assert content_store.exists(second)
    assert content_store.exists(third)


async def test_an_update_that_changes_the_manifest_id_is_rejected(client):
    await install(client, metadata_package())
    response = await client.post(
        "/api/extensions/scene-meter/inspect-update",
        files={"file": ("pkg.orbext", metadata_package(id="something-else"))},
    )
    assert response.status_code == 400
    assert "must keep the installed id" in response.json()["detail"]


async def test_a_stale_update_token_is_a_conflict_and_changes_nothing(client):
    """Two update screens open; the second to apply must not clobber the first.

    The compare-and-set is against the digest observed at *inspection*, so the
    loser is told to inspect again rather than silently overwriting a revision
    the user never diffed.
    """
    await install(client, metadata_package())
    stale = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="1.1.0"))},
        )
    ).json()
    fresh = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="1.2.0"))},
        )
    ).json()

    assert (
        await client.post("/api/extensions/scene-meter/update", json={"token": fresh["token"], "permissions": []})
    ).status_code == 200
    conflict = await client.post("/api/extensions/scene-meter/update", json={"token": stale["token"], "permissions": []})
    assert conflict.status_code == 409
    # The applied revision is untouched: a rejected update leaves everything active.
    assert (await entry(client, "scene-meter"))["version"] == "1.2.0"


async def test_a_rejected_update_leaves_the_prior_package_fully_active(client):
    await install(client, full_package())
    before = await entry(client, "scene-meter")

    broken = orbext({"orb-extension.json": full_manifest(version="2.0.0")})  # references flows that are not in the archive
    response = await client.post("/api/extensions/scene-meter/inspect-update", files={"file": ("pkg.orbext", broken)})
    assert response.status_code == 400

    after = await entry(client, "scene-meter")
    assert after["version"] == before["version"] == "1.0.0"
    assert after["active_digest"] == before["active_digest"]
    assert after["load_status"] == "available"


async def test_rollback_restores_the_previous_revision(client):
    await install(client, metadata_package())
    original = (await entry(client, "scene-meter"))["active_digest"]

    update = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package(version="2.0.0"))},
        )
    ).json()
    await client.post("/api/extensions/scene-meter/update", json={"token": update["token"], "permissions": []})
    assert (await entry(client, "scene-meter"))["can_rollback"] is True

    inspection = (await client.post("/api/extensions/scene-meter/inspect-rollback")).json()
    assert inspection["operation"] == "rollback"
    assert inspection["version"] == "1.0.0"
    applied = await client.post("/api/extensions/scene-meter/rollback", json={"token": inspection["token"], "permissions": []})
    assert applied.status_code == 200

    row = await entry(client, "scene-meter")
    assert row["active_digest"] == original
    assert row["version"] == "1.0.0"


async def test_rollback_without_a_previous_revision_is_refused(client):
    await install(client, metadata_package())
    response = await client.post("/api/extensions/scene-meter/inspect-rollback")
    assert response.status_code == 400
    assert "no previous revision" in response.json()["detail"]


# ── permissions ─────────────────────────────────────────────────────────────


async def test_revoking_a_permission_blocks_the_entry_points_that_needed_it(client):
    """Under-granted entry points are listed with a diagnostic rather than
    failing halfway through ordinary use."""
    await install(client, full_package())
    assert (await entry(client, "scene-meter"))["blocked_entry_points"] == []

    keep = [row["value"] for row in (await entry(client, "scene-meter"))["permissions"] if row["capability"] != "model.call"]
    response = await client.put("/api/extensions/scene-meter/permissions", json={"permissions": keep})
    assert response.status_code == 200

    row = await entry(client, "scene-meter")
    assert row["load_status"] == "available"  # still installed, still inspectable
    assert "hook post_pipeline" in row["blocked_entry_points"]
    assert "permissions they need" in row["diagnostic"]
    assert [p["granted"] for p in row["permissions"] if p["capability"] == "model.call"] == [False]


async def test_permissions_cannot_be_widened_beyond_the_manifest(client):
    await install(client, metadata_package())
    response = await client.put(
        "/api/extensions/scene-meter/permissions",
        json={"permissions": [{"capability": "network.request", "origin": "https://evil.example"}]},
    )
    assert response.status_code == 400
    assert "does not request" in response.json()["detail"]


# ── failure isolation and startup ───────────────────────────────────────────


async def test_a_package_needing_features_this_build_lacks_installs_but_is_inert(client):
    """Installed and available are separate axes: the user's next step is
    "update Orb", not "this package is broken"."""
    await install(client, metadata_package(requires={"operations": ["quantum.entangle"], "components": []}))
    row = await entry(client, "scene-meter")
    assert row["load_status"] == "incompatible"
    assert "quantum.entangle" in row["diagnostic"]
    record = (await client.get("/api/workflows")).json()
    assert next(w for w in record if w["id"] == "scene-meter")["load_status"] == "incompatible"


async def test_a_future_api_version_is_refused_with_a_conflict(client):
    response = await client.post(
        "/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package(extension_api=2))}
    )
    assert response.status_code == 409
    assert "extension_api 2" in response.json()["detail"]


async def test_missing_content_is_isolated_and_the_rest_still_loads(client):
    """A restart on a machine whose content directory vanished."""
    from backend.features.extensions.lifecycle import reconcile

    await install(client, metadata_package(id="alpha"))
    await install(client, metadata_package(id="beta"))
    content_store.remove((await entry(client, "alpha"))["active_digest"])

    await reconcile()

    alpha, beta = await entry(client, "alpha"), await entry(client, "beta")
    assert alpha["load_status"] == "missing_content"
    assert "reinstall" in alpha["diagnostic"]
    assert beta["load_status"] == "available"
    # Built-ins are unaffected by a broken package.
    manifest_ids = {w["id"] for w in (await client.get("/api/workflows")).json()}
    assert {"alpha", "beta", "tts"} <= manifest_ids


async def test_tampered_content_is_marked_invalid_rather_than_run(client):
    """The user consented to a digest, not to whatever is in the directory now."""
    import os

    from backend.features.extensions.lifecycle import reconcile

    await install(client, metadata_package())
    digest = (await entry(client, "scene-meter"))["active_digest"]
    with open(os.path.join(content_store.content_path(digest), "orb-extension.json"), "w") as fh:
        json.dump(manifest(version="9.9.9"), fh)

    await reconcile()

    row = await entry(client, "scene-meter")
    assert row["load_status"] == "invalid"
    assert "does not match its recorded digest" in row["diagnostic"]


async def test_reconciliation_refuses_a_contract_that_no_longer_matches_consent(client, db):
    from backend.features.extensions.lifecycle import reconcile

    await install(client, metadata_package())
    await db.execute(
        "UPDATE extension_revisions SET contract_fingerprint = ? WHERE extension_id = 'scene-meter'",
        ("0" * 64,),
    )
    await db.commit()

    await reconcile()

    row = await entry(client, "scene-meter")
    assert row["load_status"] == "incompatible"
    assert "contract changed since consent" in row["diagnostic"]

    inspection = (
        await client.post(
            "/api/extensions/scene-meter/inspect-update",
            files={"file": ("pkg.orbext", metadata_package())},
        )
    ).json()
    response = await client.post(
        "/api/extensions/scene-meter/update",
        json={"token": inspection["token"], "permissions": []},
    )
    assert response.status_code == 200
    assert (await entry(client, "scene-meter"))["load_status"] == "available"


async def test_reconciliation_survives_a_restart_and_recompiles(client):
    from backend.features.extensions.lifecycle import reconcile

    await install(client, full_package())
    digest = (await entry(client, "scene-meter"))["active_digest"]

    generation = await reconcile()
    row = await entry(client, "scene-meter")
    assert row["load_status"] == "available"
    assert row["active_digest"] == digest
    assert (await catalog(client))["runtime_generation"] == generation


async def test_staging_tokens_do_not_survive_a_restart(client):
    from backend.features.extensions.lifecycle import reconcile

    inspection = (await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package())})).json()
    await reconcile()
    response = await client.post(
        "/api/extensions/install", json={"token": inspection["token"], "permissions": [], "enabled": True}
    )
    assert response.status_code == 409


async def test_abandoned_inspections_are_collected(client):
    from backend.features.extensions.lifecycle import reconcile

    inspected = (
        await client.post("/api/extensions/inspect-file", files={"file": ("pkg.orbext", metadata_package(id="ghost"))})
    ).json()
    assert content_store.exists(inspected["content_digest"])
    staging.clear()
    await reconcile()
    assert not content_store.exists(inspected["content_digest"])


async def test_unrelated_lifecycle_mutation_keeps_live_inspection_content(client):
    inspected = (
        await client.post(
            "/api/extensions/inspect-file",
            files={"file": ("pkg.orbext", metadata_package(id="pending"))},
        )
    ).json()
    await install(client, metadata_package(id="installed"))

    assert content_store.exists(inspected["content_digest"])
    response = await client.post(
        "/api/extensions/install",
        json={"token": inspected["token"], "permissions": [], "enabled": True},
    )
    assert response.status_code == 200
    assert {item["id"] for item in (await catalog(client))["extensions"]} == {
        "installed",
        "pending",
    }


# ── uninstall and purge ─────────────────────────────────────────────────────


async def test_uninstall_removes_registration_but_preserves_namespaced_state(client, db):
    await install(client, metadata_package())
    conv = (await client.post("/api/conversations", json={"title": "T"})).json()
    from backend.database import set_workflow_state

    await set_workflow_state(conv["id"], "scene-meter", {"tension": 42})

    assert (await client.delete("/api/extensions/scene-meter")).status_code == 200
    assert (await catalog(client))["extensions"] == []

    row = await (await db.execute("SELECT workflow_state FROM conversations WHERE id = ?", (conv["id"],))).fetchone()
    assert json.loads(row["workflow_state"])["scene-meter"] == {"tension": 42}


async def test_preserved_state_is_listed_as_orphaned_so_it_stays_reachable(client):
    await install(client, metadata_package())
    conv = (await client.post("/api/conversations", json={"title": "T"})).json()
    from backend.database import set_workflow_state

    await set_workflow_state(conv["id"], "scene-meter", {"tension": 42})
    await client.delete("/api/extensions/scene-meter")

    orphaned = (await catalog(client))["orphaned_data"]
    assert [row["id"] for row in orphaned] == ["scene-meter"]
    # Built-in workflow slots are not orphans.
    assert "tts" not in {row["id"] for row in orphaned}


async def test_an_explicit_off_survives_uninstall_and_reinstall(client):
    await install(client, metadata_package())
    await client.post("/api/extensions/scene-meter/enabled", json={"enabled": False})
    await client.delete("/api/extensions/scene-meter")

    # Reinstalling without asking for a specific state must not silently
    # re-enable what the user switched off... but an explicit enable does.
    await install(client, metadata_package(), enabled=False)
    assert (await entry(client, "scene-meter"))["enabled"] is False


async def test_purge_previews_before_it_deletes_and_binds_the_confirmation(client, db):
    await install(client, metadata_package())
    conv = (await client.post("/api/conversations", json={"title": "T"})).json()
    from backend.database import set_workflow_state

    await set_workflow_state(conv["id"], "scene-meter", {"tension": 42})

    preview = (await client.post("/api/extensions/scene-meter/purge-data", json={})).json()
    assert preview["counts"]["conversations.workflow_state"] == 1
    assert preview["total"] >= 1

    # Data is untouched until the confirmation carrying the preview's token.
    row = await (await db.execute("SELECT workflow_state FROM conversations WHERE id = ?", (conv["id"],))).fetchone()
    assert "scene-meter" in json.loads(row["workflow_state"])

    applied = await client.post("/api/extensions/scene-meter/purge-data", json={"token": preview["token"]})
    assert applied.status_code == 200
    assert applied.json()["data"]["removed"]["conversations.workflow_state"] == 1

    row = await (await db.execute("SELECT workflow_state FROM conversations WHERE id = ?", (conv["id"],))).fetchone()
    assert "scene-meter" not in json.loads(row["workflow_state"])


@pytest.mark.parametrize("extension_id", ["tts", "macros", "%25"])
async def test_purge_rejects_builtin_reserved_and_invalid_namespaces(client, extension_id):
    response = await client.post(f"/api/extensions/{extension_id}/purge-data", json={})
    assert response.status_code == 400


async def test_rejected_builtin_purge_preserves_its_state(client, db):
    conv = (await client.post("/api/conversations", json={"title": "T"})).json()
    from backend.database import set_workflow_state

    await set_workflow_state(conv["id"], "tts", {"voice": "alloy"})
    assert (await client.post("/api/extensions/tts/purge-data", json={})).status_code == 400

    row = await (await db.execute("SELECT workflow_state FROM conversations WHERE id = ?", (conv["id"],))).fetchone()
    assert json.loads(row["workflow_state"])["tts"] == {"voice": "alloy"}


async def test_purge_conflicts_if_namespaced_rows_changed_after_preview(client, db):
    await install(client, metadata_package())
    first = (await client.post("/api/conversations", json={"title": "First"})).json()
    from backend.database import set_workflow_state

    await set_workflow_state(first["id"], "scene-meter", {"tension": 1})
    preview = (await client.post("/api/extensions/scene-meter/purge-data", json={})).json()

    second = (await client.post("/api/conversations", json={"title": "Second"})).json()
    await set_workflow_state(second["id"], "scene-meter", {"tension": 2})
    response = await client.post(
        "/api/extensions/scene-meter/purge-data",
        json={"token": preview["token"]},
    )
    assert response.status_code == 409
    assert "changed since preview" in response.json()["detail"]

    rows = list(
        await db.execute_fetchall(
            "SELECT id, workflow_state FROM conversations WHERE id IN (?, ?)",
            (first["id"], second["id"]),
        )
    )
    tensions = {row["id"]: json.loads(row["workflow_state"])["scene-meter"]["tension"] for row in rows}
    assert tensions == {first["id"]: 1, second["id"]: 2}


async def test_a_purge_token_cannot_be_replayed(client):
    await install(client, metadata_package())
    preview = (await client.post("/api/extensions/scene-meter/purge-data", json={})).json()
    assert (await client.post("/api/extensions/scene-meter/purge-data", json={"token": preview["token"]})).status_code == 200
    replay = await client.post("/api/extensions/scene-meter/purge-data", json={"token": preview["token"]})
    assert replay.status_code == 409


async def test_purge_leaves_the_package_disabled(client):
    """Otherwise a purge would invite the package to repopulate what it just
    deleted."""
    await install(client, metadata_package())
    preview = (await client.post("/api/extensions/scene-meter/purge-data", json={})).json()
    await client.post("/api/extensions/scene-meter/purge-data", json={"token": preview["token"]})
    assert (await entry(client, "scene-meter"))["enabled"] is False


async def test_purge_by_id_works_for_an_uninstalled_extension(client, db):
    """ "Uninstall but preserve data" would otherwise make cleanup unreachable."""
    await install(client, metadata_package())
    conv = (await client.post("/api/conversations", json={"title": "T"})).json()
    from backend.database import set_workflow_state

    await set_workflow_state(conv["id"], "scene-meter", {"tension": 42})
    await client.delete("/api/extensions/scene-meter")

    preview = (await client.post("/api/extensions/scene-meter/purge-data", json={})).json()
    assert preview["counts"]["conversations.workflow_state"] == 1
    await client.post("/api/extensions/scene-meter/purge-data", json={"token": preview["token"]})

    row = await (await db.execute("SELECT workflow_state FROM conversations WHERE id = ?", (conv["id"],))).fetchone()
    assert json.loads(row["workflow_state"]) == {}


# ── detail ──────────────────────────────────────────────────────────────────


async def test_detail_lists_entry_points_as_data_not_as_a_component_tree(client):
    await install(client, full_package())
    body = (await client.get("/api/extensions/scene-meter")).json()
    assert body["views"] == ["inspector"]
    assert body["placements"] == [{"slot": "inspector", "view": "inspector", "command": None}]
    assert body["hooks"]["post_pipeline"]["stage"] == "observe"
    assert body["requires"]["operations"] == ["model.structured", "state.set", "ui.invalidate"]
    assert body["secrets"] == []
    # The detail surface lists entry points; trees cross only the view route.
    assert "root" not in json.dumps(body)


async def test_detail_for_an_unknown_extension_is_a_404(client):
    assert (await client.get("/api/extensions/nope")).status_code == 404


async def test_a_flow_file_is_never_served_or_reachable_as_a_route(client):
    """No route serves a package *file*, and none has a path a package chooses.

    Phase 3 added the view, resource, and asset routes, so this no longer means
    "there is no URL". It means the narrower and more durable thing: the asset
    route resolves an exact compiled asset key and a flow is not one, the view
    route serves a compiled tree rather than bytes, an undeclared action is a
    404, and nothing is mounted under ``/static``.
    """
    await install(client, full_package())
    for path in (
        "/api/extensions/scene-meter/assets/flows/score-scene.json",
        "/api/extensions/scene-meter/assets/../orb-extension.json",
        "/api/extensions/scene-meter/actions/anything",
        "/static/extensions/scene-meter/flows/score-scene.json",
    ):
        assert (await client.get(path)).status_code in (404, 405), path


async def test_the_view_route_serves_a_compiled_tree_not_package_bytes(client):
    """The view route returns host-validated JSON, never the file as stored.

    The distinction matters: a route that streamed the declared source file
    would make every future component field a place where unvalidated package
    content reaches the renderer. What crosses the wire is the *parsed* model.
    """
    await install(client, full_package())
    body = (await client.get("/api/extensions/scene-meter/views/inspector")).json()
    assert body["id"] == "inspector"
    assert body["view"]["view_version"] == 1
    assert body["view"]["root"]["component"] in {"stack", "card", "meter", "text"}
    assert "$schema" not in json.dumps(body["view"])


async def test_scoring_flow_fixture_is_actually_referenced(client):
    """Guard on the fixture itself: the full package must exercise a flow file.

    Without this, a later refactor could drop the flow from the archive and the
    lifecycle tests would keep passing against a metadata-only package while
    claiming to cover compilation.
    """
    assert scoring_flow()["steps"][0]["op"] == "model.structured"
    await install(client, full_package())
    compiled = current_state().get("scene-meter")
    assert compiled is not None and compiled.compiled is not None
    assert "flows/score-scene.json" in compiled.compiled.flows
