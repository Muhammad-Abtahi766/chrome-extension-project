const BACKEND_BASE = "http://127.0.0.1:8000";
const BACKEND_URL = `${BACKEND_BASE}/process-meeting`;

const statusEl = document.getElementById("status");
const closeBtn = document.getElementById("closeBtn");
const retryBtn = document.getElementById("retryBtn");

const params = new URLSearchParams(window.location.search);
const sessionId = params.get("sessionId");

function setStatus(text) {
  statusEl.textContent = text;
}

async function markSessionDone() {
  const stored = await chrome.storage.local.get(["activeSession"]);
  if (stored.activeSession && stored.activeSession.sessionId === sessionId) {
    await chrome.storage.local.set({ activeSession: null });
  }
}

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

  const stored = await chrome.storage.local.get([
    "meetingSummarizerSession",
    "saveLocallyPref",
    "saveAudioPref",
  ]);
  const session = stored.meetingSummarizerSession;
  const saveLocally = Boolean(stored.saveLocallyPref);
  const saveAudio = Boolean(stored.saveAudioPref);

  if (saveAudio) {
    try {
      await saveAudioLocally(blob);
    } catch (e) {
      console.warn("Failed to save raw audio locally:", e);
    }
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

  // Note: no Groq/HubSpot key is sent from here - the backend looks both up
  // from this account (session.user_id) in Supabase. If the account hasn't
  // set a Groq key yet, the backend rejects with a clear 400 message below.
  const result = await uploadRecording(blob, {
    backendUrl: BACKEND_URL,
    userId: session.user_id,
    saveLocally,
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

    setStatus(
      "Recovered successfully.\n\n" +
      "Meeting: " + data.meeting_title + "\n\n" +
      "CRM push status: " + (data.crm_push ? data.crm_push.status : "unknown") +
      pdfNote +
      "\n\nThis window will close automatically."
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
