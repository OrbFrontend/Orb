"""Decision classifier configuration, preview, connection test, and card approval.

Four surfaces, one rule between them: none of these routes touch chat state.
Configuration is written through here rather than ``/settings`` because the write
also bumps the cache revision; a preview renders; a test sends one synthetic
scene; an approval records local consent. No cooldown advances and no evaluation
record is written on any of these paths.
"""

from __future__ import annotations

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
    """What the editor needs to explain the classifier's state to a person."""
    return {
        "decision_endpoint_id": settings.get("decision_endpoint_id"),
        "decision_model": settings.get("decision_model", ""),
        # The stored override, and the route actually resolved from it or from
        # the endpoint. Both, because "why is it calling that URL" is the first
        # question a failed test raises.
        "decision_url": settings.get("decision_url", ""),
        "resolved_url": config.url,
        "configured": config.configured,
        "revision": config.revision,
        # Surfaced so the editor can state the limits rather than discover them.
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
    config = await resolve_decision_config(settings)
    # The revision already made the previous namespace unreachable; dropping its
    # entries now returns the memory instead of waiting out the TTL.
    RAW_ANSWER_CACHE.clear()
    return _config_payload(settings, config)


@router.post("/api/decisions/preview")
async def api_preview_decision(data: DecisionPreviewRequest):
    """Render one definition exactly as the stage would, with its sizes.

    Against a real conversation when one is named, so an author can see the
    actual scene text a question will be asked about -- the same renderer, the
    same projection, the same limits.
    """
    problems = definition_problems(data.fragment)
    if problems:
        return {"ok": False, "problems": problems}
    snapshot = SAMPLE_SNAPSHOT
    if data.conversation_id:
        # The pipeline builds it, so the preview cannot drift from the turn.
        live = await decision_preview_snapshot(data.conversation_id)
        if live is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        snapshot = live
    return preview(data.fragment, snapshot)


@router.post("/api/decisions/test")
async def api_test_decision_endpoint():
    """Send one synthetic question to the configured gateway and report back.

    Synthetic on purpose: a connection test must not ship the user's scene to a
    provider they are still deciding whether to use.
    """
    settings = await get_settings()
    config = await resolve_decision_config(settings)
    try:
        return await connection_test(config)
    except LLMCallError as error:
        # The provider's own sentence, kept: a constant here would throw away the
        # one line that usually explains a 404 on an alpha route.
        return {"ok": False, "error": error.sentence or str(error), "url": config.url, "status": error.response.status_code}
    except DecisionTransportError as error:
        return {"ok": False, "error": str(error), "url": config.url}
    except DecisionCancelled:
        return {"ok": False, "error": "Cancelled", "url": config.url}
    except Exception as error:  # noqa: BLE001 — a test reports every failure as a result
        return {"ok": False, "error": repr(error), "url": config.url}


@router.get("/api/decisions/card-approval/{card_id}")
async def api_get_card_decision_approval(card_id: str):
    """Whether this machine has approved *card_id*'s decisions as they stand."""
    card = await get_character_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Character card not found")
    settings = await get_settings()
    fingerprint = card_decision_fingerprint(card)
    stored = (settings.get("decision_card_approvals") or {}).get(card_id)
    return {
        "card_id": card_id,
        # "" means the card contributes no valid decision, so there is nothing to
        # approve -- distinct from "has decisions, not approved".
        "fingerprint": fingerprint,
        "has_decisions": bool(fingerprint),
        "approved": bool(fingerprint) and stored == fingerprint,
        # True when consent was given against definitions the card no longer has.
        "stale": bool(stored) and bool(fingerprint) and stored != fingerprint,
    }


@router.put("/api/decisions/card-approval/{card_id}")
async def api_set_card_decision_approval(card_id: str, data: DecisionCardApproval):
    """Approve or revoke *card_id*'s decisions.

    The body echoes the fingerprint the user was shown; a mismatch is refused
    rather than silently approving whatever the card says now. That is what makes
    a definitions change revoke consent instead of inheriting it.
    """
    card = await get_character_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Character card not found")
    if data.fingerprint is None:
        await set_decision_card_approval(card_id, None)
        return await api_get_card_decision_approval(card_id)
    current = card_decision_fingerprint(card)
    if not current:
        raise HTTPException(status_code=422, detail="This character has no valid decision fragments to approve")
    if data.fingerprint != current:
        raise HTTPException(status_code=409, detail="This character's decisions changed since you reviewed them; reload")
    await set_decision_card_approval(card_id, current)
    return await api_get_card_decision_approval(card_id)
