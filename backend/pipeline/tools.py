"""Pipeline-owned tool memberships."""

DIRECTOR_LOOP_TOOL_NAMES = frozenset({"direct_scene"})
# Offered on the shared blob whenever the Agent is on; their settings toggles gate the passes, not the schemas.
AGENT_PASS_TOOLS = ("direct_scene", "editor_apply_patch", "editor_rewrite", "editor_find_replace")
