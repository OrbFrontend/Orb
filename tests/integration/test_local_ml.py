"""Local-ML routes: status tri-state, download gating, the enable toggle, and
the prose rewriter's variant selector.

NO NETWORK AND NO WEIGHTS. ``download`` and the llama-server fetch are both
monkeypatched to raise wherever a route could reach them, which is the guard
that matters here: these are the two calls that would otherwise pull gigabytes
in CI. Nothing in this file loads a model or starts a child process.
"""

from __future__ import annotations

import pytest

from backend.inference import local_ml
from backend.inference.prose_rewriter import catalog
from backend.inference.prose_rewriter import runtime as pr_runtime


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


async def test_status_reports_deps_per_feature_not_globally(client):
    """The rewriter needs only huggingface_hub; the classifiers need the binding.

    One global answer would gray out a button that works.
    """
    st = (await client.get("/api/local-ml/status")).json()
    assert {"deps_ok", "reason"} <= set(st["features"]["prose_rewriter"])
    assert {"deps_ok", "reason"} <= set(st["features"]["autocomplete"])


async def test_config_roundtrips_the_variant_and_gpu_flag(client):
    resp = await client.post("/api/local-ml/prose_rewriter/config", json={"variant": "1.7b-q8", "gpu": False})
    assert resp.status_code == 200
    assert resp.json()["local_ml_config"]["prose_rewriter"] == {"variant": "1.7b-q8", "gpu": False}
    st = (await client.get("/api/local-ml/status")).json()
    assert st["features"]["prose_rewriter"]["selected"] == "1.7b-q8"
    assert st["features"]["prose_rewriter"]["gpu"] is False


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
