"""
group_context.py — The group character-context projection.

One module decides which character fields each :data:`GroupContextMode` puts
into the shared, cached system body and which stay in the speaker's trailing
Writer message. :func:`build_prefix`, ``build_writer_content`` and the
context-size estimator all read the answer from here, so no pass can decide
card visibility on its own.

The three modes, and what each one puts in the shared body:

``private`` (default)
    The ordered public cast -- confirmed ``public_profile`` material only. A
    card's own description, personality and examples reach only that member's
    own Writer/Editor call. This is the historical group behaviour.
``shared``
    A labelled identity dossier per active member: description + personality,
    and examples. Nothing curated sits on top of that -- every member already
    reads every other member's card, so a second, hand-written view of the same
    member would only restate it. Every speaker reads the whole cast, so the
    active speaker's identity fields move *out* of the trailing message --
    nothing is repeated after history.
``swap``
    The same ordered public cast, plus the active speaker's identity fields in
    the conventional single-character layout. The public block is written
    without reference to the speaker, so it is byte-identical to the neutral
    base and each speaker's body merely extends it (see
    :func:`prefix_is_speaker_scoped`).

``private`` and ``swap`` therefore project the *same information* and differ
only in where the speaker's own card sits: after history in the trailing Writer
message, or before it in the cached body. That is a caching decision, not a
visibility one -- both hide every other member's card behind its public profile,
and only ``shared`` opens the cards up. Keeping one renderer
(:func:`_render_public_cast`) for both is what holds that equivalence true: a
second, swap-shaped cast projection would drift from the mode it is supposed to
mirror.

Two rules hold in every mode and are deliberately not configurable:

* A card's ``system_prompt`` is ignored and its ``scenario`` is never read. A
  group has exactly one premise (``conversations.character_scenario``);
  merging N card scenarios would create N competing ones. Swap therefore means
  identity-field substitution, not control-instruction substitution.
* A *card's* ``post_history_instructions`` is a directive, not a shared fact.
  Merging several of them produces contradictory control instructions, so a
  card's stays active-only in the trailing message -- still honouring
  ``prevent_prompt_overrides``. The *scene's* own directive
  (``conversations.post_history_instructions``) has no such conflict: there is
  exactly one, it is the same for every speaker, and ``build_prefix`` puts it in
  the shared cached body where a solo chat already carries it.

Within a mode the rendered bytes are stable while the active roster and its
cards are: canonical ``sort_order, id`` order, fixed headings, and macros
resolved through the conversation seed. Changing the mode is intentionally a
cache miss on the next request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core import CastMember, GroupContextMode, Macros, TurnCast

CAST_HEADING = "## Cast"
DOSSIER_HEADING = "## Character dossier: "
ACTIVE_CARD_HEADING = "## Character: "
EXAMPLE_HEADING = "## Example Dialogue"
# A dossier is a `##` section, so its examples nest one level deeper or they
# would close the dossier they belong to.
DOSSIER_EXAMPLE_HEADING = "### Example Dialogue"


def carries_public_cast(mode: GroupContextMode) -> bool:
    """True when the shared system body carries every member's public profile.

    Two of the three modes do, and for one reason: they keep a card's own text
    away from everyone but its owner, so the curated profile is the only thing
    the rest of the cast is ever told about a member. Under ``swap`` that block
    sits in the system prompt exactly as it does under ``private`` -- the active
    card is appended *after* it, not instead of it.

    ``shared`` is the exception. There every member already reads every other
    member's card, so a profile on top would be a second view of the same
    member, rendered as a label on labels (``Public profile: Appearance: ...``).
    """
    return mode != "shared"


def _shared_component_key(mode: GroupContextMode) -> str:
    """Which key the size estimator reports the shared body under.

    Derived from :func:`carries_public_cast` rather than tabulated per mode, so
    a mode cannot be measured under a heading its prefix no longer renders.
    """
    return "cast_public" if carries_public_cast(mode) else "cast_dossiers"


def prefix_is_speaker_scoped(mode: GroupContextMode) -> bool:
    """True when the shared system body depends on *who* is speaking.

    Only Classic card swap substitutes an active card before history, so only
    it needs one frozen prefix per selected speaker -- plus a neutral one for
    the Director and the pre-pipeline hooks, which both run before a speaking
    plan exists.
    """
    return mode == "swap"


def tail_carries_identity(mode: GroupContextMode) -> bool:
    """True when the speaker's description/personality/examples ride the
    trailing Writer message rather than the shared system body.

    A *card's* ``post_history_instructions`` is not covered by this: it is
    active-only in every mode and always stays in the tail. The scene's own
    directive is not a member field at all and rides the shared body.
    """
    return mode == "private"


def roster_names(cast: TurnCast) -> str:
    return ", ".join(member.name for member in cast.members)


def macro_identity(conv: Mapping[str, Any], cast: TurnCast) -> tuple[str, str]:
    """``({{char}}, {{cast}})`` for one conversation.

    A group is a *scene*, not a character: ``{{char}}`` is its title (read live,
    so a rename lands immediately, unlike ``character_name``, which keeps the
    name the group was founded under) and ``{{cast}}`` is the roster. A solo
    chat keeps its character and leaves ``{{cast}}`` empty.

    The turn prefix, the size estimator, the summarizer and the off-turn
    workflow prefix all resolve macros against the same two strings, so the one
    answer lives here beside the projection that renders them. Callers that
    want a display fallback (``"Character"``) apply it to the first value; this
    returns what the row actually holds.
    """
    if not cast.grouped:
        return str(conv.get("character_name") or ""), ""
    return str(conv.get("title") or ""), roster_names(cast)


def member_macros(macros: Macros | None, member: CastMember, roster: str) -> Macros | None:
    """Scope *macros* to one member, so ``{{char}}`` means that member.

    Card text moved into the shared body would otherwise describe the group --
    a card reading "{{char}} never lies" would start being about the scene
    title. Only the seed and user name ride along untouched, so per-member
    resolution stays byte-stable turn over turn.
    """
    if macros is None:
        return None
    return macros._replace(char=member.name, cast=roster)


def _resolver(macros: Macros | None):
    return macros.resolve_message if macros else (lambda text: text)


def _examples_block(text: str, heading: str) -> str:
    """Render example dialogue under *heading*, honouring the V2 ``<START>`` marker."""
    return text.replace("<START>", heading) if "<START>" in text else f"{heading}\n{text}"


def _render_public_cast(cast: TurnCast, macros: Macros | None, roster: str) -> str:
    """One heading per member, confirmed public profile only -- Private *and* Swap.

    Ignores ``cast.speaker`` entirely, which is what lets Swap put this block in
    the system prompt ahead of the active card: every speaker in an exchange,
    and the Director before a speaker exists, renders these same bytes.

    A member with no profile still gets its heading. That is the names-only
    floor the cast list used to provide on its own -- every active member is
    named, whether or not anyone wrote a profile for it.

    Stays on group-title macro scoping -- a public profile is scene-facing
    framing written about the character, not the card text speaking as it.
    """
    resolve = _resolver(macros._replace(cast=roster) if macros else None)
    parts = [f"\n\n{CAST_HEADING}"]
    for member in cast.members:
        parts.append(f"\n### {member.name}")
        if member.public_profile:
            parts.append(f"\n{resolve(member.public_profile)}")
    return "".join(parts)


def render_dossier(member: CastMember, macros: Macros | None, roster: str) -> str:
    """One member's identity dossier -- card text only -- or ``""`` when empty.

    No curated profile is layered on top -- this is the one mode that drops it,
    for the reason :func:`carries_public_cast` gives: every member already reads
    every other member's card here, so a hand-written public profile would be a
    second view of the same member, and it renders as a label on labels
    (``Public profile: Appearance: …``).

    A member with no card text therefore contributes no dossier and rides the
    cast list as a name -- the same floor a bare narrator already gets. An empty
    ``## Character dossier: …`` heading would be wasted prefix; the name still
    reaches the model through the cast list, ``{{cast}}`` and the speaker labels
    on history.
    """
    resolve = _resolver(member_macros(macros, member, roster))
    body = []
    if member.private_sheet:
        body.append(resolve(member.private_sheet))
    if member.mes_example:
        body.append(_examples_block(resolve(member.mes_example), DOSSIER_EXAMPLE_HEADING))
    if not body:
        return ""
    return f"\n\n{DOSSIER_HEADING}{member.name}\n" + "\n\n".join(body)


def render_active_card(speaker: CastMember, macros: Macros | None, roster: str) -> str:
    """Classic card swap: the selected member's identity fields, card-style.

    Appended after the public cast, so the speaker is described twice on
    purpose: the short public line the rest of the cast reads about it, then its
    own card. The same doubling exists under Private, where the card lands in
    the tail instead -- neither mode suppresses a member's public profile just
    because that member happens to be speaking.
    """
    resolve = _resolver(member_macros(macros, speaker, roster))
    parts = [f"\n\n{ACTIVE_CARD_HEADING}{speaker.name}"]
    if speaker.private_sheet:
        parts.append(f"\n{resolve(speaker.private_sheet)}")
    if speaker.mes_example:
        parts.append("\n\n" + _examples_block(resolve(speaker.mes_example), EXAMPLE_HEADING))
    return "".join(parts)


def render_cast_section(cast: TurnCast, macros: Macros | None) -> str:
    """The whole group identity section of the system body, leading newlines included.

    Returns ``""`` for a solo turn. In Classic card swap a *cast* with no
    ``speaker`` renders the neutral base every speaker's prefix shares: the
    public cast and nothing after it.
    """
    if not cast.grouped:
        return ""
    roster = roster_names(cast)
    if not carries_public_cast(cast.context_mode):
        # Shared keeps a names-only list ahead of the dossiers: it guarantees
        # every active member is named even when its dossier is empty. The other
        # two get that floor from the public cast's per-member headings.
        parts = [f"\n\n{CAST_HEADING}\n{roster}"]
        parts.extend(render_dossier(member, macros, roster) for member in cast.members)
        return "".join(parts)
    parts = [_render_public_cast(cast, macros, roster)]
    # Swap alone appends a card, and only once a speaker is chosen -- the
    # Director and the pre-pipeline hooks run before that and stop at the
    # public cast, which is exactly the base each speaker then extends.
    if prefix_is_speaker_scoped(cast.context_mode) and cast.speaker is not None:
        parts.append(render_active_card(cast.speaker, macros, roster))
    return "".join(parts)


def speaker_tail_fields(member: CastMember, mode: GroupContextMode, *, prevent_prompt_overrides: bool) -> list[str]:
    """The member's own fields the trailing Writer message carries, in order.

    Derived from :func:`tail_carries_identity`, so this and the tail
    ``build_writer_content`` actually renders cannot disagree about *which*
    fields ship -- only about the headings wrapped around them, which the
    size estimator does not need.
    """
    fields = [member.private_sheet, member.mes_example] if tail_carries_identity(mode) else []
    if not prevent_prompt_overrides:
        fields.append(member.post_history)
    return [field for field in fields if field]


def context_size_components(
    cast: TurnCast,
    macros: Macros | None,
    *,
    prevent_prompt_overrides: bool = False,
) -> list[tuple[str, str]]:
    """The mode's group-specific size components as ``(key, text)`` pairs.

    The estimate is a *maximum* group call, not a sum: the shared body once,
    plus the largest single speaker's share of it. Built from the same
    renderers the prompt uses so the two cannot drift.
    """
    mode = cast.context_mode
    roster = roster_names(cast)
    neutral = cast._replace(speaker=None)
    components = [(_shared_component_key(mode), render_cast_section(neutral, macros))]
    # The shared body above covers the whole roster, muted members included --
    # they are in scene. The per-speaker maxima below must not: a muted member
    # is never scheduled, so billing the exchange for a card that can never be sent
    # overstates the call. An all-muted scene generates nothing and measures 0.
    speakers = [member for member in cast.members if not member.muted]
    if prefix_is_speaker_scoped(mode):
        cards = [render_active_card(member, macros, roster) for member in speakers]
        components.append(("largest_active_card", max(cards, key=len, default="")))
    tails = [
        "\n\n".join(
            _resolver(member_macros(macros, member, roster))(field)
            for field in speaker_tail_fields(member, mode, prevent_prompt_overrides=prevent_prompt_overrides)
        )
        for member in speakers
    ]
    components.append(("largest_speaker_tail", max(tails, key=len, default="")))
    return components
