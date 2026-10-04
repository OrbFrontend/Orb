import { api } from "./api.js";
import { begin, finish } from "./operations.js";
import { S } from "./state.js";
import { convUrl, escAttr } from "./utils.js";

// Render jobs turn their initiating button into Stop, using a job id or AbortController. Use `show` for live buttons
// and `stopButtonState` during repaint; `end` restores every button carrying the job id.
//
// A double click's second press is not Stop.
const STOP_ARM_MS = 400;

export function startWorkflowJob({ convId = null, title, controller = null, messageId = null, attachmentId = null }) {
  const startedAt = performance.now();
  const record = begin("media", { conversationId: convId, messageId, attachmentId });
  const job = Object.assign(record, {
    title,
    // Set from the first Stop press; a request that then fails was stopped.
    stopping: false,
    url: (path) => `${path}?job=${job.id}`,
    show(btn) {
      if (!btn) return;
      btn.dataset.wfJob = job.id;
      btn.dataset.idleTitle = btn.getAttribute("title") || "";
      if (btn.hasAttribute("aria-label")) {
        btn.dataset.idleLabel = btn.getAttribute("aria-label");
        btn.setAttribute("aria-label", title);
      }
      btn.title = title;
      btn.classList.add("wf-running");
      btn.disabled = false;
    },
    async stop() {
      if (job.stopping || performance.now() - startedAt < STOP_ARM_MS) return;
      job.stopping = true;
      for (const btn of _buttons(job)) btn.disabled = true;
      if (controller) {
        controller.abort();
        return;
      }
      try {
        const res = await api.post(job.url(convUrl(convId, "workflows", "stop")), {});
        if (res?.stopped) return;
      } catch (e) {
        console.warn("stopping a workflow render failed", e);
      }
      // Nothing ran under this id: the request has not reached the server yet,
      // or it already finished. Either way the button can stop it again.
      job.stopping = false;
      for (const btn of _buttons(job)) btn.disabled = false;
    },
    end() {
      finish(job);
      for (const btn of _buttons(job)) {
        btn.classList.remove("wf-running");
        btn.title = btn.dataset.idleTitle || "";
        if (btn.dataset.idleLabel != null) btn.setAttribute("aria-label", btn.dataset.idleLabel);
        delete btn.dataset.wfJob;
        delete btn.dataset.idleTitle;
        delete btn.dataset.idleLabel;
        btn.disabled = false;
      }
    },
  });
  return job;
}

/** Stop the live job *jobId*; an id that ended, or never ran, is ignored. */
export function stopWorkflowJob(jobId) {
  return S.operations.get(jobId)?.stop();
}

function _buttons(job) {
  return document.querySelectorAll(`[data-wf-job="${job.id}"]`);
}

/**
 * A button's class suffix and attributes, as its Stop button while *job* runs
 * and as itself (*idleTitle*, and *idleLabel* when it carries an aria-label)
 * otherwise. The attributes include `title`.
 */
export function stopButtonState(job, idleTitle, idleLabel = null) {
  const label = (text) => (idleLabel == null ? "" : ` aria-label="${escAttr(text)}"`);
  if (!job) return { cls: "", attrs: ` title="${escAttr(idleTitle)}"${label(idleLabel)}` };
  const idle = ` data-wf-job="${job.id}" data-idle-title="${escAttr(idleTitle)}"${idleLabel == null ? "" : ` data-idle-label="${escAttr(idleLabel)}"`}`;
  return {
    cls: " wf-running",
    attrs: ` title="${escAttr(job.title)}"${label(job.title)}${idle}${job.stopping ? " disabled" : ""}`,
  };
}
