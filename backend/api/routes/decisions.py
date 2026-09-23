from typing import Any

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
from ...pipeline.passes.judge import (
    STATE_MACROS,
    TEXT_MACROS,
    connection_test,
    definition_problems,
)
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
        # Judge requests need a Judge endpoint; chat endpoints use another route and key.
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


@router.post("/api/decisions/validate")
async def api_validate_decision(data: dict[str, Any]):
    """Validate card-embedded decision data without storing it."""
    problems = definition_problems({"id": "", **data})
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    return {"ok": True}


def _rejection_sentence(error: LLMCallError) -> str:
    """Include the HTTP status and a useful hint for common endpoint failures."""
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
