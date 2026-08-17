// Shared between popup.js and recovery.js.
//
// Why this exists: for a 2-3 hour meeting, holding the entire recording only
// in a JS array in the popup window's memory is risky - if that window
// closes (crash, accidental click, laptop sleep interrupting it, etc.)
// before the upload finishes, the whole recording is gone. To prevent that,
// audio is periodically flushed to IndexedDB *while recording is still
// happening*, not just at the end. IndexedDB is per-extension-origin
// storage, so it's readable from any extension page - including a recovery
// window opened later by background.js after an unexpected close.

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

// Chunks are saved as small batched blobs (not one chunk per second) to
// keep the number of IndexedDB writes reasonable across a multi-hour
// recording. chunkIndex must increase monotonically within a session so
// they can be reassembled in the right order later.
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

async function deleteSessionFromDB(sessionId) {
  const db = await openChunkDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    const index = tx.objectStore(STORE_NAME).index("sessionId");
    const req = index.openCursor(IDBKeyRange.only(sessionId));
    req.onsuccess = (e) => {
      const cursor = e.target.result;
      if (cursor) {
        cursor.delete();
        cursor.continue();
      }
    };
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
  });
}

// Saves the raw recorded audio straight to the user's Downloads, the same
// way the summary PDF is saved via chrome.downloads. This is deliberately
// independent of uploadRecording()/the backend - it runs as soon as the
// blob exists, before the upload even starts, so the original recording is
// preserved locally even if the network is down or the backend fails.
// Called from popup.js (normal finish), recovery.js (auto-recovery), and
// popup.js's manual recovery banner - anywhere a finished blob shows up.
async function saveAudioLocally(blob) {
  if (!blob || blob.size === 0) return;

  // Blob URLs work fine here because this always runs inside an extension
  // page (popup.html/recovery.html), never the background service worker.
  const url = URL.createObjectURL(blob);
  const filename = `meeting-audio-${Date.now()}.webm`;

  try {
    await chrome.downloads.download({ url, filename, saveAs: false });
  } finally {
    // Revoking too early can abort an in-progress download read, so give
    // it a generous head start before freeing the blob URL. For a long
    // meeting recording (tens/hundreds of MB), the download itself may
    // still be copying the file well after chrome.downloads.download()'s
    // promise resolves (that promise resolves once the download STARTS,
    // not once it finishes).
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
}

// The actual upload-and-process step, shared so a fresh recording (popup.js)
// and a recovered one (recovery.js) hit the backend the exact same way -
// including triggering the local PDF download if that was requested.
// Returns { success: true, data } or { success: false, error }.
//
// Note: no Groq/HubSpot key is sent from here anymore. userId identifies
// the logged-in account, and the backend looks both keys up from that
// account in Supabase - the extension never holds either key locally.
async function uploadRecording(blob, { backendUrl, userId, saveLocally, onStatus }) {
  if (!blob || blob.size === 0) {
    return { success: false, error: "No audio data to process." };
  }
  if (!userId) {
    return { success: false, error: "Not logged in." };
  }

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
