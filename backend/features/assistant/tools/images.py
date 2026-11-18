"""Chat images as the assistant names them: reference tokens, the images a message lists, and each token's workflow origin."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ....database import get_image_attachment_ref, image_attachments_for_messages
from ....database.models import ImageAttachmentRef
from ..contracts import ToolFailure

# The longest reference token: "attachment:" and a row id, or "image:" and a panel picture's 32 hex digits.
TOKEN_CHARS = 48
# The images one message lists, newest first.
_LISTED_IMAGES = 8
_TOKEN = re.compile(r"(attachment|upload):([1-9][0-9]{0,15})|image:([0-9a-f]{32})")


@dataclass(frozen=True)
class Reference:
    """A picture a token names: its *origin* as the image workflow records it, and whether it is a panel picture."""

    origin: str
    panel: bool


def _shown(refs: Sequence[ImageAttachmentRef]) -> set[int]:
    """The ids a message shows, as the chat renderer draws them: every upload, and each variant group's active sibling (NULL
    names the newest)."""
    shown = {ref["id"] for ref in refs if ref["source"] == "upload"}
    renders = {ref["id"]: ref for ref in refs if ref["source"] == "workflow"}
    groups: dict[int, list[int]] = {}
    for ref in renders.values():
        parent = ref["parent_attachment_id"]
        groups.setdefault(parent if parent is not None and parent in renders else ref["id"], []).append(ref["id"])
    for root, ids in groups.items():
        active = renders[root]["active_sibling_id"]
        shown.add(active if active in ids else max(ids))
    return shown


async def listed_images(message_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The images each message carries as a read lists them, keyed by message; a message without any has no entry.

    Renders come before uploads, each newest first: a message's uploads arrive with it, its renders after.
    """
    listed: dict[int, dict[str, Any]] = {}
    for message_id, refs in (await image_attachments_for_messages(message_ids)).items():
        shown = _shown(refs)
        ordered = sorted(refs, key=lambda ref: (ref["source"] == "upload", -ref["id"]))
        images = [
            {
                "ref": f"{'attachment' if ref['source'] == 'workflow' else 'upload'}:{ref['id']}",
                "kind": "render" if ref["source"] == "workflow" else "upload",
                "shown": ref["id"] in shown,
            }
            for ref in ordered[:_LISTED_IMAGES]
        ]
        more = len(ordered) - len(images)
        listed[message_id] = {"images": images, **({"more_images": more} if more > 0 else {})}
    return listed


async def reference_of(token: str, *, panel: bool) -> Reference:
    """The picture *token* names, checked to exist and be an image; a panel picture only where *panel* allows one."""
    token = token.strip()
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise ToolFailure(
            f"{token!r} is not an image reference. Use a ref from read_messages images (attachment:<id> or upload:<id>), "
            "or image:<image_id> for a create_image picture."
        )
    kind, ident, image_id = match.groups()
    if image_id:
        if not panel:
            raise ToolFailure(
                "A create_image picture cannot be a saved reply's reference: a Reroll of the reply could not fetch it again. "
                "For this picture on a reply, call illustrate_reply without reference_image and put what the picture shows "
                "into direction."
            )
        return Reference(f"assistant:{image_id}", panel=True)
    ref = await get_image_attachment_ref("workflow" if kind == "attachment" else "upload", int(ident))
    if ref is None:
        raise ToolFailure(f"No chat image is {token}. read_messages lists each message's images.")
    if not ref["mime_type"].lower().startswith("image/"):
        raise ToolFailure(f"{token} is not an image ({ref['mime_type']}).")
    origin = f"attachment:{ref['id']}" if kind == "attachment" else f"upload:{ref['message_id']}:{ref['id']}"
    return Reference(origin, panel=False)
