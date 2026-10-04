"""Regression checks for the frontend import boundaries."""

from scripts.check_frontend_layers import (
    has_computed_dynamic_import,
    import_cycle,
    imported_paths,
    unregistered_actions,
    unused_exports,
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


def test_module_cycle_is_reported():
    assert import_cycle({"a.js": {"b.js"}, "b.js": {"c.js"}, "c.js": {"a.js"}}) == ["a.js", "b.js", "c.js", "a.js"]
    assert import_cycle({"a.js": {"b.js"}, "b.js": set()}) is None


def test_action_checker_rejects_unknown_scopes_and_handlers():
    source = """<button data-wf-action="chat:send">Send</button>
<button data-wf-action="caht:send">Typo</button>
const action = busy ? "chat:stop" : "chat:snd";
const mime = "image:png";
"""
    assert unregistered_actions(source, {"chat:send", "chat:stop"}) == {"caht:send", "chat:snd"}
    assert unregistered_actions('<button data-wf-action="new:missing">', set()) == {"new:missing"}


def test_workflow_template_actions_are_checked():
    source = """<button data-wf-action="${WORKFLOW_ID}:refresh">Refresh</button>
<button data-wf-action="${WORKFLOW_ID}:refersh">Typo</button>
<button data-wf-action="${action}">Computed</button>"""
    assert unregistered_actions(source, {"my_workflow:refresh"}, workflow_id="my_workflow") == {"my_workflow:refersh"}


def test_an_export_counts_only_while_something_imports_it(tmp_path):
    fe = tmp_path / "frontend"
    source = {
        "core.js": "export function used() {}\nexport function viaBarrel() {}\nexport function orphan() {}\n"
        "export function dynamic() {}\nexport function member() {}\nexport function deadChain() {}\n",
        "barrel.js": 'export { viaBarrel, deadChain } from "./core.js";\n',
        "app.js": 'import { used } from "./core.js";\nimport { viaBarrel as renamed } from "./barrel.js";\n',
        "workflow_api.js": "export function facadeOnly() {}\n",
    }
    modules = {(fe / name).resolve(): text for name, text in source.items()}
    test = (tmp_path / "tests" / "frontend" / "core.test.mjs").resolve()
    consumers = {
        test: 'const { dynamic } = await import("../../frontend/core.js");\n'
        'const core = await import("../../frontend/core.js");\ncore.member();\n'
    }

    unused = unused_exports(modules, consumers, {(fe / "workflow_api.js").resolve()}, frontend=fe)

    # deadChain is re-exported, but nothing imports the re-export, so neither end counts as used.
    assert unused == {(fe / "core.js").resolve(): {"orphan", "deadChain"}, (fe / "barrel.js").resolve(): {"deadChain"}}
