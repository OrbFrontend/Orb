"""GPU swap: server probes and control against mock servers, the arbiter's ordering, and the LLM client's lease."""

import asyncio
import json
from urllib.parse import unquote

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
            transport=transport, headers=dict(headers or {}), timeout=timeout
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


def _swap(models: dict[str, str], *, aliases: dict[str, list[str]] | None = None, group: bool = False, old: bool = False):
    """llama-swap: unload answers once the process stopped, and any request to a stopped model starts it.

    With *group*, starting one model stops the rest. *old* lacks the per-model unload route.
    """
    aliases = aliases or {}

    def start(model: str) -> None:
        for other in models if group else ():
            models[other] = "stopped"
        models[model] = "ready"

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/props":
            return httpx.Response(404, json={"src": "llama-swap", "error": {"message": "no model id could be identified"}})
        if path == "/running":
            running = [{"model": m, "state": state, "cmd": "x"} for m, state in models.items() if state != "stopped"]
            return httpx.Response(200, json={"running": running})
        if path == "/v1/models":
            data = [
                {
                    "id": m,
                    "status": {"value": "loaded" if state == "ready" else "unloaded"},
                    "meta": {"llamaswap": {"aliases": aliases.get(m, [])}},
                }
                for m, state in models.items()
            ]
            return httpx.Response(200, json={"object": "list", "data": data})
        if path.startswith("/api/models/unload/") and not old:
            models[unquote(path.removeprefix("/api/models/unload/"))] = "stopped"
            return httpx.Response(200, text="OK")
        if path.startswith("/upstream/"):
            model, rest = path.removeprefix("/upstream/").split("/", 1)
            start(model)
            return httpx.Response(200, json=[{"id": 0}, {"id": 1}] if rest == "slots" else {"n_saved": 1})
        if path.startswith("/comfyui/"):
            start("comfyui_auto")
            return httpx.Response(200, json={"devices": [{"vram_free": 0}]})
        if path == "/api/version":
            return httpx.Response(200, json={"build_date": "unknown", "commit": "abcd1234", "version": "0"})
        return httpx.Response(404, text="404 page not found")

    return handle


def _koboldcpp(request: httpx.Request) -> httpx.Response:
    """KoboldCpp's Ollama emulation: the same version body, a model that never leaves ``/api/ps``, and a real generate."""
    path = request.url.path
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.7.0"})
    if path == "/":
        return httpx.Response(200, text="<!doctype html><title>KoboldAI Lite</title>")
    if path == "/api/extra/version":
        return httpx.Response(200, json={"result": "KoboldCpp", "version": "1.80"})
    if path in ("/api/ps", "/api/tags"):
        return httpx.Response(200, json={"models": [{"name": "koboldcpp/m", "expires_at": "2055-01-01T00:00:00Z"}]})
    if path == "/api/generate":
        return httpx.Response(200, json={"response": "Once upon a time"})
    return httpx.Response(404)


def _target(kind: gpu_swap.ServerKind = "llama_router", model: str = "g") -> gpu_swap.LlmTarget:
    return gpu_swap.LlmTarget(root=ROOT, model=model, kind=kind)


# -- Probing and server control ------------------------------------------------


async def test_probe_kinds_and_cache(monkeypatch):
    responses = {
        "llama_router": lambda r: httpx.Response(200, json={"role": "router"}),
        "llama_single": lambda r: httpx.Response(200, json={"chat_template": "x"}, headers={"Server": "llama.cpp"}),
        "ollama": lambda r: (
            httpx.Response(404)
            if r.url.path == "/props"
            else httpx.Response(200, text="Ollama is running")
            if r.url.path == "/"
            else httpx.Response(200, json={"version": "0.9"})
        ),
        "llama_swap": _swap({}),
        "koboldcpp": _koboldcpp,
        "needs_admin": lambda r: httpx.Response(  # llama-swap with apiKeys, called without the key
            401, json={"src": "llama-swap", "error": {"message": "unauthorized: invalid or missing API key"}}
        ),
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


SETTINGS = {"endpoint_url": f"{ROOT}/v1", "model_name": "g", "agent_same_as_writer": True}


async def _host_plan(monkeypatch, kind, *, option=True, comfy_url=COMFY + "/", probed: list[str] | None = None):
    from backend.workflows import gpu_swap_host

    config = {"timeout_seconds": 60, "external_comfy": {"api_url": comfy_url, "gpu_swap": option}}
    probed = [] if probed is None else probed

    async def probe(root, headers=None, proxy=None, *, fresh=False):
        probed.append(root)
        return kind

    async def get_settings():
        return SETTINGS

    async def get_config(workflow_id):
        return config

    monkeypatch.setattr(gpu_swap_host, "get_settings", get_settings)
    monkeypatch.setattr(gpu_swap_host, "get_workflow_config", get_config)
    monkeypatch.setattr(gpu_swap, "probe", probe)
    gpu_swap_host.invalidate()
    plan = await gpu_swap_host.plan()
    gpu_swap_host.invalidate()
    return plan


@pytest.mark.parametrize(
    "option,kind,enabled",
    [
        (True, "llama_router", True),
        (True, "llama_swap", True),
        (True, "ollama", True),
        (True, "llama_single", False),
        (True, "koboldcpp", False),
        (False, "llama_router", False),
    ],
)
async def test_host_plan_needs_the_option_and_an_unloadable_lane(monkeypatch, option, kind, enabled):
    probed: list[str] = []
    plan = await _host_plan(monkeypatch, kind, option=option, probed=probed)
    assert plan.enabled is enabled
    assert probed == ([ROOT] if option else [])  # off never touches the network
    if enabled:
        assert (plan.target_for(ROOT + "/"), plan.comfy_url, plan.wait_cap) == (plan.llm_targets[0], COMFY, 60.0)
        assert (plan.comfy_host, plan.comfy_model) == ("", "")


@pytest.mark.parametrize(
    "comfy_url,hosted",
    [
        (f"{ROOT}/comfyui/", (ROOT, "comfyui_auto")),
        (f"{ROOT}/upstream/comfy%2Fsdxl", (ROOT, "comfy/sdxl")),
        ("http://llm:5001/comfyui", ("", "")),
        (f"{ROOT}/other", ("", "")),
        ("http://llm:port/comfyui", ("", "")),  # malformed: not hosted, and swapping stays on
    ],
)
async def test_host_plan_finds_comfy_behind_the_lanes_llama_swap(monkeypatch, comfy_url, hosted):
    plan = await _host_plan(monkeypatch, "llama_swap", comfy_url=comfy_url)
    assert plan.enabled
    assert (plan.comfy_host, plan.comfy_model) == hosted
    assert plan.identity()[-2:] == hosted  # moving ComfyUI behind llama-swap resets the arbiter


async def test_koboldcpp_reads_as_unable_to_unload_and_is_never_sent_a_generate(monkeypatch):
    from backend.workflows import gpu_swap_host

    seen = _serve(monkeypatch, _koboldcpp)
    support = await gpu_swap_host.support(SETTINGS)
    assert support["available"] is False
    assert support["reason"].startswith("KoboldCpp can't unload")
    assert support["lanes"] == [{"lane": "writer", "kind": "koboldcpp", "model": "g"}]
    assert seen and all(r.method == "GET" for r in seen)


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


# -- Slot save and restore (phase 3) -------------------------------------------


async def test_slot_calls_name_the_model_and_a_fixed_file(monkeypatch):
    def handle(request):
        if request.method == "GET":
            assert (request.url.params["model"], request.url.params["autoload"]) == ("org/m:Q4", "false")
            return httpx.Response(200, json=[{"id": 0}, {"id": 1}])
        if request.url.path == "/slots/1" and request.url.params["action"] == "save":
            return httpx.Response(501, json={"error": "slot save is not enabled"})
        return httpx.Response(200, json={"n_saved": 10})

    seen = _serve(monkeypatch, handle)
    target = _target(model="org/m:Q4")
    assert await gpu_swap.save_slots(target, "org/m:Q4") == (0,)
    await gpu_swap.restore_slots(target, "org/m:Q4", (0,))
    posts = [(r.url.path, r.url.params["action"], json.loads(r.content)) for r in seen if r.method == "POST"]
    name = gpu_swap.slot_filename("org/m:Q4", 0)
    assert posts[0] == ("/slots/0", "save", {"model": "org/m:Q4", "filename": name})
    assert posts[-1] == ("/slots/0", "restore", {"model": "org/m:Q4", "filename": name})
    assert "/" not in name and ":" not in name
    assert name == gpu_swap.slot_filename("org/m:Q4", 0) != gpu_swap.slot_filename("org/m:Q5", 0)


class _SlotServers(_Servers):
    def __init__(self, monkeypatch, loaded, *, saves=(0, 1)):
        super().__init__(monkeypatch, loaded)
        self.saves = saves
        monkeypatch.setattr(gpu_swap, "save_slots", self._save)
        monkeypatch.setattr(gpu_swap, "restore_slots", self._restore)

    async def _save(self, target, model):
        self.log.append(f"save {model}")
        return self.saves

    async def _restore(self, target, model, slots):
        self.log.append(f"restore {model} {list(slots)}")
        self.loaded.add(model)


def _restoring(monkeypatch, *targets):
    plan = gpu_swap.SwapPlan(enabled=True, llm_targets=targets or (_target(),), comfy_url=COMFY, slot_restore=True)

    async def provider():
        return plan

    monkeypatch.setattr(gpu_swap, "_provider", provider)


async def test_render_saves_before_unloading_and_the_next_call_restores_after_free(monkeypatch):
    servers = _SlotServers(monkeypatch, {"g"})
    _restoring(monkeypatch)
    heard: list[str] = []
    async with gpu_swap.comfy_lease(heard.append):
        pass
    for _ in range(2):
        async with gpu_swap.llm_lease(ROOT, "g"):
            pass
    assert servers.log == ["save g", "unload g", "free", "restore g [0, 1]"]
    assert heard == ["saving", "unloading"]


async def test_the_lane_the_router_swapped_out_restores_on_its_own_next_call(monkeypatch):
    servers = _SlotServers(monkeypatch, {"writer"})
    _restoring(monkeypatch, _target(model="writer"), _target(model="agent"))
    await _render()
    async with gpu_swap.llm_lease(ROOT, "agent"):
        servers.loaded.add("agent")
    servers.loaded.discard("agent")  # --models-max 1: the router swaps the agent out for the writer
    async with gpu_swap.llm_lease(ROOT, "writer"):
        pass
    assert servers.log == ["save writer", "unload writer", "free", "restore writer [0, 1]"]


async def test_a_failed_save_still_unloads_and_restores_nothing(monkeypatch):
    servers = _SlotServers(monkeypatch, {"g"}, saves=())
    _restoring(monkeypatch)
    await _render()
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass
    assert servers.log == ["save g", "unload g", "free"]


async def test_turning_restore_off_drops_saved_slots(monkeypatch):
    servers = _SlotServers(monkeypatch, {"g"})
    _restoring(monkeypatch)
    await _render()
    _plan(monkeypatch)
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass
    assert servers.log == ["save g", "unload g", "free"]


async def test_a_failed_listing_forgets_the_server_kind(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"role": "router"}))
    assert await gpu_swap.probe(ROOT) == "llama_router"
    _serve(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await gpu_swap.loaded_models(_target())
    _serve(monkeypatch, lambda r: httpx.Response(200, json={}, headers={"Server": "llama.cpp"}))
    assert await gpu_swap.probe(ROOT) == "llama_single"


async def test_a_cached_kind_expires(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"role": "router"}))
    assert await gpu_swap.probe(ROOT) == "llama_router"
    _serve(monkeypatch, lambda r: httpx.Response(200, json={}, headers={"Server": "llama.cpp"}))
    assert await gpu_swap.probe(ROOT) == "llama_router"
    monkeypatch.setattr(gpu_swap, "KIND_TTL", 0.0)
    assert await gpu_swap.probe(ROOT) == "llama_single"


# -- One-model servers that Orb reloads ----------------------------------------

TABBY_CARD = {
    "id": "q27",
    "parameters": {"max_seq_len": 32768, "cache_size": 32768, "cache_mode": "6,6", "max_batch_size": 4, "use_vision": False},
}


def _tabby(state: dict, log: list[str], *, admin: bool = True, load_error: str = ""):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/serviceinfo":
            return httpx.Response(200, json={"software": {"name": "TabbyAPI"}})
        if path == "/v1/auth/permission":
            return httpx.Response(200, json={"permission": "admin" if admin else "api"})
        if path == "/v1/model":
            return httpx.Response(200, json=state["card"]) if state.get("card") else httpx.Response(503)
        if path == "/v1/model/unload":
            log.append("unload")
            state["card"] = None
            return httpx.Response(200)
        if path == "/v1/model/load":
            body = json.loads(request.content)
            log.append(f"load {sorted(body.items())}")
            if load_error:
                return httpx.Response(200, text=f'data: {{"error": {{"message": "{load_error}"}}}}\n\n')
            state["card"] = TABBY_CARD
            return httpx.Response(200, text='data: {"module": 1, "modules": 1, "status": "finished"}\n\n')
        return httpx.Response(404)

    return handle


def _ooba(state: dict, log: list[str], *, admin: bool = True):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/internal/model/info":
            return httpx.Response(200, json={"model_name": state["name"], "lora_names": state["loras"], "loader": "llama.cpp"})
        if path == "/v1/internal/model/list":
            return httpx.Response(200, json={"model_names": []}) if admin else httpx.Response(401)
        if path == "/v1/internal/model/unload":
            log.append("unload")
            state.update(name="None", loras=[])
            return httpx.Response(200, json="OK")
        if path == "/v1/internal/model/load":
            body = json.loads(request.content)
            log.append(f"load {body}")
            state["name"] = body["model_name"]
            return httpx.Response(200, json="OK")
        if path == "/v1/internal/lora/load":
            state["loras"] = json.loads(request.content)["lora_names"]
            log.append(f"loras {state['loras']}")
            return httpx.Response(200, json="OK")
        return httpx.Response(404)

    return handle


@pytest.mark.parametrize("server", [_tabby, _ooba])
async def test_one_model_servers_need_the_admin_key(monkeypatch, server):
    kind = "tabby" if server is _tabby else "ooba"
    state = {"card": TABBY_CARD, "name": "m.gguf", "loras": []}
    _serve(monkeypatch, server(state, [], admin=True))
    assert await gpu_swap.probe(ROOT, fresh=True) == kind
    _serve(monkeypatch, server(state, [], admin=False))
    assert await gpu_swap.probe(ROOT, fresh=True) == "needs_admin"


def _reloading(monkeypatch, kind: gpu_swap.ServerKind, log: list[str]):
    async def free(url, api_key="", *, queue_cap=0.0):
        log.append("free")

    monkeypatch.setattr(gpu_swap, "comfy_free", free)
    _plan(monkeypatch, targets=(_target(kind, model="configured-name"),))


async def test_tabby_reloads_with_the_settings_it_held_after_comfy_frees(monkeypatch):
    log: list[str] = []
    state = {"card": TABBY_CARD}
    _serve(monkeypatch, _tabby(state, log))
    _reloading(monkeypatch, "tabby", log)
    await _render()
    for _ in range(2):
        async with gpu_swap.llm_lease(ROOT, "configured-name"):
            pass
    expected = [("cache_mode", "6,6"), ("cache_size", 32768), ("max_seq_len", 32768), ("model_name", "q27"), ("vision", False)]
    assert log == ["unload", "free", f"load {expected}"]


async def test_ooba_reloads_its_model_loader_and_loras(monkeypatch):
    log: list[str] = []
    state = {"name": "m.gguf", "loras": ["style"]}
    _serve(monkeypatch, _ooba(state, log))
    _reloading(monkeypatch, "ooba", log)
    await _render()
    async with gpu_swap.llm_lease(ROOT, "configured-name"):
        pass
    assert log == ["unload", "free", "load {'model_name': 'm.gguf', 'args': {'loader': 'llama.cpp'}}", "loras ['style']"]


async def test_a_failed_reload_names_the_swap_and_stays_pending(monkeypatch):
    log: list[str] = []
    state = {"card": TABBY_CARD}
    _serve(monkeypatch, _tabby(state, log, load_error="out of memory"))
    _reloading(monkeypatch, "tabby", log)
    await _render()
    with pytest.raises(gpu_swap.GpuSwapError, match="Could not reload q27.*out of memory"):
        async with gpu_swap.llm_lease(ROOT, "configured-name"):
            pass
    _serve(monkeypatch, _tabby(state, log))
    async with gpu_swap.llm_lease(ROOT, "configured-name"):
        pass
    assert state["card"] == TABBY_CARD


# -- llama-swap ----------------------------------------------------------------


def _hosting(monkeypatch, *, slot_restore=True) -> None:
    plan = gpu_swap.SwapPlan(
        enabled=True,
        llm_targets=(_target("llama_swap"),),
        comfy_url=f"{ROOT}/comfyui",
        slot_restore=slot_restore,
        comfy_host=ROOT,
        comfy_model="comfyui_auto",
    )

    async def provider():
        return plan

    monkeypatch.setattr(gpu_swap, "_provider", provider)


def _upstream_posts(seen: list[httpx.Request]) -> list[tuple[str, str]]:
    return [(r.url.path, r.url.params["action"]) for r in seen if r.method == "POST" and r.url.path.startswith("/upstream/")]


async def test_swap_render_unloads_every_resident_model_by_id(monkeypatch):
    models = {"a": "ready", "b": "stopping", "c": "stopped"}
    seen = _serve(monkeypatch, _swap(models))
    assert await gpu_swap.probe(ROOT, fresh=True) == "llama_swap"
    _plan(monkeypatch, targets=(_target("llama_swap", "a"),))
    await _render()
    assert set(models.values()) == {"stopped"}
    assert sorted(r.url.path for r in seen if r.method == "POST") == ["/api/models/unload/a", "/api/models/unload/b"]


async def test_swap_slots_saved_under_the_real_id_restore_for_an_alias(monkeypatch):
    models = {"real": "ready", "b": "stopped"}
    seen = _serve(monkeypatch, _swap(models, aliases={"real": ["alias"]}))
    freed: list[str] = []

    async def free(url, api_key="", *, queue_cap=0.0):
        freed.append(url)

    monkeypatch.setattr(gpu_swap, "comfy_free", free)
    _restoring(monkeypatch, _target("llama_swap", "alias"), _target("llama_swap", "b"))
    await _render()
    async with gpu_swap.llm_lease(ROOT, "b"):
        pass  # the other lane claims the GPU first; the alias's slots must still restore on its own call
    for _ in range(2):
        async with gpu_swap.llm_lease(ROOT, "alias"):
            pass
    assert _upstream_posts(seen) == [
        ("/upstream/real/slots/0", "save"),
        ("/upstream/real/slots/1", "save"),
        ("/upstream/real/slots/0", "restore"),
        ("/upstream/real/slots/1", "restore"),
    ]
    restore = next(r for r in seen if r.url.params.get("action") == "restore")
    assert restore.extensions["timeout"]["read"] == gpu_swap.LOAD_TIMEOUT  # the first restore cold-starts the model
    assert models["real"] == "ready"
    assert freed == [COMFY]


async def test_swap_slots_no_lane_restores_stop_claiming_after_one_call(monkeypatch):
    seen = _serve(monkeypatch, _swap({"other-app": "ready", "g": "stopped"}))

    async def free(url, api_key="", *, queue_cap=0.0):
        pass

    monkeypatch.setattr(gpu_swap, "comfy_free", free)
    _restoring(monkeypatch, _target("llama_swap"))
    await _render()
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass
    before = len(seen)
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass
    assert len(seen) == before


async def test_swap_save_never_starts_a_model(monkeypatch):
    seen = _serve(monkeypatch, _swap({"s": "starting", "x": "stopped"}))
    target = _target("llama_swap")
    assert await gpu_swap.save_slots(target, "s") == ()
    assert await gpu_swap.save_slots(target, "x") == ()
    assert not any(r.url.path.startswith("/upstream/") for r in seen)


async def test_old_swap_without_the_per_model_unload_names_llama_swap(monkeypatch):
    _serve(monkeypatch, _swap({"a": "ready"}, old=True))
    with pytest.raises(gpu_swap.GpuSwapError, match="upgrade llama-swap"):
        await gpu_swap.unload(_target("llama_swap", "a"), "a")


async def test_hosted_comfy_render_starts_it_and_the_next_call_restores_without_free(monkeypatch):
    models = {"g": "ready", "comfyui_auto": "stopped"}
    seen = _serve(monkeypatch, _swap(models, group=True))
    _hosting(monkeypatch)
    heard: list[str] = []
    async with gpu_swap.comfy_lease(heard.append):
        assert models == {"g": "stopped", "comfyui_auto": "ready"}  # llama-swap evicted the model for ComfyUI
    async with gpu_swap.comfy_lease(heard.append):
        pass  # already running: no second start
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass
    assert heard == ["saving", "starting"]
    assert models == {"g": "ready", "comfyui_auto": "stopped"}
    paths = [r.url.path for r in seen]
    assert not any(path.startswith("/api/models/unload") or path.endswith("/free") for path in paths)
    prewarm = [r for r in seen if r.url.path == "/comfyui/system_stats"]
    assert len(prewarm) == 1 and prewarm[0].extensions["timeout"]["read"] == gpu_swap.LOAD_TIMEOUT
    assert _upstream_posts(seen)[-2:] == [("/upstream/g/slots/0", "restore"), ("/upstream/g/slots/1", "restore")]


async def test_hosted_comfy_without_slot_restore_touches_only_comfy(monkeypatch):
    models = {"g": "ready", "comfyui_auto": "stopped"}
    seen = _serve(monkeypatch, _swap(models, group=True))
    _hosting(monkeypatch, slot_restore=False)
    await _render()
    async with gpu_swap.llm_lease(ROOT, "g"):
        pass  # the call itself makes llama-swap swap ComfyUI out
    assert [r.url.path for r in seen if r.url.path != "/running"] == ["/comfyui/system_stats", "/v1/models"]
