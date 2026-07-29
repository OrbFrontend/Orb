"""Phase 6 hardening: golden digests, parser/compiler fuzzing, and budgets.

Three properties that the per-feature suites cannot state, because each of them
is about the compiler as a whole rather than about any one rule it enforces:

* **Golden.** The content digest, the canonical manifest encoding, and the
  consent-contract fingerprint are stable across refactors. They are not
  internal details: the digest is a revision's identity in the content store,
  and the fingerprint is what startup compares against the record of what the
  user consented to. Change either accidentally and every installed package
  demands fresh consent for bytes that did not move. The golden package here is
  written out literally rather than built from ``tests/extension_packages.py``
  precisely so that editing a shared fixture cannot silently rewrite the
  expectation this test exists to hold still.
* **Fuzz.** Mutating a well-formed package -- structurally, and at the byte
  level -- produces a ``PackageError`` or a different valid package, and never
  anything else. ``PackageError`` is the vocabulary the routes map to status
  codes; a ``KeyError``, ``TypeError``, or ``RecursionError`` escaping
  compilation is a 500 on an install request, which is the shape of bug this
  catches. Seeded, so a failure is reproducible from the reported seed.
* **Budgets.** A package sitting at its declaration limits compiles in bounded
  time. The numbers are deliberately loose -- they catch an accidentally
  quadratic walk, not a five-percent regression, because a tight wall-clock
  assertion on shared CI is a test that fails for reasons unrelated to the code.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

import pytest

from backend.features.extensions.compiler import compile_package
from backend.features.extensions.digest import canonical_json_bytes
from backend.features.extensions.errors import PackageError
from backend.features.extensions.json_loader import load_json
from backend.features.extensions.limits import (
    MAX_ACTIONS,
    MAX_FLOW_STEPS_DECLARED,
    MAX_JSON_DEPTH,
    MAX_JSON_MEMBERS,
    MAX_MANIFEST_BYTES,
)
from backend.features.extensions.sources import ArchiveSource
from tests.extension_packages import (
    api_artifact_package,
    conversation_map_package,
    fragment_meter_package,
    orbext,
    outcome_resolver_package,
    scene_meter_package,
    tag_librarian_package,
)


def compile_bytes(data: bytes):
    with ArchiveSource(data) as source:
        return compile_package(source)


# ── golden: digest, canonical encoding, and consent fingerprint ─────────────
#
# Regenerating these is a deliberate act. If a change moves them, the question
# to answer first is whether every already-installed package should be asked to
# re-consent -- because that is what the change does to a real install.

GOLDEN_MANIFEST: dict[str, Any] = {
    "extension_api": 1,
    "id": "golden-package",
    "name": "Golden Package",
    "version": "1.0.0",
    "author": "Orb",
    "description": "A frozen package whose compiled identity must not drift.",
    "requires": {
        "operations": ["model.structured", "state.set", "ui.invalidate"],
        "components": ["meter", "stack", "text"],
    },
    "permissions": [
        {"capability": "context.read", "field": "draft"},
        {"capability": "model.call", "lane": "agent"},
        {"capability": "state.read", "scope": "conversation"},
        {"capability": "state.write", "scope": "conversation"},
        {"capability": "ui.contribute", "slot": "inspector"},
    ],
    "hooks": {"post_pipeline": {"flow": "flows/score.json", "stage": "observe"}},
    "views": {"inspector": {"source": "ui/inspector.json"}},
    "placements": [{"slot": "inspector", "view": "inspector"}],
}

GOLDEN_FLOW: dict[str, Any] = {
    "flow_version": 1,
    "steps": [
        {
            "id": "score",
            "op": "model.structured",
            "lane": "agent",
            "prompt": {"$template": "Rate scene tension from 0 to 100.\n\n{{ctx.draft}}"},
            "output_schema": {
                "type": "object",
                "properties": {"tension": {"type": "integer", "minimum": 0, "maximum": 100}},
                "required": ["tension"],
                "additionalProperties": False,
            },
        },
        {"op": "state.set", "scope": "conversation", "path": "tension", "value": {"$ref": "steps.score.tension"}},
        {"op": "ui.invalidate", "view": "inspector"},
    ],
}

GOLDEN_VIEW: dict[str, Any] = {
    "view_version": 1,
    "root": {
        "component": "stack",
        "children": [
            {"component": "text", "value": {"$template": "Tension: {{state.conversation.tension}}"}},
            {"component": "meter", "value": {"$ref": "state.conversation.tension"}, "minimum": 0, "maximum": 100},
        ],
    },
}

GOLDEN_DIGEST = "e963d129ca6847e17ba63c8cb79fee230cbac34363941f2aa6a4e398d27cb200"
GOLDEN_FINGERPRINT = "63a5a0ac667a6c01661bd46301d12401109d5ebb7cb660a0562cdd76deb3105d"


def golden_package() -> bytes:
    return orbext(
        {
            "orb-extension.json": GOLDEN_MANIFEST,
            "flows/score.json": GOLDEN_FLOW,
            "ui/inspector.json": GOLDEN_VIEW,
        }
    )


def test_golden_package_identity_is_stable():
    """Digest and fingerprint pin what a user consented to. They do not move."""
    compiled = compile_bytes(golden_package())
    assert compiled.digest == GOLDEN_DIGEST
    assert compiled.contract_fingerprint == GOLDEN_FINGERPRINT


def test_golden_canonical_manifest_encoding_is_stable():
    """The digest is taken over this encoding, so its shape is part of the contract."""
    compiled = compile_bytes(golden_package())
    assert compiled.manifest_json() == canonical_json_bytes(json.loads(compiled.manifest_json())).decode("utf-8")
    assert '"extension_api":1' in compiled.manifest_json()
    # Canonical form: sorted keys, no whitespace, no trailing float noise.
    assert ", " not in compiled.manifest_json()


def test_golden_derived_requirements_are_stable():
    """What the compiler derives from the same bytes is itself a frozen answer.

    Derivation is the half of consent the manifest does not control: a change
    that widens it grants a package authority its author never declared, and a
    change that narrows it lets an under-declared package install.
    """
    derived = compile_bytes(golden_package()).requirements
    assert sorted(f"{cap}:{param}" for cap, param in derived.permissions) == [
        "context.read:draft",
        "model.call:agent",
        "state.read:conversation",
        "state.write:conversation",
        # Two entries, not one: the placement needs the named slot, and
        # ``ui.invalidate`` needs the unparameterized capability.
        "ui.contribute:None",
        "ui.contribute:inspector",
    ]
    assert sorted(derived.operations) == ["model.structured", "state.set", "ui.invalidate"]
    assert sorted(derived.components) == ["meter", "stack", "text"]
    assert derived.secrets == frozenset()


# ── fuzz: only PackageError escapes compilation ─────────────────────────────

REFERENCE_PACKAGES = {
    "scene-meter": scene_meter_package,
    "conversation-map": conversation_map_package,
    "tag-librarian": tag_librarian_package,
    "api-artifact": api_artifact_package,
    "fragment-meter": fragment_meter_package,
    "outcome-resolver": outcome_resolver_package,
    "golden": golden_package,
}

# Values chosen to reach the interesting rejections: type confusion, the depth
# and breadth limits, unicode, and numbers outside the JSON contract.
FUZZ_VALUES: list[Any] = [
    None,
    True,
    0,
    -1,
    2**63,
    1.5,
    "",
    "\x00",
    "\ud7ff\uffff",
    "../../etc/passwd",
    "x" * 4096,
    [],
    {},
    [1, 2, 3],
    {"op": "state.set"},
    {"$ref": "ctx.draft"},
    {"$template": "{{ctx.history}}"},
    [{} for _ in range(MAX_JSON_MEMBERS + 1)],
]


def _containers(value: Any) -> list[dict | list]:
    """Every dict and list in *value*, found without recursion."""
    found: list[dict | list] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            found.append(node)
            stack.extend(node)
    return found


def _mutate(value: Any, rng: random.Random) -> Any:
    """Apply one structural mutation in place, returning the same root."""
    targets = _containers(value)
    node = rng.choice(targets)
    if isinstance(node, dict) and node:
        key = rng.choice(sorted(node))
        choice = rng.randrange(3)
        if choice == 0:
            del node[key]
        elif choice == 1:
            node[key] = rng.choice(FUZZ_VALUES)
        else:
            node[f"orb_fuzz_{rng.randrange(1000)}"] = rng.choice(FUZZ_VALUES)
    elif isinstance(node, list) and node:
        index = rng.randrange(len(node))
        if rng.random() < 0.5:
            node.pop(index)
        else:
            node[index] = rng.choice(FUZZ_VALUES)
    elif isinstance(node, dict):
        node["orb_fuzz"] = rng.choice(FUZZ_VALUES)
    else:
        node.append(rng.choice(FUZZ_VALUES))
    return value


def _repack(data: bytes, rng: random.Random) -> bytes:
    """Decode a package's JSON files, mutate one, and rebuild the archive."""
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    json_names = sorted(name for name in files if name.endswith(".json"))
    target = rng.choice(json_names)
    decoded = json.loads(files[target])
    for _ in range(rng.randrange(1, 4)):
        decoded = _mutate(decoded, rng)
    rebuilt: dict[str, Any] = {}
    for name, raw in files.items():
        rebuilt[name] = decoded if name == target else (json.loads(raw) if name.endswith(".json") else raw)
    return orbext(rebuilt)


@pytest.mark.parametrize("seed", range(24))
def test_structural_fuzz_only_raises_package_errors(seed: int):
    """Mutated packages are rejected by the contract, not by an unhandled crash."""
    rng = random.Random(f"orb-ext-structural-{seed}")
    for name, build in sorted(REFERENCE_PACKAGES.items()):
        try:
            compile_bytes(_repack(build(), rng))
        except PackageError:
            pass
        except Exception as exc:  # pragma: no cover -- the failure this test exists for
            pytest.fail(f"{name} (seed {seed}) raised {type(exc).__name__}: {exc}")


@pytest.mark.parametrize("seed", range(24))
def test_byte_fuzz_only_raises_package_errors(seed: int):
    """The same guarantee below the JSON layer: a corrupted archive is a 400.

    Byte mutation mostly breaks the zip container, which is the point -- the
    archive reader is the first thing an uploaded file touches, and it must
    fail the same way a bad manifest does.
    """
    rng = random.Random(f"orb-ext-bytes-{seed}")
    for name, build in sorted(REFERENCE_PACKAGES.items()):
        data = bytearray(build())
        for _ in range(rng.randrange(1, 8)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            compile_bytes(bytes(data))
        except PackageError:
            pass
        except Exception as exc:  # pragma: no cover -- the failure this test exists for
            pytest.fail(f"{name} (seed {seed}) raised {type(exc).__name__}: {exc}")


def test_truncation_fuzz_only_raises_package_errors():
    """Every prefix of a package is either a package or a PackageError."""
    data = scene_meter_package()
    rng = random.Random("orb-ext-truncate")
    for _ in range(48):
        try:
            compile_bytes(data[: rng.randrange(len(data))])
        except PackageError:
            pass
        except Exception as exc:  # pragma: no cover -- the failure this test exists for
            pytest.fail(f"truncated package raised {type(exc).__name__}: {exc}")


# ── budgets ─────────────────────────────────────────────────────────────────


def limit_sized_package() -> bytes:
    """A legal package sitting at its declaration limits.

    ``MAX_ACTIONS`` flows of ``MAX_FLOW_STEPS_DECLARED`` steps each is the
    largest reference graph the contract admits without any single file being
    unusual, which makes it the shape that exposes an accidentally quadratic
    walk over steps, references, or requirements.
    """
    files: dict[str, Any] = {}
    actions: dict[str, Any] = {}
    for index in range(MAX_ACTIONS):
        path = f"flows/action-{index}.json"
        files[path] = {
            "flow_version": 1,
            "steps": [{"id": f"step_{n}", "op": "math.add", "a": n, "b": 1} for n in range(MAX_FLOW_STEPS_DECLARED - 1)]
            + [{"op": "return", "value": {"$ref": f"steps.step_{MAX_FLOW_STEPS_DECLARED - 2}"}}],
        }
        actions[f"action_{index}"] = {"flow": path, "label": f"Action {index}"}
    files["orb-extension.json"] = {
        "extension_api": 1,
        "id": "limit-sized",
        "name": "Limit Sized",
        "version": "1.0.0",
        "requires": {"operations": ["math.add", "return"]},
        "actions": actions,
    }
    return orbext(files)


def test_limit_sized_package_compiles_within_budget():
    data = limit_sized_package()
    started = time.perf_counter()
    compiled = compile_bytes(data)
    elapsed = time.perf_counter() - started
    assert len(compiled.flows) == MAX_ACTIONS
    assert elapsed < 5.0, f"compiling a limit-sized package took {elapsed:.2f}s"


def test_nesting_bomb_is_rejected_before_it_is_built():
    """A depth bomb costs one linear scan, not an allocation of what it describes."""
    bomb = b"[" * 200_000 + b"]" * 200_000
    started = time.perf_counter()
    with pytest.raises(PackageError):
        load_json(bomb, what="bomb.json", max_bytes=MAX_MANIFEST_BYTES)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"rejecting a depth-{MAX_JSON_DEPTH} violation took {elapsed:.2f}s"


def test_wide_document_is_rejected_within_budget():
    """Breadth is walked with an explicit stack, so it is linear too."""
    wide = json.dumps({"steps": [{"op": "return"} for _ in range(MAX_JSON_MEMBERS + 1)]}).encode()
    started = time.perf_counter()
    with pytest.raises(PackageError):
        load_json(wide, what="wide.json", max_bytes=MAX_MANIFEST_BYTES)
    assert time.perf_counter() - started < 1.0
