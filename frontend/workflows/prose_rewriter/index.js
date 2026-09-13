import { registerWorkflowToolsPanelCard } from "/static/workflow_api.js";

const WORKFLOW_ID = "prose_rewriter";

registerWorkflowToolsPanelCard(
  WORKFLOW_ID,
  () => `<div class="tool-card-desc">
    Automatically rewrites Writer drafts.
  </div>`,
);
