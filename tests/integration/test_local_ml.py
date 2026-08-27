"""Local-ML routes: status tri-state, download gating, the enable toggle, and
the prose rewriter's variant selector.

NO NETWORK AND NO WEIGHTS. ``download`` and the llama-server fetch are both
monkeypatched to raise wherever a route could reach them, which is the guard
that matters here: these are the two calls that would otherwise pull gigabytes
in CI. Nothing in this file loads a model or starts a child process.
"""

from __future__ import annotations

import pytest

from backend.api.routes import local_ml as ml_routes
from backend.inference import local_ml
from backend.inference.prose_rewriter import catalog
from backend.inference.prose_rewriter import runtime as pr_runtime


@pytest.fixture(autouse=True)
def _no_child_process(monkeypatch):
    """No llama-server child, ever — the third leg of this file's house rule.

    Selecting a variant pre-warms it, and a developer machine that has both a
    real ``llama-server`` on PATH and a fake GGUF written by these tests has
    everything the host needs to go and start one. Neutralised at the two
    seams the routes use: the pre-warm task, and the release the delete path
    takes before it unlinks. ``HOST`` is a module singleton, so a task that
    grabbed its lock under one test's event loop would also fail the next test
    with "bound to a different event loop".
    """

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ml_routes, "_prewarm", _noop)
    monkeypatch.setattr(ml_routes.prose_rewriter.HOST, "release", _noop)


@pytest.fixture(autouse=True)
def _empty_model_dir(tmp_path, monkeypatch):
    """Point data/models/ at an empty temp dir for every test here.

    These tests describe a fresh install -- nothing downloaded -- but
    ``model_dir()`` is a fixed repo path, so on a developer machine that has
    actually fetched a variant ``present`` read True and the status test
    failed. The delete test is the sharper reason: it calls the real
    ``delete_model``, which on such a machine would remove a multi-GB weight
    file as a side effect of running the suite. ``local_ml.present`` and
    ``catalog.variant_path`` both reach disk through this one function (the
    latter imports it lazily, so patching the attribute covers it), which
    makes it the single seam that isolates every path in this module.
    """
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(local_ml, "model_dir", lambda: str(models))
    return models


@pytest.fixture
def _deps_installed(monkeypatch):
    """Report the rewriter's extras as installed, for the tests past the gate.

    CI installs requirements-dev.txt and never requirements-ml.txt, so
    ``huggingface_hub`` is absent, ``deps_ok("prose_rewriter")`` is False, and
    the download route 400s at its deps check -- before it reaches the patched
    ``download`` or the selection sweep that follows it. A developer machine
    with the extras 200s instead, which is why the two selection tests below
    passed locally and failed in CI (the second one silently: its assertions
    still hold when the sweep never runs, so it passed there for the wrong
    reason). Those tests are about the sweep, not the gate; the gate has its
    own tests above and they patch ``deps_ok`` the other way.
    """
    monkeypatch.setattr(local_ml, "deps_ok", lambda feature=None: (True, ""))


async def test_download_400_when_deps_missing(client, monkeypatch):
    monkeypatch.setattr(local_ml, "deps_ok", lambda feature=None: (False, "extras not installed"))
    # download() must never run; guard against an accidental network hit.
    monkeypatch.setattr(local_ml, "download", lambda f: (_ for _ in ()).throw(AssertionError("must not download")))
    resp = await client.post("/api/local-ml/autocomplete/download")
    assert resp.status_code == 400


async def test_download_unknown_feature_404(client):
    resp = await client.post("/api/local-ml/nope/download")
    assert resp.status_code == 404


async def test_status_covers_every_registered_feature(client):
    # The Settings card is generic over MODELS, so a new entry (pov_classifier)
    # only reaches the UI if status enumerates the registry rather than a list.
    st = (await client.get("/api/local-ml/status")).json()
    assert set(st["features"]) == set(local_ml.MODELS)
    assert st["features"]["pov_classifier"]["size_mb"] == local_ml.MODELS["pov_classifier"].size_mb


async def test_enable_toggle_roundtrips(client):
    resp = await client.post("/api/local-ml/autocomplete/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["local_ml_enabled"] == {"autocomplete": False}
    # Status reflects the flip.
    st = (await client.get("/api/local-ml/status")).json()
    assert st["features"]["autocomplete"]["enabled"] is False


# ── prose rewriter: the variant-bearing shape ────────────────────────────────


async def test_status_enumerates_the_rewriter_variants(client):
    st = (await client.get("/api/local-ml/status")).json()
    info = st["features"]["prose_rewriter"]
    assert [v["id"] for v in info["variants"]] == [v.id for v in catalog.variants()]
    assert all({"id", "label", "detail", "size_mb", "present"} <= set(v) for v in info["variants"])
    # Nothing downloaded in CI, so nothing is selected and the card offers
    # downloads rather than a selector.
    assert info["selected"] is None
    assert info["present"] is False
    assert info["runtime"] == "llama_server"
    assert info["batch_size"] == 4


async def test_status_reports_deps_per_feature_not_globally(client):
    """The rewriter needs only huggingface_hub; the classifiers need the binding.

    One global answer would gray out a button that works.
    """
    st = (await client.get("/api/local-ml/status")).json()
    assert {"deps_ok", "reason"} <= set(st["features"]["prose_rewriter"])
    assert {"deps_ok", "reason"} <= set(st["features"]["autocomplete"])


async def test_config_roundtrips_variant_gpu_and_batch_size(client):
    resp = await client.post("/api/local-ml/prose_rewriter/config", json={"variant": "1.7b-q8", "gpu": False, "batch_size": 2})
    assert resp.status_code == 200
    assert resp.json()["local_ml_config"]["prose_rewriter"] == {
        "variant": "1.7b-q8",
        "gpu": False,
        "batch_size": 2,
    }
    st = (await client.get("/api/local-ml/status")).json()
    assert st["features"]["prose_rewriter"]["selected"] == "1.7b-q8"
    assert st["features"]["prose_rewriter"]["gpu"] is False
    assert st["features"]["prose_rewriter"]["batch_size"] == 2


@pytest.mark.parametrize("batch_size", [0, 5, 1.5, "2", True])
async def test_config_rejects_an_invalid_batch_size(client, batch_size):
    resp = await client.post(
        "/api/local-ml/prose_rewriter/config",
        json={"variant": "1.7b-q8", "gpu": True, "batch_size": batch_size},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "batch_size must be an integer from 1 to 4"


async def test_config_rejects_an_unknown_variant(client):
    resp = await client.post("/api/local-ml/prose_rewriter/config", json={"variant": "9b-q2", "gpu": True})
    assert resp.status_code == 404


async def test_config_404s_for_a_feature_with_no_variants(client):
    resp = await client.post("/api/local-ml/autocomplete/config", json={"variant": "x"})
    assert resp.status_code == 404


async def test_config_404s_for_an_unknown_feature(client):
    assert (await client.post("/api/local-ml/nope/config", json={})).status_code == 404


async def test_a_variant_download_never_reaches_the_network(client, monkeypatch):
    # The house guard, extended to the variant path: the route must refuse on
    # deps before it can touch hf_hub_download.
    monkeypatch.setattr(local_ml, "deps_ok", lambda feature=None: (False, "extras not installed"))
    monkeypatch.setattr(local_ml, "download", lambda f, v=None: (_ for _ in ()).throw(AssertionError("must not download")))
    resp = await client.post("/api/local-ml/prose_rewriter/download", json={"variant": "1.7b-q8"})
    assert resp.status_code == 400


async def test_downloading_an_unknown_variant_404s(client, monkeypatch):
    monkeypatch.setattr(local_ml, "download", lambda f, v=None: (_ for _ in ()).throw(AssertionError("must not download")))
    resp = await client.post("/api/local-ml/prose_rewriter/download", json={"variant": "9b-q2"})
    assert resp.status_code == 404


async def test_deleting_a_model_that_is_not_there_is_not_an_error(client):
    resp = await client.request("DELETE", "/api/local-ml/prose_rewriter/model", params={"variant": "1.7b-q8"})
    assert resp.status_code == 200
    assert resp.json()["removed"] is False


async def test_deleting_an_unknown_variant_404s(client):
    resp = await client.request("DELETE", "/api/local-ml/prose_rewriter/model", params={"variant": "9b-q2"})
    assert resp.status_code == 404


async def test_the_runtime_fetch_is_never_reached_by_accident(client, monkeypatch):
    """A ~100 MB GitHub download behind an explicit button, and only that button.

    Status reads the binary's *presence*; nothing on the ordinary paths may
    decide to go and get one.
    """
    monkeypatch.setattr(pr_runtime, "fetch", lambda backend="gpu": (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert (await client.get("/api/local-ml/status")).status_code == 200
    assert (await client.post("/api/local-ml/prose_rewriter/config", json={"variant": None})).status_code == 200
    assert (await client.post("/api/local-ml/prose_rewriter/enabled", json={"enabled": True})).status_code == 200


async def _select(client) -> str | None:
    st = (await client.get("/api/local-ml/status")).json()
    return st["features"]["prose_rewriter"]["selected"]


async def test_a_download_arms_the_feature_when_nothing_usable_is_selected(
    client, monkeypatch, _empty_model_dir, _deps_installed
):
    """Downloading a checkpoint selects it; the radio was the only thing that did.

    Without this, the obvious path — download, switch on — left the feature
    enabled and silently inert, because ``resolve_prose_rewrite`` reads the
    stored variant and there wasn't one.
    """
    variant = catalog.variants()[0]
    monkeypatch.setattr(local_ml, "download", lambda f, v=None: (_empty_model_dir / variant.local_name).write_text("gguf"))

    resp = await client.post("/api/local-ml/prose_rewriter/download", json={"variant": variant.id})

    assert resp.status_code == 200
    assert resp.json()["local_ml_config"]["prose_rewriter"]["variant"] == variant.id
    assert await _select(client) == variant.id


async def test_a_second_download_never_steals_a_working_selection(client, monkeypatch, _empty_model_dir, _deps_installed):
    """The pick is user data: filling a hole is allowed, overriding is not."""
    first, second = catalog.variants()[0], catalog.variants()[1]
    (_empty_model_dir / first.local_name).write_text("gguf")
    await client.post("/api/local-ml/prose_rewriter/config", json={"variant": first.id, "gpu": False, "batch_size": 1})
    monkeypatch.setattr(local_ml, "download", lambda f, v=None: (_empty_model_dir / second.local_name).write_text("gguf"))

    await client.post("/api/local-ml/prose_rewriter/download", json={"variant": second.id})

    assert await _select(client) == first.id
    # And the fields it did not come to change are left alone.
    st = (await client.get("/api/local-ml/status")).json()["features"]["prose_rewriter"]
    assert st["gpu"] is False
    assert st["batch_size"] == 1


async def test_deleting_the_selected_model_hands_the_selection_to_one_that_is_there(client, _empty_model_dir):
    first, second = catalog.variants()[0], catalog.variants()[1]
    for v in (first, second):
        (_empty_model_dir / v.local_name).write_text("gguf")
    await client.post("/api/local-ml/prose_rewriter/config", json={"variant": first.id, "gpu": True, "batch_size": 4})

    resp = await client.request("DELETE", "/api/local-ml/prose_rewriter/model", params={"variant": first.id})

    assert resp.json()["local_ml_config"]["prose_rewriter"]["variant"] == second.id
    assert await _select(client) == second.id


async def test_deleting_the_last_model_clears_the_selection(client, _empty_model_dir):
    only = catalog.variants()[0]
    (_empty_model_dir / only.local_name).write_text("gguf")
    await client.post("/api/local-ml/prose_rewriter/config", json={"variant": only.id, "gpu": True, "batch_size": 4})

    await client.request("DELETE", "/api/local-ml/prose_rewriter/model", params={"variant": only.id})

    assert await _select(client) is None


async def test_enabling_repairs_a_selection_that_points_at_nothing(client, _empty_model_dir):
    """The self-heal for an install that downloaded before the sweep existed."""
    present = catalog.variants()[1]
    (_empty_model_dir / present.local_name).write_text("gguf")

    resp = await client.post("/api/local-ml/prose_rewriter/enabled", json={"enabled": True})

    assert resp.json()["local_ml_config"]["prose_rewriter"]["variant"] == present.id
    assert await _select(client) == present.id
