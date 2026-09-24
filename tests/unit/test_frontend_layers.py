"""Regression checks for the workflow import boundary."""

from scripts.check_frontend_layers import (
    has_computed_dynamic_import,
    imported_paths,
    workflow_import_allowed,
)


def test_workflow_import_must_stay_within_its_own_slice(tmp_path):
    root = tmp_path / "workflows"
    module = root / "tts" / "panel.js"

    assert workflow_import_allowed(module, "/static/workflow_api.js", root)
    assert workflow_import_allowed(module, "./local.js", root)
    assert workflow_import_allowed(module, "./nested/view.js", root)
    assert not workflow_import_allowed(module, "../../state.js", root)
    assert not workflow_import_allowed(module, "../other/panel.js", root)
    assert not workflow_import_allowed(module, "/static/state.js", root)


def test_workflow_checker_sees_all_import_forms():
    source = """import { a } from './static.js';
import './side_effect.js';
await import('./dynamic.js');
"""

    assert set(imported_paths(source)) == {"./static.js", "./side_effect.js", "./dynamic.js"}
    assert not has_computed_dynamic_import(source)
    assert has_computed_dynamic_import("await import(modulePath);")
