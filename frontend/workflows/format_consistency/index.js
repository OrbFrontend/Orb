// Frontend module for the "format_consistency" workflow. It has no config,
// artifacts, or controls -- just a one-line description folded under its on/off
// card. The framework owns the name + toggle header; this renderer supplies the
// body (see buildWorkflowToggleRows in settings.js). Registering it also stops the
// boot loader logging an expected 404 for a missing module.

import { registerWorkflowToolsPanelCard } from "/static/workflow_api.js";

registerWorkflowToolsPanelCard(
  "format_consistency",
  () =>
    `<div class="tool-card-desc">Keeps quotes and *asterisks* in replies consistent with the style of your recent messages.</div>`,
);
