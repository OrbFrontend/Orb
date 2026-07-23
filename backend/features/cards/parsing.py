"""
Tavern Cards, as defined by Character Card Spec V2:
    https://github.com/malfoyslastname/character-card-spec-v2
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
from typing import Any, Dict, List, Literal, Optional, Union

from PIL import Image, PngImagePlugin
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


# extra="ignore" mirrors the old dataclasses_json Undefined.EXCLUDE on the two
# card-data entry points; nested models inherit pydantic's default (also ignore).
class TavernCardV1(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    fav: Optional[bool] = None
    chat: Optional[str] = None
    creatorcomment: Optional[str] = None
    avatar: Optional[str] = None
    create_date: Optional[str] = None
    talkativeness: Optional[float] = None


PositionType = Optional[Literal["before_char", "after_char"]]


class CharacterBookEntry(BaseModel):
    keys: List[str] = Field(default_factory=list)
    content: str = ""
    extensions: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    insertion_order: Union[int, float] = 0
    case_sensitive: Optional[bool] = None
    name: Optional[str] = None
    priority: Optional[Union[int, float]] = None
    id: Optional[Union[int, float]] = None
    comment: Optional[str] = None
    selective: Optional[bool] = None
    secondary_keys: Optional[List[str]] = None
    constant: Optional[bool] = None
    position: PositionType = None

    # Runs before Literal validation so numeric/string world-info positions are
    # mapped into the spec's two values (or dropped). See position_converter.
    @field_validator("position", mode="before")
    @classmethod
    def _coerce_position(cls, v: Any) -> Any:
        return position_converter(v)


class CharacterBook(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    scan_depth: Optional[int] = None
    token_budget: Optional[Union[int, float]] = None
    recursive_scanning: Optional[bool] = None
    extensions: Dict[str, Any] = Field(default_factory=dict)
    entries: List[CharacterBookEntry] = Field(default_factory=list)


class TavernCardV2Data(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    creator_notes: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: List[str] = Field(default_factory=list)
    character_book: Optional[CharacterBook] = None
    tags: List[str] = Field(default_factory=list)
    creator: str = ""
    character_version: str = ""
    extensions: Dict[str, Any] = Field(default_factory=dict)
    fav: Optional[bool] = None
    chat: Optional[str] = None
    creatorcomment: Optional[str] = None
    avatar: Optional[str] = None
    create_date: Optional[str] = None
    talkativeness: Optional[float] = None


class TavernCardV2(BaseModel):
    spec: Literal["chara_card_v2"] = "chara_card_v2"
    spec_version: Literal["2.0"] = "2.0"
    data: TavernCardV2Data = Field(default_factory=TavernCardV2Data)


def extract_exif_data(image_path: str) -> Dict[str, Any]:
    img = Image.open(image_path)
    img.load()
    return {k: v for k, v in img.info.items() if isinstance(k, str)}


def position_converter(data: Any) -> Any:
    """Coerce a lorebook entry's ``position`` to the V2 spec's literal values.

    The V2 spec only allows ``before_char``/``after_char``, but cards exported
    from SillyTavern (and mirrored by sites like botbooru) store the numeric
    world-info position instead — often as a string — where 0 = before char
    defs and 1 = after, plus higher values (author's note, at-depth, …) with no
    V2 equivalent. Map the two representable values and drop anything else so a
    single odd field doesn't reject the whole card down to the V1 parser.
    """
    if data in ("before_char", "after_char"):
        return data
    if data in (0, "0"):
        return "before_char"
    if data in (1, "1"):
        return "after_char"
    return None


def first_chara_chunk(image_path: str) -> Optional[str]:
    """Return the first ``chara`` tEXt chunk of a PNG, or None.

    PIL's ``img.info`` is a plain dict, so a card carrying *several* ``chara``
    chunks (some editors append instead of replacing) collapses to the **last**
    one — often a stale copy missing alternate_greetings. SillyTavern and
    chub.ai take the first match, so we do too.
    """
    with open(image_path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    i = 8
    while i + 12 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        if data[i + 4 : i + 8] == b"tEXt":
            key, _, value = data[i + 8 : i + 8 + length].partition(b"\x00")
            if key == b"chara":
                return value.decode("latin-1")
        i += 12 + length
    return None


def parse(image_path: str) -> Union[TavernCardV2, TavernCardV1]:
    """
    Parses Tavern Card data from an image file's metadata.
    Attempts to parse as V2 first, falls back to V1 if needed.
    """
    logger.info(f"Parsing tavern card from: {image_path}")
    metadata = extract_exif_data(image_path)
    # Duplicate chara chunks: PIL kept the last, the spec readers use the first.
    if first := first_chara_chunk(image_path):
        if first != metadata.get("chara"):
            logger.warning("PNG carries multiple 'chara' chunks - using the first, as SillyTavern/chub do")
        metadata["chara"] = first
    if "chara" not in metadata:
        logger.error("Invalid Tavern card format - missing 'chara' field in image metadata")
        raise ValueError("Invalid Tavern card format - missing 'chara' field in image metadata")

    try:
        raw_json_bytes = base64.b64decode(metadata["chara"])
        raw_json_string = raw_json_bytes.decode("utf-8")
        logger.info(f"Decoded JSON string length: {len(raw_json_string)} chars")
    except (TypeError, binascii.Error) as e:
        logger.error(f"Invalid Tavern card format - 'chara' field is not valid base64: {e}")
        raise ValueError(f"Invalid Tavern card format - 'chara' field is not valid base64: {e}") from e
    except UnicodeDecodeError as e:
        logger.error(f"Invalid Tavern card format - 'chara' field does not decode to UTF-8: {e}")
        raise ValueError(f"Invalid Tavern card format - 'chara' field does not decode to UTF-8: {e}") from e

    try:
        jobj = json.loads(raw_json_string)
        logger.info(f"Parsed JSON object keys: {list(jobj.keys())}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid Tavern card format - 'chara' field does not contain valid JSON: {e}")
        raise ValueError(f"Invalid Tavern card format - 'chara' field does not contain valid JSON: {e}") from e

    return from_json_obj(jobj)


def from_json_obj(jobj: Dict[str, Any]) -> Union[TavernCardV2, TavernCardV1]:
    """Build a Tavern card from an already-parsed JSON object.

    Tries the V2 spec first and falls back to V1, mirroring :func:`parse`
    but for callers that obtained the card JSON directly rather than embedded
    in a PNG (e.g. an archive API that serves the definition as JSON).
    """
    is_v2 = "spec" in jobj and jobj["spec"] == "chara_card_v2"
    logger.info(f"Detected card version: {'V2' if is_v2 else 'V1'}")

    if is_v2:
        try:
            card = TavernCardV2.model_validate(jobj)
            logger.info(f"Successfully parsed V2 card: {card.data.name}")
            logger.info(f"V2 card has {len(card.data.alternate_greetings)} alternate greetings")
            if card.data.character_book is not None:
                logger.info(f"V2 card has character_book with {len(card.data.character_book.entries)} entries")
            else:
                logger.info("V2 card has no character_book")
            logger.info(f"V2 card fields: name={card.data.name}, first_mes={len(card.data.first_mes)} chars")
            return card
        except ValidationError as error:
            logger.warning(f"Error parsing as TavernCardV2, attempting V1 format: {error}")

    try:
        card = TavernCardV1.model_validate(jobj)
        logger.info(f"Successfully parsed V1 card: {card.name}")
        logger.info(f"V1 card fields: name={card.name}, first_mes={len(card.first_mes)} chars")
        return card
    except ValidationError as error:
        logger.error(f"Error parsing TavernCardV1 data: {error}")
        raise


def read_orb_id(image_path: str) -> str | None:
    """Return the orb_id tEXt chunk from a PNG produced by to_png, or None."""
    try:
        metadata = extract_exif_data(image_path)
        return metadata.get("orb_id") or None
    except Exception:
        return None


def to_png(card_dict: dict, avatar_bytes: bytes | None = None) -> bytes:
    """Serialize card_dict to a SillyTavern V2-compatible PNG.

    The chara tEXt chunk contains only the standard V2 fields.
    An additional orb_id tEXt chunk carries the card UUID so that
    re-importing an exported card relinks existing conversation history.
    """
    # Build strictly-spec-compliant V2 JSON (no extra fields)
    v2_payload = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": card_dict.get("name", ""),
            "description": card_dict.get("description", ""),
            "personality": card_dict.get("personality", ""),
            "scenario": card_dict.get("scenario", ""),
            "first_mes": card_dict.get("first_mes", ""),
            "mes_example": card_dict.get("mes_example", ""),
            "creator_notes": card_dict.get("creator_notes", ""),
            "system_prompt": card_dict.get("system_prompt", ""),
            "post_history_instructions": card_dict.get("post_history_instructions", ""),
            "alternate_greetings": card_dict.get("alternate_greetings", []),
            "tags": card_dict.get("tags", []),
            "creator": card_dict.get("creator", ""),
            "character_version": card_dict.get("character_version", ""),
            "extensions": card_dict.get("extensions", {}),
        },
    }
    # Include character_book if present
    cb = card_dict.get("character_book")
    if cb:
        v2_payload["data"]["character_book"] = cb
    chara_b64 = base64.b64encode(json.dumps(v2_payload, ensure_ascii=False).encode("utf-8")).decode("ascii")

    # Load avatar image or create a neutral placeholder
    if avatar_bytes:
        img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    else:
        img = Image.new("RGBA", (400, 400), (128, 128, 128, 255))

    # Build PNG metadata: chara (spec) + orb_id (stable round-trip identity)
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("chara", chara_b64)
    if card_id := card_dict.get("id"):
        pnginfo.add_text("orb_id", card_id)

    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=pnginfo)
    return buf.getvalue()


def card_to_dict(card: Union[TavernCardV2, TavernCardV1]) -> dict:
    """Normalize a parsed card (V1 or V2) into a flat dictionary for storage."""
    if isinstance(card, TavernCardV2):
        d = card.data
        logger.info(f"Converting V2 card to dict: name={d.name}, alternate_greetings={len(d.alternate_greetings)}")
        for i, greeting in enumerate(d.alternate_greetings[:3]):  # Log first 3 greetings
            logger.info(f"  Alternate greeting {i}: {greeting[:100]}{'...' if len(greeting) > 100 else ''}")
        if len(d.alternate_greetings) > 3:
            logger.info(f"  ... and {len(d.alternate_greetings) - 3} more")
        if d.character_book is not None:
            logger.info(f"  Character book: {len(d.character_book.entries)} entries")
        result = {
            "name": d.name,
            "description": d.description,
            "personality": d.personality,
            "scenario": d.scenario,
            "first_mes": d.first_mes,
            "mes_example": d.mes_example,
            "creator_notes": d.creator_notes or "",
            "system_prompt": d.system_prompt or "",
            "post_history_instructions": d.post_history_instructions or "",
            "alternate_greetings": d.alternate_greetings or [],
            "tags": d.tags or [],
            "creator": d.creator or "",
            "character_version": d.character_version or "",
            "extensions": d.extensions if d.extensions else {},
            "source_format": "tavern_v2",
        }
        if d.character_book is not None:
            # exclude_none reproduces the spec-compliant projection: required
            # fields always present, optional fields only when set.
            result["character_book"] = d.character_book.model_dump(exclude_none=True)
        return result
    else:
        logger.info(f"Converting V1 card to dict: name={card.name}, no alternate greetings")
        return {
            "name": card.name,
            "description": card.description,
            "personality": card.personality,
            "scenario": card.scenario,
            "first_mes": card.first_mes,
            "mes_example": card.mes_example,
            "creator_notes": card.creatorcomment or "",
            "system_prompt": "",
            "post_history_instructions": "",
            "alternate_greetings": [],
            "tags": [],
            "creator": "",
            "character_version": "",
            "source_format": "tavern_v1",
        }
