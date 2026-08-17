// BACKEND_BASE is declared in auth.js (loaded first). This is the one
// endpoint popup.js itself needs.
const BACKEND_URL = `${BACKEND_BASE}/process-meeting`;

const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const statusEl = document.getElementById("status");
const timerEl = document.getElementById("timer");
const resultEl = document.getElementById("result");
const micSelectEl = document.getElementById("micSelect");
const saveLocallyCheckbox = document.getElementById("saveLocallyCheckbox");
const saveAudioCheckbox = document.getElementById("saveAudioCheckbox");
const recoveryBanner = document.getElementById("recoveryBanner");
const recoverBtn = document.getElementById("recoverBtn");
const groqKeyMissingBanner = document.getElementById("groqKeyMissingBanner");
const openSettingsForKeyBtn = document.getElementById("openSettingsForKeyBtn");
const settingsBtn = document.getElementById("settingsBtn");
const settingsOverlay = document.getElementById("settingsOverlay");
const settingsCloseBtn = document.getElementById("settingsCloseBtn");

// --- Settings panel open/close ---
settingsBtn.addEventListener("click", () => settingsOverlay.classList.add("open"));
settingsCloseBtn.addEventListener("click", () => settingsOverlay.classList.remove("open"));
openSettingsForKeyBtn.addEventListener("click", () => {
  settingsOverlay.classList.add("open");
  // Jump straight to the Groq key's edit form rather than just opening the panel.
  if (!groqKeyEditForm.classList.contains("open")) groqKeyEditToggle.click();
  groqKeyInput.focus();
});
// Click on the dimmed backdrop (not the panel itself) also closes it.
settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) settingsOverlay.classList.remove("open");
});

// A Groq key is required (looked up from the account, via auth.js's
// hasGroqKey) before recording can start. This runs whenever key status is
// refreshed and keeps the Start button's enabled state in sync.
function updateStartAvailability() {
  groqKeyMissingBanner.style.display = hasGroqKey ? "none" : "block";
  // Don't fight a recording/processing in-flight state - only ever toggle
  // the button when we're not mid-recording (stopBtn enabled means we are).
  if (stopBtn.disabled) {
    startBtn.disabled = !hasGroqKey;
  }
}

// Remember the checkbox state across popup opens, same pattern as the mic
// selection below - so the user doesn't have to re-check it every meeting.
chrome.storage.local.get(["saveLocallyPref"], (result) => {
  saveLocallyCheckbox.checked = Boolean(result.saveLocallyPref);
});
saveLocallyCheckbox.addEventListener("change", () => {
  chrome.storage.local.set({ saveLocallyPref: saveLocallyCheckbox.checked });
});

// Same pattern, separate preference - saving the raw audio is optional and
// independent of saving the PDF (recordings can be large, so this defaults
// to unchecked/off rather than being bundled into the PDF toggle).
chrome.storage.local.get(["saveAudioPref"], (result) => {
  saveAudioCheckbox.checked = Boolean(result.saveAudioPref);
});
saveAudioCheckbox.addEventListener("change", () => {
  chrome.storage.local.set({ saveAudioPref: saveAudioCheckbox.checked });
});

// If a previous recording session never made it to "done" - e.g. this
// window got closed or crashed before background.js's own auto-recovery
// window managed to open - offer a manual way to recover it here too. This
// is a second safety net on top of the automatic one in background.js.
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
        try {
          await saveAudioLocally(blob);
        } catch (e) {
          console.warn("Failed to save raw audio locally:", e);
        }
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

let mediaRecorder = null;
let recordedChunks = [];
let capturedStream = null;   // tab audio (what plays out of the meeting)
let micStream = null;        // your own microphone
let audioContext = null;
let timerInterval = null;
let recordingStartTime = null;

// Identifies one recording end-to-end (start click -> fully processed).
// Used as the IndexedDB key so audio saved during recording can be found
// again later - by this same window on a normal finish, or by a recovery
// window if this one closes unexpectedly first.
let currentSessionId = null;

// Chunks not yet flushed to IndexedDB, and how many batches have been
// flushed so far (used as the ordering key within a session).
let pendingFlushChunks = [];
let flushIndex = 0;
const FLUSH_EVERY_N_CHUNKS = 10; // ~10s of audio per IndexedDB write, given the 1s MediaRecorder timeslice below

// Populate the mic dropdown with real device names. getUserMedia({audio:true})
// alone just grabs whatever the OS treats as default, which is often NOT the
// mic you actually picked inside Google Meet's own device settings (e.g. a
// headset vs. the laptop's built-in mic) - that mismatch is why recordings
// can end up capturing the wrong source. Requesting a throwaway stream first
// is necessary because Chrome hides device labels until permission is granted.
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

    // Restore the last-used mic if it's still available, otherwise default to the first.
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

function setStatus(text) {
  statusEl.textContent = text;
}

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
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
}

function notifySessionState(phase) {
  // phase: "recording" | "processing" | "done"
  chrome.runtime.sendMessage({ type: "SESSION_STATE", sessionId: currentSessionId, phase });
}

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

  // getMediaStreamId targets a SPECIFIC tab by ID (unlike the older
  // tabCapture.capture(), which only works on whichever tab is currently
  // focused - not useful now that recording happens in its own window).
  chrome.tabCapture.getMediaStreamId({ targetTabId }, (streamId) => {
    if (chrome.runtime.lastError || !streamId) {
      setStatus(
        "Could not capture tab audio: " +
          (chrome.runtime.lastError ? chrome.runtime.lastError.message : "unknown error") +
          ". Make sure that tab is still open."
      );
      startBtn.disabled = false;
      return;
    }

    navigator.mediaDevices
      .getUserMedia({
        audio: {
          mandatory: {
            chromeMediaSource: "tab",
            chromeMediaSourceId: streamId,
          },
        },
      })
      .then((tabStream) => {
        capturedStream = tabStream;
        setStatus("Requesting microphone access...");

        // Use the specific mic the user picked in the dropdown, instead of
        // letting the browser silently choose the OS default device (which
        // may not be the one actually selected inside Google Meet itself).
        const selectedMicId = micSelectEl.value;
        const micConstraints = selectedMicId
          ? { audio: { deviceId: { exact: selectedMicId } } }
          : { audio: true };

        // Also grab the user's own mic so both sides of the conversation get recorded.
        return navigator.mediaDevices.getUserMedia(micConstraints).then((userMicStream) => {
          micStream = userMicStream;

          audioContext = new AudioContext();

          // AudioContext can start "suspended" - especially likely here since
          // we're several .then() hops away from the original click, so the
          // browser may not treat this as directly tied to a user gesture.
          // A suspended context processes NO audio at all: MediaRecorder still
          // produces a normal-looking file, just filled with silence. Force it
          // to actually run before wiring anything up.
          const ensureRunning = audioContext.state === "running"
            ? Promise.resolve()
            : audioContext.resume();

          return ensureRunning.then(() => {
          // Mix tab audio + mic audio into a single stream for recording.
          const mixedDestination = audioContext.createMediaStreamDestination();

          // Tab audio -> goes both into the recording AND back out to speakers
          // so the user still hears the meeting normally.
          const tabSource = audioContext.createMediaStreamSource(tabStream);
          tabSource.connect(mixedDestination);
          tabSource.connect(audioContext.destination);

          // Mic audio -> goes into the recording only (NOT back to speakers,
          // otherwise the user would hear an echo of their own voice).
          const micSource = audioContext.createMediaStreamSource(micStream);
          micSource.connect(mixedDestination);

          recordedChunks = [];
          currentSessionId = crypto.randomUUID();
          pendingFlushChunks = [];
          flushIndex = 0;

          mediaRecorder = new MediaRecorder(mixedDestination.stream, {
            mimeType: "audio/webm;codecs=opus",
          });

          mediaRecorder.ondataavailable = (event) => {
            if (event.data && event.data.size > 0) {
              recordedChunks.push(event.data);
              pendingFlushChunks.push(event.data);

              // Periodically persist to IndexedDB so a crash/close mid-meeting
              // loses at most ~10s of audio, not the whole recording. This
              // matters a lot for a 2-3 hour meeting where the in-memory
              // array alone is not a safe place to keep the only copy.
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

          mediaRecorder.onstop = async () => {
            // Flush whatever's left (less than a full batch) before finalizing,
            // so the IndexedDB backup is complete right up to the stop click.
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
        setStatus(
          "Could not access microphone: " +
            err.message +
            ". Recording will only include tab audio, or you can allow mic access and try again."
        );
        if (capturedStream) {
          capturedStream.getTracks().forEach((track) => track.stop());
        }
        startBtn.disabled = false;
      });
  });
});

stopBtn.addEventListener("click", () => {
  stopBtn.disabled = true;
  setStatus("Stopping recording...");
  stopTimer();
  // Recording capture is done, but the upload/summarize/CRM-push pipeline
  // hasn't happened yet - keep the session marked as in-flight (not "done")
  // so a crash/close during upload still triggers recovery.
  notifySessionState("processing");

  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }

  if (capturedStream) {
    capturedStream.getTracks().forEach((track) => track.stop());
  }

  if (micStream) {
    micStream.getTracks().forEach((track) => track.stop());
    micStream = null;
  }

  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
});

async function handleRecordingStop() {
  setStatus("Preparing audio for upload...");

  const blob = new Blob(recordedChunks, { type: "audio/webm" });
  recordedChunks = [];

  if (blob.size === 0) {
    setStatus("No audio was captured. Try again on an active meeting tab.");
    startBtn.disabled = false;
    notifySessionState("done"); // nothing was captured, nothing to recover
    return;
  }

  resultEl.style.display = "none";

  if (saveAudioCheckbox.checked) {
    // Deliberately done BEFORE the upload, and not inside a try/catch that
    // would block it - if saving fails we log it and keep going, but if the
    // upload below fails or times out, the raw audio is already safe on
    // disk either way.
    try {
      await saveAudioLocally(blob);
    } catch (e) {
      console.warn("Failed to save raw audio locally:", e);
    }
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
    resultEl.textContent =
      "Summary:\n" +
      data.summary +
      "\n\nCRM push status: " +
      (data.crm_push ? data.crm_push.status : "unknown") +
      pdfNote;

    // Upload succeeded - the IndexedDB backup has served its purpose and can
    // be cleared, and the session is now safe to forget for recovery purposes.
    await deleteSessionFromDB(currentSessionId).catch(() => {});
    notifySessionState("done");
  } else {
    setStatus(
      "Failed to process meeting: " + result.error +
      ". The recorded audio is still saved and safe - reopen the extension to recover it."
    );
    // Deliberately NOT marking the session "done" and NOT deleting the
    // IndexedDB backup here - if this window is closed after a failed
    // upload, background.js's crash-recovery will still be able to retry it.
  }

  startBtn.disabled = false;
  timerEl.textContent = "00:00";
}

// Called by auth.js once a session is confirmed (fresh login or restored
// from storage) - everything here needs to know who's logged in, so none
// of it runs until then.
function onAppScreenReady() {
  populateMicList();
  checkForRecoverableSession();
  updateStartAvailability();
}
