// Tracks recording state across the extension and reflects it on the toolbar icon.
// The actual capture/recording logic lives in popup.js (chrome.tabCapture.capture
// must be called from an extension view in direct response to a user gesture).

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ isRecording: false, recorderWindowId: null, activeSession: null });
  chrome.action.setBadgeBackgroundColor({ color: "#6E2A2A" });
});

// Popups (default_popup) auto-close the instant they lose focus - which is
// exactly what happens when the microphone permission dialog appears,
// cancelling the mic request as "dismissed". So instead of a popup, open
// the recorder UI as its own small persistent window that survives that
// permission prompt.
//
// recorderWindowId is persisted to chrome.storage (not just a local
// variable) because MV3 service workers can be killed and restarted by
// Chrome at any time - an in-memory-only value would silently reset to
// null on restart, breaking both the "focus the existing window" shortcut
// below and the crash-recovery check in onRemoved further down.
async function getRecorderWindowId() {
  const stored = await chrome.storage.local.get(["recorderWindowId"]);
  return stored.recorderWindowId ?? null;
}

async function setRecorderWindowId(id) {
  await chrome.storage.local.set({ recorderWindowId: id });
}

chrome.action.onClicked.addListener(async (clickedTab) => {
  // Remember which tab the user was on when they clicked the icon - this
  // is the tab we actually want to capture audio from. We pass its ID to
  // the recorder window explicitly, since tabCapture.getMediaStreamId
  // needs a specific target tab and can't rely on "whatever tab is
  // currently focused" once we're inside a separate window.
  const targetTabId = clickedTab.id;

  const existingWindowId = await getRecorderWindowId();
  if (existingWindowId !== null) {
    try {
      await chrome.windows.update(existingWindowId, { focused: true });
      return;
    } catch (e) {
      // Window no longer exists (closed some other way) - fall through and
      // open a fresh one.
      await setRecorderWindowId(null);
    }
  }

  const win = await chrome.windows.create({
    url: chrome.runtime.getURL(`popup.html?tabId=${targetTabId}`),
    type: "popup",
    width: 360,
    height: 520,
  });
  await setRecorderWindowId(win.id);
});

chrome.windows.onRemoved.addListener(async (closedWindowId) => {
  const stored = await chrome.storage.local.get(["recorderWindowId", "activeSession"]);

  if (stored.recorderWindowId === closedWindowId) {
    await chrome.storage.local.set({ recorderWindowId: null });

    // If a recording or an in-progress upload was still active when the
    // window closed - crash, accidental click, browser hiccup, whatever -
    // the audio captured so far was already being saved to IndexedDB as it
    // came in (see session-utils.js / popup.js). Open a small recovery
    // window to pick up where things left off: finish uploading, push to
    // CRM if that was enabled, and save the PDF locally if that was
    // enabled. A closed window doesn't mean a lost meeting.
    const session = stored.activeSession;
    if (session && session.phase !== "done") {
      chrome.windows.create({
        url: chrome.runtime.getURL(`recovery.html?sessionId=${encodeURIComponent(session.sessionId)}`),
        type: "popup",
        width: 360,
        height: 340,
      });
    }
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "SESSION_STATE") {
    // phase is one of: "recording" (mic/tab capture in progress),
    // "processing" (recording stopped, upload/summarize/CRM push under
    // way), or "done" (fully finished - safe to forget about this session).
    const phase = message.phase;

    chrome.storage.local.set({
      isRecording: phase === "recording",
      activeSession: phase === "done" ? null : { sessionId: message.sessionId, phase },
    });

    if (phase === "recording") {
      chrome.action.setBadgeText({ text: "REC" });
    } else {
      chrome.action.setBadgeText({ text: "" });
    }

    sendResponse({ ok: true });
  }

  // Keep the message channel open for async sendResponse usage if needed later.
  return true;
});
