import { esc } from "./utils.js";

export function renderDefaultWidget(att) {
  const b64 = att.b64 || att.data_b64 || "";
  const mime = att.mime || att.mime_type || "application/octet-stream";
  const filename = att.filename || att.workflow_id || "artifact";
  if (mime.startsWith("image/")) {
    return `<img class="workflow-artifact-image" src="data:${mime};base64,${b64}" alt="${esc(filename)}">`;
  }
  if (mime.startsWith("audio/")) {
    return `<audio class="workflow-artifact-audio" controls src="data:${mime};base64,${b64}"></audio>`;
  }
  if (mime.startsWith("video/")) {
    return `<video class="workflow-artifact-video" controls src="data:${mime};base64,${b64}"></video>`;
  }
  return `<a class="workflow-artifact-link" href="data:${mime};base64,${b64}" download="${esc(filename)}">${esc(filename)}</a>`;
}
