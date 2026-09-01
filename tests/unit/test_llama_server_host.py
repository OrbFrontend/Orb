"""The host's lifecycle: what a swap waits for, and what a release lets go of.

Every assertion here is about the *generic* host. What may be in a profile —
lane counts, model paths — is the owning feature's business and is tested in
``test_prose_rewriter_config.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.inference.local_models.llama_server import manager
from backend.inference.local_models.llama_server.client import LaunchProfile
from backend.inference.local_models.llama_server.host import ManagedLlamaServerHost

pytestmark = pytest.mark.asyncio


def _profile(**overrides) -> LaunchProfile:
    fields = {
        "model_id": "test",
        "model_path": "/models/test.gguf",
        "alias": "test-feature",
        "gpu_layers": 999,
        "ctx_size": 5120,
        "parallel": 4,
        "http_threads": 12,
    }
    return LaunchProfile(**{**fields, **overrides})


@pytest.fixture
def host():
    """A throwaway host, kept out of the shared registry.

    Registration is default-on because a forgotten one orphans a child on exit;
    a test host has no child and must not turn up in ``shutdown_all``.
    """
    made = ManagedLlamaServerHost(name="test", idle_timeout=300.0, register=False)
    yield made
    manager.unregister(made)  # belt and braces if a test flips register on


class _StoppableServer:
    """Stands in for a loaded client; records that it was stopped."""

    def __init__(self, profile: LaunchProfile | None = None) -> None:
        self.profile = profile or _profile()
        self.alive = True
        self.ready = True
        self.stopped = False

    async def stop(self) -> None:
        self.alive = False
        self.ready = False
        self.stopped = True


async def test_a_changed_profile_marks_the_loaded_host_stale(host):
    """The settings write that has to reach a running child — eventually.

    ``mark_stale`` records and returns; the restart happens on the next
    ``ensure``, because a turn may be mid-generation and a settings write has
    no business blocking on it or killing it.
    """
    host.server = _StoppableServer()
    host.profile = _profile(parallel=4, ctx_size=5120, http_threads=12)
    host._stale = False
    assert host.healthy is True

    host.mark_stale(_profile(parallel=2, ctx_size=2560, http_threads=8))

    assert host.profile.parallel == 2
    assert host.healthy is False


async def test_an_identical_profile_does_not_mark_the_host_stale(host):
    """The branch that stops a settings write that changed nothing from
    restarting a healthy child."""
    host.server = _StoppableServer()
    host.profile = _profile()
    host._stale = False

    host.mark_stale(_profile())

    assert host.healthy is True


async def test_release_waits_for_an_in_flight_request_before_stopping_the_child(host):
    """Deleting a GGUF or replacing the binary has to let go of the files first
    — Windows will not unlink a mapped weight or a running executable — but it
    must not cut off work already decoding, the way a bare stop would."""
    profile = _profile()
    host.server = _StoppableServer(profile)
    host.state = "ready"
    host.profile = profile
    order: list[str] = []

    async def working() -> None:
        async with host.use(profile):
            await asyncio.sleep(0.05)
            order.append("request finished")

    async def releasing() -> None:
        await asyncio.sleep(0)  # let the request take its slot first
        assert host._inflight == 1
        await host.release()
        order.append("released")

    server = host.server
    await asyncio.gather(working(), releasing())

    assert order == ["request finished", "released"]
    assert server.stopped is True
    assert host.server is None
    assert host.state == "idle"


async def test_release_with_no_child_still_forces_the_next_load(host):
    """The file may have been deleted while nothing was loaded; the next
    ``ensure`` must not trust a 'healthy' it inherited from before."""
    await host.release()
    assert host.healthy is False


async def test_the_gpu_setting_selects_which_build_is_launched(host, monkeypatch):
    """The switch, at the seam that performs it.

    ``--n-gpu-layers`` alone was never the switch: every build parses it and a
    CPU-only one then offloads nothing, silently and with a zero exit status.
    The host asks for the build that can honour the number it is about to send.
    """
    from backend.inference.local_models.llama_server import host as H

    asked: list[bool] = []
    monkeypatch.setattr(H.binary, "find_binary", lambda gpu=True: (asked.append(gpu), "/bin/llama-server")[1])
    monkeypatch.setattr(H.binary, "gpu_capable", lambda _binary: True)
    monkeypatch.setattr(H, "LlamaServerClient", lambda profile, executable: _StoppableServer(profile))

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("stop here — resolution is what this test is about")

    monkeypatch.setattr(_StoppableServer, "start", _boom, raising=False)

    for gpu_layers in (999, 0):
        with pytest.raises(RuntimeError):
            await host.ensure(_profile(gpu_layers=gpu_layers))

    assert asked == [True, False]


async def test_a_gpu_request_a_build_cannot_honour_is_logged(host, monkeypatch, caplog):
    """The silence this whole change exists to break. Nothing raises — a
    self-supplied binary is allowed to be CPU-only — but it stops being
    invisible."""
    from backend.inference.local_models.llama_server import host as H

    monkeypatch.setattr(H.binary, "find_binary", lambda gpu=True: "/bin/llama-server")
    monkeypatch.setattr(H.binary, "gpu_capable", lambda _binary: False)
    monkeypatch.setattr(H, "LlamaServerClient", lambda profile, executable: _StoppableServer(profile))

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("not the point of this test")

    monkeypatch.setattr(_StoppableServer, "start", _boom, raising=False)

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        await host.ensure(_profile(gpu_layers=999))

    assert "reports no GPU device" in caplog.text
