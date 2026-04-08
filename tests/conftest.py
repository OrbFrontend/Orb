"""
Shared pytest fixtures for the Orb test suite.

Fixtures here are available to all test modules automatically.
Module-specific fixtures should live in the test file itself.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def base_settings() -> dict:
    """Minimal settings dict that satisfies the orchestrator pipeline."""
    return {
        "model_name": "test-model",
        "system_prompt": "You are {{char}}, a helpful assistant.",
        "endpoint_url": "http://localhost:8080",
        "api_key": "",
        "enable_agent": 1,
        "enabled_tools": {
            "set_writing_styles": True,
            "rewrite_user_prompt": False,
            "refine_assistant_output": False,
        },
        "user_name": "Tester",
        "user_description": "",
    }


@pytest.fixture
def base_director() -> dict:
    return {"active_styles": []}


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
