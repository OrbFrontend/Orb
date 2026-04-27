from __future__ import annotations

"""
Tavern Cards, as defined by Character Card Spec V2:
    https://github.com/malfoyslastname/character-card-spec-v2
"""

import io
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union
import dacite
from dataclasses_json import dataclass_json, Undefined
from PIL import Image, PngImagePlugin
import base64
import json

logger = logging.getLogger(__name__)


@dataclass_json
@dataclass
class TavernCardV1:
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


@dataclass_json
@dataclass
class CharacterBookEntry:
    keys: List[str] = field(default_factory=lambda: [])
    content: str = ""
    extensions: Dict[str, Any] = field(default_factory=lambda: dict())
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


@dataclass_json
@dataclass
class CharacterBook:
    name: Optional[str] = None
    description: Optional[str] = None
    scan_depth: Optional[int] = None
    token_budget: Optional[Union[int, float]] = None
    recursive_scanning: Optional[bool] = None
    extensions: Dict[str, Any] = field(default_factory=lambda: dict())
    entries: List[CharacterBookEntry] = field(default_factory=lambda: [])


@dataclass_json(undefined=Undefined.EXCLUDE)
@dataclass
class TavernCardV2Data:
    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    creator_notes: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: List[str] = field(default_factory=lambda: [])
    character_book: Optional[CharacterBook] = None
    tags: List[str] = field(default_factory=lambda: [])
    creator: str = ""
    character_version: str = ""
    extensions: Dict[str, Any] = field(default_factory=lambda: dict())
    fav: Optional[bool] = None
    chat: Optional[str] = None
    creatorcomment: Optional[str] = None
    avatar: Optional[str] = None
    create_date: Optional[str] = None
    talkativeness: Optional[float] = None


@dataclass_json(undefined=Undefined.EXCLUDE)
@dataclass
class TavernCardV2:
    spec: Literal["chara_card_v2"] = "chara_card_v2"
    spec_version: Literal["2.0"] = "2.0"
    data: TavernCardV2Data = field(
        default_factory=lambda: TavernCardV2Data(),
    )


def extract_exif_data(image_path: str) -> Dict[str, Any]:
    img = Image.open(image_path)
    img.load()
    return img.info


def position_converter(data: Any) -> Any:
    if data == 0:
        return None
    return data


def float_converter(value: Any) -> float:
    if isinstance(value, str):
        return float(value)
    return value


def parse(image_path: str) -> Union[TavernCardV2, TavernCardV1]:
    """
    Parses Tavern Card data from an image file's metadata.
    Attempts to parse as V2 first, falls back to V1 if needed.
    """
    logger.info(f"Parsing tavern card from: {image_path}")
    metadata = extract_exif_data(image_path)
    if "chara" not in metadata:
        logger.error(
            "Invalid Tavern card format - missing 'chara' field in image metadata"
        )
        raise ValueError(
            "Invalid Tavern card format - missing 'chara' field in image metadata"
        )

    try:
        raw_json_bytes = base64.b64decode(metadata["chara"])
        raw_json_string = raw_json_bytes.decode("utf-8")
        logger.info(f"Decoded JSON string length: {len(raw_json_string)} chars")
    except (TypeError, base64.binascii.Error) as e:
        logger.error(
            f"Invalid Tavern card format - 'chara' field is not valid base64: {e}"
        )
        raise ValueError(
            f"Invalid Tavern card format - 'chara' field is not valid base64: {e}"
        ) from e
    except UnicodeDecodeError as e:
        logger.error(
            f"Invalid Tavern card format - 'chara' field does not decode to UTF-8: {e}"
        )
        raise ValueError(
            f"Invalid Tavern card format - 'chara' field does not decode to UTF-8: {e}"
        ) from e

    try:
        jobj = json.loads(raw_json_string)
        logger.info(f"Parsed JSON object keys: {list(jobj.keys())}")
    except json.JSONDecodeError as e:
        logger.error(
            f"Invalid Tavern card format - 'chara' field does not contain valid JSON: {e}"
        )
        raise ValueError(
            f"Invalid Tavern card format - 'chara' field does not contain valid JSON: {e}"
        ) from e

    is_v2 = "spec" in jobj and jobj["spec"] == "chara_card_v2"
    logger.info(f"Detected card version: {'V2' if is_v2 else 'V1'}")

    if is_v2:
        config = dacite.Config(
            type_hooks={
                PositionType: position_converter,
                float: float_converter,
            },
            strict=False,
        )
        try:
            card = dacite.from_dict(data_class=TavernCardV2, data=jobj, config=config)
            logger.info(f"Successfully parsed V2 card: {card.data.name}")
            logger.info(
                f"V2 card has {len(card.data.alternate_greetings)} alternate greetings"
            )
            if card.data.character_book is not None:
                logger.info(
                    f"V2 card has character_book with {len(card.data.character_book.entries)} entries"
                )
            else:
                logger.info("V2 card has no character_book")
            logger.info(
                f"V2 card fields: name={card.data.name}, first_mes={len(card.data.first_mes)} chars"
            )
            return card
        except dacite.DaciteError as error:
            logger.warning(
                f"Error parsing as TavernCardV2, attempting V1 format: {error}"
            )

    try:
        config = dacite.Config(
            strict=False,
            type_hooks={float: float_converter},
        )
        card = dacite.from_dict(data_class=TavernCardV1, data=jobj, config=config)
        logger.info(f"Successfully parsed V1 card: {card.name}")
        logger.info(
            f"V1 card fields: name={card.name}, first_mes={len(card.first_mes)} chars"
        )
        return card
    except dacite.DaciteError as error:
        logger.error(f"Error parsing TavernCardV1 data from {image_path!r}: {error}")
        raise
    except Exception as error:
        logger.error(
            f"An unexpected error occurred while parsing {image_path!r}: {error}"
        )
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
    chara_b64 = base64.b64encode(
        json.dumps(v2_payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

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


def _character_book_entry_to_dict(entry: CharacterBookEntry) -> dict:
    """Serialize a CharacterBookEntry to a spec-compliant dictionary."""
    d: Dict[str, Any] = {
        "keys": entry.keys,
        "content": entry.content,
        "extensions": entry.extensions if entry.extensions else {},
        "enabled": entry.enabled,
        "insertion_order": entry.insertion_order,
    }
    if entry.case_sensitive is not None:
        d["case_sensitive"] = entry.case_sensitive
    if entry.name is not None:
        d["name"] = entry.name
    if entry.priority is not None:
        d["priority"] = entry.priority
    if entry.id is not None:
        d["id"] = entry.id
    if entry.comment is not None:
        d["comment"] = entry.comment
    if entry.selective is not None:
        d["selective"] = entry.selective
    if entry.secondary_keys is not None:
        d["secondary_keys"] = entry.secondary_keys
    if entry.constant is not None:
        d["constant"] = entry.constant
    if entry.position is not None:
        d["position"] = entry.position
    return d


def _character_book_to_dict(book: CharacterBook) -> dict:
    """Serialize a CharacterBook to a spec-compliant dictionary."""
    d: Dict[str, Any] = {
        "extensions": book.extensions if book.extensions else {},
        "entries": [_character_book_entry_to_dict(e) for e in book.entries],
    }
    if book.name is not None:
        d["name"] = book.name
    if book.description is not None:
        d["description"] = book.description
    if book.scan_depth is not None:
        d["scan_depth"] = book.scan_depth
    if book.token_budget is not None:
        d["token_budget"] = book.token_budget
    if book.recursive_scanning is not None:
        d["recursive_scanning"] = book.recursive_scanning
    return d


def card_to_dict(card: Union[TavernCardV2, TavernCardV1]) -> dict:
    """Normalize a parsed card (V1 or V2) into a flat dictionary for storage."""
    if isinstance(card, TavernCardV2):
        d = card.data
        logger.info(
            f"Converting V2 card to dict: name={d.name}, alternate_greetings={len(d.alternate_greetings)}"
        )
        for i, greeting in enumerate(
            d.alternate_greetings[:3]
        ):  # Log first 3 greetings
            logger.info(
                f"  Alternate greeting {i}: {greeting[:100]}{'...' if len(greeting) > 100 else ''}"
            )
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
            result["character_book"] = _character_book_to_dict(d.character_book)
        return result
    else:
        logger.info(
            f"Converting V1 card to dict: name={card.name}, no alternate greetings"
        )
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
