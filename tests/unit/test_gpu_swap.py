"""GPU swap: server probes and control against mock servers, the arbiter's ordering, and the LLM client's lease."""

import asyncio
import json

import httpx
import pytest

from backend.inference import gpu_swap
from backend.inference.client import LLMClient

ROOT = "http://llm:5000"
COMFY = "http://comfy:8188"
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(gpu_swap, "_arbiter", gpu_swap._Arbiter())
    monkeypatch.setattr(gpu_swap, "_kinds", {})
    monkeypatch.setattr(gpu_swap, "_provider", None)
    monkeypatch.setattr(gpu_swap, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(gpu_swap, "SETTLE_CAP", 0.05)


def _serve(monkeypatch, handle):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handle(request)

    transport = httpx.MockTransport(record)
    monkeypatch.setattr(
        gpu_swap,
        "_client",
        lambda headers, proxy, timeout=gpu_swap.PROBE_TIMEOUT: httpx.AsyncClient(
            transport=transport, headers=dict(headers or {})
        ),
    )
    return seen


def _router(models: dict[str, str], *, lag: int = 2):
    """A router whose unload is acknowledged at once and lands *lag* listings later."""
    pending: dict[str, int] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"role": "router"}, headers={"Server": "llama.cpp"})
        if request.url.path == "/models/unload":
            pending[json.loads(request.content)["model"]] = lag
            return httpx.Response(200, json={"success": True})
        for model in list(pending):
            pending[model] -= 1
            if pending[model] < 0:
                models[model] = "unloaded"
                del pending[model]
        data = [{"id": mid, "aliases": [f"{mid}-alias"], "status": {"value": status}} for mid, status in models.items()]
        return httpx.Response(200, json={"data": [{"id": "hf/cache-entry", "source": "cache"}, *data]})

    return handle


def _target(kind: gpu_swap.ServerKind = "llama_router", model: str = "g") -> gpu_swap.LlmTarget:
    return gpu_swap.LlmTarget(root=ROOT, model=model, kind=kind)


# -- Probing and server control ------------------------------------------------


async def test_probe_kinds_and_cache(monkeypatch):
    responses = {
        "llama_router": lambda r: httpx.Response(200, json={"role": "router"}),
        "llama_single": lambda r: httpx.Response(200, json={"chat_template": "x"}, headers={"Server": "llama.cpp"}),
        "ollama": lambda r: httpx.Response(404) if r.url.path == "/props" else httpx.Response(200, json={"version": "0.9"}),
        "other": lambda r: httpx.Response(404),
    }
    for kind, handle in responses.items():
        _serve(monkeypatch, handle)
        assert await gpu_swap.probe(ROOT, fresh=True) == kind
    seen = _serve(monkeypatch, responses["llama_router"])
    assert await gpu_swap.probe(ROOT) == "other"  # cached from the last fresh probe
    assert not seen
    assert await gpu_swap.probe(ROOT, fresh=True) == "llama_router"


async def test_probe_does_not_cache_an_unreachable_server(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    _serve(monkeypatch, refuse)
    assert await gpu_swap.probe(ROOT) == "other"
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"role": "router"}))
    assert await gpu_swap.probe(ROOT) == "llama_router"


async def test_router_unload_waits_until_the_listing_drops_it(monkeypatch):
    models = {"g": "loaded", "q": "sleeping"}
    seen = _serve(monkeypatch, _router(models))
    target = _target()
    assert await gpu_swap.loaded_models(target) == {"g"}  # sleeping holds no memory; cache entries have no status
    assert await gpu_swap.model_loaded(target, "g-alias")
    await gpu_swap.unload(target, "g")
    assert models["g"] == "unloaded"
    assert sum(r.url.path == "/models" for r in seen) >= 3
    assert not await gpu_swap.model_loaded(target, "g")
    assert all("model" not in r.url.params for r in seen)  # no probe names a model, so none can autoload


async def test_unload_cap_raises(monkeypatch):
    monkeypatch.setattr(gpu_swap, "UNLOAD_CAP", 0.01)
    _serve(monkeypatch, _router({"g": "loaded"}, lag=10_000))
    with pytest.raises(gpu_swap.GpuSwapError):
        await gpu_swap.unload(_target(), "g")


async def test_ollama_names_and_unload(monkeypatch):
    resident = {"llama3.2:latest"}

    def handle(request):
        if request.url.path == "/api/generate":
            body = json.loads(request.content)
            assert body["keep_alive"] == 0
            resident.discard(body["model"])
            return httpx.Response(200, json={"done_reason": "unload"})
        return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in resident]})

    _serve(monkeypatch, handle)
    target = _target("ollama", "llama3.2")
    assert await gpu_swap.model_loaded(target, "llama3.2")
    assert await gpu_swap.loaded_models(target) == {"llama3.2:latest"}
    await gpu_swap.unload(target, "llama3.2:latest")
    assert not await gpu_swap.model_loaded(target, "llama3.2")


def _comfy(vram: list[int], running: list[bool] | None = None):
    reads = iter(vram)
    queue = iter(running or [])
    last = [vram[0]]

    def handle(request):
        if request.url.path == "/system_stats":
            last[0] = next(reads, last[0])
            return httpx.Response(200, json={"devices": [{"vram_free": last[0]}]})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [["job"]] if next(queue, False) else []})
        return httpx.Response(200)

    return handle


async def test_comfy_free_settles_on_a_rise_then_two_agreeing_reads(monkeypatch):
    base = 1_000 * MIB
    seen = _serve(monkeypatch, _comfy([base, base, base + 300 * MIB, base + 4_000 * MIB, base + 4_010 * MIB], [True, False]))
    await gpu_swap.comfy_free(COMFY, "k")
    stats = [r for r in seen if r.url.path == "/system_stats"]
    assert len(stats) == 5
    free = next(r for r in seen if r.url.path == "/free")
    assert json.loads(free.content) == {"unload_models": True, "free_memory": True}
    assert free.headers["authorization"] == "Bearer k"
    assert [r.url.path for r in seen].index("/queue") < [r.url.path for r in seen].index("/system_stats", 1)


async def test_comfy_free_never_settles_on_stable_reads_without_a_rise(monkeypatch):
    seen = _serve(monkeypatch, _comfy([1_000 * MIB] * 1_000))
    await gpu_swap.comfy_free(COMFY)
    assert sum(r.url.path == "/system_stats" for r in seen) > 3  # ran to the cap instead of passing on the first pair


# -- The arbiter ---------------------------------------------------------------


class _Servers:
    """Stands in for both servers at the arbiter's seams."""

    def __init__(self, monkeypatch, loaded: set[str]):
        self.loaded = loaded
        self.log: list[str] = []
        monkeypatch.setattr(gpu_swap, "loaded_models", self._loaded_models)
        monkeypatch.setattr(gpu_swap, "model_loaded", self._model_loaded)
        monkeypatch.setattr(gpu_swap, "unload", self._unload)
        monkeypatch.setattr(gpu_swap, "comfy_free", self._free)

    async def _loaded_models(self, target):
        return set(self.loaded)

    async def _model_loaded(self, target, model):
        return model in self.loaded

    async def _unload(self, target, model):
        self.log.append(f"unload {model}")
        self.loaded.discard(model)

    async def _free(self, url, api_key="", *, queue_cap=0.0):
        self.log.append("free")


def _plan(monkeypatch, *, enabled=True, cap=5.0, targets=None) -> gpu_swap.SwapPlan:
    plan = gpu_swap.SwapPlan(enabled=enabled, llm_targets=targets or (_target(),), comfy_url=COMFY, comfy_key="", wait_cap=cap)

    async def provider():
        return plan

    monkeypatch.setattr(gpu_swap, "_provider", provider)
    return plan


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _render():
    async with gpu_swap.comfy_lease():
        pass


async def test_live_llm_lease_and_turn_hold_block_a_render_until_both_end(monkeypatch):
    servers = _Servers(monkeypatch, {"g"})
    _plan(monkeypatch)
    async with gpu_swap.turn_hold():
        lease = gpu_swap.llm_lease(ROOT, "g")
        await lease.__aenter__()
        render = asyncio.create_task(_render())
        await _settle()
        await lease.__aexit__(None, None, None)
        await _settle()
        assert not render.done()  # between two passes of the turn
        async with gpu_swap.llm_lease(ROOT, "g"):
            pass
        assert not render.done()
    await render
    assert servers.log == ["unload g"]


async def test_render_unloads_only_what_is_loaded(monkeypatch):
    servers = _Servers(monkeypatch, set())
    _plan(monkeypatch)
    await _render()
    assert servers.log == []
    servers.loaded.add("g")
    await _render()
    assert servers.log == ["unload g"]


async def test_llm_frees_comfy_only_after_a_render_and_only_when_not_loaded(monkeypatch):
    servers = _Servers(monkeypatch, {"g"})
    _plan(monkeypatch)
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass
    assert servers.log == []  # loaded already: ComfyUI must be empty
    await _render()
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass
    async with gpu_swap.llm_lease(ROOT, "other"):
        pass  # the router swapping its own models: ComfyUI is already empty
    assert servers.log == ["unload g", "free"]


async def test_concurrent_llm_leases_share_without_swapping(monkeypatch):
    servers = _Servers(monkeypatch, set())
    _plan(monkeypatch)
    entered = 0

    async def call():
        nonlocal entered
        async with gpu_swap.llm_lease(ROOT, "g"):
            entered += 1
            await asyncio.sleep(0.01)

    await asyncio.gather(call(), call(), call())
    assert entered == 3
    assert servers.log == ["free"]


async def test_nested_llm_lease_does_not_deadlock_against_a_waiting_render(monkeypatch):
    servers = _Servers(monkeypatch, {"g"})
    _plan(monkeypatch)
    async with gpu_swap.llm_lease(ROOT, "g"):
        render = asyncio.create_task(_render())
        await _settle()
        async with asyncio.timeout(1):
            async with gpu_swap.llm_lease(ROOT, "g"):
                pass
    await render
    assert servers.log == ["unload g"]


async def test_disabled_plan_and_unplanned_root_are_no_ops(monkeypatch):
    servers = _Servers(monkeypatch, {"g"})
    _plan(monkeypatch, enabled=False)
    async with gpu_swap.turn_hold(), gpu_swap.llm_lease(ROOT, "g"):
        await _render()
    _plan(monkeypatch)
    async with gpu_swap.llm_lease("https://openrouter.ai/api", "g") as admitted:
        assert admitted
        await _render()
    assert servers.log == ["unload g"]
    assert gpu_swap._arbiter.llm_inflight == 0


async def test_drain_cap_raises_and_touches_neither_server(monkeypatch):
    servers = _Servers(monkeypatch, {"g"})
    _plan(monkeypatch, cap=0.02)
    async with gpu_swap.llm_lease(ROOT, "g"):
        with pytest.raises(gpu_swap.GpuSwapError, match="LLM"):
            await _render()
    async with gpu_swap.comfy_lease():
        with pytest.raises(gpu_swap.GpuSwapError, match="ComfyUI"):
            async with gpu_swap.llm_lease(ROOT, "g"):
                pass
    assert servers.log == ["unload g"]


async def test_stop_while_waiting_is_not_admitted(monkeypatch):
    _Servers(monkeypatch, {"g"})
    _plan(monkeypatch)
    stop = asyncio.Event()
    async with gpu_swap.comfy_lease():
        waiter = asyncio.create_task(_admitted(gpu_swap.llm_lease(ROOT, "g", stop=stop.wait)))
        await _settle()
        stop.set()
        assert await waiter is False
    assert gpu_swap._arbiter.llm_inflight == 0


async def _admitted(lease) -> bool:
    async with lease as admitted:
        return admitted


async def test_turn_handler_holds_the_turn(monkeypatch):
    from backend.pipeline import entrypoints

    _Servers(monkeypatch, set())
    _plan(monkeypatch)

    async def context(conversation_id, abort_token=None):
        return object()

    async def body(ctx):
        yield {"event": "token", "data": gpu_swap._arbiter.holds}

    monkeypatch.setattr(entrypoints, "load_pipeline_context", context)
    events = [e async for e in entrypoints._run_turn_handler("c", None, body, log_label="t")]
    assert events[0]["data"] == 1
    assert gpu_swap._arbiter.holds == 0


# -- The LLM client's lease ----------------------------------------------------


def _streaming_client(gate: asyncio.Event | None = None) -> LLMClient:
    client = LLMClient(f"{ROOT}/v1", completion_mode="text")

    async def stream(url, body):
        yield {"content": "a", "stop": False}
        if gate is not None:
            await gate.wait()
        yield {"content": "b", "stop": True}

    client._stream_completion = stream  # type: ignore[method-assign]
    return client


async def test_client_calls_take_the_lease(monkeypatch):
    servers = _Servers(monkeypatch, set())
    _plan(monkeypatch)
    client = _streaming_client()

    async def render(messages, *, model, **kwargs):
        assert gpu_swap._arbiter.llm_inflight == 1
        return "P"

    client._render_prompt = render  # type: ignore[method-assign]
    assert await client.render_prompt([{"role": "user", "content": "Hi"}], model="g") == "P"
    events = [e async for e in client.complete_raw("p", "g")]
    assert events[-1]["message"]["content"] == "ab"
    assert servers.log == ["free"]
    assert gpu_swap._arbiter.llm_inflight == 0


async def test_aborted_stream_releases_its_lease(monkeypatch):
    _Servers(monkeypatch, {"g"})
    _plan(monkeypatch)
    gate = asyncio.Event()
    client = _streaming_client(gate)
    stream = client.complete_raw("p", "g")
    assert (await anext(stream))["delta"] == "a"
    assert gpu_swap._arbiter.llm_inflight == 1
    client.abort()
    assert [e async for e in stream] == []
    assert gpu_swap._arbiter.llm_inflight == 0
    await _render()


async def test_stopped_while_waiting_streams_nothing(monkeypatch):
    _Servers(monkeypatch, {"g"})
    _plan(monkeypatch)
    client = _streaming_client()
    async with gpu_swap.comfy_lease():
        task = asyncio.create_task(_drain(client.complete_raw("p", "g")))
        await _settle()
        client.abort()
        assert await task == []


async def _drain(stream):
    return [e async for e in stream]


# -- The host's plan -----------------------------------------------------------


@pytest.mark.parametrize(
    "option,kind,enabled",
    [(True, "llama_router", True), (True, "ollama", True), (True, "llama_single", False), (False, "llama_router", False)],
)
async def test_host_plan_needs_the_option_and_an_unloadable_lane(monkeypatch, option, kind, enabled):
    from backend.workflows import gpu_swap_host

    settings = {"endpoint_url": f"{ROOT}/v1", "model_name": "g", "agent_same_as_writer": True}
    config = {"timeout_seconds": 60, "external_comfy": {"api_url": COMFY + "/", "gpu_swap": option}}
    probed: list[str] = []

    async def probe(root, headers=None, proxy=None, *, fresh=False):
        probed.append(root)
        return kind

    async def get_settings():
        return settings

    async def get_config(workflow_id):
        return config

    monkeypatch.setattr(gpu_swap_host, "get_settings", get_settings)
    monkeypatch.setattr(gpu_swap_host, "get_workflow_config", get_config)
    monkeypatch.setattr(gpu_swap, "probe", probe)
    gpu_swap_host.invalidate()
    plan = await gpu_swap_host.plan()
    gpu_swap_host.invalidate()
    assert plan.enabled is enabled
    assert probed == ([ROOT] if option else [])  # off never touches the network
    if enabled:
        assert (plan.target_for(ROOT + "/"), plan.comfy_url, plan.wait_cap) == (plan.llm_targets[0], COMFY, 60.0)


async def test_render_reports_unloading_only_when_something_is_loaded(monkeypatch):
    servers = _Servers(monkeypatch, set())
    _plan(monkeypatch)
    heard: list[str] = []
    async with gpu_swap.comfy_lease(heard.append):
        pass
    servers.loaded.add("g")
    async with gpu_swap.comfy_lease(heard.append):
        pass
    assert heard == ["unloading"]


async def test_unreachable_llm_server_still_frees_comfy_and_leaves_the_error_to_the_call(monkeypatch):
    servers = _Servers(monkeypatch, set())
    _plan(monkeypatch)

    async def down(target, model):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(gpu_swap, "model_loaded", down)
    async with gpu_swap.llm_lease(ROOT, "g") as admitted:
        assert admitted
    assert servers.log == ["free"]


def test_a_swap_failure_in_a_turn_names_the_swap():
    from backend.pipeline.failures import GPU_SWAP_HEADLINE, describe_failure

    failure = describe_failure(gpu_swap.GpuSwapError("Timed out waiting for ComfyUI to finish with the GPU (180s)"))
    assert (failure["headline"], failure["kind"]) == (GPU_SWAP_HEADLINE, "workflow")
    assert failure["sentence"].startswith("Timed out")
