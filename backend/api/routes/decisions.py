from fastapi import APIRouter, HTTPException

from ...core import (
    DECISION_RESOLUTIONS_BY_TYPE,
    DECISION_TYPES,
    DEFAULT_STATE_TEMPLATE,
    MAX_CHOICE_OPTIONS,
    MAX_SCORE_LEVELS,
    MIN_SCORE_LEVELS,
)
from ...database import (
    get_endpoint,
    get_settings,
    update_decision_config,
)
from ...inference import RAW_ANSWER_CACHE, DecisionTransportError, LLMCallError
from ...pipeline import resolve_judge_config
from ...pipeline.passes.judge import STATE_MACROS, TEXT_MACROS, connection_test
from ..schemas import DecisionConfigUpdate

router = APIRouter()


def _config_payload(settings, config) -> dict:
    return {
        "decision_endpoint_id": settings.get("decision_endpoint_id"),
        "decision_model": settings.get("decision_model", ""),
        "resolved_url": config.url,
        "configured": config.configured,
        "state_macros": sorted(STATE_MACROS),
        "text_macros": sorted(TEXT_MACROS),
        "default_state_template": DEFAULT_STATE_TEMPLATE,
        "question_types": sorted(DECISION_TYPES),
        "resolution_policies": {key: list(value) for key, value in DECISION_RESOLUTIONS_BY_TYPE.items()},
        "choice": {"min_options": 2, "max_options": MAX_CHOICE_OPTIONS},
        "score": {"min_levels": MIN_SCORE_LEVELS, "max_levels": MAX_SCORE_LEVELS},
    }


@router.get("/api/decisions/config")
async def api_get_decision_config():
    settings = await get_settings()
    return _config_payload(settings, await resolve_judge_config(settings))


@router.put("/api/decisions/config")
async def api_update_decision_config(data: DecisionConfigUpdate):
    update = data.model_dump(exclude_unset=True)
    endpoint_id = update.get("decision_endpoint_id")
    if endpoint_id is not None:
        # Refuse a chat endpoint rather than storing one and failing at the
        # gateway: a Writer row has neither the classifier's URL nor its key,
        # and the resulting 404 reads like a broken feature, not a misselection.
        endpoint = await get_endpoint(int(endpoint_id))
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        if endpoint["kind"] != "judge":
            raise HTTPException(
                status_code=422, detail="That endpoint belongs to the chat lanes; save a Judge endpoint instead"
            )
    settings = await update_decision_config(update)
    RAW_ANSWER_CACHE.clear()
    return _config_payload(settings, await resolve_judge_config(settings))


def _rejection_sentence(error: LLMCallError) -> str:
    """Word a provider rejection so it names what was wrong with *this* request.

    The provider's own sentence alone is not diagnostic: a bare "Not Found" next
    to a route the panel is already showing reads as "the feature is broken"
    rather than "nothing answers at that URL". The status code always leads, and
    a 404 says which of the two fields to look at.
    """
    status = error.response.status_code
    parts = [f"HTTP {status}"]
    if error.sentence:
        parts.append(error.sentence)
    if status == 404:
        parts.append("no decisions route answered at this URL — check the Judge Endpoint URL")
    elif status in (401, 403):
        parts.append("the endpoint's API key was rejected")
    return " · ".join(parts)


@router.post("/api/decisions/test")
async def api_test_decision_endpoint():
    config = await resolve_judge_config(await get_settings())
    try:
        return await connection_test(config)
    except LLMCallError as error:
        return {
            "ok": False,
            "error": _rejection_sentence(error),
            "url": config.url,
            "status": error.response.status_code,
        }
    except DecisionTransportError as error:
        return {"ok": False, "error": str(error), "url": config.url}
    except Exception as error:  # noqa: BLE001 -- this diagnostic endpoint reports failures
        return {"ok": False, "error": repr(error), "url": config.url}
