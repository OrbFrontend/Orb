import { memberById, S } from "./state.js";
import { avatarUrl, esc, escAttr } from "./utils.js";

export function memberFor(msg) {
  return msg?.speaker_member_id ? memberById(msg.speaker_member_id) : null;
}

export function speakerLabel(msg) {
  if (msg?.role === "user") return "You";
  if (!S.groupCast) return S.conversations.find((c) => c.id === S.activeConvId)?.character_name || "Character";
  if (!msg?.speaker_member_id) return "Summary";
  return memberFor(msg)?.display_name || "Unknown speaker";
}

export function castAvatars() {
  if (!S.groupCast) return "";
  return S.groupCast.members
    .map((member) => {
      const pinned = member.id === S.pinnedSpeakerId ? " pinned" : "";
      const speaking = member.id === S.currentSpeaker?.member_id ? " speaking" : "";
      const muted = member.muted ? " muted" : "";
      const avatar = member.character_card_id
        ? `<img src="${escAttr(avatarUrl(member.character_card_id))}" alt="">`
        : `<span>${member.member_kind === "narrator" ? "✒️" : "👤"}</span>`;
      return `<button type="button" class="cast-member${pinned}${speaking}${muted}" data-cast-member-id="${escAttr(member.id)}" ${member.muted ? "disabled" : ""} title="${escAttr(member.display_name)}">${avatar}<span>${esc(member.display_name)}</span></button>`;
    })
    .join("");
}

export function speakingPlanHtml() {
  if (!S.groupCast || S.speakingPlan == null) return "";
  if (!S.speakingPlan.length) return '<span class="scene-rests">The scene rests.</span>';
  return S.speakingPlan
    .map(
      (item, index) =>
        `<span class="plan-pill${index < (S.currentSpeaker?.index ?? -1) ? " done" : ""}${item.member_id === S.currentSpeaker?.member_id ? " active" : ""}">${esc(item.name)}${item.beat ? ` · ${esc(item.beat)}` : ""}</span>`,
    )
    .join("");
}
