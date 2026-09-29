import { S } from "./state.js";
import { $ } from "./utils.js";

// Workflow renders the stop button ends alongside the chat turn.

// A turn swaps Send for Stop; a workflow render alone shows Stop beside Send,
// since a reply can still be sent while an image renders.
export function syncStopButton() {
  const turn = S.isStreaming || S.proseRewriteMsgId != null;
  $("send-btn").style.display = turn ? "none" : "flex";
  $("stop-btn").style.display = turn || S.stoppableJobs.size ? "flex" : "none";
}

/**
 * Let the stop button end a workflow render. *stop()* ends the request's own
 * client side; a *convId* also asks the server to cancel that conversation's
 * workflow jobs. Returns the function that unregisters the job.
 */
export function trackStoppableJob({ convId = null, stop }) {
  const job = { convId, stop };
  S.stoppableJobs.add(job);
  syncStopButton();
  return () => {
    S.stoppableJobs.delete(job);
    syncStopButton();
  };
}

export function stopStoppableJobs() {
  const jobs = [...S.stoppableJobs];
  for (const job of jobs) job.stop();
  for (const convId of new Set(jobs.map((job) => job.convId).filter(Boolean))) {
    fetch(`/api/conversations/${convId}/workflows/stop`, { method: "POST" }).catch(() => {});
  }
}
