import {
  api,
  convUrl,
  esc,
  escAttr,
  getActiveConvId,
  getGroupCast,
  registerAction,
  toast,
} from "/static/workflow_api.js";

const WORKFLOW_ID = "image_gen";

export const MAX_REFERENCE_IMAGE_BYTES = 10_000_000;
export const REFERENCE_IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp"];

let referenceImage = { reference_image_b64: "", reference_mime: "" };
let loadedProfile = null;
let memberId = null;

export function initCharacterProfile() {
  registerAction(WORKFLOW_ID, "referenceFile", (el) => pickReferenceImage(el));
  registerAction(WORKFLOW_ID, "referenceClear", () =>
    setReferenceImage({ reference_image_b64: "", reference_mime: "" }),
  );
  registerAction(WORKFLOW_ID, "profileMember", (el) => selectMember(el));
}

export function resetCharacterProfile() {
  referenceImage = { reference_image_b64: "", reference_mime: "" };
  loadedProfile = null;
  memberId = null;
}

function castWithCards() {
  return (getGroupCast() || []).filter((member) => member.card_id);
}

function profileTarget() {
  return memberId ? { speaker_member_id: memberId } : {};
}

function currentProfile() {
  return {
    appearance_prompt: document.getElementById("ig-appearance")?.value || "",
    negative_prompt: document.getElementById("ig-profile-negative")?.value || "",
    ...referenceImage,
  };
}

export function profileIsDirty() {
  if (!loadedProfile || !document.getElementById("ig-appearance")) return false;
  return JSON.stringify(currentProfile()) !== JSON.stringify(loadedProfile);
}

function selectMember(select) {
  const next = select.value;
  if (next === memberId) return;
  if (profileIsDirty() && !window.confirm("Discard your unsaved changes for this character?")) {
    select.value = memberId;
    return;
  }
  memberId = next;
  populateProfile();
}

function memberPickerHtml(cast) {
  if (!cast.length) return "";
  const options = cast
    .map(
      (member) =>
        `<option value="${escAttr(member.id)}"${member.id === memberId ? " selected" : ""}>${esc(member.name)}</option>`,
    )
    .join("");
  return `<label class="ig-profile-member">Cast member
      <select id="ig-profile-member" data-wf-action="image_gen:profileMember" data-wf-on="change">${options}</select>
    </label>
    <span class="image-gen-note">Each character in the scene has its own appearance, negative prompt, and reference image.</span>`;
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
  const cast = getGroupCast() ? castWithCards() : null;
  if (cast) {
    if (!cast.length) {
      el.textContent = "This scene has no character cards to describe.";
      return;
    }
    if (!cast.some((member) => member.id === memberId)) memberId = cast[0].id;
  } else {
    memberId = null;
  }
  try {
    const res = await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
      action: "get_profile",
      ...profileTarget(),
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
        ${cast ? memberPickerHtml(cast) : ""}
        <label>Positive prompt<textarea id="ig-appearance" placeholder="Permanent tags, fill with permanent traits (e.g. Hatsune Miku, black and white)">${esc(res.profile.appearance_prompt || "")}</textarea></label>
        <label>Negative prompt<textarea id="ig-profile-negative" placeholder="Describe what to avoid (e.g. 3D, color). Quality and scene-specific negatives are handled automatically.">${esc(res.profile.negative_prompt || "")}</textarea></label>
        <div class="ig-profile-reference">
          <span class="ig-profile-reference-label">Reference image</span>
          <span class="image-gen-note">Used by workflows with reference image slots.</span>
          <div id="ig-reference-host">${referenceImageHtml()}</div>
        </div>
      </div>`;
    loadedProfile = currentProfile();
  } catch {
    el.textContent = "Could not load character appearance.";
  }
}

export async function saveProfile() {
  const appearanceEl = document.getElementById("ig-appearance");
  if (!appearanceEl || !getActiveConvId()) return;
  const res = await api.post(convUrl(getActiveConvId(), "workflows", WORKFLOW_ID, "trigger"), {
    action: "set_profile",
    profile: currentProfile(),
    ...profileTarget(),
  });
  if (res?.warning) {
    toast(res.warning, "error");
    referenceImage = {
      reference_image_b64: res.profile?.reference_image_b64 || "",
      reference_mime: res.profile?.reference_mime || "",
    };
  }
}
