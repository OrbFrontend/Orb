// The "This Character Only" section of the settings modal: the per-character
// appearance prompts and the reference image.
//
// Split out of config_panel.js because it shares nothing with the rest of the form —
// no draft, no connection list, no style rows. It reads and writes one conversation's
// character state over the trigger route, and its only contract with the panel is
// four calls: mount, reset, populate, save.

import { api, convUrl, esc, escAttr, getActiveConvId, registerAction, toast } from "/static/workflow_api.js";

const WORKFLOW_ID = "image_gen";

// Mirrored from the backend normalizer, which drops what it will not store. Checked
// here so an over-size or unsupported file becomes a message, rather than an image
// that previews fine and is silently gone on the next open.
export const MAX_REFERENCE_IMAGE_BYTES = 10_000_000;
export const REFERENCE_IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp"];

// The character's reference image as the form holds it — loaded with the profile,
// replaced by the picker, emptied by Clear, written back on Save. Module state
// rather than read off the rendered <img>, so a save that never touched the picker
// round-trips the stored bytes untouched.
let referenceImage = { reference_image_b64: "", reference_mime: "" };

export function initCharacterProfile() {
  registerAction(WORKFLOW_ID, "referenceFile", (el) => pickReferenceImage(el));
  registerAction(WORKFLOW_ID, "referenceClear", () =>
    setReferenceImage({ reference_image_b64: "", reference_mime: "" }),
  );
}

// Called as the modal opens: a reference image picked for a different character must
// not survive into this form.
export function resetCharacterProfile() {
  referenceImage = { reference_image_b64: "", reference_mime: "" };
}

function referenceImageHtml() {
  const stored = !!referenceImage.reference_image_b64;
  return `<div class="ig-reference-image">
      ${stored ? `<div class="ig-reference-preview"><img class="ig-reference-thumb" alt="Character reference image" src="data:${escAttr(referenceImage.reference_mime || "image/png")};base64,${escAttr(referenceImage.reference_image_b64)}"></div>` : ""}
      <div class="ig-reference-controls">
        <input type="file" accept="image/png,image/jpeg,image/webp" data-wf-action="image_gen:referenceFile" data-wf-on="change">
        ${
          stored
            ? `<button class="btn btn-sm" data-wf-action="image_gen:referenceClear">Clear</button>`
            : `<span class="image-gen-note ig-reference-empty">No reference image — the character card's avatar is used.</span>`
        }
      </div>
    </div>`;
}

function setReferenceImage(next) {
  referenceImage = next;
  const host = document.getElementById("ig-reference-host");
  if (host) host.innerHTML = referenceImageHtml();
}

async function pickReferenceImage(input) {
  const file = input.files?.[0];
  if (!file) return;
  if (file.size > MAX_REFERENCE_IMAGE_BYTES) {
    toast("That image is too large — use one under 10 MB", "error");
    input.value = "";
    return;
  }
  if (!REFERENCE_IMAGE_MIMES.includes((file.type || "").toLowerCase())) {
    toast("Orb accepts PNG, JPEG and WebP reference images", "error");
    input.value = "";
    return;
  }
  try {
    // Chunked: a single spread of the whole array blows String.fromCharCode's
    // argument limit on a multi-MB image.
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    setReferenceImage({ reference_image_b64: btoa(binary), reference_mime: file.type.toLowerCase() });
  } catch {
    toast("Could not read that image", "error");
  }
  input.value = "";
}

export async function populateProfile() {
  const el = document.getElementById("ig-profile");
  if (!el || !getActiveConvId()) return;
  try {
    const res = await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
      action: "get_profile",
    });
    if (!res?.profile) {
      el.textContent = "This conversation has no character.";
      return;
    }
    el.classList.remove("image-gen-note");
    referenceImage = {
      reference_image_b64: res.profile.reference_image_b64 || "",
      reference_mime: res.profile.reference_mime || "",
    };
    el.innerHTML = `<div class="ig-profile-fields">
        <label>Positive prompt<textarea id="ig-appearance" placeholder="Permanent tags, fill with permanent traits (e.g. Hatsune Miku, black and white)">${esc(res.profile.appearance_prompt || "")}</textarea></label>
        <label>Negative prompt<textarea id="ig-profile-negative" placeholder="Things to never render (e.g. 3D, colored, color). Quality and scene negatives are already handled.">${esc(res.profile.negative_prompt || "")}</textarea></label>
        <div class="ig-profile-reference">
          <span class="ig-profile-reference-label">Reference image</span>
          <span class="image-gen-note">Used by workflows with reference image slots.</span>
          <div id="ig-reference-host">${referenceImageHtml()}</div>
        </div>
      </div>`;
  } catch {
    el.textContent = "Could not load character appearance.";
  }
}

export async function saveProfile() {
  // No fields rendered means no active character: sending blanks would wipe a
  // saved appearance.
  const appearanceEl = document.getElementById("ig-appearance");
  if (!appearanceEl || !getActiveConvId()) return;
  const res = await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
    action: "set_profile",
    profile: {
      appearance_prompt: appearanceEl.value || "",
      negative_prompt: document.getElementById("ig-profile-negative")?.value || "",
      ...referenceImage,
    },
  });
  // A save that reports success while discarding what the form is still previewing
  // is the one outcome the user cannot diagnose, so the handler's warning is shown
  // and the local copy is brought back in line with what was stored.
  if (res?.warning) {
    toast(res.warning, "error");
    referenceImage = {
      reference_image_b64: res.profile?.reference_image_b64 || "",
      reference_mime: res.profile?.reference_mime || "",
    };
  }
}
