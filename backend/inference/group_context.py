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
    the member's curated profile (scene override or card profile, labelled by
    which), and examples. Every speaker reads the whole cast, so the active
    speaker's identity fields move *out* of the trailing message -- nothing is
    repeated after history.
``swap``
    A names-only cast list plus the active speaker's identity fields, in the
    conventional single-character layout. No other member's profile is sent,
    and the body therefore differs per speaker (see
    :func:`prefix_is_speaker_scoped`).

Two rules hold in every mode and are deliberately not configurable:

* A card's ``system_prompt`` is ignored and its ``scenario`` is never read. A
  group has exactly one premise (``conversations.character_scenario``);
  merging N card scenarios would create N competing ones. Swap therefore means
  identity-field substitution, not control-instruction substitution.
* ``post_history_instructions`` is a directive, not a shared fact. Merging
  several of them produces contradictory control instructions, so it stays
  active-only in the trailing message -- still honouring
  ``prevent_prompt_overrides``.

Within a mode the rendered bytes are stable while the active roster and its
cards are: canonical ``sort_order, id`` order, fixed headings, and macros
resolved through the conversation seed. Changing the mode is intentionally a
cache miss on the next request.
"""

from __future__ import annotations

from ..core import CastMember, GroupContextMode, Macros, TurnCast

CAST_HEADING = "## Cast"
DOSSIER_HEADING = "## Character dossier: "
ACTIVE_CARD_HEADING = "## Character: "
SCENE_PROFILE_LABEL = "Scene profile: "
CARD_PROFILE_LABEL = "Public profile: "
EXAMPLE_HEADING = "## Example Dialogue"
# A dossier is a `##` section, so its examples nest one level deeper or they
# would close the dossier they belong to.
DOSSIER_EXAMPLE_HEADING = "### Example Dialogue"

# Which group-specific component key the size estimator reports the shared
# body under. Distinct per mode because the three measure different things.
_SHARED_COMPONENT_KEY: dict[GroupContextMode, str] = {
    "private": "cast_public",
    "shared": "cast_dossiers",
    "swap": "cast_names",
}


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

    ``post_history_instructions`` is not covered by this: it is active-only in
    every mode and always stays in the tail.
    """
    return mode == "private"


def roster_names(cast: TurnCast) -> str:
    return ", ".join(member.name for member in cast.members)


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
    """Private perspective: one heading per member, confirmed public profile only.

    Stays on group-title macro scoping -- a public profile is scene-facing
    framing written about the character, not the card text speaking as it.
    """
    resolve = _resolver(macros)
    parts = [f"\n\n{CAST_HEADING}"]
    for member in cast.members:
        parts.append(f"\n### {member.name}")
        if member.public_profile:
            parts.append(f"\n{resolve(member.public_profile.replace('{{cast}}', roster))}")
    return "".join(parts)


def render_dossier(member: CastMember, macros: Macros | None, roster: str) -> str:
    """One member's identity dossier, or ``""`` when it would be empty.

    A member with nothing to say about itself — no card text, no profile, as a
    bare narrator usually is — is skipped rather than announced: an empty
    ``## Character dossier: …`` heading is wasted prefix. Its name still reaches
    the model through the cast list, ``{{cast}}`` and the speaker labels on
    history.
    """
    resolve = _resolver(member_macros(macros, member, roster))
    body = []
    if member.private_sheet:
        body.append(resolve(member.private_sheet))
    # The curated profile is what the user chose to say about this member to the
    # rest of the cast, and this mode hides nothing -- so it ships beside the
    # card text rather than being dropped. Dropping it would also erase a member
    # whose card carries a profile but no description at all: its dossier would
    # render empty and it would survive only as a name. Labelled by provenance,
    # because a scene override is local to this conversation while a card
    # profile travels with the card.
    if member.public_profile:
        label = SCENE_PROFILE_LABEL if member.scene_profile else CARD_PROFILE_LABEL
        body.append(f"{label}{resolve(member.public_profile)}")
    if member.mes_example:
        body.append(_examples_block(resolve(member.mes_example), DOSSIER_EXAMPLE_HEADING))
    if not body:
        return ""
    return f"\n\n{DOSSIER_HEADING}{member.name}\n" + "\n\n".join(body)


def render_active_card(speaker: CastMember, macros: Macros | None, roster: str) -> str:
    """Classic card swap: the selected member's identity fields, card-style."""
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
    names-only list and nothing else.
    """
    if not cast.grouped:
        return ""
    roster = roster_names(cast)
    if cast.context_mode == "private":
        return _render_public_cast(cast, macros, roster)
    # Shared and Swap both keep a names-only list. It guarantees every active
    # member is named even when its dossier is empty, and in Swap it is what
    # lets the Director's neutral base and each Writer base share every byte
    # up to the active card.
    parts = [f"\n\n{CAST_HEADING}\n{roster}"]
    if cast.context_mode == "shared":
        parts.extend(render_dossier(member, macros, roster) for member in cast.members)
    elif cast.speaker is not None:
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
    components = [(_SHARED_COMPONENT_KEY[mode], render_cast_section(neutral, macros))]
    # The shared body above covers the whole roster, muted members included --
    # they are in scene. The per-speaker maxima below must not: a muted member
    # is never scheduled, so billing the beat for a card that can never be sent
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
