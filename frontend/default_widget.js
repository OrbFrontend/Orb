import { attachmentDataUrl, attachmentMime, escAttr } from "./utils.js";

export function renderDefaultWidget(att) {
  const mime = attachmentMime(att.mime || att.mime_type) || "application/octet-stream";
  const filename = escAttr(att.filename || att.workflow_id || "artifact");
  // Every field below is model- or client-supplied and lands in an attribute,
  // so it is quoted with escAttr, not esc. A src the metadata cannot justify
  // becomes "" rather than a half-built `data:` URL.
  const src = escAttr(attachmentDataUrl(mime, att.b64 || att.data_b64 || ""));
  if (mime.startsWith("image/")) {
    return `<img class="workflow-artifact-image" loading="lazy" decoding="async" src="${src}" alt="${filename}">`;
  }
  if (mime.startsWith("audio/")) {
    return `<audio class="workflow-artifact-audio" controls src="${src}"></audio>`;
  }
  if (mime.startsWith("video/")) {
    return `<video class="workflow-artifact-video" controls src="${src}"></video>`;
  }
  return `<a class="workflow-artifact-link" href="${src}" download="${filename}">${filename}</a>`;
}
