import { registerWorkflowToolsPanelCard } from "/static/workflow_api.js";

const WORKFLOW_ID = "prose_rewriter";

registerWorkflowToolsPanelCard(
  WORKFLOW_ID,
  () => `<div class="tool-card-desc">
    Automatically rewrites the post-Editor draft before Format Consistency and artifact workflows.
    Model installation and runtime settings remain under Settings → Local ML; manual message rewrites remain available
    when this automatic workflow is off.
  </div>`,
);
