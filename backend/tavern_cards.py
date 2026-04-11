from __future__ import annotations
"""
Tavern Cards, as defined by Character Card Spec V2:
    https://github.com/malfoyslastname/character-card-spec-v2
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union
import dacite
from dataclasses_json import dataclass_json, Undefined
from PIL import Image
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


def parse(image_path: str) -> Union[TavernCardV2, TavernCardV1]:
    """
    Parses Tavern Card data from an image file's metadata.
    Attempts to parse as V2 first, falls back to V1 if needed.
    """
    logger.info(f"Parsing tavern card from: {image_path}")
    metadata = extract_exif_data(image_path)
    if "chara" not in metadata:
        logger.error("Invalid Tavern card format - missing 'chara' field in image metadata")
        raise ValueError("Invalid Tavern card format - missing 'chara' field in image metadata")

    try:
        raw_json_bytes = base64.b64decode(metadata["chara"])
        raw_json_string = raw_json_bytes.decode("utf-8")
        logger.info(f"Decoded JSON string length: {len(raw_json_string)} chars")
    except (TypeError, base64.binascii.Error) as e:
        logger.error(f"Invalid Tavern card format - 'chara' field is not valid base64: {e}")
        raise ValueError(f"Invalid Tavern card format - 'chara' field is not valid base64: {e}")
    except UnicodeDecodeError as e:
        logger.error(f"Invalid Tavern card format - 'chara' field does not decode to UTF-8: {e}")
        raise ValueError(f"Invalid Tavern card format - 'chara' field does not decode to UTF-8: {e}")

    try:
        jobj = json.loads(raw_json_string)
        logger.info(f"Parsed JSON object keys: {list(jobj.keys())}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid Tavern card format - 'chara' field does not contain valid JSON: {e}")
        raise ValueError(f"Invalid Tavern card format - 'chara' field does not contain valid JSON: {e}")

    is_v2 = "spec" in jobj and jobj["spec"] == "chara_card_v2"
    logger.info(f"Detected card version: {'V2' if is_v2 else 'V1'}")

    if is_v2:
        config = dacite.Config(
            type_hooks={PositionType: position_converter},
            strict=False,
        )
        try:
            card = dacite.from_dict(data_class=TavernCardV2, data=jobj, config=config)
            logger.info(f"Successfully parsed V2 card: {card.data.name}")
            logger.info(f"V2 card has {len(card.data.alternate_greetings)} alternate greetings")
            logger.info(f"V2 card fields: name={card.data.name}, first_mes={len(card.data.first_mes)} chars")
            return card
        except dacite.DaciteError as error:
            logger.warning(f"Error parsing as TavernCardV2, attempting V1 format: {error}")

    try:
        config = dacite.Config(strict=False)
        card = dacite.from_dict(data_class=TavernCardV1, data=jobj, config=config)
        logger.info(f"Successfully parsed V1 card: {card.name}")
        logger.info(f"V1 card fields: name={card.name}, first_mes={len(card.first_mes)} chars")
        return card
    except dacite.DaciteError as error:
        logger.error(f"Error parsing TavernCardV1 data from '{image_path}': {error}")
        raise
    except Exception as error:
        logger.error(f"An unexpected error occurred while parsing '{image_path}': {error}")
        raise


def card_to_dict(card: Union[TavernCardV2, TavernCardV1]) -> dict:
    """Normalize a parsed card (V1 or V2) into a flat dictionary for storage."""
    if isinstance(card, TavernCardV2):
        d = card.data
        logger.info(f"Converting V2 card to dict: name={d.name}, alternate_greetings={len(d.alternate_greetings)}")
        for i, greeting in enumerate(d.alternate_greetings[:3]):  # Log first 3 greetings
            logger.info(f"  Alternate greeting {i}: {greeting[:100]}{'...' if len(greeting) > 100 else ''}")
        if len(d.alternate_greetings) > 3:
            logger.info(f"  ... and {len(d.alternate_greetings) - 3} more")
        return {
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
            "source_format": "tavern_v2",
        }
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
