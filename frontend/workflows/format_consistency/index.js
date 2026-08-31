import { registerWorkflowToolsPanelCard } from "/static/workflow_api.js";

registerWorkflowToolsPanelCard(
  "format_consistency",
  () =>
    `<div class="tool-card-desc">Keeps quotes and *asterisks* in replies consistent with the style of your recent messages.</div>`,
);
