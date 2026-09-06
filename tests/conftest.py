"""
Shared pytest fixtures for the Orb test suite.

Fixtures here are available to all test modules automatically.
Module-specific fixtures should live in the test file itself.
"""

from __future__ import annotations

import pytest

import backend.database.connection as db_connection


@pytest.fixture(scope="session", autouse=True)
def _never_the_real_database(tmp_path_factory):
    """Point ``DB_PATH`` away from the developer's own database, for every test.

    ``DB_PATH`` defaults to ``backend/data/app.db`` -- a real, populated install --
    and only a fixture redirects it. So a test that reaches the database and forgets
    to take one does not fail; it quietly opens the real file and writes to it. That
    is not a flaky test, it is data loss, and it is silent: nothing in the output says
    which database was touched.

    It happened here. An unfixtured test called ``set_workflow_config`` and replaced a
    live install's entire image_gen slot -- styles, cloud connections, API keys and
    imported ComfyUI graphs -- and the suite reported nine passes.

    No assertion catches that afterwards, so the guard is arranged in front of it: the
    session default is a path with no schema, and a test that reaches the database
    without isolating it fails on a missing table naming this file. Per-test fixtures
    monkeypatch straight over it, so isolated tests are unaffected.

    Session-scoped and autouse, so it is in place before the first test and before any
    fixture that reads ``DB_PATH`` in order to restore it later.
    """
    db_connection.DB_PATH = str(tmp_path_factory.mktemp("db_guard") / "unisolated-see-tests-conftest.db")


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
