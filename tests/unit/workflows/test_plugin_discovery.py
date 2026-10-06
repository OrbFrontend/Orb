"""Workflow plug-ins register from their own packages, with no host wiring.

Every package under ``backend/workflows/`` declares its ``Workflow`` and hook
subscriptions as ``WORKFLOW``. Discovery registers them in package-name order,
which is the manifest order the frontend loads workflow modules in.
"""

import importlib
from pathlib import Path

import backend.workflows
from backend.workflows import (
    HookType,
    get_subscription,
    iter_subscriptions,
    list_workflows,
    prose_rewriter_host,
)
from backend.workflows.format_consistency import hooks as format_consistency_hooks
from backend.workflows.tts import hooks as tts_hooks

_PACKAGES = sorted(path.name for path in Path(backend.workflows.__file__).parent.iterdir() if (path / "__init__.py").is_file())


def test_every_plugin_package_is_registered_in_name_order_with_its_declared_subscriptions():
    # Scoped to the packages: other tests register ad-hoc workflows into the same process-wide registry.
    assert [w.id for w in list_workflows() if w.id in _PACKAGES] == _PACKAGES
    for name in _PACKAGES:
        declared = importlib.import_module(f"backend.workflows.{name}").WORKFLOW
        assert declared.id == name
        for sub in declared.subscriptions:
            bound = get_subscription(name, sub.hook_type)
            assert bound is not None, f"{name} declares {sub.hook_type.value} but it is not bound"
            assert (bound.callable, bound.priority, bound.workflow_id) == (sub.callable, sub.priority, name)


def test_post_pipeline_runs_rewriter_then_format_consistency_then_tts():
    assert [(s.workflow_id, s.priority, s.callable) for s in iter_subscriptions(HookType.POST_PIPELINE)] == [
        ("prose_rewriter", -20, prose_rewriter_host.post_pipeline),
        ("format_consistency", -10, format_consistency_hooks.post_pipeline),
        ("tts", 0, tts_hooks.post_pipeline),
    ]
