"""Shared pytest fixtures for the Orb test suite.

Fixtures here are available to all test modules automatically. Module-specific fixtures should live in the test file itself.
"""

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
    """Hide downloaded Local ML weights to keep tests isolated from real inference.

    An empty model_dir selects the same fallback on developer machines and CI.
    Classifier tests explicitly stub their lane; real_model_dir opts out.
    """
    if request.node.get_closest_marker("real_model_dir"):
        return
    monkeypatch.setattr(assets, "model_dir", lambda: _empty_models_dir)
    # The autocomplete GGUF has an env override that bypasses model_dir entirely.
    monkeypatch.delenv("ORB_AUTOCOMPLETE_MODEL", raising=False)
