from fastapi import APIRouter, HTTPException

from ...core import DEFAULT_STATE_TEMPLATE
from ...database import (
    card_decision_fingerprint,
    get_character_card,
    get_settings,
    set_decision_card_approval,
    update_decision_config,
)
from ...inference import (
    RAW_ANSWER_CACHE,
    DecisionCancelled,
    DecisionTransportError,
    LLMCallError,
)
from ...pipeline import decision_preview_snapshot, resolve_decision_config
from ...pipeline.passes.decisions import (
    MAX_DECISIONS_PER_CARD,
    MAX_DECISIONS_PER_EXCHANGE,
    SAMPLE_SNAPSHOT,
    STAGE_BUDGET_SECONDS,
    STATE_MACROS,
    TEXT_MACROS,
    connection_test,
    definition_problems,
    preview,
)
from ..schemas import DecisionCardApproval, DecisionConfigUpdate, DecisionPreviewRequest

router = APIRouter()


def _config_payload(settings, config) -> dict:
    return {
        "decision_endpoint_id": settings.get("decision_endpoint_id"),
        "decision_model": settings.get("decision_model", ""),
        "decision_url": settings.get("decision_url", ""),
        "resolved_url": config.url,
        "configured": config.configured,
        "revision": config.revision,
        "budgets": {
            "per_exchange": MAX_DECISIONS_PER_EXCHANGE,
            "per_card": MAX_DECISIONS_PER_CARD,
            "stage_seconds": STAGE_BUDGET_SECONDS,
        },
        "state_macros": sorted(STATE_MACROS),
        "text_macros": sorted(TEXT_MACROS),
        "default_state_template": DEFAULT_STATE_TEMPLATE,
    }


@router.get("/api/decisions/config")
async def api_get_decision_config():
    settings = await get_settings()
    return _config_payload(settings, await resolve_decision_config(settings))


@router.put("/api/decisions/config")
async def api_update_decision_config(data: DecisionConfigUpdate):
    settings = await update_decision_config(data.model_dump(exclude_unset=True))
    RAW_ANSWER_CACHE.clear()
    return _config_payload(settings, await resolve_decision_config(settings))


@router.post("/api/decisions/preview")
async def api_preview_decision(data: DecisionPreviewRequest):
    if problems := definition_problems(data.fragment):
        return {"ok": False, "problems": problems}
    snapshot = SAMPLE_SNAPSHOT
    if data.conversation_id:
        snapshot = await decision_preview_snapshot(data.conversation_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    return preview(data.fragment, snapshot)


@router.post("/api/decisions/test")
async def api_test_decision_endpoint():
    config = await resolve_decision_config(await get_settings())
    try:
        return await connection_test(config)
    except LLMCallError as error:
        return {
            "ok": False,
            "error": error.sentence or str(error),
            "url": config.url,
            "status": error.response.status_code,
        }
    except DecisionTransportError as error:
        return {"ok": False, "error": str(error), "url": config.url}
    except DecisionCancelled:
        return {"ok": False, "error": "Cancelled", "url": config.url}
    except Exception as error:  # noqa: BLE001 -- this diagnostic endpoint reports failures
        return {"ok": False, "error": repr(error), "url": config.url}


async def _card_approval(card_id: str) -> dict:
    card = await get_character_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Character card not found")
    fingerprint = card_decision_fingerprint(card)
    stored = ((await get_settings()).get("decision_card_approvals") or {}).get(card_id)
    return {
        "card_id": card_id,
        "fingerprint": fingerprint,
        "has_decisions": bool(fingerprint),
        "approved": bool(fingerprint) and stored == fingerprint,
        "stale": bool(stored and fingerprint and stored != fingerprint),
    }


@router.get("/api/decisions/card-approval/{card_id}")
async def api_get_card_decision_approval(card_id: str):
    return await _card_approval(card_id)


@router.put("/api/decisions/card-approval/{card_id}")
async def api_set_card_decision_approval(card_id: str, data: DecisionCardApproval):
    card = await get_character_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Character card not found")
    current = card_decision_fingerprint(card)
    if data.fingerprint is not None:
        if not current:
            raise HTTPException(status_code=422, detail="This character has no valid decision fragments to approve")
        if data.fingerprint != current:
            raise HTTPException(status_code=409, detail="This character's decisions changed since you reviewed them; reload")
    await set_decision_card_approval(card_id, current if data.fingerprint is not None else None)
    return await _card_approval(card_id)
