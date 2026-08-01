"""
Tavern Cards, as defined by Character Card Spec V3:
    https://github.com/kwaroran/character-card-spec-v3
with V2 (``chara_card_v2``) and V1 (flat) accepted as fallbacks:
    https://github.com/malfoyslastname/character-card-spec-v2
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
from typing import Any, Literal

from PIL import Image, PngImagePlugin
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


# extra="ignore" on the two card-data entry points: unknown fields are dropped
# rather than rejected; nested models inherit pydantic's default (also ignore).
class TavernCardV1(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    fav: bool | None = None
    chat: str | None = None
    creatorcomment: str | None = None
    avatar: str | None = None
    create_date: str | None = None
    talkativeness: float | None = None


PositionType = Literal["before_char", "after_char"] | None


class CharacterBookEntry(BaseModel):
    keys: list[str] = Field(default_factory=list)
    content: str = ""
    extensions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    insertion_order: int | float = 0
    case_sensitive: bool | None = None
    name: str | None = None
    priority: int | float | None = None
    id: int | float | None = None
    comment: str | None = None
    selective: bool | None = None
    secondary_keys: list[str] | None = None
    constant: bool | None = None
    position: PositionType = None
    use_regex: bool = False  # V3: `keys` are regular expressions rather than substrings

    # Runs before Literal validation so numeric/string world-info positions are
    # mapped into the spec's two values (or dropped). See position_converter.
    @field_validator("position", mode="before")
    @classmethod
    def _coerce_position(cls, v: Any) -> Any:
        return position_converter(v)


class CharacterBook(BaseModel):
    name: str | None = None
    description: str | None = None
    scan_depth: int | None = None
    token_budget: int | float | None = None
    recursive_scanning: bool | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)
    entries: list[CharacterBookEntry] = Field(default_factory=list)


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
    alternate_greetings: list[str] = Field(default_factory=list)
    character_book: CharacterBook | None = None
    tags: list[str] = Field(default_factory=list)
    creator: str = ""
    character_version: str = ""
    extensions: dict[str, Any] = Field(default_factory=dict)
    fav: bool | None = None
    chat: str | None = None
    creatorcomment: str | None = None
    avatar: str | None = None
    create_date: str | None = None
    talkativeness: float | None = None


class TavernCardV2(BaseModel):
    spec: Literal["chara_card_v2"] = "chara_card_v2"
    # Not a Literal: pinning the version *string* turns a cosmetic mismatch into
    # a total parse failure that degrades silently to V1. `spec` is the
    # discriminator we actually dispatch on.
    spec_version: str = "2.0"
    data: TavernCardV2Data = Field(default_factory=TavernCardV2Data)


# V3-only card fields. Orb has no column for any of them, so they park at
# extensions.orb.v3 on import and are rehydrated to the top level on export
# (see card_to_dict / to_png).
V3_ONLY_FIELDS = (
    "nickname",
    "creator_notes_multilingual",
    "source",
    "assets",
    "group_only_greetings",
    "creation_date",
    "modification_date",
)


class TavernCardV3Data(TavernCardV2Data):
    """V3 card data. Purely additive over V2 — every V2 field keeps its type."""

    nickname: str = ""
    creator_notes_multilingual: dict[str, str] | None = None
    source: list[str] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)
    group_only_greetings: list[str] = Field(default_factory=list)
    creation_date: int | None = None
    modification_date: int | None = None


# A sibling of TavernCardV2, not a subclass: overriding `spec`/`data` on a
# subclass is a mutable-attribute variance error under Pyright, and AGENTS.md
# forbids suppressions.
class TavernCardV3(BaseModel):
    spec: Literal["chara_card_v3"] = "chara_card_v3"
    spec_version: str = "3.0"
    data: TavernCardV3Data = Field(default_factory=TavernCardV3Data)


TavernCard = TavernCardV3 | TavernCardV2 | TavernCardV1


def extract_exif_data(image_path: str) -> dict[str, Any]:
    img = Image.open(image_path)
    img.load()
    return {k: v for k, v in img.info.items() if isinstance(k, str)}


def position_converter(data: Any) -> Any:
    """Coerce a lorebook entry's ``position`` to the V2 spec's literal values.

    The V2 spec only allows ``before_char``/``after_char``, but cards exported
    by frontends (and mirrored by sites like botbooru) store the numeric
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


# V3 requires the payload in `ccv3`; `chara` is an optional legacy backfill that
# may hold a downgraded V2 projection, so readers must prefer `ccv3`.
CARD_CHUNKS = ("ccv3", "chara")


def first_text_chunk(image_path: str, key: str) -> str | None:
    """Return the first tEXt chunk of a PNG under *key*, or None.

    PIL's ``img.info`` is a plain dict, so a card carrying *several* chunks under
    one key (some editors append instead of replacing) collapses to the **last**
    one — often a stale copy missing alternate_greetings. Other card readers
    take the first match, so we do too.
    """
    with open(image_path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    want = key.encode("latin-1")
    i = 8
    while i + 12 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        if data[i + 4 : i + 8] == b"tEXt":
            k, _, value = data[i + 8 : i + 8 + length].partition(b"\x00")
            if k == want:
                return value.decode("latin-1")
        i += 12 + length
    return None


def _decode_chunk(key: str, raw: str) -> dict[str, Any]:
    """base64 → utf-8 → JSON for one card chunk. Raises ValueError on any step."""
    try:
        raw_json_string = base64.b64decode(raw).decode("utf-8")
        logger.info(f"Decoded '{key}' JSON string length: {len(raw_json_string)} chars")
    except (TypeError, binascii.Error) as e:
        raise ValueError(f"Invalid Tavern card format - '{key}' field is not valid base64: {e}") from e
    except UnicodeDecodeError as e:
        raise ValueError(f"Invalid Tavern card format - '{key}' field does not decode to UTF-8: {e}") from e

    try:
        jobj = json.loads(raw_json_string)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid Tavern card format - '{key}' field does not contain valid JSON: {e}") from e
    logger.info(f"Parsed JSON object keys: {list(jobj.keys())}")
    return jobj


def parse(image_path: str) -> TavernCard:
    """
    Parses Tavern Card data from an image file's metadata.
    Prefers the V3 ``ccv3`` chunk and falls back to the legacy ``chara`` one;
    the payload itself is then parsed as V3, V2 or V1 (see :func:`from_json_obj`).
    """
    logger.info(f"Parsing tavern card from: {image_path}")
    metadata = extract_exif_data(image_path)
    # Duplicate chunks: PIL kept the last, the spec readers use the first.
    for key in CARD_CHUNKS:
        if first := first_text_chunk(image_path, key):
            if first != metadata.get(key):
                logger.warning(f"PNG carries multiple '{key}' chunks - using the first, as other card readers do")
            metadata[key] = first

    present = [k for k in CARD_CHUNKS if metadata.get(k)]
    if not present:
        logger.error("Invalid Tavern card format - missing 'chara' field in image metadata")
        raise ValueError("Invalid Tavern card format - missing 'chara' field in image metadata")

    error: ValueError | None = None
    for key in present:
        try:
            return from_json_obj(_decode_chunk(key, metadata[key]))
        except ValueError as e:
            error = e
            logger.warning(f"'{key}' chunk is unusable ({e}) - trying the next chunk")
    assert error is not None
    logger.error(str(error))
    raise error


def from_json_obj(jobj: dict[str, Any]) -> TavernCard:
    """Build a Tavern card from an already-parsed JSON object.

    Dispatches on ``spec`` (V3, then V2) and falls back to V1, mirroring
    :func:`parse` but for callers that obtained the card JSON directly rather
    than embedded in a PNG (e.g. an archive API that serves the definition as JSON).
    """
    spec = jobj.get("spec")
    logger.info(f"Detected card version: {spec or 'V1'}")

    if spec in ("chara_card_v3", "chara_card_v2"):
        wrapper = TavernCardV3 if spec == "chara_card_v3" else TavernCardV2
        try:
            card = wrapper.model_validate(jobj)
            logger.info(f"Successfully parsed {spec} card: {card.data.name}")
            logger.info(f"Card has {len(card.data.alternate_greetings)} alternate greetings")
            if card.data.character_book is not None:
                logger.info(f"Card has character_book with {len(card.data.character_book.entries)} entries")
            else:
                logger.info("Card has no character_book")
            logger.info(f"Card fields: name={card.data.name}, first_mes={len(card.data.first_mes)} chars")
            return card
        except ValidationError as error:
            logger.warning(f"Error parsing as {spec}, attempting V1 format: {error}")

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


def _b64_payload(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")


def to_png(card_dict: dict, avatar_bytes: bytes | None = None) -> bytes:
    """Serialize card_dict to a Character Card V3 PNG with a V2 legacy chunk.

    ``ccv3`` carries the V3 payload (V3-only fields rehydrated from
    extensions.orb.v3 to the top level), ``chara`` the V2 projection for
    V2-only readers. An additional orb_id tEXt chunk carries the card UUID so
    that re-importing an exported card relinks existing conversation history.
    """
    # Build strictly-spec-compliant V2 JSON (no extra fields)
    v2_data: dict[str, Any] = {
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
        "extensions": card_dict.get("extensions") or {},
    }
    # Include character_book if present
    cb = card_dict.get("character_book")
    if cb:
        v2_data["character_book"] = cb
    chara_b64 = _b64_payload({"spec": "chara_card_v2", "spec_version": "2.0", "data": v2_data})

    # V3: lift the parked V3-only fields back to the top level and drop the
    # parking slot from the exported extensions so nothing is duplicated.
    ext: dict[str, Any] = v2_data["extensions"]
    orb = ext.get("orb") or {}
    v3_data = {**v2_data, **(orb.get("v3") or {})}
    if orb.get("v3"):
        rest = {k: v for k, v in orb.items() if k != "v3"}
        v3_data["extensions"] = {**ext, "orb": rest} if rest else {k: v for k, v in ext.items() if k != "orb"}
    ccv3_b64 = _b64_payload({"spec": "chara_card_v3", "spec_version": "3.0", "data": v3_data})

    # Load avatar image or create a neutral placeholder
    if avatar_bytes:
        img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    else:
        img = Image.new("RGBA", (400, 400), (128, 128, 128, 255))

    # Build PNG metadata: ccv3 (V3 spec) + chara (V2 legacy) + orb_id (stable round-trip identity)
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("ccv3", ccv3_b64)
    pnginfo.add_text("chara", chara_b64)
    if card_id := card_dict.get("id"):
        pnginfo.add_text("orb_id", card_id)

    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=pnginfo)
    return buf.getvalue()


def card_to_dict(card: TavernCard) -> dict:
    """Normalize a parsed card (V1, V2 or V3) into a flat dictionary for storage."""
    if isinstance(card, TavernCardV2 | TavernCardV3):
        d = card.data
        logger.info(f"Converting V2/V3 card to dict: name={d.name}, alternate_greetings={len(d.alternate_greetings)}")
        for i, greeting in enumerate(d.alternate_greetings[:3]):  # Log first 3 greetings
            logger.info(f"  Alternate greeting {i}: {greeting[:100]}{'...' if len(greeting) > 100 else ''}")
        if len(d.alternate_greetings) > 3:
            logger.info(f"  ... and {len(d.alternate_greetings) - 3} more")
        if d.character_book is not None:
            logger.info(f"  Character book: {len(d.character_book.entries)} entries")
        result: dict[str, Any] = {
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
            "source_format": "tavern_v3" if isinstance(card, TavernCardV3) else "tavern_v2",
        }
        if d.character_book is not None:
            # exclude_none reproduces the spec-compliant projection: required
            # fields always present, optional fields only when set.
            result["character_book"] = d.character_book.model_dump(exclude_none=True)
        if isinstance(card, TavernCardV3):
            # Orb has no columns for these; park them beside extensions.orb.fragments
            # so an edit-and-save round trip keeps them and to_png can rehydrate.
            parked = {f: v for f in V3_ONLY_FIELDS if (v := getattr(card.data, f))}
            if parked:
                ext = dict(result["extensions"])
                ext["orb"] = {**(ext.get("orb") or {}), "v3": parked}
                result["extensions"] = ext
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
