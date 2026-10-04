import { registerActions } from "./actions.js";
import { onRefreshRequired } from "./api.js";
import { initAudioPlayer } from "./audio_transport.js";
import {
  initAutoscroll,
  initChatKeyNav,
  initChatSwipeNav,
  initWorkflowMutationListener,
  loadConversations,
  loadWorkflowManifest,
  refreshConversationMessages,
  renderMessages,
  toggleInspector,
} from "./chat.js";
import { initComposer } from "./chat_composer.js";
import { initDocumentMode, loadDocuments } from "./document.js";
import { handleExpressionPlaybackKey } from "./expression_playback.js";
import { initGroupSetup } from "./group_setup.js";
import { loadInteractiveFragments, loadMoodFragments } from "./library.js";
// Imported for the actions it registers.
import "./library_browser.js";
import { loadCharacters } from "./library_sidebar.js";
import { initWorldProposalActions, loadWorlds, setWorldProposalRefresh } from "./lorebooks.js";
import { initMessageHtmlActions } from "./message_html.js";
import { initMobileUi } from "./mobile.js";
// Imported for the actions it registers.
import "./presets.js";
import { initTheme, initThemeList, loadSettings, renderToolsPanel } from "./settings.js";
// Imported for the actions it registers.
import "./slop_score.js";
import { S } from "./state.js";
import { initTabLock } from "./tabLock.js";
import { $, fromMessageBody, initImageFallbacks } from "./utils.js";
import { loadWorkflowModules, preloadWorkflowModules } from "./workflow_loader.js";
import { initWorkflowTextInteraction } from "./workflow_text_interaction.js";

// Sidebar sections collapse from their header. A collapsed header that was
// pinned at the top scrolls back into view so its old offset does not hide it.
function initSidebarSections() {
  const scroller = document.querySelector("#sidebar .sidebar-scroll");
  if (!scroller) return;
  const markStuck = () => {
    const top = scroller.getBoundingClientRect().top;
    for (const header of scroller.querySelectorAll(".sidebar-section-header")) {
      const section = header.parentElement.getBoundingClientRect();
      header.classList.toggle("stuck", section.top < top && section.bottom > top);
    }
  };
  scroller.addEventListener("scroll", markStuck, { passive: true });
  registerActions("sidebar", {
    toggleSection: (header) => {
      header.querySelector(".arrow").classList.toggle("collapsed");
      const body = header.nextElementSibling;
      body.classList.toggle("collapsed");
      if (body.classList.contains("collapsed")) {
        const overshoot = scroller.getBoundingClientRect().top - header.getBoundingClientRect().top;
        if (overshoot > 0) scroller.scrollTop -= overshoot;
      }
      markStuck();
    },
  });
}

function toggleBurger() {
  $("burger-dropdown").classList.toggle("open");
}
function closeBurger() {
  $("burger-dropdown").classList.remove("open");
}

registerActions("menu", { toggleBurger: () => toggleBurger() });

// Clicking outside the menu closes it, and so does picking one of its items.
document.addEventListener("click", (e) => {
  if (e.target.closest("#burger-btn")) return;
  if (!e.target.closest("#burger-dropdown") || e.target.closest(".burger-menu-item")) closeBurger();
});

document.addEventListener("click", (e) => {
  const item = e.target.closest("[data-chat-action]");
  if (!item || fromMessageBody(item)) return;
  if (item.dataset.chatAction === "inspector") toggleInspector();
  else document.dispatchEvent(new CustomEvent(`${item.dataset.chatAction}-request`));
});

document.addEventListener("click", (e) => {
  const src = e.target.closest(".workflow-artifact-image");
  if (!src) return;
  const box = document.createElement("div");
  box.className = "image-lightbox";
  const big = document.createElement("img");
  big.src = src.src;
  big.alt = src.alt;
  box.appendChild(big);
  const onKey = (ev) => {
    if (ev.key === "Escape") close();
  };
  const close = () => {
    box.remove();
    document.removeEventListener("keydown", onKey);
  };
  box.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  document.body.appendChild(box);
});

initImageFallbacks();
initTheme();
initThemeList();
initMessageHtmlActions();
initComposer();
initChatKeyNav();
document.addEventListener("keydown", handleExpressionPlaybackKey);
initAutoscroll();
initChatSwipeNav();
initWorkflowTextInteraction();
initAudioPlayer();
initTabLock();
initWorkflowMutationListener();
initGroupSetup();
initSidebarSections();

if (!S.activeConvId) {
  renderMessages();
}

/** Run one startup load, logging a failure without stopping the others. */
async function startupStep(what, load) {
  try {
    await load();
  } catch (e) {
    console.error(`Failed to ${what}:`, e);
  }
}

async function initAll() {
  initMobileUi({ closeBurger });
  setWorldProposalRefresh(refreshConversationMessages);
  initWorldProposalActions();
  initDocumentMode();

  // Load independent lanes concurrently, preserving order within each lane.
  const manifest = startupStep("load workflow manifest", loadWorkflowManifest).then(preloadWorkflowModules);
  const settings = startupStep("load settings", loadSettings);
  await Promise.all([
    // Conversations first: loadCharacters fetches the (large) conversation list
    // itself when S.conversations is not set yet.
    startupStep("load conversations", loadConversations).then(() => startupStep("load characters", loadCharacters)),
    startupStep("load worlds", loadWorlds),
    // These render against state loadSettings fills in (the Agent switch and
    // enabled tools gate the fragment lists), so they wait for it.
    settings.then(() =>
      Promise.all([
        startupStep("load interactive fragments", loadInteractiveFragments),
        startupStep("load mood fragments", async () => {
          try {
            await loadMoodFragments();
          } catch (e) {
            $("frag-list").innerHTML =
              '<div style="color:var(--text-muted);font-size:12px;padding:4px 0;">Failed to load mood fragments</div>';
            throw e;
          }
        }),
        startupStep("load documents", loadDocuments),
        // Workflow modules register UI in the order they evaluate, so they still evaluate one at a time in manifest
        // order; the preload above has their entry files in flight already.
        manifest.then(() =>
          startupStep("load workflow modules", async () => {
            // Plug-ins register Tools panel cards as they evaluate.
            if (await loadWorkflowModules()) renderToolsPanel();
          }),
        ),
      ]),
    ),
  ]);
}

initAll();

onRefreshRequired(() => {
  const input = document.getElementById("chat-input");
  if (input && S.activeConvId) localStorage.setItem(`orb-chat-draft:${S.activeConvId}`, input.value);
  // The reload fires the real beforeunload, which flushes document drafts.
  window.location.reload();
});
