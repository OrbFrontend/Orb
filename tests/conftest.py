"""
Shared pytest fixtures for the Orb test suite.

Fixtures here are available to all test modules automatically.
Module-specific fixtures should live in the test file itself.
"""

from __future__ import annotations

import pytest

import backend.database.connection as db_connection
from backend.inference.local_models import assets


@pytest.fixture(scope="session", autouse=True)
def _never_the_real_database(tmp_path_factory):
    """Make an unisolated database access fail instead of touching a real install."""
    db_connection.DB_PATH = str(tmp_path_factory.mktemp("db_guard") / "unisolated-see-tests-conftest.db")


@pytest.fixture(scope="session")
def _empty_models_dir(tmp_path_factory) -> str:
    """One empty directory standing in for ``backend/data/models/`` all session."""
    return str(tmp_path_factory.mktemp("no_models"))


@pytest.fixture(autouse=True)
def _no_downloaded_models(request, monkeypatch, _empty_models_dir):
    """Run every test as if no local-ML weights had been downloaded.

    THIS IS A SPEED FIX AND AN ISOLATION FIX, IN THAT ORDER OF VISIBILITY BUT
    NOT OF IMPORTANCE.

    ``local_feature_ready`` is ``available()`` and-ed with the user's setting,
    and ``available()`` is "extras importable AND the GGUF is on disk". Both
    halves are true on a developer box that has run ``requirements-ml.txt`` and
    pressed Download, so the pipeline's classifier doors -- ``markup_axes``
    above all -- opened onto *real llama.cpp inference* in the middle of
    ordinary integration tests. One run of ``test_group_chats.py`` fired 114
    markup-classifier ``embed()`` calls; each one loads and decodes on
    ``n_threads=4``, which is why that file alone burned ~240 CPU-seconds in
    16s of wall clock, and why ``tests.sh all`` (``-n 8``) oversubscribed the
    machine badly enough to blow the httpx read timeouts in the real-loopback
    streaming tests.

    The isolation half is the part that outlives the stopwatch: with the
    weights present those tests exercised the classifier, and in CI -- which
    installs requirements-dev.txt and never requirements-ml.txt -- the very
    same tests exercised the ``classify_axes`` heuristic fallback. Two
    different code paths behind one green tick, chosen by what happened to be
    in a gitignored directory. No test asserts a real model's output (the ones
    that want the classifier lane force the gate open and stub the classifier,
    e.g. ``test_format_consistency_kv``'s ``voice_on``), so pinning every
    machine to the CI path loses no coverage.

    ``model_dir`` is the seam because ``resolve_path``, ``present`` and
    ``variant_path`` all reach disk through it and look it up in this module's
    globals at call time -- the same seam ``test_local_ml``'s ``_empty_model_dir``
    already uses per-module, and module-level autouse fixtures still win over
    this one. Tests *about* where the directory resolves opt out with
    ``@pytest.mark.real_model_dir``.
    """
    if request.node.get_closest_marker("real_model_dir"):
        return
    monkeypatch.setattr(assets, "model_dir", lambda: _empty_models_dir)
    # The autocomplete GGUF has an env override that bypasses model_dir entirely.
    monkeypatch.delenv("ORB_AUTOCOMPLETE_MODEL", raising=False)


@pytest.fixture
def base_settings() -> dict:
    """Minimal settings dict that satisfies the orchestrator pipeline."""
    return {
        "model_name": "test-model",
        "system_prompt": "You are a helpful assistant.",
        "endpoint_url": "http://localhost:8080",
        "api_key": "",
        "enable_agent": 1,
        "enabled_tools": {
            "direct_scene": True,
            "editor_apply_patch": False,
        },
        "user_name": "Tester",
        "user_description": "",
    }


@pytest.fixture
def base_director() -> dict:
    return {"active_moods": []}


@pytest.fixture
def base_fragments() -> list[dict]:
    return [
        {
            "id": "tense",
            "description": "Tense, urgent prose",
            "prompt_text": "Write with short, punchy sentences.",
            "negative_prompt": "Avoid flowing, relaxed sentences.",
        },
        {
            "id": "lyrical",
            "description": "Lyrical, flowing prose",
            "prompt_text": "Write in long, melodic sentences.",
            "negative_prompt": "",
        },
    ]
