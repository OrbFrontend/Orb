import { S, subscribe } from "./state.js";

/** Every running task keeps a Stop control after its original view unmounts. */
export function initOperationStatus() {
  const strip = document.createElement("div");
  strip.className = "operation-status";
  strip.setAttribute("aria-live", "polite");
  document.body.appendChild(strip);
  const render = () => {
    strip.replaceChildren();
    for (const op of S.operations.values()) {
      const row = document.createElement("div");
      const conv = S.conversations.find((c) => c.id === op.target.conversationId);
      const label = document.createElement("span");
      label.textContent = `${conv?.title || conv?.character_name || op.kind}: ${op.state?.generationStep || op.phase}`;
      row.appendChild(label);
      if (op.stop) {
        const button = document.createElement("button");
        button.textContent = op.phase === "unknown" ? "Check status" : op.phase === "stopping" ? "Stopping…" : "Stop";
        button.disabled = op.phase === "stopping";
        button.dataset.operationStop = op.id;
        row.appendChild(button);
      }
      strip.appendChild(row);
    }
    strip.hidden = !strip.childElementCount;
  };
  strip.addEventListener("click", (event) => {
    const button = event.target.closest("[data-operation-stop]");
    const op = S.operations.get(button?.dataset.operationStop);
    if (!op?.stop) return;
    if (op.phase !== "unknown") op.phase = "stopping";
    op.stop();
    render();
  });
  subscribe("operations", render);
  render();
}
