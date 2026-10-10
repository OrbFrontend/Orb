"""Database schema, queries, and bootstrap helpers."""

from __future__ import annotations

from .bootstrap import init_db, reset_to_defaults
from .connection import checkpoint_wal, close_wal_anchor, current_db_path, get_db, immediate_tx, open_wal_anchor
from .queries.access import clear_access_password, get_access_password, set_access_password
from .queries.character_cards import (
    card_embedded_fragments,
    cast_embedded_fragments,
    create_character_card,
    delete_character_card,
    get_card_activity,
    get_character_avatar,
    get_character_avatar_stamp,
    get_character_card,
    get_character_usage,
    get_workflow_character_state,
    insert_alternate_greeting_swipes,
    list_character_cards,
    merge_fragments_by_id,
    render_public_profile,
    reroll_unfrozen_greetings,
    set_public_profile,
    set_workflow_character_state,
    sync_conversations_for_card,
    update_character_card,
    upgrade_card_fragment_types,
)
from .queries.character_expressions import (
    delete_character_expressions,
    get_character_expression,
    list_expression_labels,
    set_character_expressions,
)
from .queries.conversation_logs import (
    add_conversation_log,
    get_conversation_logs,
    get_director_log_for_message,
    get_director_logs_for_messages,
    get_moods_before_turn,
    logs_size_before,
    wipe_logs_older_than,
)
from .queries.conversations import (
    create_conversation,
    delete_conversation,
    delete_group_family,
    fork_conversation,
    get_conversation,
    get_workflow_state,
    group_family_ids,
    group_root_of,
    list_conversations,
    set_workflow_state,
    touch_conversation,
    update_conversation,
)
from .queries.director_state import get_director_state, update_director_state
from .queries.documents import create_document, delete_document, get_document, get_documents, update_document
from .queries.endpoints import (
    create_endpoint,
    create_model_config,
    delete_endpoint,
    delete_model_config,
    get_endpoint,
    get_endpoints,
    get_model_configs,
    update_endpoint,
    update_model_config,
)
from .queries.fragment_state import (
    add_state_events,
    copy_state_events,
    delete_fragment_state,
    fold_path_state,
    get_state_events_for_message,
    get_state_events_for_messages,
    get_state_events_for_path,
    snapshot_state_to_message,
)
from .queries.group_members import (
    allocate_speaker_key,
    convert_to_group,
    create_group_conversation,
    get_group_member,
    get_group_member_scripts,
    get_group_members,
    get_speaker_names,
    resolve_cast,
    sync_group_members,
)
from .queries.interactive_fragments import (
    InteractiveFragmentReorderLaneMismatch,
    create_interactive_fragment,
    delete_interactive_fragment,
    get_interactive_fragment,
    get_interactive_fragments,
    reorder_interactive_fragments,
    update_interactive_fragment,
)
from .queries.library_dedupe import (
    add_dismissals,
    apply_avatar_dhash,
    get_dismissals,
    get_relink_impact,
    list_cards_for_dedupe,
    list_stale_avatar_ids,
    read_avatar_b64,
    remove_dismissals,
    resolve_duplicate_cards,
)
from .queries.library_sql import run_library_query
from .queries.library_tags import (
    VocabularyConflict,
    apply_auto_tags,
    get_auto_tag_counts,
    get_vocabulary,
    list_pending_auto_tag_ids,
    replace_vocabulary,
)
from .queries.member_sheets import (
    PROPOSAL_STATUSES,
    REVIEW_STATUSES,
    SheetProposalConflict,
    apply_sheet_proposal,
    create_sheet_proposals,
    get_pending_sheet_proposals,
    get_sheet_proposals,
    reject_sheet_proposal,
)
from .queries.message_subjects import get_message_subjects, set_message_subjects
from .queries.messages import (
    add_message,
    clear_writer_draft,
    decision_evaluations_of,
    delete_message_with_descendants,
    get_active_path,
    get_deepest_descendant,
    get_message_by_id,
    get_message_delete_preview,
    get_messages,
    get_messages_before,
    get_messages_decisions,
    get_messages_with_branch_info,
    get_path_to_leaf,
    get_user_attachment_by_id,
    get_user_attachments_for_message,
    get_workflow_attachments_for_message,
    get_workflow_message_state,
    set_active_leaf,
    set_workflow_message_state,
    switch_to_branch,
    update_message_content,
    user_attachment_payloads,
)
from .queries.mood_fragments import (
    create_mood_fragment,
    delete_mood_fragment,
    get_mood_fragment,
    get_mood_fragments,
    update_mood_fragment,
)
from .queries.phrase_bank import (
    add_phrase_group,
    delete_phrase_group,
    get_phrase_bank,
    get_phrase_bank_rows,
    update_phrase_group,
)
from .queries.settings import (
    get_card_source_auth,
    get_settings,
    get_workflow_config,
    set_card_source_auth,
    set_local_ml_config,
    set_local_ml_enabled,
    set_workflow_config,
    set_workflow_enabled,
    update_decision_config,
    update_settings,
)
from .queries.slop_suggestions import (
    accept_slop_suggestion,
    dismiss_slop_suggestion,
    get_slop_mining_state,
    iter_model_replies,
    list_slop_dismissals,
    list_slop_suggestion_keys,
    list_slop_suggestions,
    open_readonly,
    read_card_rows,
    read_names,
    replace_slop_suggestions,
)
from .queries.stats import add_generated_chars, get_generated_chars, get_global_stats
from .queries.user_personas import (
    create_user_persona,
    delete_user_persona,
    get_persona_avatar,
    get_persona_conversation_counts,
    get_user_persona,
    get_user_personas,
    update_user_persona,
)
from .queries.workflow_attachments import (
    conversation_attachment_ids,
    get_workflow_attachment_by_id,
    get_workflow_attachment_bytes,
    get_workflow_attachment_meta,
    insert_workflow_attachment_row,
)
from .queries.worlds import (
    OverlayStateConflict,
    RevisionConflict,
    apply_changeset,
    count_pending_changesets,
    create_and_apply_changeset,
    create_lorebook_entry,
    create_world,
    create_world_changeset,
    delete_lorebook_entry,
    delete_world,
    entry_snapshot,
    get_active_dynamic_entries,
    get_active_lorebook_entries,
    get_changesets_for_messages,
    get_content_revision,
    get_effective_world_ids,
    get_lorebook_entries,
    get_lorebook_entry,
    get_world,
    get_world_changeset,
    get_world_changesets,
    get_worlds,
    import_lorebook_entries,
    mark_changesets_stale_for_messages,
    mark_orphaned_changesets_stale,
    set_conversation_world,
    supersede_world_changeset,
    update_lorebook_entry,
    update_world,
    update_world_changeset,
)
from .seeds import (
    DEFAULT_ENABLED_TOOLS,
    DEFAULT_SETTINGS,
    NOTES_STATE_FRAGMENT,
    SEED_INTERACTIVE_FRAGMENTS,
    SEED_MOOD_FRAGMENTS,
    SEED_PHRASE_BANK,
    STARTER_STATE_FRAGMENTS,
)
