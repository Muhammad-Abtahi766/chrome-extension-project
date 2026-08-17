// Account auth (register/login/logout) + the settings-panel UI for editing
// the account's saved Groq/HubSpot keys. Shared groundwork used by popup.js.
//
// Session shape saved to chrome.storage.local under "meetingSummarizerSession":
//   { user_id, username }
// That's it - no password is ever stored locally. Logging in again just
// re-checks username+password against the backend and re-saves this object.

const BACKEND_BASE = "https://chrome-extension-project-blush.vercel.app";

const authScreen = document.getElementById("authScreen");
const appScreen = document.getElementById("appScreen");
const authUsernameInput = document.getElementById("authUsername");
const authPasswordInput = document.getElementById("authPassword");
const authSubmitBtn = document.getElementById("authSubmitBtn");
const authErrorEl = document.getElementById("authError");
const authToggleBtn = document.getElementById("authToggleBtn");
const authToggleText = document.getElementById("authToggleText");
const accountUsernameEl = document.getElementById("accountUsername");
const logoutBtn = document.getElementById("logoutBtn");

let authMode = "login"; // "login" | "register"
let currentSession = null; // { user_id, username }

function showAuthError(message) {
  authErrorEl.textContent = message;
  authErrorEl.style.display = "block";
}
function clearAuthError() {
  authErrorEl.style.display = "none";
  authErrorEl.textContent = "";
}

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

authSubmitBtn.addEventListener("click", async () => {
  const username = authUsernameInput.value.trim();
  const password = authPasswordInput.value;
  clearAuthError();

  if (!username || !password) {
    showAuthError("Enter a username and password.");
    return;
  }

  authSubmitBtn.disabled = true;
  authSubmitBtn.textContent = authMode === "register" ? "Creating account..." : "Logging in...";

  const endpoint = authMode === "register" ? "/auth/register" : "/auth/login";

  try {
    const res = await fetch(`${BACKEND_BASE}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));

    if (!res.ok) {
      throw new Error(data.detail || `Server returned ${res.status}`);
    }

    await saveSession({ user_id: data.user_id, username: data.username });
    authPasswordInput.value = "";
    await enterAppScreen();
  } catch (err) {
    showAuthError(
      err.message.includes("fetch")
        ? "Can't reach the backend. Please check your internet connection and try again."
        : err.message
    );
  } finally {
    authSubmitBtn.disabled = false;
    authSubmitBtn.textContent = authMode === "register" ? "Create account" : "Log in";
  }
});

logoutBtn.addEventListener("click", async () => {
  await clearSession();
  location.reload();
});

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

async function enterAppScreen() {
  authScreen.style.display = "none";
  appScreen.style.display = "block";
  accountUsernameEl.textContent = currentSession.username;
  await refreshKeyStatuses();
  if (typeof onAppScreenReady === "function") {
    // popup.js defines this - it wires up recording/mic/etc once we know
    // who's logged in.
    onAppScreenReady();
  }
}

// --- Settings panel: Groq/HubSpot key status + edit forms ---
// Values are never fetched back down once saved - only whether a key is
// set. Editing always means typing a brand new value and saving it, same
// as changing a password.

const groqKeyStatusEl = document.getElementById("groqKeyStatus");
const groqKeyEditToggle = document.getElementById("groqKeyEditToggle");
const groqKeyEditForm = document.getElementById("groqKeyEditForm");
const groqKeyInput = document.getElementById("groqKeyInput");
const groqKeySaveBtn = document.getElementById("groqKeySaveBtn");
const groqKeyCancelBtn = document.getElementById("groqKeyCancelBtn");

const hubspotKeyStatusEl = document.getElementById("hubspotKeyStatus");
const hubspotKeyEditToggle = document.getElementById("hubspotKeyEditToggle");
const hubspotKeyEditForm = document.getElementById("hubspotKeyEditForm");
const hubspotKeyInput = document.getElementById("hubspotKeyInput");
const hubspotKeySaveBtn = document.getElementById("hubspotKeySaveBtn");
const hubspotKeyCancelBtn = document.getElementById("hubspotKeyCancelBtn");

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

wireKeyEditor({
  toggleBtn: groqKeyEditToggle,
  form: groqKeyEditForm,
  input: groqKeyInput,
  saveBtn: groqKeySaveBtn,
  cancelBtn: groqKeyCancelBtn,
  field: "groq_api_key",
});

wireKeyEditor({
  toggleBtn: hubspotKeyEditToggle,
  form: hubspotKeyEditForm,
  input: hubspotKeyInput,
  saveBtn: hubspotKeySaveBtn,
  cancelBtn: hubspotKeyCancelBtn,
  field: "hubspot_api_key",
});

// --- Boot: show auth screen or go straight into the app if already logged in ---
// Deferred to DOMContentLoaded (rather than run immediately) so popup.js -
// loaded after this file - has already defined onAppScreenReady() by the
// time enterAppScreen() might try to call it for an already-logged-in user.
document.addEventListener("DOMContentLoaded", async () => {
  const session = await loadSession();
  if (session && session.user_id) {
    await enterAppScreen();
  } else {
    authScreen.style.display = "block";
  }
});
