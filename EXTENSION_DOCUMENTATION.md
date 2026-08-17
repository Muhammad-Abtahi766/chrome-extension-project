# Extension Documentation — "Meeting Summarizer & CRM Sync"

A Manifest V3 Chrome extension: records meeting tab audio, sends it to the local Python backend (`http://127.0.0.1:8000`) for transcription/summarization/CRM sync, and shows the result.

**Files covered:** `manifest.json`, `background.js`, `popup.html`, `auth.js`, `popup.js`, `session-utils.js`, `recovery.html`, `recovery.js`.

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
| `permissions.storage` | Enables `chrome.storage.local` — used everywhere for the logged-in session, recording preferences, and session state. |
| `permissions.scripting` | Reserved for script-injection capability (declared, available if needed). |
| `permissions.downloads` | Needed for `chrome.downloads.download(...)` — used to save the generated PDF and/or raw audio locally. |
| `permissions.unlimitedStorage` | Lifts the default storage quota — relevant since IndexedDB may hold hours of audio chunks per session. |
| `host_permissions` | Restricts network access to exactly the local backend (`127.0.0.1:8000`) — the extension can't silently call any other host. |
| `background.service_worker` | Registers `background.js` as the MV3 service worker (event-driven, can be killed/restarted by Chrome at any time — this matters, see Section 2). |
| `action.default_title` | Tooltip text on the toolbar icon. **Notably, there's no `default_popup`** — clicking the icon is instead handled entirely in code via `chrome.action.onClicked` (see Section 2), which is what allows opening a persistent window instead of an auto-closing popup. |

---

## 2. `background.js` — Service Worker

**Why this file exists at all, and why capture logic is NOT here:** `chrome.tabCapture.getMediaStreamId()`/capture must be driven from an extension view (like a popup or window) in direct response to a user gesture — it can't be called from a service worker. So `background.js` only handles orchestration: opening/tracking the recorder window, badge state, and crash recovery. The actual capture/recording logic lives in `popup.js`.

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
- **The actual recovery trigger:** if there was an `activeSession` and its `phase` wasn't `"done"` — meaning a recording or an in-progress upload was still active when the window closed (crash, accidental click, browser hiccup) — it opens a small `recovery.html` popup window, passing the session ID. The comment in the code frames the intent well: *"A closed window doesn't mean a lost meeting"* — because audio was already being incrementally saved to IndexedDB as it was captured (see `session-utils.js`, Section 6), so `recovery.html` can pick up where things left off.

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

- **`#authScreen`** (shown when nobody's logged in): a tagline explaining that Groq/HubSpot keys are saved to the account, a username field, a password field, a submit button (`#authSubmitBtn`, label toggles between "Log in"/"Create account"), an error area (`#authError`), and a Register/Log in toggle link (`#authToggleBtn`/`#authToggleText`).
- **`#appScreen`** (shown once logged in):
  - **Header row**: title + a gear (`⚙`) settings button (`#settingsBtn`).
  - **Settings overlay** (`#settingsOverlay` → `#settingsPanel`): a slide-in panel containing:
    - **`#accountRow`**: "Logged in as `<username>`" plus a Log out button (`#logoutBtn`).
    - **Groq API key row** (`#groqKeyRow`): status text (`#groqKeyStatus`, "Key saved" / "Not set"), an Edit toggle button, and a collapsible edit form (`#groqKeyEditForm`) with a password-type input, Save and Cancel buttons, and hint text pointing to console.groq.com.
    - **HubSpot API key row** (`#hubspotKeyRow`): identical pattern, marked optional, with hint text walking through generating a HubSpot Private App token.
    - **`#saveLocallyRow`**: "Save summary as PDF on this computer" checkbox.
    - **`#saveAudioRow`**: "Save raw audio recording on this computer" checkbox.
  - **`#groqKeyMissingBanner`**: hidden by default; shown when the account has no Groq key set, blocking recording, with an "Open Settings" shortcut button.
  - **`#recoveryBanner`**: hidden by default; shown when `popup.js` detects an unfinished prior session, with a "Recover it now" button.
  - **`#status`**: main status line, updated throughout the recording/upload lifecycle.
  - **`#micRow`**: a `<select>` (`#micSelect`) populated dynamically with real microphone device names.
  - **`#timer`**: `MM:SS` elapsed-recording display.
  - **Buttons**: `#startBtn` ("Start recording", disabled until the account has a Groq key) and `#stopBtn` ("Stop & process", disabled until recording starts).
  - **`#result`**: hidden by default; shows the final summary text plus CRM/PDF status after processing completes.

Three scripts are loaded in order at the bottom: `session-utils.js`, then `auth.js`, then `popup.js`. Order matters twice over: `auth.js` and `popup.js` both call functions (`saveChunkToDB`, `loadChunksFromDB`, `uploadRecording`, `saveAudioLocally`) defined in `session-utils.js`; and `auth.js` must load before `popup.js` so that `popup.js`'s `onAppScreenReady()` function already exists by the time `auth.js`'s `enterAppScreen()` might call it for an already-logged-in user on page load.

Visual styling is a dark navy/cream theme (`#1c2b39` background, `#f5f0e6` text) with maroon/gold accent buttons — purely cosmetic, no functional impact.

---

## 4. `auth.js` — Account Login/Register + Settings Key Editor

**Why this exists as its own file:** account/session management and the settings panel's key-editing UI are shared "groundwork" needed before any recording logic can run — `popup.js` depends on knowing who's logged in (`getCurrentUserId()`) and whether a Groq key is set (`hasGroqKey`) before its own `onAppScreenReady()` does anything.

### Session shape
```javascript
const BACKEND_BASE = "http://127.0.0.1:8000";
```
`BACKEND_BASE` is declared here (not in `popup.js`) since `auth.js` loads first and both files need it.

The session object saved to `chrome.storage.local` under the key `meetingSummarizerSession` is exactly:
```javascript
{ user_id, username }
```
**No password is ever stored locally.** Logging in again just re-checks username+password against the backend and re-saves this object.

### Auth mode toggle (Login ⇄ Register)
```javascript
let authMode = "login"; // "login" | "register"

authToggleBtn.addEventListener("click", () => {
  authMode = authMode === "login" ? "register" : "login";
  clearAuthError();
  if (authMode === "register") {
    authSubmitBtn.textContent = "Create account";
    authToggleText.textContent = "Already have an account?";
    authToggleBtn.textContent = "Log in";
    authPasswordInput.setAttribute("autocomplete", "new-password");
  } else {
    authSubmitBtn.textContent = "Log in";
    authToggleText.textContent = "Don't have an account?";
    authToggleBtn.textContent = "Register";
    authPasswordInput.setAttribute("autocomplete", "current-password");
  }
});
```
Flips between the two modes, relabeling the submit button and the toggle link, and swaps the password field's `autocomplete` hint (`new-password` vs `current-password`) so the browser's own password manager offers the right behavior (suggest-a-strong-password vs. autofill-a-saved-one).

### Submitting the auth form
```javascript
authSubmitBtn.addEventListener("click", async () => {
  const username = authUsernameInput.value.trim();
  const password = authPasswordInput.value;
  ...
  const endpoint = authMode === "register" ? "/auth/register" : "/auth/login";

  try {
    const res = await fetch(`${BACKEND_BASE}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Server returned ${res.status}`);

    await saveSession({ user_id: data.user_id, username: data.username });
    authPasswordInput.value = "";
    await enterAppScreen();
  } catch (err) {
    showAuthError(
      err.message.includes("fetch")
        ? "Can't reach the backend. Make sure it's running (python main.py)."
        : err.message
    );
  } finally {
    authSubmitBtn.disabled = false;
    authSubmitBtn.textContent = authMode === "register" ? "Create account" : "Log in";
  }
});
```
- Client-side validation only checks both fields are non-empty; length/format rules (username ≥ 3 chars, password ≥ 8 chars) are enforced server-side and surfaced via the error message if violated.
- Posts to `/auth/register` or `/auth/login` depending on the current mode.
- On success: saves the returned `{ user_id, username }` as the session, clears the password field from memory, and transitions into the app screen.
- On failure: a fetch-level failure (backend not running) gets a specific, actionable message; any other error (bad credentials, username taken, validation failure) shows the backend's own `detail` message directly.
- Button is disabled and relabeled ("Logging in..."/"Creating account...") for the duration of the request, always restored in `finally` regardless of outcome.

### Logout
```javascript
logoutBtn.addEventListener("click", async () => {
  await clearSession();
  location.reload();
});
```
Clears the stored session and reloads the whole popup document — simplest way to guarantee every piece of in-memory state (`currentSession`, key statuses, mic list, etc.) resets cleanly back to the auth screen.

### Session helpers
```javascript
async function saveSession(session) {
  currentSession = session;
  await chrome.storage.local.set({ meetingSummarizerSession: session });
}
async function loadSession() {
  const stored = await chrome.storage.local.get(["meetingSummarizerSession"]);
  currentSession = stored.meetingSummarizerSession || null;
  return currentSession;
}
async function clearSession() {
  currentSession = null;
  await chrome.storage.local.remove(["meetingSummarizerSession"]);
}
function getCurrentUserId() {
  return currentSession ? currentSession.user_id : null;
}
```
`getCurrentUserId()` is the function `popup.js` and the recovery flow call whenever they need to attach `user_id` to an upload — it always reflects whatever `currentSession` currently holds, so it can't go stale independently of the session object.

### `enterAppScreen()`
```javascript
async function enterAppScreen() {
  authScreen.style.display = "none";
  appScreen.style.display = "block";
  accountUsernameEl.textContent = currentSession.username;
  await refreshKeyStatuses();
  if (typeof onAppScreenReady === "function") {
    onAppScreenReady();
  }
}
```
Swaps screens, shows the username in the settings panel, refreshes Groq/HubSpot key status from the backend, and — only once all of that's done — hands off to `popup.js`'s `onAppScreenReady()` (mic list population, recovery check, Start-button gating). The `typeof onAppScreenReady === "function"` guard is defensive: it lets `auth.js` be loaded/tested independent of `popup.js` without throwing if that function isn't defined yet.

### Settings panel: key status + edit forms
```javascript
let hasGroqKey = false;
let hasHubspotKey = false;

function renderKeyStatus(el, isSet, label) {
  el.textContent = isSet ? `${label} saved` : "Not set";
  el.className = "keyStatus " + (isSet ? "set" : "notSet");
}

async function refreshKeyStatuses() {
  if (!currentSession) return;
  try {
    const res = await fetch(`${BACKEND_BASE}/user/keys?user_id=${encodeURIComponent(currentSession.user_id)}`);
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    const data = await res.json();
    hasGroqKey = Boolean(data.has_groq_key);
    hasHubspotKey = Boolean(data.has_hubspot_key);
  } catch (err) {
    hasGroqKey = false;
    hasHubspotKey = false;
  }
  renderKeyStatus(groqKeyStatusEl, hasGroqKey, "Key");
  renderKeyStatus(hubspotKeyStatusEl, hasHubspotKey, "Key");
  if (typeof updateStartAvailability === "function") updateStartAvailability();
}
```
- `hasGroqKey`/`hasHubspotKey` are module-level booleans that `popup.js` reads directly (e.g. to gate the Start button) — this is the extension-side mirror of what the backend's `GET /user/keys` reports.
- A failed status check (backend down, network error) fails safe: both flags reset to `false`, which correctly disables recording rather than assuming a key is present.
- After updating the display, also pokes `popup.js`'s `updateStartAvailability()` if it exists, so the Start button reacts immediately to a status change (e.g. right after a key is saved).

**Values are never fetched back down once saved — only whether one is set.** Editing always means typing a brand new value and saving it, the same pattern as changing a password.

```javascript
function wireKeyEditor({ toggleBtn, form, input, saveBtn, cancelBtn, field }) {
  toggleBtn.addEventListener("click", () => {
    form.classList.toggle("open");
    if (form.classList.contains("open")) input.focus();
  });
  cancelBtn.addEventListener("click", () => {
    input.value = "";
    form.classList.remove("open");
  });
  saveBtn.addEventListener("click", async () => {
    const value = input.value.trim();
    if (!value) return;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    try {
      const body = { user_id: currentSession.user_id };
      body[field] = value;
      const res = await fetch(`${BACKEND_BASE}/user/keys`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Server returned ${res.status}`);
      }
      input.value = "";
      form.classList.remove("open");
      await refreshKeyStatuses();
    } catch (err) {
      alert("Couldn't save key: " + err.message);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
    }
  });
}

wireKeyEditor({ toggleBtn: groqKeyEditToggle, form: groqKeyEditForm, input: groqKeyInput,
                 saveBtn: groqKeySaveBtn, cancelBtn: groqKeyCancelBtn, field: "groq_api_key" });
wireKeyEditor({ toggleBtn: hubspotKeyEditToggle, form: hubspotKeyEditForm, input: hubspotKeyInput,
                 saveBtn: hubspotKeySaveBtn, cancelBtn: hubspotKeyCancelBtn, field: "hubspot_api_key" });
```
A single reusable function wires up both the Groq and HubSpot key rows identically (avoiding duplicated logic for two nearly-identical UI patterns):
- **Toggle** opens/closes the collapsible edit form and focuses the input when opening.
- **Cancel** clears whatever was typed and closes the form without saving.
- **Save**: skips silently on an empty/whitespace-only value (nothing to save); otherwise PUTs `{ user_id, [field]: value }` to `/user/keys`, clears and closes the form, and re-fetches key statuses so the row immediately reflects "saved." A failure surfaces via a plain `alert()` with the backend's error detail — deliberately simple since this is a low-frequency settings action, not part of the main recording flow.

### Boot sequence
```javascript
document.addEventListener("DOMContentLoaded", async () => {
  const session = await loadSession();
  if (session && session.user_id) {
    await enterAppScreen();
  } else {
    authScreen.style.display = "block";
  }
});
```
Deferred to `DOMContentLoaded` (rather than run immediately at script-parse time) specifically so `popup.js` — loaded *after* this file — has already defined `onAppScreenReady()` by the time `enterAppScreen()` might try to call it for an already-logged-in user. Checks for a saved session; if one exists, skips straight past the auth screen into the app.

---

## 5. `popup.js` — Recording and Upload Logic

`BACKEND_URL` is derived from `auth.js`'s `BACKEND_BASE` (already declared by the time this file runs, since `auth.js` loads first):
```javascript
const BACKEND_URL = `${BACKEND_BASE}/process-meeting`;
```

### Settings panel open/close
```javascript
settingsBtn.addEventListener("click", () => settingsOverlay.classList.add("open"));
settingsCloseBtn.addEventListener("click", () => settingsOverlay.classList.remove("open"));
openSettingsForKeyBtn.addEventListener("click", () => {
  settingsOverlay.classList.add("open");
  if (!groqKeyEditForm.classList.contains("open")) groqKeyEditToggle.click();
  groqKeyInput.focus();
});
settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) settingsOverlay.classList.remove("open");
});
```
- Gear icon and close (`×`) button toggle the `.open` class (which controls visibility via CSS).
- The "Open Settings" shortcut inside the missing-key banner opens the panel **and** jumps straight into the Groq key's edit form (`groqKeyEditToggle.click()`, only if not already open) before focusing the input — so the user lands directly on the thing they need to fix, not just the panel in general.
- Clicking the dimmed backdrop closes the panel too — but only when the click target is the backdrop itself (`e.target === settingsOverlay`), not a click that merely bubbles up from something inside the panel.

### `updateStartAvailability()`
```javascript
function updateStartAvailability() {
  groqKeyMissingBanner.style.display = hasGroqKey ? "none" : "block";
  if (stopBtn.disabled) {
    startBtn.disabled = !hasGroqKey;
  }
}
```
Reads `hasGroqKey` directly from `auth.js` (a shared top-level variable, not passed as an argument) — this runs whenever key status is refreshed (from `auth.js`'s `refreshKeyStatuses()`) and keeps the Start button's enabled state in sync. The `if (stopBtn.disabled)` guard means this never fights an in-progress recording/processing state — it only ever toggles `startBtn` when we're NOT mid-recording (`stopBtn` is only enabled while actively recording).

### Save-locally / save-audio checkbox persistence
```javascript
chrome.storage.local.get(["saveLocallyPref"], (result) => {
  saveLocallyCheckbox.checked = Boolean(result.saveLocallyPref);
});
saveLocallyCheckbox.addEventListener("change", () => {
  chrome.storage.local.set({ saveLocallyPref: saveLocallyCheckbox.checked });
});

chrome.storage.local.get(["saveAudioPref"], (result) => {
  saveAudioCheckbox.checked = Boolean(result.saveAudioPref);
});
saveAudioCheckbox.addEventListener("change", () => {
  chrome.storage.local.set({ saveAudioPref: saveAudioCheckbox.checked });
});
```
Two independent preferences, same restore-on-load/persist-on-change pattern as the mic selection below, so neither has to be re-checked every meeting. They're intentionally separate (not one combined toggle) since raw audio files can be large and default to **off**, while the PDF summary is comparatively lightweight.

### `checkForRecoverableSession()`
```javascript
async function checkForRecoverableSession() {
  const stored = await chrome.storage.local.get(["activeSession"]);
  const session = stored.activeSession;
  if (session && session.phase && session.phase !== "done" && session.sessionId !== currentSessionId) {
    recoveryBanner.style.display = "block";
    recoverBtn.onclick = async () => {
      if (!hasGroqKey) {
        setStatus("Enter your Groq API key in Settings before recovering.");
        groqKeyMissingBanner.style.display = "block";
        settingsOverlay.classList.add("open");
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
      if (saveAudioCheckbox.checked) {
        try { await saveAudioLocally(blob); } catch (e) { console.warn(...); }
      }

      const result = await uploadRecording(blob, {
        backendUrl: BACKEND_URL,
        userId: getCurrentUserId(),
        saveLocally: saveLocallyCheckbox.checked,
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
This is a **second, manual** safety net on top of `background.js`'s automatic recovery-window trigger (Section 2) — it covers the case where a previous session never made it to `"done"` but the automatic `recovery.html` window somehow also didn't manage to open. Notably it calls `getCurrentUserId()` (from `auth.js`) rather than handling any key material itself — the upload only needs to know *who* is logged in; the backend resolves the actual Groq/HubSpot keys server-side.

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
const FLUSH_EVERY_N_CHUNKS = 10; // ~10s of audio per IndexedDB write, given the 1s MediaRecorder timeslice
```
- `currentSessionId` identifies one recording end-to-end (start click → fully processed) — used as the IndexedDB key so audio can be found again by this same window normally, or by a recovery window if this one closes unexpectedly.
- `pendingFlushChunks` / `flushIndex` track chunks not yet written to IndexedDB and the running batch-order index within the session.
- `FLUSH_EVERY_N_CHUNKS = 10`, combined with the 1-second `MediaRecorder` timeslice, means roughly every 10 seconds of audio gets written to IndexedDB as one batch.

### `populateMicList()`
```javascript
async function populateMicList() {
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    tempStream.getTracks().forEach((track) => track.stop());

    const devices = await navigator.mediaDevices.enumerateDevices();
    const mics = devices.filter((d) => d.kind === "audioinput");
    ...
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
```
**Why it requests a throwaway mic stream first:** Chrome hides real device labels (`mic.label`) from `enumerateDevices()` until microphone *permission* has actually been granted — without it, you'd just see generic unlabeled entries. So this grabs a temporary stream purely to trigger the permission prompt/unlock labels, immediately stops its tracks (releasing the mic right away since this stream isn't the one that will actually be recorded), then lists the real devices.
- Filters to `audioinput` devices only; falls back to a generic "Microphone N" label if one is somehow still blank.
- Restores the last-used mic selection from storage if that device is still present in the current list.
- Saves the choice to `chrome.storage.local` on every change so it persists across sessions.
- **Why this matters at all:** `getUserMedia({audio:true})` alone just grabs whatever the OS treats as default — which is often NOT the mic the user actually selected inside Google Meet/Zoom's own device settings (e.g. a headset vs. the laptop's built-in mic). Explicitly listing and selecting avoids that mismatch.
- Called from `onAppScreenReady()` (see end of this section), not at script-load time — it needs a logged-in session to make sense of the rest of the screen state around it.

### Small utility functions
```javascript
function setStatus(text) { statusEl.textContent = text; }
function formatElapsed(ms) { ... }  // ms -> zero-padded "MM:SS"
function startTimer() { ... }       // updates #timer every 500ms
function stopTimer() { ... }

function notifySessionState(phase) {
  chrome.runtime.sendMessage({ type: "SESSION_STATE", sessionId: currentSessionId, phase });
}
```
`notifySessionState(phase)` is the sending half of the `SESSION_STATE` messaging protocol handled in `background.js` (Section 2) — every phase transition (`"recording"`, `"processing"`, `"done"`) gets broadcast so the badge and crash-recovery tracking stay accurate.

### Starting a recording — `startBtn` click handler
```javascript
startBtn.addEventListener("click", () => {
  if (!hasGroqKey) {
    setStatus("Enter your Groq API key in Settings before recording.");
    groqKeyMissingBanner.style.display = "block";
    settingsOverlay.classList.add("open");
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

  chrome.tabCapture.getMediaStreamId({ targetTabId }, (streamId) => {
    ...
    navigator.mediaDevices.getUserMedia({
      audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    }).then((tabStream) => {
      capturedStream = tabStream;
      setStatus("Requesting microphone access...");

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

          recordedChunks = [];
          currentSessionId = crypto.randomUUID();
          pendingFlushChunks = [];
          flushIndex = 0;

          mediaRecorder = new MediaRecorder(mixedDestination.stream, { mimeType: "audio/webm;codecs=opus" });
          mediaRecorder.ondataavailable = (event) => { ... };
          mediaRecorder.onstop = async () => { ... };
          mediaRecorder.onerror = (event) => { setStatus("Recording error: " + event.error.message); };

          mediaRecorder.start(1000);
          startTimer();
          notifySessionState("recording");
          setStatus("Recording meeting audio (tab + mic)...");
          stopBtn.disabled = false;
        });
      });
    }).catch((err) => { ... });
  });
});
```
Step by step:
1. **First gate: no Groq key** (`hasGroqKey`, from `auth.js`) → block recording entirely, show the missing-key banner, and open settings. This mirrors the backend's hard `400` rejection for a missing key, so the failure is caught here instead of after an entire meeting is recorded for nothing.
2. Reads `tabId` back out of the URL query string that `background.js` set when it opened this window (`popup.html?tabId=...`). If missing/invalid, fails clearly with instructions to close and retry from the meeting tab.
3. `chrome.tabCapture.getMediaStreamId({ targetTabId }, ...)` — targets a **specific** tab by ID (unlike the older `chrome.tabCapture.capture()`, which only works on whichever tab is currently focused, not useful now that recording happens in a separate window).
4. That stream ID is then handed to `getUserMedia` with `chromeMediaSource: "tab"` to actually capture the tab's audio.
5. Separately requests the user's own microphone, honoring whichever device is selected in the dropdown (`deviceId: { exact: selectedMicId }`) rather than letting the browser pick the OS default.
6. Both streams are mixed via the Web Audio API: an `AudioContext` (explicitly `resume()`d if it started `"suspended"` — several `.then()` hops removed from the original click, the browser may not treat context creation as directly gesture-tied, and a suspended context silently records pure silence rather than erroring) feeds a `MediaStreamDestination`. Tab audio is connected to **both** the mixed destination and back out to the speakers (so the user keeps hearing the meeting normally); mic audio is connected **only** to the mixed destination (not looped back to speakers, to avoid the user hearing an echo of their own voice).
7. A fresh `currentSessionId` (UUID) is generated and flush-tracking state reset, then `MediaRecorder` starts on the mixed stream with a 1-second timeslice (so `ondataavailable` fires roughly once per second, feeding both the in-memory buffer and the periodic IndexedDB flush).
8. `notifySessionState("recording")` tells `background.js` to show the "REC" badge and start tracking this as an active session for crash-recovery purposes.
9. Failure at any stage in this chain (tab capture, mic access) is caught and reported with a specific message, and any partially-acquired stream is stopped so nothing keeps the mic/tab busy uselessly.

### Recording data handling (`ondataavailable` / `onstop`)
```javascript
mediaRecorder.ondataavailable = (event) => {
  if (event.data && event.data.size > 0) {
    recordedChunks.push(event.data);
    pendingFlushChunks.push(event.data);
    if (pendingFlushChunks.length >= FLUSH_EVERY_N_CHUNKS) {
      const toFlush = pendingFlushChunks;
      pendingFlushChunks = [];
      const batchBlob = new Blob(toFlush, { type: "audio/webm" });
      const thisFlushIndex = flushIndex++;
      saveChunkToDB(currentSessionId, thisFlushIndex, batchBlob).catch((e) => { console.warn(...); });
    }
  }
};

mediaRecorder.onstop = async () => {
  if (pendingFlushChunks.length > 0) {
    const batchBlob = new Blob(pendingFlushChunks, { type: "audio/webm" });
    pendingFlushChunks = [];
    const thisFlushIndex = flushIndex++;
    try { await saveChunkToDB(currentSessionId, thisFlushIndex, batchBlob); }
    catch (e) { console.warn(...); }
  }
  handleRecordingStop();
};
```
- Every ~10 seconds' worth of chunks gets batched into one `Blob` and written to IndexedDB as one row (keeping write volume reasonable across a multi-hour meeting, rather than one write per second).
- On stop, whatever's left in the pending buffer (less than a full batch) is flushed **before** `handleRecordingStop()` runs, so the IndexedDB backup is complete right up to the moment the user clicked Stop.

### Stopping a recording — `stopBtn` click handler
```javascript
stopBtn.addEventListener("click", () => {
  stopBtn.disabled = true;
  setStatus("Stopping recording...");
  stopTimer();
  notifySessionState("processing");

  if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
  if (capturedStream) capturedStream.getTracks().forEach((track) => track.stop());
  if (micStream) { micStream.getTracks().forEach((track) => track.stop()); micStream = null; }
  if (audioContext) { audioContext.close(); audioContext = null; }
});
```
Notably calls `notifySessionState("processing")` **before** the actual `mediaRecorder.stop()` — capture is done, but the upload/summarize/CRM-push pipeline hasn't happened yet, so the session must stay marked as in-flight (not `"done"`) the whole time; if the window closes anywhere during that window, `background.js`'s crash recovery still needs to trigger. All three underlying resources (recorder, tab stream, mic stream, audio context) are torn down explicitly rather than left to garbage collection, since Chrome will otherwise keep the tab-capture and mic indicators active.

### `handleRecordingStop()`
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

  if (saveAudioCheckbox.checked) {
    try { await saveAudioLocally(blob); } catch (e) { console.warn(...); }
  }

  const result = await uploadRecording(blob, {
    backendUrl: BACKEND_URL,
    userId: getCurrentUserId(),
    saveLocally: saveLocallyCheckbox.checked,
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
    resultEl.textContent = "Summary:\n" + data.summary + "\n\nCRM push status: " +
      (data.crm_push ? data.crm_push.status : "unknown") + pdfNote;

    await deleteSessionFromDB(currentSessionId).catch(() => {});
    notifySessionState("done");
  } else {
    setStatus("Failed to process meeting: " + result.error + ". The recorded audio is still saved and safe - reopen the extension to recover it.");
    // Deliberately NOT marking "done" and NOT deleting the IndexedDB backup here.
  }

  startBtn.disabled = false;
  timerEl.textContent = "00:00";
}
```
- An empty recording (zero bytes — nothing was actually captured) is handled as a normal dead-end, not an error: status message, re-enable Start, mark the session `"done"` immediately since there's nothing to recover.
- If **Save raw audio** is checked, that happens *before* the upload and independent of it — even if the upload fails or times out afterward, the original recording is already safe on disk.
- The actual upload uses only `userId` (from `getCurrentUserId()`) — no key material is attached client-side; the backend resolves both keys from that account.
- **On success:** builds a human-readable result string (summary text, CRM push status, and a note about where the PDF landed or why it failed), clears the IndexedDB backup (no longer needed since the upload succeeded), and marks the session `"done"`.
- **On failure:** explicitly reassures the user the audio is still safe and tells them to reopen the extension to recover it — and, critically, does **not** mark the session done or delete the IndexedDB backup, so if this window is later closed, `background.js`'s crash recovery can still pick it up and retry.

### `onAppScreenReady()`
```javascript
function onAppScreenReady() {
  populateMicList();
  checkForRecoverableSession();
  updateStartAvailability();
}
```
Called by `auth.js`'s `enterAppScreen()` once a session is confirmed (fresh login or restored from storage) — everything in `popup.js` that depends on knowing who's logged in waits for this single entry point rather than running at script-load time.

---

## 6. `session-utils.js` — Shared IndexedDB + Upload Helpers

Loaded by both `popup.html` and `recovery.html`, and used by `auth.js`/`popup.js` on the one hand and `recovery.js` on the other — this is what keeps the "happy path" and "crash recovery path" from drifting apart.

**Why IndexedDB at all:** for a 2-3 hour meeting, holding the entire recording only in a JS array in the popup window's memory is risky — if that window closes (crash, accidental click, laptop sleep interrupting it, etc.) before the upload finishes, the whole recording is gone. Audio is periodically flushed to IndexedDB *while recording is still happening*, not just at the end. IndexedDB is per-extension-origin storage, readable from any extension page — including a recovery window opened later by `background.js` after an unexpected close.

### Database setup
```javascript
const DB_NAME = "meetingSummarizerDB";
const DB_VERSION = 1;
const STORE_NAME = "audioChunks";

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
Standard IndexedDB open-with-upgrade pattern, wrapped in a Promise for `async`/`await` use everywhere else in the file. A `sessionId` index lets `loadChunksFromDB`/`deleteSessionFromDB` efficiently query all rows for one recording without a full table scan.

### `saveChunkToDB(sessionId, chunkIndex, blob)`
Adds one batched blob row (`{ sessionId, chunkIndex, blob, savedAt }`). Chunks are saved as small **batched** blobs (not one row per second) to keep IndexedDB write volume reasonable across a multi-hour recording; `chunkIndex` must increase monotonically within a session so rows can be reassembled in the right order later.

### `loadChunksFromDB(sessionId)`
Queries all rows for a session via the `sessionId` index, sorts them by `chunkIndex` (IndexedDB doesn't guarantee retrieval order), and returns just the array of blobs in the correct sequence.

### `deleteSessionFromDB(sessionId)`
Opens a cursor over all rows matching `sessionId` and deletes each one — called only after a confirmed-successful upload, so the backup persists through any failure that would otherwise lose the recording.

### `saveAudioLocally(blob)`
```javascript
async function saveAudioLocally(blob) {
  if (!blob || blob.size === 0) return;
  const url = URL.createObjectURL(blob);
  const filename = `meeting-audio-${Date.now()}.webm`;
  try {
    await chrome.downloads.download({ url, filename, saveAs: false });
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
}
```
Saves the raw recorded audio straight to Downloads, the same way the summary PDF is saved — deliberately independent of `uploadRecording()`/the backend, so the original recording is preserved locally even if the network is down or the backend fails entirely. Blob URLs only work inside an extension page (never the background service worker), which this always runs from. Revocation is deliberately delayed a full minute: `chrome.downloads.download()`'s returned promise resolves once the download **starts**, not once it finishes, and for a long meeting recording (tens/hundreds of MB) the actual file copy may still be in progress well after that — revoking the blob URL too early would abort an in-progress read.

### `uploadRecording(blob, { backendUrl, userId, saveLocally, onStatus })`
```javascript
async function uploadRecording(blob, { backendUrl, userId, saveLocally, onStatus }) {
  if (!blob || blob.size === 0) return { success: false, error: "No audio data to process." };
  if (!userId) return { success: false, error: "Not logged in." };

  const formData = new FormData();
  formData.append("file", blob, `meeting-${Date.now()}.webm`);
  formData.append("user_id", userId);
  formData.append("save_locally", saveLocally ? "true" : "false");

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
The single upload-and-process step shared by a fresh recording (`popup.js`) and a recovered one (`recovery.js`), so both hit the backend the exact same way — including triggering the local PDF download if requested.
- **No Groq/HubSpot key is sent from here** — `userId` identifies the logged-in account, and the backend looks both keys up from that account in Supabase server-side. The extension never holds either key value locally at any point.
- Guards against both an empty blob and a missing `userId` before even attempting the request.
- On a non-`data.pdf.data_base64` response (no PDF requested, or PDF generation failed server-side and only `data.pdf.error` came back), the download step is simply skipped.
- If the PDF *was* generated but the local `chrome.downloads.download()` call itself fails, that failure is attached to `data.pdf.download_error` rather than failing the whole upload — the summary/transcript/CRM result are still returned successfully to the caller.
- Returns a consistent `{ success: true, data }` or `{ success: false, error }` shape that every caller relies on.

---

## 7. `recovery.html` — Recovery Window UI

A minimal window opened either automatically by `background.js` (Section 2) or manually via the popup's recovery banner (Section 5) — never opened any other way. Structure:
- Title: "Recovering Interrupted Recording".
- `#status`: main message area, updated throughout the recovery process.
- **Retry** button (`#retryBtn`): hidden by default, shown only when recovery fails or the user isn't logged in.
- **Close** button (`#closeBtn`): always available.

Same dark theme styling as `popup.html`. Loads `session-utils.js` then `recovery.js` (same ordering reason as `popup.html` — `recovery.js` calls functions defined in `session-utils.js`). Notably, `recovery.js` does **not** load `auth.js` — it has no login UI of its own; it only reads whatever session already exists in `chrome.storage.local`.

---

## 8. `recovery.js` — Automatic Recovery Flow

```javascript
const BACKEND_BASE = "http://127.0.0.1:8000";
const BACKEND_URL = `${BACKEND_BASE}/process-meeting`;

const params = new URLSearchParams(window.location.search);
const sessionId = params.get("sessionId");
```
Reads `sessionId` from the URL query string — exactly what `background.js` passed when it opened this window (`recovery.html?sessionId=...`).

### `markSessionDone()`
```javascript
async function markSessionDone() {
  const stored = await chrome.storage.local.get(["activeSession"]);
  if (stored.activeSession && stored.activeSession.sessionId === sessionId) {
    await chrome.storage.local.set({ activeSession: null });
  }
}
```
Clears the tracked `activeSession` in storage, but only if it still refers to *this specific* session — avoids accidentally clearing a different, newer session that might have started in the meantime.

### `runRecovery()`
```javascript
async function runRecovery() {
  if (!sessionId) { setStatus("No interrupted recording found for this window."); return; }

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

  const stored = await chrome.storage.local.get(["meetingSummarizerSession", "saveLocallyPref", "saveAudioPref"]);
  const session = stored.meetingSummarizerSession;
  const saveLocally = Boolean(stored.saveLocallyPref);
  const saveAudio = Boolean(stored.saveAudioPref);

  if (saveAudio) {
    try { await saveAudioLocally(blob); } catch (e) { console.warn(...); }
  }

  if (!session || !session.user_id) {
    setStatus(
      "You're not logged in, so this can't be uploaded yet.\n\n" +
      "Your audio is still safely saved - nothing was lost. Open the extension popup, " +
      "log into your account, then come back and press Retry."
    );
    retryBtn.classList.remove("hidden");
    return;
  }

  const result = await uploadRecording(blob, {
    backendUrl: BACKEND_URL,
    userId: session.user_id,
    saveLocally,
    onStatus: setStatus,
  });

  if (result.success) {
    ...
    await deleteSessionFromDB(sessionId);
    await markSessionDone();
    setTimeout(() => window.close(), 8000);
  } else {
    setStatus("Recovery upload failed: " + result.error + "\n\nYour audio is still safely saved - nothing was lost. Make sure the backend (python main.py) is running, then press Retry.");
    retryBtn.classList.remove("hidden");
  }
}

retryBtn.addEventListener("click", runRecovery);
closeBtn.addEventListener("click", () => window.close());
runRecovery();
```
Step by step:
1. If somehow no `sessionId` was passed at all, shows a message and stops (shouldn't normally happen since `background.js` always includes it when opening this window).
2. Loads whatever audio chunks were saved to IndexedDB for this session (Section 6's `loadChunksFromDB`). If that read itself fails (IndexedDB error), shows the error and offers Retry.
3. **If there are literally zero saved chunks** — legitimate if the window closed within the first few seconds of recording, before the first ~10-second IndexedDB flush ever happened — there's nothing to recover. Treated as a normal (not an error) outcome: the message explains why, and the session is simply marked done.
4. Reassembles the chunks into one `Blob` and reports its size in MB, so the user can see roughly how much was actually captured.
5. Reads `meetingSummarizerSession`, the save-locally preference, and the save-audio preference straight from `chrome.storage.local` — this window has no UI of its own for logging in or changing preferences, since it's meant to run unattended/automatically.
6. If **Save raw audio** was on, saves it locally immediately — same reasoning as `popup.js`: independent of the upload, so the original is preserved even if the network is down.
7. **If there's no saved session (not logged in)**, it can't proceed — but explicitly reassures the user the audio isn't lost, and tells them exactly what to do (open the extension, log in, come back and press Retry). Unlike an earlier version of this flow, there is **no client-side Groq-key check here at all** — that check now lives entirely on the backend (a `400` if the account has no Groq key), since the key itself is never held or checked client-side anymore.
8. Calls the same shared `uploadRecording()` helper as everywhere else (Section 6), passing `session.user_id` directly from storage (no `getCurrentUserId()` helper here, since `auth.js` isn't loaded in this window).
9. **On success:** shows a detailed success message (meeting title, CRM status, PDF note — same formatting logic as `popup.js`'s success path), deletes the IndexedDB backup, marks the session done, and **auto-closes the window after 8 seconds** — long enough to actually read the result before it disappears on its own.
10. **On failure:** explicit reassurance again that nothing was lost, a concrete troubleshooting hint (make sure `python main.py` is running), and reveals the Retry button.
- `retryBtn` re-runs the entire `runRecovery()` function from scratch on click.
- `closeBtn` just closes the window immediately at any time.
- `runRecovery()` is invoked immediately at the bottom of the file — the whole flow runs automatically the instant this window opens, no user action required to kick it off.
