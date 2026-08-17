# Meeting Summarizer & CRM Sync — Architecture & System Workflow

## 1. System Overview

**Meeting Summarizer & CRM Sync** is a two-tier application that records audio from a browser meeting tab (Google Meet, Zoom, etc.), transcribes it with speaker diarization, summarizes it with an LLM, and optionally pushes the summary into a user's HubSpot CRM as a Note and/or exports it as a local PDF.

The system is composed of two independently deployable parts:

- **A Chrome Extension (Manifest V3)** — the client. Captures tab + microphone audio in the browser, manages recording state, persists in-progress audio for crash recovery, uploads the finished recording to a backend, and (via an auth screen + settings panel) handles account login/registration and lets the user manage their saved Groq and HubSpot keys.
- **A Python FastAPI backend** (single-file, `main.py`) — the server. Owns user accounts (username/password) and each account's Groq/HubSpot API keys via Supabase, receives the uploaded audio, and orchestrates a multi-stage processing pipeline (preprocess → diarized transcription → summarize → push to CRM → optional PDF).

**Architectural pattern:** This is a **client-server / modular monolith** hybrid:
- The extension itself follows an **event-driven** pattern (Chrome extension lifecycle events, `MediaRecorder` events, message-passing between extension contexts).
- The backend is a **modular monolith with a linear processing pipeline** (a single FastAPI service organized into clearly separated numbered "steps," rather than separate microservices), fronted by one primary REST endpoint (`/process-meeting`) plus a small set of account endpoints (`/auth/register`, `/auth/login`, `/user/keys`).

**Transcription architecture:** audio is not split into independent chunks for transcription. ElevenLabs Scribe v2 diarization (`speaker_id` values) is only consistent *within a single request*, so the whole (downsampled/compressed) recording is sent to ElevenLabs as one request with `diarize=true`, and the resulting word-level output is reshaped into speaker turns and a timestamped, speaker-labeled transcript. The map-reduce step used for long-meeting summarization still splits the transcript into sections, but only along turn boundaries, so speaker/timestamp attribution is never broken mid-line.

**Accounts & credential storage:** there is no anonymous per-device ID anymore. A user registers or logs into a real account (username + bcrypt-hashed password) via `/auth/register` / `/auth/login`, both backed by a Supabase `users` table. Each account row also holds that user's own `groq_api_key` and (optionally) `hubspot_api_key`, entered once via the extension's settings panel and edited there any time. Every `/process-meeting` request supplies only `user_id`; the backend looks both keys up from Supabase server-side. Neither key is ever sent from the extension on a recording upload, held in `chrome.storage.local`, or returned back down to the extension once saved — the settings panel only ever sees a "key is set" boolean (`GET /user/keys`). A request from an account with no Groq key set is rejected outright (`400`) before any audio processing starts.

**HubSpot connection:** there is no OAuth flow, no client ID/secret, and no redirect URIs. The user generates their own HubSpot **Private App access token** from their own HubSpot account (Settings → Integrations → Private Apps → Create a private app → grant CRM notes read/write → copy the token) and pastes it into the settings panel, the same simple paste-and-save pattern as the Groq key. It's stored as `hubspot_api_key` and used directly as a Bearer token on every CRM push — no refresh flow needed, since Private App tokens don't expire the way OAuth access tokens do.

There is no shared database between client and server beyond what's passed over HTTP — the extension holds transient recording/session state in `chrome.storage.local` and audio chunks in `IndexedDB`; the backend holds accounts and per-account keys in Supabase (Postgres) and nothing else persistent. The backend does not persist recordings, transcripts, or summaries — everything downstream of a single `/process-meeting` request is stateless and discarded (temp files/dirs deleted) after the response is returned.

---

## 2. End-to-End System Workflow

### Step-by-step flow

1. **User clicks the extension toolbar icon.** `background.js` opens a persistent popup window (not the default MV3 popup, since that auto-closes when the OS microphone permission dialog steals focus). It remembers which tab the user was on so the recorder window knows which tab to capture.
2. **`popup.html` loads `session-utils.js`, then `auth.js`, then `popup.js`.** On `DOMContentLoaded`, `auth.js` checks `chrome.storage.local` for a saved `meetingSummarizerSession` (`{ user_id, username }`). If none exists (or it's the user's first time), the **auth screen** is shown: a username/password form that can register a new account (`POST /auth/register`) or log into an existing one (`POST /auth/login`).
3. **Once logged in**, `auth.js` saves the session to `chrome.storage.local`, calls `enterAppScreen()`, which fetches key status (`GET /user/keys`) and then calls `popup.js`'s `onAppScreenReady()` — this populates the microphone dropdown and checks for any unfinished prior session to offer recovery. Nothing recording-related runs until a session is known.
4. **Settings panel (gear icon in the header).** Opens a slide-in panel showing: the logged-in username with a Log out button, a Groq API key row (shows "saved"/"Not set", with an Edit form to type and save a new value), and a HubSpot API key row (same pattern, optional), plus the "save summary as PDF" and "save raw audio" checkboxes. Editing a key always means typing a brand-new value and saving it (`PUT /user/keys`) — values are never fetched back down once saved, only whether one is set. **The "Start recording" button stays disabled, and a warning banner is shown, until the account has a Groq key saved** — avoiding recording an entire meeting only to fail at the summarization step.
5. **User clicks "Start recording"** (only enabled once a Groq key is set on the account). The extension captures tab audio (`chrome.tabCapture.getMediaStreamId` + `getUserMedia`) and microphone audio (`getUserMedia` against the selected mic device), mixes both streams via the Web Audio API into one `MediaStream` (mic audio does not loop back to speakers, to avoid echo), and starts a `MediaRecorder` (`audio/webm;codecs=opus`, 1s timeslice).
6. **During recording:** audio chunks are collected in memory and also periodically flushed to **IndexedDB** (batched roughly every 10 chunks / ~10s of audio) via `session-utils.js`, so a crash or accidental window close loses at most a few seconds of audio, not the whole meeting.
7. **User clicks "Stop & process."** The recorder stops, remaining unflushed audio is written to IndexedDB, and the full recording (all chunks) is assembled into one `Blob` and uploaded via `fetch()` as `multipart/form-data` to the backend's `POST /process-meeting` endpoint, sending only `file`, `user_id`, and `save_locally` — no key material travels with the upload.
8. **Backend receives the upload.** It first looks up the account's row in Supabase from `user_id` and checks that a Groq key is present, rejecting with `400` immediately if not (before writing any temp file or touching audio). Otherwise it writes the upload to a temp file and runs it through the processing pipeline:
   - **Preprocessing (`preprocess_audio`)** — pydub/ffmpeg downsamples the whole recording to 16kHz mono and re-exports it as a single compressed MP3 (32kbps). Audio is **not split into chunks** for transcription — see the diarization note below.
   - **Diarized transcription (`transcribe_audio_with_diarization`)** — the entire preprocessed file is POSTed to ElevenLabs' Scribe v2 endpoint **in one request** with `diarize=true`, word-level timestamps, and audio-event tagging. This is deliberate: ElevenLabs' `speaker_id` values (`speaker_0`, `speaker_1`, ...) are only consistent *within a single request*, so transcribing pieces separately would produce unrelated "Speaker 1" labels that don't refer to the same person across chunks. The request retries up to 3 times (5s/10s backoff) using a single generous timeout (`ELEVENLABS_TIMEOUT = 1800.0`) applied uniformly to connect/write/read, to avoid a known `requests`/urllib3 quirk where a tuple timeout can kill a large upload's write phase prematurely. The word-level response is collapsed into speaker turns and formatted as a timestamped, speaker-labeled transcript (`[HH:MM:SS] Speaker N: ...`).
   - **Summarization (`summarize_transcript`)** — the diarized transcript is sent to Groq's **`openai/gpt-oss-120b`** model (via `langchain-groq`), authenticated with **that account's own Groq API key** (looked up from Supabase, never a server-side key). `reasoning_effort="medium"` is passed as a top-level `ChatGroq` constructor argument. Short transcripts go through in a single call; long transcripts (>3,000 words) are split into ~2,200-word sections **without ever breaking a `[HH:MM:SS] Speaker N: ...` line in half** (to preserve speaker/timestamp attribution), summarized individually (map step), then combined into one final structured summary (reduce step). Prompts explicitly instruct the model to attribute points to specific speakers wherever the transcript supports it. Response text is defensively cleaned of any leaked gpt-oss "Harmony" channel markers before use.
   - **CRM push (`push_to_crm`)** — if the account has a saved HubSpot Private App token, the structured summary is posted to HubSpot as a CRM Note (`POST /crm/v3/objects/notes`) using that token as a Bearer credential. If not set, this step is skipped without failing the request.
   - **PDF export (`generate_summary_pdf`)** — if `save_locally=true` was passed, the summary (Markdown-aware rendering of the `# Title` / `## N. ...` structure) is rendered into a PDF via `fpdf2`, with the full diarized transcript appended on a following page, and returned as base64 in the response for the extension to save via `chrome.downloads`.
9. **Backend returns a JSON response** containing the meeting title, a structured transcript object (formatted text, plain text, per-speaker turns with timestamps, detected speaker count, and language code), the summary, CRM push result, and (optionally) the base64 PDF.
10. **Extension receives the response**, displays the summary in the popup, triggers the local PDF download if applicable, and clears the IndexedDB backup for that session (since it's no longer needed) by marking the session `"done"`.
11. **Crash/interruption recovery path:** if the popup window closes before step 10 completes (crash, accidental close, browser hiccup), `background.js` detects the incomplete session (tracked via `chrome.storage.local`'s `activeSession`) and opens `recovery.html`, which re-reads the saved audio from IndexedDB and re-runs the same upload-and-process flow (`recovery.js`, sharing `uploadRecording()` from `session-utils.js`). `recovery.js` reads the saved `meetingSummarizerSession` from `chrome.storage.local`; if the user isn't logged in, it stops and tells them to log in via the popup before retrying (the Groq/HubSpot keys themselves are never checked or sent from the recovery window — the backend looks them up from the account once it's uploaded).

### Separate flow: account login / registration

1. User opens the popup for the first time (or after logging out) → auth screen is shown.
2. User enters a username + password and clicks **Register** or **Log in**.
3. `POST /auth/register` (new account) or `POST /auth/login` (existing account) is called against the backend.
4. On success, the backend returns `{ user_id, username, has_groq_key, has_hubspot_key }` (password hash is stripped before the response ever leaves the server); the extension saves `{ user_id, username }` to `chrome.storage.local` as `meetingSummarizerSession` and enters the main app screen.
5. From any computer, logging into the same account immediately has that account's previously-saved Groq/HubSpot keys available — keys follow the account, not the browser profile.

### Mermaid diagram

```mermaid
flowchart TD
    A[User clicks extension icon] --> B[background.js opens<br/>persistent popup window]
    B --> C{Session saved in<br/>chrome.storage.local?}
    C -->|no| C1[Show auth screen:<br/>Register / Log in]
    C1 --> C2[POST /auth/register<br/>or /auth/login]
    C2 --> C3[Save session,<br/>enter app screen]
    C -->|yes| C3
    C3 --> D0[GET /user/keys<br/>check Groq/HubSpot status]
    D0 --> D1{Groq key<br/>saved on account?}
    D1 -->|no| D2[Start button disabled,<br/>missing-key banner shown]
    D1 -->|yes| E[User clicks Start Recording]
    D2 -.user opens gear icon,<br/>saves key via PUT /user/keys.-> E
    E --> F[Capture tab audio +<br/>microphone via Web Audio API]
    F --> G[MediaRecorder records<br/>mixed stream]
    G -->|every ~10s| H[(IndexedDB<br/>chunk backup)]
    E -.crash/close.-> I[background.js detects<br/>incomplete session]
    I --> J[Opens recovery.html]
    J --> J2{Logged in<br/>(session saved)?}
    J2 -->|no| J3[Show 'log in first',<br/>wait for Retry]
    J2 -->|yes| H
    G --> K[User clicks Stop & Process]
    K --> L[Assemble full Blob]
    L --> M[POST /process-meeting<br/>multipart upload: file + user_id + save_locally]
    J --> M

    M --> M2[FastAPI: look up account<br/>+ Groq/HubSpot keys in Supabase]
    M2 --> M3{Groq key<br/>set on account?}
    M3 -->|no| M4[400 - reject before<br/>any audio processing]
    M3 -->|yes| N[Save temp file]
    N --> O[preprocess_audio<br/>pydub + ffmpeg<br/>whole file, 16kHz mono MP3]
    O --> P[transcribe_audio_with_diarization<br/>ElevenLabs Scribe v2<br/>single request, diarize=true,<br/>retry+backoff]
    P --> Q[summarize_transcript<br/>Groq gpt-oss-120b<br/>using the account's own key<br/>speaker-attributed, map-reduce if long]
    Q --> R{HubSpot key<br/>set on account?}
    R -->|yes| S[push_to_crm<br/>POST note to HubSpot API]
    R -->|no| T[Skip CRM push]
    Q --> U{save_locally?}
    U -->|yes| V[generate_summary_pdf<br/>fpdf2, summary + full transcript]
    U -->|no| W[Skip PDF]
    S --> X[Return JSON response]
    T --> X
    V --> X
    W --> X
    X --> Y[popup.js / recovery.js:<br/>show summary,<br/>trigger PDF download,<br/>clear IndexedDB backup]
```

---

## 3. Libraries & Dependencies Analysis

| Library / Package | Category / Type | Specific Purpose in This Project | Why It's Used |
| :--- | :--- | :--- | :--- |
| `fastapi` | Web framework | Defines the backend's HTTP API (`/auth/*`, `/user/keys`, `/process-meeting`, `/health`) | Async-capable, type-hint-driven request validation, minimal boilerplate for a single-file service |
| `uvicorn` | ASGI server | Runs the FastAPI app (`uvicorn.run(app, ...)`) | Standard production-grade ASGI server for FastAPI |
| `pydub` | Audio processing | Loads the uploaded recording, downsamples to 16kHz mono, and exports the whole file as one compressed MP3 (32kbps) — no chunking, so ElevenLabs diarization stays consistent across the recording | Simplifies audio manipulation via a Pythonic API over ffmpeg |
| `requests` | HTTP client | All outbound calls to ElevenLabs and HubSpot's CRM Notes API, and (in test scripts) Groq/httpbin.org | Synchronous, well-understood HTTP client with fine-grained timeout control |
| `python-dotenv` | Configuration | Loads `ELEVENLABS_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` from a `.env` file. Groq and HubSpot keys are deliberately **not** loaded from `.env` — there is no server-side key for either; each is stored per-account in Supabase and entered by the user in the settings panel. | Keeps secrets out of source code |
| `langchain-groq` | LLM integration | Wraps the Groq `openai/gpt-oss-120b` chat model (`ChatGroq`) for the summarization step | Provides a consistent message-based interface (`SystemMessage`/`HumanMessage`) over Groq's API |
| `groq` (SDK) | LLM client | Used directly in `test_groq.py` for isolated connectivity testing | Lower-level access to the Groq API for debugging outside the LangChain wrapper |
| `fpdf2` (`fpdf`) | PDF generation | Renders the meeting title + structured summary + full transcript into a downloadable PDF (`generate_summary_pdf`) | Lightweight, dependency-free PDF creation without a headless browser |
| `pydantic` | Data validation | Defines request/response models (`RegisterRequest`, `LoginRequest`, `UpdateKeysRequest`, `CrmPushResult`) | Type-safe request/response schemas, integrates natively with FastAPI |
| `supabase` | Backend-as-a-service client | Reads/writes the `users` table (accounts, password hashes, Groq/HubSpot keys) via `create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)` | Managed Postgres with a simple table API — replaces the old local SQLite token store, and lets a user's keys follow their account across machines |
| `bcrypt` | Security | Hashes and verifies account passwords (`bcrypt.hashpw` / `bcrypt.checkpw`) | Industry-standard adaptive password hashing; no plaintext password is ever stored |
| `tempfile` (stdlib) | File handling | Creates temp files/directories for uploaded and preprocessed audio | Ensures per-request working files are isolated and easy to clean up |
| `logging` (stdlib) | Observability | Structured log output across every pipeline stage | Debuggability for a pipeline with multiple external API dependencies |
| **Chrome Extension APIs** | Browser platform | `chrome.tabCapture`, `chrome.storage.local`, `chrome.windows`, `chrome.downloads`, `chrome.runtime` | Native browser APIs required for tab audio capture, persistent extension/session state, and file downloads — no external JS libraries are used in the extension |
| **IndexedDB** (browser built-in) | Client-side storage | Persists audio chunks during recording for crash recovery (`session-utils.js`) | Only browser storage mechanism capable of holding large binary blobs across a session |
| **Web Audio API** (browser built-in) | Audio processing | `AudioContext`, `createMediaStreamSource`, `createMediaStreamDestination` — mixes tab + mic audio into one stream | Native, dependency-free way to combine two live audio sources in-browser |

---

## 4. Core Modules & Component Architecture

### Backend (`main.py`) — single-file FastAPI service, organized into numbered steps

| Module/Section | Responsibility |
| :--- | :--- |
| **Config** | Loads environment variables, sets up logging, CORS, the Supabase client, and global constants (model names, thresholds, timeouts). |
| **Step 0 — Accounts & per-user API keys** (`/auth/register`, `/auth/login`, `GET /user/keys`, `PUT /user/keys`, `_get_user_row`, `_public_user`) | Supabase-backed persistence for accounts (bcrypt password hashing) and each account's `groq_api_key` / `hubspot_api_key`. `_public_user` always strips the password hash before a row is returned to the extension, and `/user/keys` exposes only booleans (`has_groq_key`/`has_hubspot_key`), never the key values themselves. |
| **Step 1 — Audio preprocessing** (`preprocess_audio`) | Converts the raw uploaded audio into one downsampled, compressed, transcription-ready file (16kHz mono MP3, 32kbps) — the whole recording, not chunks. |
| **Step 2 — Diarized transcription** (`transcribe_audio_with_diarization`, `_build_speaker_turns`, `_format_timestamp`, `ELEVENLABS_TIMEOUT`) | Sends the entire file to ElevenLabs Scribe v2 in one request with `diarize=true` and word-level timestamps (retry/backoff, single generous timeout), then collapses the word-level output into speaker turns and a formatted, timestamped, speaker-labeled transcript. |
| **Step 3 — Summarization** (`summarize_transcript`, `_split_into_sections`, `_get_summary_llm`, `_extract_response_text`) | Produces structured meeting notes via Groq `openai/gpt-oss-120b`, authenticated with the requesting account's own Groq API key (looked up server-side, no server-side fallback), using a map-reduce strategy for long transcripts and defensively stripping any leaked reasoning-channel markers from the response. |
| **Step 3.5 — PDF export** (`generate_summary_pdf`, `sanitize_filename`) | Optional, user-triggered rendering of the summary (plus, if provided, the full transcript on a following page) into a downloadable PDF; never persisted server-side. |
| **Step 4 — CRM push** (`push_to_crm`) | Posts the summary as a HubSpot CRM Note using the account's saved Private App access token as a Bearer credential; degrades gracefully (skips) if no token is set. |
| **API endpoint** (`/process-meeting`, `/health`) | Looks up the account from `user_id`, validates a Groq key is set (rejects with `400` before any processing if not), then orchestrates steps 1–4 in sequence for a single request; `/health` reports configuration status (ElevenLabs key, Supabase connectivity; Groq/HubSpot reported as "per-account") without exposing secrets. |

### Extension

| File | Responsibility |
| :--- | :--- |
| `manifest.json` | MV3 manifest declaring permissions (`tabCapture`, `activeTab`, `storage`, `scripting`, `downloads`, `unlimitedStorage`) and registering `background.js` as the service worker. `host_permissions` restricts network access to `http://127.0.0.1:8000/*`. |
| `background.js` | Persistent (event-driven) service worker. Opens the recorder popup window on icon click (remembering the target tab), tracks recording/session state in `chrome.storage.local`, updates the toolbar badge, and triggers crash recovery by opening `recovery.html` when the recorder window closes mid-session. |
| `auth.js` | The auth screen (register/login/logout) and the settings panel's key-editing UI. Owns the `meetingSummarizerSession` (`{ user_id, username }`) saved to `chrome.storage.local` — no password is ever stored locally. Fetches/renders Groq and HubSpot "key is set" status (`GET /user/keys`) and saves edits (`PUT /user/keys`). Calls `popup.js`'s `onAppScreenReady()` once a session is confirmed. |
| `popup.html` / `popup.js` | The main recording UI: mic selection, timer, start/stop controls, result display, settings-panel open/close, and the recovery banner. Owns the actual `MediaRecorder` capture logic and gates the Start button on `hasGroqKey` (set by `auth.js`). |
| `recovery.html` / `recovery.js` | A minimal UI shown only after an interrupted session is detected. Re-reads unfinished audio from IndexedDB, reads the saved `meetingSummarizerSession` from `chrome.storage.local` (prompting the user to log in via the popup if missing), and re-runs the same upload pipeline as `popup.js`. |
| `session-utils.js` | Shared utility module (loaded by both `popup.html` and `recovery.html`) providing: IndexedDB chunk read/write/delete functions, `saveAudioLocally()` for the optional raw-audio download, and the single `uploadRecording()` function used by both normal completion and recovery flows — sending only `file`, `user_id`, and `save_locally` (no key material), ensuring both paths hit the backend identically. |

### Interaction pattern

- `background.js` is the only long-lived component; it coordinates window lifecycle and crash detection but does **not** touch audio directly.
- `auth.js` owns the account/session and key-editing UI; it loads before `popup.js` so `enterAppScreen()` can safely call `popup.js`'s `onAppScreenReady()` once a session exists.
- `popup.js` and `recovery.js` are two independent entry points into the **same** upload/processing logic (`uploadRecording`), which prevents drift between the "happy path" and "recovery path."
- The backend has **no knowledge of the extension's internal recording state** — it only ever sees a finished audio file plus a `user_id` per request, keeping the client/server boundary clean. All credential lookup happens server-side from that `user_id`.

---

## 5. Data Flow & Execution Sequence

### Request/response cycle for `POST /process-meeting`

1. **Input:** `multipart/form-data` containing `file` (audio blob), `user_id` (string, required), `save_locally` (boolean, default `False`), `num_speakers` (optional int).
2. **Account + key lookup (before any I/O):** `_get_user_row(user_id)` fetches the account from Supabase (404 if not found); `groq_api_key` is read from that row and stripped, and a missing/blank value is rejected immediately with `400` — before the temp file write or any audio work. Only after that does the upload get streamed to a `NamedTemporaryFile` on disk; an empty file is rejected immediately (`400`) too.
3. **Preprocessing (synchronous, local compute):** `preprocess_audio()` shells out to ffmpeg via pydub — no network call. Produces one downsampled/compressed MP3 in a fresh temp directory (no chunk files).
4. **Diarized transcription (single external API call, retried):** `transcribe_audio_with_diarization()` sends the whole preprocessed file to ElevenLabs in one request (`diarize=true`), with up to 3 attempts (5s/10s backoff) against transient failures. On success, word-level output is collapsed into speaker turns and a formatted transcript. If the request never succeeds, or if it succeeds but produces no usable text, the endpoint returns `422`/`500` with the underlying error(s) — there's no partial-chunk fallback since it's a single request for the whole recording.
5. **Summarization (external API call(s)):** `summarize_transcript()` makes either one or several sequential calls to Groq's `openai/gpt-oss-120b` depending on transcript length (word-count threshold of 3,000 triggers map-reduce), authenticated with the account's own Groq API key looked up in step 2 — never a server-side key. Each call is a blocking `llm.invoke()`.
6. **Title extraction:** a regex (`extract_meeting_title`) pulls the leading Markdown `# Title` line out of the structured summary text — pure in-memory string processing, no I/O.
7. **CRM push (external API call, conditional):** `push_to_crm()` uses the account's saved HubSpot Private App token (if any) directly as a Bearer token and POSTs a Note object to HubSpot's CRM API — no token refresh needed. Failure here is caught and reported in the response (`status: "failed"`) rather than raising — the rest of the pipeline's output is still returned to the user.
8. **PDF generation (conditional, local compute):** only runs if `save_locally=true`; renders the summary plus, on a following page, the full diarized transcript; failure is caught and reported inline (`pdf.error`) without failing the whole request.
9. **Response assembly:** all stage outputs (`meeting_title`, `transcript`, `summary`, `crm_push`, `pdf`) are combined into one JSON object and returned with `200`.
10. **Cleanup (`finally` block):** the temp input file and the entire preprocessing work directory are deleted regardless of success or failure, so no audio or intermediate files persist on the server after the request completes.

### State changes and side effects

- **Persistent state changed by `/process-meeting`:** none directly (HubSpot's own CRM data changes as a side effect of the push, but nothing is written to Supabase during this request — the Groq/HubSpot keys are only *read*). The account's Groq key is used in-memory for the duration of the request only (passed straight into the `ChatGroq` client) and is never written to disk, logged, or stored anywhere beyond the account row it already lived in.
- **Persistent state changed by the account endpoints:** `POST /auth/register` inserts a new row into Supabase's `users` table; `PUT /user/keys` updates `groq_api_key` and/or `hubspot_api_key` on an existing row (only the fields actually supplied, or explicitly flagged for clearing, are touched).
- **Client-side state changes per recording:** IndexedDB gains chunk rows during recording, and they're deleted (`deleteSessionFromDB`) only after a confirmed-successful upload — guaranteeing the backup outlives any failure that would otherwise lose the recording. Separately, `chrome.storage.local` holds `meetingSummarizerSession` (`{ user_id, username }`, no password) once logged in — this persists across popup opens so the user doesn't have to log in every time, and the same account (with its keys) can be logged into from any computer.

### Background/async behavior

- The backend endpoint itself is `async def` (FastAPI), but the actual pipeline steps (`preprocess_audio`, `transcribe_audio_with_diarization`, `summarize_transcript`, `push_to_crm`) are synchronous, blocking functions called sequentially within it — there is no background task queue or worker process; a single request occupies the handler for the full duration of the pipeline (audio processing + all external API round-trips).
- On the client, the only asynchronous/background behavior is the periodic IndexedDB flush during recording (`FLUSH_EVERY_N_CHUNKS = 10`), which runs opportunistically inside the `MediaRecorder.ondataavailable` handler without blocking the recording itself.
