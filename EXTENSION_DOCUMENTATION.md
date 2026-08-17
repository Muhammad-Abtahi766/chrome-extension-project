# Extension Documentation — "Meeting Summarizer & CRM Sync"

A Manifest V3 Chrome extension: records meeting tab audio, sends it to the local Python backend (`http://127.0.0.1:8000`) for transcription/summarization/CRM sync, and shows the result.

**Files covered:** `manifest.json`, `background.js`, `popup.html`, `popup.js`, `session-utils.js`, `recovery.html`, `recovery.js`.

---

## 1. `manifest.json`

```json
{
  "manifest_version": 3,
  "name": "Meeting Summarizer & CRM Sync",
  "version": "1.0.0",
  "description": "Records meeting tab audio, sends it to a local Python backend for transcription, summarization, and CRM sync.",
  "permissions": ["tabCapture", "activeTab", "storage", "scripting", "downloads", "unlimitedStorage"],
  "host_permissions": ["http://127.0.0.1:8000/*"],
  "background": { "service_worker": "background.js" },
  "action": { "default_title": "Meeting Summarizer" }
}
```

| Field | Meaning |
|---|---|
| `manifest_version: 3` | Uses the current Chrome extension platform (MV3) — background logic runs as a **service worker**, not a persistent background page. |
| `permissions.tabCapture` | Lets the extension capture audio from a specific browser tab (`chrome.tabCapture`). |
| `permissions.activeTab` | Grants temporary access to the currently active tab when the user interacts with the extension. |
| `permissions.storage` | Enables `chrome.storage.local` — used everywhere for settings, session state, and IDs. |
| `permissions.scripting` | Reserved for script-injection capability (declared, available if needed). |
| `permissions.downloads` | Needed for `chrome.downloads.download(...)` — used to save the generated PDF locally. |
| `permissions.unlimitedStorage` | Lifts the default storage quota — relevant since IndexedDB may hold hours of audio chunks per session. |
| `host_permissions` | Restricts network access to exactly the local backend (`127.0.0.1:8000`) — the extension can't silently call any other host. |
| `background.service_worker` | Registers `background.js` as the MV3 service worker (event-driven, can be killed/restarted by Chrome at any time — this matters, see Section 2). |
| `action.default_title` | Tooltip text on the toolbar icon. **Notably, there's no `default_popup`** — clicking the icon is instead handled entirely in code via `chrome.action.onClicked` (see Section 2), which is what allows opening a persistent window instead of an auto-closing popup. |

---

## 2. `background.js` — Service Worker

**Why this file exists at all, and why capture logic is NOT here:** `chrome.tabCapture.capture()` must be called from an extension view (like a popup or window) in direct response to a user gesture — it can't be called from a service worker. So `background.js` only handles orchestration: opening/tracking the recorder window, badge state, and crash recovery. The actual capture/recording logic lives in `popup.js`.

### Install-time setup
```javascript
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ isRecording: false, recorderWindowId: null, activeSession: null });
  chrome.action.setBadgeBackgroundColor({ color: "#6E2A2A" });
});
```
Runs once when the extension is installed/updated: initializes the three pieces of persistent state (`isRecording`, `recorderWindowId`, `activeSession`) and sets the toolbar badge's background color (a dark red, matching the UI theme) ahead of ever needing to show it.

### Why a persistent window instead of the default popup
Comment in the code explains the core design decision: MV3's `default_popup` auto-closes the instant it loses focus — and that's **exactly** what happens the moment the browser's microphone permission dialog appears, which cancels the mic request as "dismissed" before the user can even respond. The fix is to never use `default_popup` at all (see `manifest.json`, Section 1) and instead open the recorder UI as its own small persistent `type: "popup"` window via `chrome.windows.create`, which survives that permission prompt.

### `getRecorderWindowId()` / `setRecorderWindowId(id)`
```javascript
async function getRecorderWindowId() {
  const stored = await chrome.storage.local.get(["recorderWindowId"]);
  return stored.recorderWindowId ?? null;
}
async function setRecorderWindowId(id) {
  await chrome.storage.local.set({ recorderWindowId: id });
}
```
**Why persisted to storage instead of a plain JS variable:** MV3 service workers can be killed and restarted by Chrome at any time (e.g. after a period of inactivity). An in-memory-only variable would silently reset to `null` on restart, breaking both the "focus the existing window instead of opening a new one" shortcut below and the crash-recovery check in `onRemoved`. Persisting to `chrome.storage.local` makes this survive service worker restarts.

### `chrome.action.onClicked` — toolbar icon click
```javascript
chrome.action.onClicked.addListener(async (clickedTab) => {
  const targetTabId = clickedTab.id;

  const existingWindowId = await getRecorderWindowId();
  if (existingWindowId !== null) {
    try {
      await chrome.windows.update(existingWindowId, { focused: true });
      return;
    } catch (e) {
      await setRecorderWindowId(null);
    }
  }

  const win = await chrome.windows.create({
    url: chrome.runtime.getURL(`popup.html?tabId=${targetTabId}`),
    type: "popup", width: 360, height: 520,
  });
  await setRecorderWindowId(win.id);
});
```
- Only fires because there's no `default_popup` declared — otherwise Chrome would show the popup automatically instead of running this listener.
- `targetTabId = clickedTab.id` — remembers which tab the user was on **when they clicked the icon**. This matters because `tabCapture.getMediaStreamId` (used later in `popup.js`) needs a specific target tab and can't rely on "whatever tab is focused," which becomes ambiguous once the recorder is a separate window.
- If a recorder window is already tracked, it tries to just focus it (`chrome.windows.update(..., { focused: true })`) rather than opening a duplicate — avoiding multiple simultaneous recording windows.
- If that focus call throws (window was closed some other way, e.g. manually, so the ID is stale), it clears the stored ID and falls through to opening a fresh window.
- Otherwise, opens a new `360x520` popup-type window pointed at `popup.html`, passing `tabId` as a query param so `popup.js` knows which tab to capture, and stores the new window's ID.

### `chrome.windows.onRemoved` — crash recovery trigger
```javascript
chrome.windows.onRemoved.addListener(async (closedWindowId) => {
  const stored = await chrome.storage.local.get(["recorderWindowId", "activeSession"]);

  if (stored.recorderWindowId === closedWindowId) {
    await chrome.storage.local.set({ recorderWindowId: null });

    const session = stored.activeSession;
    if (session && session.phase !== "done") {
      chrome.windows.create({
        url: chrome.runtime.getURL(`recovery.html?sessionId=${encodeURIComponent(session.sessionId)}`),
        type: "popup", width: 360, height: 340,
      });
    }
  }
});
```
Fires whenever **any** browser window closes; the listener filters to only care if it was specifically the tracked recorder window (`stored.recorderWindowId === closedWindowId`).
- Clears the stored window ID either way (it's gone now).
- **The actual recovery trigger:** if there was an `activeSession` and its `phase` wasn't `"done"` — meaning a recording or an in-progress upload was still active when the window closed (crash, accidental click, browser hiccup) — it opens a small `recovery.html` popup window, passing the session ID. The comment in the code frames the intent well: *"A closed window doesn't mean a lost meeting"* — because audio was already being incrementally saved to IndexedDB as it was captured (see `session-utils.js`, Section 5), so `recovery.html` can pick up where things left off.

### `chrome.runtime.onMessage` — session state tracking
```javascript
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "SESSION_STATE") {
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
  return true;
});
```
- Listens for `SESSION_STATE` messages sent from `popup.js` (via `notifySessionState()`, Section 4) whenever the recording lifecycle changes phase.
- `phase` is one of three values: `"recording"` (mic/tab capture in progress), `"processing"` (recording stopped, upload/summarize/CRM push under way), or `"done"` (fully finished, safe to forget).
- Updates two pieces of persistent state: `isRecording` (a simple boolean) and `activeSession` (the full session object, or `null` once `"done"` — this is exactly what `onRemoved` above checks to decide whether recovery is needed).
- Shows a red "REC" badge on the toolbar icon while actively recording; clears it for any other phase.
- `return true` at the end keeps the message channel open for potential async `sendResponse` usage — a standard MV3 pattern even though this particular handler responds synchronously.

---

## 3. `popup.html` — Recorder Window UI

Structure (not a "popup" in the MV3 auto-closing sense — it's the content of the persistent window `background.js` opens):

- **Header row**: title + a gear (`⚙`) settings button (`#settingsBtn`).
- **Settings overlay** (`#settingsOverlay` → `#settingsPanel`): a slide-in panel containing:
  - HubSpot connection status text + "Connect HubSpot" button (`#crmRow`).
  - "Save summary as PDF on this computer" checkbox (`#saveLocallyRow`).
  - Groq API key input (`#groqKeyInput`, `type="password"`, marked **required**) with hint text and a "Saved." confirmation note that fades in after saving.
- **`#groqKeyMissingBanner`**: hidden by default; shown when no Groq key is set, blocking recording, with an "Open Settings" shortcut button.
- **`#recoveryBanner`**: hidden by default; shown when `popup.js` detects an unfinished prior session, with a "Recover it now" button.
- **`#status`**: main status line, updated throughout the recording/upload lifecycle.
- **`#micRow`**: a `<select>` (`#micSelect`) populated dynamically with real microphone device names.
- **`#timer`**: `MM:SS` elapsed-recording display.
- **Buttons**: `#startBtn` ("Start recording", disabled until a Groq key is present) and `#stopBtn` ("Stop & process", disabled until recording starts).
- **`#result`**: hidden by default; shows the final summary text plus CRM/PDF status after processing completes.

Two scripts are loaded in order at the bottom: `session-utils.js` then `popup.js` — order matters because `popup.js` calls functions (`saveChunkToDB`, `loadChunksFromDB`, `uploadRecording`, etc.) defined in `session-utils.js`.

Visual styling is a dark navy/cream theme (`#1c2b39` background, `#f5f0e6` text) with maroon/gold accent buttons — purely cosmetic, no functional impact.

---

## 4. `popup.js` — Recording, Upload, and Settings Logic

### Element references (top of file)
```javascript
const BACKEND_BASE = "http://127.0.0.1:8000";
const BACKEND_URL = `${BACKEND_BASE}/process-meeting`;

const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const statusEl = document.getElementById("status");
const timerEl = document.getElementById("timer");
const resultEl = document.getElementById("result");
const micSelectEl = document.getElementById("micSelect");
const crmStatusTextEl = document.getElementById("crmStatusText");
const connectHubspotBtn = document.getElementById("connectHubspotBtn");
const saveLocallyCheckbox = document.getElementById("saveLocallyCheckbox");
const recoveryBanner = document.getElementById("recoveryBanner");
const recoverBtn = document.getElementById("recoverBtn");
const groqKeyInput = document.getElementById("groqKeyInput");
const groqKeySavedNote = document.getElementById("groqKeySavedNote");
const groqKeyMissingBanner = document.getElementById("groqKeyMissingBanner");
const openSettingsForKeyBtn = document.getElementById("openSettingsForKeyBtn");
const settingsBtn = document.getElementById("settingsBtn");
const settingsOverlay = document.getElementById("settingsOverlay");
const settingsPanel = document.getElementById("settingsPanel");
const settingsCloseBtn = document.getElementById("settingsCloseBtn");
```
Grabs a reference to every interactive/status element in `popup.html` once, up front. *(Note: the `settingsBtn`/`settingsOverlay`/`settingsPanel`/`settingsCloseBtn` declarations shown here are the corrected version — these four were previously missing, which caused a `ReferenceError` on load; see the fix applied earlier in this conversation.)*

### Settings panel open/close
```javascript
settingsBtn.addEventListener("click", () => settingsOverlay.classList.add("open"));
settingsCloseBtn.addEventListener("click", () => settingsOverlay.classList.remove("open"));
openSettingsForKeyBtn.addEventListener("click", () => {
  settingsOverlay.classList.add("open");
  groqKeyInput.focus();
});
settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) settingsOverlay.classList.remove("open");
});
```
- Gear icon and close (`×`) button toggle the `.open` class (which controls visibility via CSS).
- The "Open Settings" link inside the missing-key banner both opens the panel AND focuses the key input directly, so the user can start typing immediately.
- Clicking the dimmed backdrop closes the panel too — but only when the click target is the backdrop itself (`e.target === settingsOverlay`), not a click that merely bubbles up from something inside the panel.

### Groq key: load, debounced save, and Start-button gating
```javascript
function updateStartAvailability() {
  const hasKey = groqKeyInput.value.trim().length > 0;
  groqKeyMissingBanner.style.display = hasKey ? "none" : "block";
  if (stopBtn.disabled) {
    startBtn.disabled = !hasKey;
  }
}
```
- Central function keeping the Start button's enabled state and the missing-key banner in sync with whether a key is currently typed in.
- `if (stopBtn.disabled)` guard: `stopBtn` is only enabled while actively recording. This check prevents `updateStartAvailability()` from fighting an in-progress recording/processing state — it only ever toggles `startBtn` when we're NOT mid-recording.

```javascript
let groqKeySaveTimeout = null;
chrome.storage.local.get(["userGroqApiKey"], (result) => {
  groqKeyInput.value = result.userGroqApiKey || "";
  updateStartAvailability();
});
groqKeyInput.addEventListener("input", () => {
  updateStartAvailability();
  clearTimeout(groqKeySaveTimeout);
  groqKeySavedNote.style.display = "none";
  groqKeySaveTimeout = setTimeout(() => {
    const value = groqKeyInput.value.trim();
    chrome.storage.local.set({ userGroqApiKey: value }, () => {
      groqKeySavedNote.style.display = "block";
    });
  }, 500);
});
```
- On load, restores any previously saved key from `chrome.storage.local` into the input field, then immediately runs `updateStartAvailability()`.
- On every keystroke: re-checks availability instantly (so the Start button reacts immediately), hides the "Saved." note (since the value is now different from what's stored), and **debounces the actual storage write by 500ms** — `clearTimeout` cancels any pending save from the previous keystroke, so rapid typing only results in one storage write 500ms after the user pauses, not one write per character.

### Save-locally checkbox persistence
```javascript
chrome.storage.local.get(["saveLocallyPref"], (result) => {
  saveLocallyCheckbox.checked = Boolean(result.saveLocallyPref);
});
saveLocallyCheckbox.addEventListener("change", () => {
  chrome.storage.local.set({ saveLocallyPref: saveLocallyCheckbox.checked });
});
```
Same pattern as the mic selection (below) and the Groq key — restore on load, persist on change — so the user's preference carries across popup-window opens without re-checking it every meeting.

### `checkForRecoverableSession()`
```javascript
async function checkForRecoverableSession() {
  const stored = await chrome.storage.local.get(["activeSession"]);
  const session = stored.activeSession;
  if (session && session.phase && session.phase !== "done" && session.sessionId !== currentSessionId) {
    recoveryBanner.style.display = "block";
    recoverBtn.onclick = async () => {
      const groqKey = groqKeyInput.value.trim();
      if (!groqKey) {
        setStatus("Enter your Groq API key in Settings before recovering.");
        groqKeyMissingBanner.style.display = "block";
        settingsOverlay.classList.add("open");
        groqKeyInput.focus();
        return;
      }
      recoverBtn.disabled = true;
      recoverBtn.textContent = "Recovering...";
      setStatus("Recovering unfinished recording from earlier...");

      const chunks = await loadChunksFromDB(session.sessionId).catch(() => []);
      if (!chunks || chunks.length === 0) {
        setStatus("No saved audio found for that session.");
        await chrome.storage.local.set({ activeSession: null });
        recoveryBanner.style.display = "none";
        return;
      }

      const blob = new Blob(chunks, { type: "audio/webm" });
      const result = await uploadRecording(blob, {
        backendUrl: BACKEND_URL, userId: currentUserId,
        saveLocally: saveLocallyCheckbox.checked, groqApiKey: groqKeyInput.value.trim(),
        onStatus: setStatus,
      });

      if (result.success) {
        await deleteSessionFromDB(session.sessionId);
        await chrome.storage.local.set({ activeSession: null });
        recoveryBanner.style.display = "none";
        setStatus("Recovered previous recording successfully.");
        resultEl.style.display = "block";
        resultEl.textContent = "Summary:\n" + result.data.summary;
      } else {
        setStatus("Recovery failed: " + result.error + ". Your audio is still saved - try again later.");
        recoverBtn.disabled = false;
        recoverBtn.textContent = "Recover it now";
      }
    };
  }
}
```
**A second, manual safety net** on top of `background.js`'s automatic recovery window — covers the case where the auto-recovery window itself somehow didn't open, or the user just reopened the extension normally and there's leftover unfinished work.
- Only shows the banner if there's a genuinely unfinished session (`phase !== "done"`) that ISN'T the session currently in progress in *this* window (`session.sessionId !== currentSessionId` — avoids showing a "recover" banner for the very recording that's actively happening right now).
- The click handler re-validates the Groq key is present (same gate as starting a fresh recording), loads whatever chunks were saved to IndexedDB, and if none exist, just clears the stale session record.
- Otherwise reassembles the chunks into a `Blob` and runs it through the exact same `uploadRecording()` helper used by the normal flow (Section 5) — **no separate/duplicate upload logic**, so recovery and normal completion can't drift apart in behavior.
- On success: deletes the IndexedDB backup (no longer needed), clears the session record, hides the banner, and shows the recovered summary inline.
- On failure: re-enables the button so the user can retry, and explicitly reassures that the audio is still safely saved.

### User identity
```javascript
let currentUserId = null;
async function getOrCreateUserId() {
  const stored = await chrome.storage.local.get(["meetingSummarizerUserId"]);
  if (stored.meetingSummarizerUserId) {
    return stored.meetingSummarizerUserId;
  }
  const newId = crypto.randomUUID();
  await chrome.storage.local.set({ meetingSummarizerUserId: newId });
  return newId;
}
```
Generates a UUID **once per browser profile** and reuses it forever after — this is the extension's own permanent identity for the person using it, distinct from any HubSpot login. It's what lets the backend recognize "this recording belongs to the same person who connected HubSpot earlier" across every session (this is the `user_id` sent with every backend request, matching `main.py`'s per-user token lookup).

### HubSpot connection status
```javascript
async function refreshHubspotConnectionStatus() {
  try {
    const res = await fetch(`${BACKEND_BASE}/oauth/status?user_id=${encodeURIComponent(currentUserId)}`);
    const data = await res.json();
    if (data.connected) {
      crmStatusTextEl.textContent = "HubSpot connected ✅";
      crmStatusTextEl.className = "connected";
      connectHubspotBtn.classList.add("hidden");
    } else {
      crmStatusTextEl.textContent = "HubSpot not connected";
      crmStatusTextEl.className = "notConnected";
      connectHubspotBtn.classList.remove("hidden");
    }
  } catch (err) {
    crmStatusTextEl.textContent = "Can't reach backend to check HubSpot status";
    crmStatusTextEl.className = "notConnected";
    connectHubspotBtn.classList.remove("hidden");
  }
}
```
Calls the backend's `/oauth/status` endpoint and reflects the result in the settings panel's status line, toggling visibility of the "Connect HubSpot" button accordingly. If the fetch itself fails (backend not running, unreachable), it degrades gracefully — shows a distinct "Can't reach backend" message rather than a misleading "not connected," and doesn't block anything else (recording still works either way).

```javascript
connectHubspotBtn.addEventListener("click", () => {
  chrome.tabs.create({
    url: `${BACKEND_BASE}/oauth/connect?user_id=${encodeURIComponent(currentUserId)}`,
  });
});
window.addEventListener("focus", refreshHubspotConnectionStatus);
```
- Clicking "Connect HubSpot" opens the backend's `/oauth/connect` URL (tagged with this user's ID) in a **new browser tab** — that's where the real HubSpot login/consent screen runs (see `main.py` Section 6).
- Re-checks connection status whenever this window **regains focus** — so after the user finishes the HubSpot login flow in the other tab and clicks back, the "Connect HubSpot" button disappears automatically without a manual refresh.

### Startup sequence
```javascript
(async () => {
  currentUserId = await getOrCreateUserId();
  await refreshHubspotConnectionStatus();
  await checkForRecoverableSession();
})();
```
An immediately-invoked async function that runs the three initialization steps in order the moment the script loads: establish identity → check CRM connection → check for anything left unfinished from before.

### Recording state variables
```javascript
let mediaRecorder = null;
let recordedChunks = [];
let capturedStream = null;   // tab audio (what plays out of the meeting)
let micStream = null;        // your own microphone
let audioContext = null;
let timerInterval = null;
let recordingStartTime = null;
let currentSessionId = null;
let pendingFlushChunks = [];
let flushIndex = 0;
const FLUSH_EVERY_N_CHUNKS = 10; // ~10s of audio per IndexedDB write
```
- `currentSessionId` identifies one recording end-to-end (start click → fully processed) — used as the IndexedDB key so audio can be found again by this same window normally, or by a recovery window if this one closes unexpectedly.
- `pendingFlushChunks` / `flushIndex` track chunks not yet written to IndexedDB and the running batch-order index within the session.
- `FLUSH_EVERY_N_CHUNKS = 10`, combined with the 1-second `MediaRecorder` timeslice (see below), means roughly every 10 seconds of audio gets written to IndexedDB as one batch.

### `populateMicList()`
```javascript
async function populateMicList() {
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    tempStream.getTracks().forEach((track) => track.stop());

    const devices = await navigator.mediaDevices.enumerateDevices();
    const mics = devices.filter((d) => d.kind === "audioinput");

    micSelectEl.innerHTML = "";
    if (mics.length === 0) {
      micSelectEl.innerHTML = '<option value="">No microphone found</option>';
      return;
    }
    mics.forEach((mic, i) => {
      const opt = document.createElement("option");
      opt.value = mic.deviceId;
      opt.textContent = mic.label || `Microphone ${i + 1}`;
      micSelectEl.appendChild(opt);
    });

    chrome.storage.local.get(["preferredMicId"], (result) => {
      const savedId = result.preferredMicId;
      if (savedId && mics.some((m) => m.deviceId === savedId)) {
        micSelectEl.value = savedId;
      }
    });
  } catch (err) {
    micSelectEl.innerHTML = '<option value="">Could not list microphones</option>';
    setStatus("Could not access microphone list: " + err.message);
  }
}
micSelectEl.addEventListener("change", () => {
  chrome.storage.local.set({ preferredMicId: micSelectEl.value });
});
populateMicList();
```
**Why it requests a throwaway mic stream first:** Chrome hides real device labels (`mic.label`) from `enumerateDevices()` until microphone *permission* has actually been granted — without it, you'd just see generic unlabeled entries. So this grabs a temporary stream purely to trigger the permission prompt/unlock labels, immediately stops its tracks (`tempStream.getTracks().forEach(track => track.stop())` — releasing the mic right away since this stream isn't the one that will actually be recorded), then lists the real devices.
- Filters to `audioinput` devices only.
- Populates the `<select>` with real device labels (falling back to a generic "Microphone N" if a label is somehow still blank).
- Restores the last-used mic selection from storage if that device is still present in the current list; otherwise leaves the browser's default selection.
- Saves the choice to `chrome.storage.local` on every change so it persists across sessions.
- **Why this matters at all:** the code comment explains that `getUserMedia({audio:true})` alone just grabs whatever the OS treats as default — which is often NOT the mic the user actually selected inside Google Meet/Zoom's own device settings (e.g. a headset vs. the laptop's built-in mic). Explicitly listing and selecting avoids that mismatch.

### Small utility functions
```javascript
function setStatus(text) { statusEl.textContent = text; }

function formatElapsed(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function startTimer() {
  recordingStartTime = Date.now();
  timerInterval = setInterval(() => {
    timerEl.textContent = formatElapsed(Date.now() - recordingStartTime);
  }, 500);
}
function stopTimer() {
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
}

function notifySessionState(phase) {
  chrome.runtime.sendMessage({ type: "SESSION_STATE", sessionId: currentSessionId, phase });
}
```
- `formatElapsed`: converts milliseconds into a zero-padded `MM:SS` string.
- `startTimer`/`stopTimer`: drive the on-screen timer, updating every 500ms (twice per second, smoother than once per second while still cheap).
- `notifySessionState(phase)`: the sending half of the `SESSION_STATE` messaging protocol handled in `background.js` — every phase transition (`"recording"`, `"processing"`, `"done"`) gets broadcast so the badge and crash-recovery tracking stay accurate.

### Starting a recording — `startBtn` click handler
```javascript
startBtn.addEventListener("click", () => {
  const groqKey = groqKeyInput.value.trim();
  if (!groqKey) {
    setStatus("Enter your Groq API key in Settings before recording.");
    groqKeyMissingBanner.style.display = "block";
    settingsOverlay.classList.add("open");
    groqKeyInput.focus();
    return;
  }

  startBtn.disabled = true;
  recoveryBanner.style.display = "none";
  setStatus("Requesting tab audio...");

  const params = new URLSearchParams(window.location.search);
  const targetTabId = parseInt(params.get("tabId"), 10);

  if (!targetTabId) {
    setStatus("Could not determine which tab to record. Close this window and click the extension icon again from your Meet/Zoom tab.");
    startBtn.disabled = false;
    return;
  }
  ...
```
- First gate: no Groq key → block recording entirely, show the missing-key banner, open settings, and focus the field — recording literally cannot start without a key (matches the backend's hard `400` rejection for a missing key, so the failure is caught here instead of after a wasted recording).
- Reads `tabId` back out of the URL query string that `background.js` set when it opened this window (`popup.html?tabId=...`) — this is how the popup knows which tab to target, since it can't just assume "the currently focused tab."
- If `tabId` is somehow missing/invalid, fails clearly with instructions to close and retry from the meeting tab.

```javascript
  chrome.tabCapture.getMediaStreamId({ targetTabId }, (streamId) => {
    if (chrome.runtime.lastError || !streamId) {
      setStatus("Could not capture tab audio: " + (chrome.runtime.lastError ? chrome.runtime.lastError.message : "unknown error") + ". Make sure that tab is still open.");
      startBtn.disabled = false;
      return;
    }

    navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    }).then((tabStream) => {
      capturedStream = tabStream;
      setStatus("Requesting microphone access...");
      ...
```
- `chrome.tabCapture.getMediaStreamId({ targetTabId }, ...)` — **targets a specific tab by ID**, unlike the older `tabCapture.capture()` API which only works on whichever tab is currently focused (not useful once recording happens in a separate window, since the meeting tab won't be "focused" anymore).
- Uses the returned `streamId` inside a special `getUserMedia` call with `chromeMediaSource: "tab"` — this is the standard MV3 pattern for turning a tab-capture stream ID into an actual `MediaStream`.
- Any failure here (permission denied, tab closed) surfaces a clear status message and re-enables the Start button.

```javascript
      const selectedMicId = micSelectEl.value;
      const micConstraints = selectedMicId ? { audio: { deviceId: { exact: selectedMicId } } } : { audio: true };

      return navigator.mediaDevices.getUserMedia(micConstraints).then((userMicStream) => {
        micStream = userMicStream;
        audioContext = new AudioContext();

        const ensureRunning = audioContext.state === "running" ? Promise.resolve() : audioContext.resume();

        return ensureRunning.then(() => {
          const mixedDestination = audioContext.createMediaStreamDestination();

          const tabSource = audioContext.createMediaStreamSource(tabStream);
          tabSource.connect(mixedDestination);
          tabSource.connect(audioContext.destination);

          const micSource = audioContext.createMediaStreamSource(micStream);
          micSource.connect(mixedDestination);
          ...
```
- Requests the user's own mic, using the specific device chosen in the dropdown if one was selected (`deviceId: { exact: ... } }`), otherwise the OS default.
- **`AudioContext` suspended-state check is important and deliberate:** a fresh `AudioContext` can start in a `"suspended"` state — especially likely here since this code runs several `.then()` hops away from the original click, so the browser may not treat it as directly tied to the user gesture anymore. A suspended context processes **no audio at all** — `MediaRecorder` would still produce a normal-looking file, just filled with silence, which would be a very confusing silent failure. `ensureRunning` forces `audioContext.resume()` if needed before wiring anything up.
- **Audio mixing via Web Audio API:**
  - `mixedDestination` — a virtual destination node that becomes the actual `MediaStream` fed to `MediaRecorder`.
  - Tab audio is connected to BOTH `mixedDestination` (so it's recorded) AND `audioContext.destination` (so it's also still routed to the speakers — otherwise the user would record fine but hear silence during their own meeting).
  - Mic audio is connected ONLY to `mixedDestination` (recorded) and deliberately NOT to `audioContext.destination` — routing it back to speakers too would create an audible echo of the user's own voice.

```javascript
          recordedChunks = [];
          currentSessionId = crypto.randomUUID();
          pendingFlushChunks = [];
          flushIndex = 0;

          mediaRecorder = new MediaRecorder(mixedDestination.stream, { mimeType: "audio/webm;codecs=opus" });

          mediaRecorder.ondataavailable = (event) => {
            if (event.data && event.data.size > 0) {
              recordedChunks.push(event.data);
              pendingFlushChunks.push(event.data);

              if (pendingFlushChunks.length >= FLUSH_EVERY_N_CHUNKS) {
                const toFlush = pendingFlushChunks;
                pendingFlushChunks = [];
                const batchBlob = new Blob(toFlush, { type: "audio/webm" });
                const thisFlushIndex = flushIndex++;
                saveChunkToDB(currentSessionId, thisFlushIndex, batchBlob).catch((e) => {
                  console.warn("Failed to persist audio chunk to IndexedDB:", e);
                });
              }
            }
          };
```
- A brand-new `currentSessionId` (UUID) is generated for every recording — this is the key used for both the in-memory chunk array and the IndexedDB backup.
- `MediaRecorder` is created against the **mixed** stream (tab + mic combined), using the `opus` codec inside a `webm` container.
- `ondataavailable` fires periodically (driven by the `mediaRecorder.start(1000)` timeslice below, i.e. roughly once per second) with a small chunk of audio data:
  - Every chunk is kept in the full in-memory `recordedChunks` array (used to build the final upload blob when recording stops normally).
  - Every chunk is ALSO added to `pendingFlushChunks`, a separate buffer that gets periodically written to IndexedDB.
  - Once `pendingFlushChunks` reaches `FLUSH_EVERY_N_CHUNKS` (10, i.e. roughly every 10 seconds of audio given the 1-second timeslice), those chunks are batched into one `Blob` and saved to IndexedDB via `saveChunkToDB()` (defined in `session-utils.js`) under an incrementing `flushIndex`. Batching into groups of 10 (rather than writing every single 1-second chunk individually) keeps the number of IndexedDB writes reasonable across a multi-hour recording.
  - Any IndexedDB write failure is caught and logged (`console.warn`) but doesn't interrupt the recording itself — the in-memory copy is still intact for the normal (non-crash) path.

```javascript
          mediaRecorder.onstop = async () => {
            if (pendingFlushChunks.length > 0) {
              const batchBlob = new Blob(pendingFlushChunks, { type: "audio/webm" });
              pendingFlushChunks = [];
              const thisFlushIndex = flushIndex++;
              try {
                await saveChunkToDB(currentSessionId, thisFlushIndex, batchBlob);
              } catch (e) {
                console.warn("Failed to persist final audio chunk to IndexedDB:", e);
              }
            }
            handleRecordingStop();
          };

          mediaRecorder.onerror = (event) => {
            setStatus("Recording error: " + event.error.message);
          };

          mediaRecorder.start(1000);
          startTimer();
          notifySessionState("recording");

          setStatus("Recording meeting audio (tab + mic)...");
          stopBtn.disabled = false;
        });
      });
    })
    .catch((err) => {
      setStatus("Could not access microphone: " + err.message + ". Recording will only include tab audio, or you can allow mic access and try again.");
      if (capturedStream) { capturedStream.getTracks().forEach((track) => track.stop()); }
      startBtn.disabled = false;
    });
  });
});
```
- `onstop`: before moving on, flushes whatever's left in `pendingFlushChunks` (less than a full batch of 10) so the IndexedDB backup is complete right up to the moment "Stop" was clicked — then calls `handleRecordingStop()` to kick off the upload pipeline.
- `onerror`: surfaces any `MediaRecorder`-level error directly in the status line.
- `mediaRecorder.start(1000)` — starts recording with a **1-second timeslice**, meaning `ondataavailable` fires roughly every second rather than only once at the very end. This is essential for both the periodic IndexedDB flushing above and for keeping memory usage sane during long recordings.
- Notifies `background.js` of the `"recording"` phase (turns on the "REC" badge, per Section 2) and updates the UI (enables Stop, disables further Start clicks implicitly via the earlier `startBtn.disabled = true`).
- The outer `.catch()` handles a **mic access denial** specifically — note this is scoped so that if the mic fails but tab capture already succeeded, the message explicitly tells the user recording will continue with tab-only audio... though as written, this catch actually aborts entirely (`startBtn.disabled = false` re-enables Start) rather than proceeding tab-only; the message describes the intent but the current code path stops rather than degrading gracefully. If the mic step fails, `capturedStream`'s tracks are stopped to release the tab-capture resource cleanly.

### Stopping a recording
```javascript
stopBtn.addEventListener("click", () => {
  stopBtn.disabled = true;
  setStatus("Stopping recording...");
  stopTimer();
  notifySessionState("processing");

  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  if (capturedStream) { capturedStream.getTracks().forEach((track) => track.stop()); }
  if (micStream) { micStream.getTracks().forEach((track) => track.stop()); micStream = null; }
  if (audioContext) { audioContext.close(); audioContext = null; }
});
```
- Immediately disables the Stop button (prevents double-clicks) and stops the visible timer.
- **Notifies `"processing"` phase BEFORE the upload actually starts** — deliberate: the capture is done, but the upload/summarize/CRM-push pipeline hasn't happened yet, so the session must stay marked as in-flight (not `"done"`) so that a crash/close during the upload itself still triggers recovery.
- Stops the `MediaRecorder` (triggering its `onstop` handler above), and releases all underlying media resources: tab capture tracks, mic tracks, and closes the `AudioContext` — proper cleanup so the tab's microphone/capture indicators turn off and resources aren't leaked.

### `handleRecordingStop()` — the actual upload
```javascript
async function handleRecordingStop() {
  setStatus("Preparing audio for upload...");
  const blob = new Blob(recordedChunks, { type: "audio/webm" });
  recordedChunks = [];

  if (blob.size === 0) {
    setStatus("No audio was captured. Try again on an active meeting tab.");
    startBtn.disabled = false;
    notifySessionState("done");
    return;
  }

  resultEl.style.display = "none";

  const result = await uploadRecording(blob, {
    backendUrl: BACKEND_URL, userId: currentUserId,
    saveLocally: saveLocallyCheckbox.checked, groqApiKey: groqKeyInput.value.trim(),
    onStatus: setStatus,
  });

  if (result.success) {
    const data = result.data;
    let pdfNote = "";
    if (data.pdf) {
      if (data.pdf.data_base64) {
        pdfNote = data.pdf.download_error
          ? `\n\nCouldn't save the PDF locally: ${data.pdf.download_error}`
          : `\n\nSaved locally as "${data.pdf.filename}".`;
      } else if (data.pdf.error) {
        pdfNote = `\n\nLocal PDF save failed: ${data.pdf.error}`;
      }
    }
    setStatus("Done. Summary generated and sent to CRM.");
    resultEl.style.display = "block";
    resultEl.textContent = "Summary:\n" + data.summary + "\n\nCRM push status: " + (data.crm_push ? data.crm_push.status : "unknown") + pdfNote;

    await deleteSessionFromDB(currentSessionId).catch(() => {});
    notifySessionState("done");
  } else {
    setStatus("Failed to process meeting: " + result.error + ". The recorded audio is still saved and safe - reopen the extension to recover it.");
  }

  startBtn.disabled = false;
  timerEl.textContent = "00:00";
}
```
- Assembles the complete recording into one `Blob` from the in-memory `recordedChunks` array (the fast/normal path — IndexedDB is only consulted during recovery, not on a normal successful finish).
- Zero-size guard: if somehow nothing was captured, tells the user, marks the session `"done"` (nothing to recover), and stops here.
- Delegates the actual upload to the shared `uploadRecording()` helper (Section 5) — passing the current user ID, the save-locally preference, and the Groq key.
- On success: builds a friendly status message combining the summary text, CRM push status, and (if requested) a note about whether the local PDF save succeeded or failed — then cleans up the now-unneeded IndexedDB backup and marks the session `"done"`.
- On failure: explicitly reassures the user the audio is still safe and can be recovered by reopening the extension — **and deliberately does NOT mark the session `"done"` or delete the IndexedDB backup here** — this is what allows `background.js`'s crash-recovery (if this window later closes) or the manual recovery banner (Section 4) to still retry the same upload later.
- Either way, re-enables Start and resets the timer display back to `00:00`.

---

## 5. `session-utils.js` — Shared IndexedDB + Upload Logic

Loaded by both `popup.html` and `recovery.html` — this is the file that guarantees the normal recording flow and the crash-recovery flow behave **identically** when it comes to storing and uploading audio, so there's no risk of the two paths drifting apart in behavior.

**Why IndexedDB at all:** for a 2-3 hour meeting, holding the entire recording only in a JS array in the popup window's memory is risky — if that window closes (crash, accidental click, laptop sleep interrupting it, etc.) before the upload finishes, the whole recording is gone. IndexedDB is per-extension-origin storage, readable from any extension page — including a recovery window opened later by `background.js` after an unexpected close.

```javascript
const DB_NAME = "meetingSummarizerDB";
const DB_VERSION = 1;
const STORE_NAME = "audioChunks";
```

### `openChunkDB()`
```javascript
function openChunkDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        const store = db.createObjectStore(STORE_NAME, { keyPath: "id", autoIncrement: true });
        store.createIndex("sessionId", "sessionId", { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
```
Wraps the callback-based IndexedDB API in a Promise.
- `onupgradeneeded` fires the first time the database is created (or the version number changes): creates the `audioChunks` object store with an auto-incrementing `id` primary key, plus a **non-unique** index on `sessionId` — this index is what lets `loadChunksFromDB` efficiently fetch all chunks belonging to one specific session without scanning the entire store.

### `saveChunkToDB(sessionId, chunkIndex, blob)`
```javascript
async function saveChunkToDB(sessionId, chunkIndex, blob) {
  const db = await openChunkDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).add({ sessionId, chunkIndex, blob, savedAt: Date.now() });
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    db.close();
  });
}
```
Adds one batched blob record, tagged with its `sessionId` and `chunkIndex`. **`chunkIndex` must increase monotonically within a session** — this is what lets the chunks be reassembled in the correct chronological order later, since IndexedDB doesn't otherwise guarantee retrieval order matches insertion order across an index query.

### `loadChunksFromDB(sessionId)`
```javascript
async function loadChunksFromDB(sessionId) {
  const db = await openChunkDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const index = tx.objectStore(STORE_NAME).index("sessionId");
    const req = index.getAll(IDBKeyRange.only(sessionId));
    req.onsuccess = () => {
      const rows = req.result.sort((a, b) => a.chunkIndex - b.chunkIndex);
      resolve(rows.map((r) => r.blob));
    };
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => db.close();
  });
}
```
Uses the `sessionId` index to fetch only the rows belonging to this session, then **explicitly sorts by `chunkIndex`** before returning just the blobs — this sort is what guarantees correct chronological reassembly regardless of the underlying storage/retrieval order.

### `deleteSessionFromDB(sessionId)`
```javascript
async function deleteSessionFromDB(sessionId) {
  const db = await openChunkDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    const index = tx.objectStore(STORE_NAME).index("sessionId");
    const req = index.openCursor(IDBKeyRange.only(sessionId));
    req.onsuccess = (e) => {
      const cursor = e.target.result;
      if (cursor) { cursor.delete(); cursor.continue(); }
    };
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => { db.close(); resolve(); };
  });
}
```
Uses a cursor over the `sessionId` index to walk through and delete every row belonging to that session one at a time (`cursor.delete()` then `cursor.continue()` to advance to the next match), since IndexedDB doesn't offer a bulk "delete where" operation on an index range directly. Resolves once the whole transaction completes.

### `uploadRecording(blob, options)`
```javascript
async function uploadRecording(blob, { backendUrl, userId, saveLocally, groqApiKey, onStatus }) {
  if (!blob || blob.size === 0) {
    return { success: false, error: "No audio data to process." };
  }

  const formData = new FormData();
  formData.append("file", blob, `meeting-${Date.now()}.webm`);
  formData.append("user_id", userId);
  formData.append("save_locally", saveLocally ? "true" : "false");
  formData.append("groq_api_key", groqApiKey || "");

  if (onStatus) onStatus("Uploading and processing (this can take a while for long meetings)...");

  try {
    const response = await fetch(backendUrl, { method: "POST", body: formData });
    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({}));
      throw new Error(errorBody.detail || `Server returned ${response.status}`);
    }
    const data = await response.json();

    if (data.pdf && data.pdf.data_base64) {
      try {
        await chrome.downloads.download({
          url: `data:application/pdf;base64,${data.pdf.data_base64}`,
          filename: data.pdf.filename,
          saveAs: false,
        });
      } catch (downloadErr) {
        data.pdf.download_error = downloadErr.message;
      }
    }
    return { success: true, data };
  } catch (err) {
    return { success: false, error: err.message };
  }
}
```
**The single shared upload function used by every call site** — `popup.js`'s normal completion, `popup.js`'s manual recovery banner, and `recovery.js`'s automatic recovery flow all call this exact function, guaranteeing identical behavior across all three.
- Guards against an empty blob up front.
- Builds a `multipart/form-data` body matching exactly what the backend's `/process-meeting` endpoint expects: `file` (named with a timestamp), `user_id`, `save_locally` (as the string `"true"`/`"false"`, since form fields are text), and `groq_api_key`.
- Fires a status callback before the request, since this can take a long time for a large meeting.
- On a non-OK HTTP response, tries to parse the backend's JSON error body for its `detail` field (matching FastAPI's `HTTPException` shape) and throws that as the error message; falls back to a generic `"Server returned {status}"` if the body isn't parseable JSON.
- On success, if the backend included base64 PDF data, immediately triggers a browser download via `chrome.downloads.download()` using a `data:` URL (no separate file-hosting needed — the PDF bytes are embedded directly in the URL). `saveAs: false` means it saves automatically without prompting the user for a location. If the download itself fails, the error is attached back onto `data.pdf.download_error` rather than failing the whole upload — the summary and CRM push already succeeded at that point, so only the local-save step should be reported as failed.
- Returns a consistent `{ success: true, data }` or `{ success: false, error }` shape that every caller relies on.

---

## 6. `recovery.html` — Recovery Window UI

A minimal window opened either automatically by `background.js` (Section 2) or manually never — it's only ever auto-opened, there's no button elsewhere that opens it directly. Structure:
- Title: "Recovering Interrupted Recording".
- `#status`: main message area, updated throughout the recovery process.
- **Retry** button (`#retryBtn`): hidden by default, shown only when recovery fails or needs a missing Groq key.
- **Close** button (`#closeBtn`): always available.

Same dark theme styling as `popup.html`. Loads `session-utils.js` then `recovery.js` (same ordering reason as `popup.html` — `recovery.js` calls functions defined in `session-utils.js`).

---

## 7. `recovery.js` — Automatic Recovery Flow

```javascript
const BACKEND_BASE = "http://127.0.0.1:8000";
const BACKEND_URL = `${BACKEND_BASE}/process-meeting`;

const statusEl = document.getElementById("status");
const closeBtn = document.getElementById("closeBtn");
const retryBtn = document.getElementById("retryBtn");

const params = new URLSearchParams(window.location.search);
const sessionId = params.get("sessionId");

function setStatus(text) { statusEl.textContent = text; }
```
Reads `sessionId` from the URL query string — this is exactly what `background.js` passed when it opened this window (`recovery.html?sessionId=...`).

### `markSessionDone()`
```javascript
async function markSessionDone() {
  const stored = await chrome.storage.local.get(["activeSession"]);
  if (stored.activeSession && stored.activeSession.sessionId === sessionId) {
    await chrome.storage.local.set({ activeSession: null });
  }
}
```
Clears the tracked `activeSession` in storage, but only if it still refers to *this* specific session (avoids accidentally clearing a different, newer session that might have started in the meantime).

### `runRecovery()`
```javascript
async function runRecovery() {
  if (!sessionId) {
    setStatus("No interrupted recording found for this window.");
    return;
  }

  retryBtn.classList.add("hidden");
  setStatus("Your previous recording session closed before it finished. Recovering the audio that was already saved...");

  let chunks;
  try {
    chunks = await loadChunksFromDB(sessionId);
  } catch (e) {
    setStatus("Could not read the saved audio from storage: " + e.message);
    retryBtn.classList.remove("hidden");
    return;
  }

  if (!chunks || chunks.length === 0) {
    setStatus("No audio had been saved for this session yet - nothing to recover. (This can happen if the window closed within the first few seconds of recording.)");
    await markSessionDone();
    return;
  }

  const blob = new Blob(chunks, { type: "audio/webm" });
  setStatus(`Found ${(blob.size / (1024 * 1024)).toFixed(1)} MB of saved audio. Uploading and processing...`);

  const stored = await chrome.storage.local.get(["meetingSummarizerUserId", "saveLocallyPref", "userGroqApiKey"]);
  const userId = stored.meetingSummarizerUserId || "default-user";
  const saveLocally = Boolean(stored.saveLocallyPref);
  const groqApiKey = stored.userGroqApiKey || "";

  if (!groqApiKey) {
    setStatus(
      "A Groq API key is required before this can be uploaded, and none is saved yet.\n\n" +
      "Your audio is still safely saved - nothing was lost. Open the extension popup, " +
      "enter your Groq API key in Settings, then come back and press Retry."
    );
    retryBtn.classList.remove("hidden");
    return;
  }

  const result = await uploadRecording(blob, { backendUrl: BACKEND_URL, userId, saveLocally, groqApiKey, onStatus: setStatus });

  if (result.success) {
    const data = result.data;
    let pdfNote = "";
    if (data.pdf) {
      if (data.pdf.data_base64) {
        pdfNote = data.pdf.download_error
          ? `\n\nCouldn't save the PDF locally: ${data.pdf.download_error}`
          : `\n\nSaved locally as "${data.pdf.filename}".`;
      } else if (data.pdf.error) {
        pdfNote = `\n\nLocal PDF save failed: ${data.pdf.error}`;
      }
    }
    setStatus(
      "Recovered successfully.\n\nMeeting: " + data.meeting_title + "\n\n" +
      "CRM push status: " + (data.crm_push ? data.crm_push.status : "unknown") +
      pdfNote + "\n\nThis window will close automatically."
    );
    await deleteSessionFromDB(sessionId);
    await markSessionDone();
    setTimeout(() => window.close(), 8000);
  } else {
    setStatus(
      "Recovery upload failed: " + result.error +
      "\n\nYour audio is still safely saved - nothing was lost. Make sure the backend " +
      "(python main.py) is running, then press Retry."
    );
    retryBtn.classList.remove("hidden");
  }
}

retryBtn.addEventListener("click", runRecovery);
closeBtn.addEventListener("click", () => window.close());
runRecovery();
```
Step by step:
1. If somehow no `sessionId` was passed at all, shows a message and stops (shouldn't normally happen since `background.js` always includes it when opening this window).
2. Loads whatever audio chunks were saved to IndexedDB for this session (Section 5's `loadChunksFromDB`). If that read itself fails (IndexedDB error), shows the error and offers Retry.
3. **If there are literally zero saved chunks** — this can legitimately happen if the window closed within the first few seconds of recording, before the first ~10-second IndexedDB flush ever happened — there's nothing to recover. This is treated as a normal (not an error) outcome: the message explains why, and the session is simply marked done.
4. Reassembles the chunks into one `Blob` and reports its size in MB to the user, so they can see roughly how much was actually captured.
5. Pulls the saved `user_id`, save-locally preference, and Groq key straight from `chrome.storage.local` — this window has no UI of its own for entering these, since it's meant to run unattended/automatically.
6. **If there's no saved Groq key**, it can't proceed — but explicitly reassures the user the audio isn't lost, and tells them exactly what to do (open the extension, enter the key in Settings, come back and press Retry).
7. Calls the same shared `uploadRecording()` helper as everywhere else (Section 5).
8. On success: shows a detailed success message (meeting title, CRM status, PDF note — same formatting logic as `popup.js`'s success path), deletes the IndexedDB backup, marks the session done, and **auto-closes the window after 8 seconds** (`setTimeout(() => window.close(), 8000)`) — long enough for the user to actually read the result before it disappears on its own.
9. On failure: explicit reassurance again that nothing was lost, a concrete troubleshooting hint (make sure `python main.py` is running), and reveals the Retry button.
- `retryBtn` re-runs the entire `runRecovery()` function from scratch on click.
- `closeBtn` just closes the window immediately at any time.
- `runRecovery()` is invoked immediately at the bottom of the file — the whole flow runs automatically the instant this window opens, no user action required to kick it off.
