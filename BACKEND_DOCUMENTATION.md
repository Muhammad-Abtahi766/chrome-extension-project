# Backend Documentation — `main.py`

**Meeting Summarizer & CRM Sync Pipeline** — a single-file FastAPI service.

This document explains every section, function, and meaningful line of `main.py` in detail — what it does, why it's written that way, and how it connects to the rest of the pipeline.

---

## 1. High-Level Pipeline

```
account login (username/password, Supabase)
   -> audio upload
   -> preprocess (pydub: downsample to 16kHz mono, compress to 32kbps mp3)
   -> ElevenLabs Scribe v2 transcription WITH SPEAKER DIARIZATION
      (one single request for the WHOLE recording, not chunked)
   -> Groq gpt-oss-120b summarization (map-reduce for long meetings)
   -> CRM push (HubSpot, per-account Private App token)
   -> optional local PDF export (summary + full transcript)
```

**Why the whole recording is sent as ONE request (not chunks):** ElevenLabs' diarization assigns speaker IDs (`speaker_0`, `speaker_1`, ...) that are only consistent *within a single API call*. If the recording were split into independent chunks and each diarized separately, "Speaker 1" in chunk 3 would not refer to the same person as "Speaker 1" in chunk 9. So the whole file goes out as one upload, and is only downsampled/compressed beforehand to keep that single upload as small as practical.

**Why accounts instead of an anonymous per-device ID:** every user needs somewhere durable to keep their own Groq API key (required) and HubSpot Private App token (optional) that follows them across machines, not just across recordings on one browser profile. A real username/password account, stored in Supabase, does that — log into the same account from a different computer and both keys are already there.

---

## 2. Setup Requirements (from the module docstring)

- Install: `fastapi uvicorn[standard] pydub langchain-groq python-dotenv requests python-multipart fpdf2 supabase bcrypt` (see `requirements.txt`).
- **ffmpeg** must be installed and on `PATH` — `pydub` shells out to it for audio conversion.
- `.env` file needs:
  - `ELEVENLABS_API_KEY` — server-side key used for transcription (shared across all accounts, from elevenlabs.io/app/settings/api-keys).
  - `SUPABASE_URL` — from the Supabase project's Settings → API.
  - `SUPABASE_SERVICE_KEY` — the project's **service_role** secret key, same page. **Never** ship this to the extension; it's server-side only.
- **Groq and HubSpot are NOT configured server-side at all.** Each user registers an account and pastes their own Groq API key and (optionally) their own HubSpot Private App token into the extension's settings panel. Both are stored per-account in the Supabase `users` table and looked up by `user_id` on every `/process-meeting` request — there is no shared server-side key for either, and no HubSpot OAuth client ID/secret anywhere in this file.
- Before first run, create the `users` table once in the Supabase SQL editor:
  ```sql
  create table users (
    id uuid primary key default gen_random_uuid(),
    username text unique not null,
    password_hash text not null,
    groq_api_key text,
    hubspot_api_key text,
    created_at timestamptz default now()
  );
  ```
- Run with `python main.py` or `uvicorn main:app --host 127.0.0.1 --port 8000 --reload`.

---

## 3. Imports

```python
import os, re, time, base64, shutil, logging, tempfile
from typing import List, Tuple, Dict, Any, Optional

import bcrypt
import requests
from dotenv import load_dotenv
from pydub import AudioSegment
from fpdf import FPDF
from supabase import create_client, Client

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage
```

| Import | Purpose |
|---|---|
| `os`, `re`, `time`, `base64`, `shutil`, `tempfile` | Filesystem/temp-file handling, regex for parsing, timing, base64-encoding the PDF for the JSON response, deleting temp work directories. |
| `logging` | Structured logging (`logger.info`, `.warning`, `.error`) throughout the pipeline so failures are traceable. |
| `bcrypt` | Hashes and verifies account passwords. No plaintext password is ever stored or logged. |
| `requests` | All outbound HTTP calls: ElevenLabs transcription, HubSpot CRM Note push, and (in test scripts) Groq/httpbin.org. |
| `dotenv.load_dotenv` | Loads `.env` into `os.environ`. |
| `pydub.AudioSegment` | Audio loading/resampling/exporting (via ffmpeg). |
| `fpdf.FPDF` | Renders the meeting summary (and, optionally, the full transcript) as a downloadable PDF. |
| `supabase.create_client` / `Client` | Talks to the Supabase-hosted Postgres `users` table — accounts, password hashes, and per-account Groq/HubSpot keys. |
| `fastapi.*` | Web framework: routing, file uploads, form fields, query params, error responses. |
| `pydantic.BaseModel` | Typed request bodies (`RegisterRequest`, `LoginRequest`, `UpdateKeysRequest`) and the `CrmPushResult` response shape. |
| `langchain_groq.ChatGroq` / `langchain_core.messages` | Wraps the Groq `openai/gpt-oss-120b` chat API for summarization calls. |

---

## 4. Configuration Block

```python
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)
```
Loads environment variables and sets up a logger that prefixes every line with a timestamp and level (`INFO`, `WARNING`, `ERROR`).

```python
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
```
The server's own ElevenLabs key — this one IS shared across all accounts (unlike Groq/HubSpot).

```python
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    logger.warning(
        "SUPABASE_URL / SUPABASE_SERVICE_KEY not set - account registration/login and "
        "per-user API key storage will not work until these are configured."
    )
```
If either Supabase env var is missing, `supabase` stays `None` and a warning is logged at startup rather than crashing immediately — the process still comes up (useful for e.g. checking `/health`), but any endpoint that needs the database will fail clearly via `_require_supabase()` (see Step 0).

```python
ELEVENLABS_MODEL = "scribe_v2"
SUMMARY_MODEL = "openai/gpt-oss-120b"
DEFAULT_NUM_SPEAKERS: Optional[int] = None
LLM_TEMPERATURE = 0.2
```
- `SUMMARY_MODEL` is OpenAI's open-weight `gpt-oss-120b`, served on Groq — swapped in from an earlier `llama-3.3-70b-versatile`. Same ~131K context window, so the map-reduce chunking (`WORDS_PER_SECTION`) didn't need to change, but it reasons noticeably better over the multi-section synthesis the summarization prompt asks for, and is cheaper per-token than Llama 3.3 70B.
- `DEFAULT_NUM_SPEAKERS = None` means ElevenLabs auto-detects speaker count unless the caller explicitly passes `num_speakers`.
- `LLM_TEMPERATURE = 0.2` keeps summaries fairly deterministic/factual rather than creative.

```python
app = FastAPI(title="Meeting Summarizer & CRM Sync Pipeline")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)
```
CORS is wide open (`*`) — necessary because the request originates from a Chrome extension context, not a normal web origin, and `allow_credentials=False` means no cookies are involved (auth is via `user_id` in the request body/query, not cookies/sessions).

```python
class CrmPushResult(BaseModel):
    status: str
    detail: Dict[str, Any] = {}
```
A typed shape for CRM push results (declared but `push_to_crm()` returns plain dicts matching this shape informally).

---

## 5. STEP 0 — Accounts & Per-User API Keys (Supabase)

**Why this exists:** a user's Groq/HubSpot keys need to follow their **account**, not one specific browser/computer, so they can log into the same account from any machine and their keys are already there. This replaces an earlier design based on a per-device random `user_id` plus a local SQLite HubSpot-OAuth-token table.

```python
def _require_supabase() -> Client:
    if supabase is None:
        raise HTTPException(
            status_code=500,
            detail="Backend is not configured with Supabase credentials (SUPABASE_URL / SUPABASE_SERVICE_KEY).",
        )
    return supabase
```
Every endpoint that touches the database calls this first — if Supabase wasn't configured at startup, callers get an immediate, clearly-worded `500` instead of a confusing `AttributeError` on `None`.

```python
def _public_user(row: Dict[str, Any]) -> Dict[str, Any]:
    """Strips the password hash before a user row ever goes back to the extension."""
    return {
        "user_id": row["id"],
        "username": row["username"],
        "has_groq_key": bool(row.get("groq_api_key")),
        "has_hubspot_key": bool(row.get("hubspot_api_key")),
    }
```
The single choke point that shapes any Supabase `users` row into what's safe to send to the client: no `password_hash`, and the actual key **values** are never included — only booleans for whether each is set. Used by `/auth/register`, `/auth/login`, and `GET /user/keys`.

### Request models

```python
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class UpdateKeysRequest(BaseModel):
    user_id: str
    groq_api_key: Optional[str] = None
    hubspot_api_key: Optional[str] = None
    clear_groq_api_key: bool = False
    clear_hubspot_api_key: bool = False
```
`UpdateKeysRequest` supports both setting a key to a new value and explicitly clearing it (`clear_*` flags) without the two being conflated — an omitted field is left untouched, not cleared.

### `POST /auth/register`
```python
@app.post("/auth/register")
def register(payload: RegisterRequest):
    db = _require_supabase()
    username = payload.username.strip()
    password = payload.password

    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    existing = db.table("users").select("id").eq("username", username).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="That username is already taken.")

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    result = db.table("users").insert({"username": username, "password_hash": password_hash}).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Could not create account.")

    return _public_user(result.data[0])
```
Validates minimum username/password length, checks for a username collision (`409` if taken), hashes the password with bcrypt (a fresh random salt via `bcrypt.gensalt()` every time), inserts the new row, and returns the public shape (`user_id`, `username`, and both key booleans — `false`/`false` for a brand-new account).

### `POST /auth/login`
```python
@app.post("/auth/login")
def login(payload: LoginRequest):
    db = _require_supabase()
    username = payload.username.strip()

    result = db.table("users").select("*").eq("username", username).execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    row = result.data[0]
    if not bcrypt.checkpw(payload.password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    return _public_user(row)
```
Looks up by username, verifies the password with `bcrypt.checkpw`. Deliberately uses the **same** `401` detail message ("Incorrect username or password") whether the username doesn't exist or the password is wrong — this avoids leaking which usernames are registered (username enumeration).

### `GET /user/keys`
```python
@app.get("/user/keys")
def get_user_keys(user_id: str = Query(...)):
    db = _require_supabase()
    result = db.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found.")
    return _public_user(result.data[0])
```
Used by the settings panel to show whether Groq/HubSpot keys are already set. Returns only the booleans via `_public_user` — actual key values are never sent back down to the extension once saved.

### `PUT /user/keys`
```python
@app.put("/user/keys")
def update_user_keys(payload: UpdateKeysRequest):
    db = _require_supabase()
    existing = db.table("users").select("id").eq("id", payload.user_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Account not found.")

    updates: Dict[str, Any] = {}
    if payload.clear_groq_api_key:
        updates["groq_api_key"] = None
    elif payload.groq_api_key is not None and payload.groq_api_key.strip():
        updates["groq_api_key"] = payload.groq_api_key.strip()

    if payload.clear_hubspot_api_key:
        updates["hubspot_api_key"] = None
    elif payload.hubspot_api_key is not None and payload.hubspot_api_key.strip():
        updates["hubspot_api_key"] = payload.hubspot_api_key.strip()

    if not updates:
        raise HTTPException(status_code=400, detail="No key changes provided.")

    result = db.table("users").update(updates).eq("id", payload.user_id).execute()
    return _public_user(result.data[0])
```
Lets the user edit/replace their saved Groq and/or HubSpot keys from the settings panel at any time, from any computer. Only the fields actually provided (or explicitly flagged for clearing) are touched — omitted fields are left as-is. A request with no actual changes is rejected with `400` rather than silently succeeding.

### `_get_user_row(user_id)`
```python
def _get_user_row(user_id: str) -> Dict[str, Any]:
    db = _require_supabase()
    result = db.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found. Please log in again.")
    return result.data[0]
```
The internal (non-`_public_user`) lookup used by `/process-meeting` — this one needs the **actual** `groq_api_key`/`hubspot_api_key` values, not just booleans, since it's used server-side to authenticate the summarization and CRM-push calls.

---

## 6. STEP 1 — Audio Preprocessing (single file, no chunking)

```python
def preprocess_audio(file_path: str) -> Tuple[str, str]:
    audio = AudioSegment.from_file(file_path)
    if len(audio) == 0:
        raise RuntimeError("Uploaded audio file has zero duration.")

    audio = audio.set_frame_rate(16000).set_channels(1)

    work_dir = tempfile.mkdtemp(prefix="meeting_audio_")
    out_path = os.path.join(work_dir, "meeting.mp3")
    audio.export(out_path, format="mp3", bitrate="32k")

    return out_path, work_dir
```
Loads the uploaded recording (any container `ffmpeg` understands), converts to 16kHz mono (speech models don't benefit from stereo or higher sample rates — this cuts bandwidth 6-8x versus a typical 48kHz stereo tab+mic capture with zero accuracy cost), and re-exports as a 32kbps mono MP3 in a fresh temp directory. Loading failures (e.g. missing ffmpeg) and zero-duration files raise a `RuntimeError` with an actionable message. The caller is responsible for deleting `work_dir` afterward (handled in the endpoint's `finally` block).

Earlier versions of this pipeline split the recording into many small chunks before transcription so each upload stayed small; that's incompatible with diarization (see Section 1), so the whole file now goes out as one request, and this step's only job is to keep that one upload as small as reasonably possible.

---

## 7. STEP 2 — Speech-to-Text with Speaker Diarization (ElevenLabs Scribe v2)

```python
ELEVENLABS_TIMEOUT = 1800.0  # seconds, applied to connect + write + read alike
MAX_TRANSCRIPTION_ATTEMPTS = 3
```
**Why a single float timeout, not a `(connect, read)` tuple:** `requests`/urllib3 has a well-known quirk (most visible on Windows) where, for a large request body, the timeout actually enforced during the write/upload phase can end up being the *connect* timeout rather than the read timeout — the socket timeout isn't always re-armed before `sendall()`. With a tuple like `(15.0, 1800.0)`, a multi-minute upload of a whole meeting recording gets its write killed after ~15s every time, surfacing as `"ConnectionError: The write operation timed out"` on every retry. A single float applies uniformly to connect AND read/write, so there's no phase left exposed to a short window. This bug and its fix are exactly what `test_upload_speed.py` and `test_elevenlabs_audio.py` were built to isolate (see Section 13).

### `_format_timestamp(seconds)`
Formats a float seconds value as `HH:MM:SS` for display in the transcript, clamping negative/`None` input to `0`.

### `_build_speaker_turns(words)`
Collapses ElevenLabs' word-level diarization output into speaker turns: consecutive words from the same `speaker_id` are merged into one turn, with a boundary wherever the speaker changes. `"spacing"`-type entries (pure whitespace between words) are skipped — words are rejoined with a single space instead. Audio-event tags (e.g. `(laughter)`) are kept inline as part of whichever speaker's turn they fall within.

### `transcribe_audio_with_diarization(file_path, num_speakers=None)`
```python
request_data = {
    "model_id": ELEVENLABS_MODEL,
    "diarize": "true",
    "timestamps_granularity": "word",
    "tag_audio_events": "true",
}
if num_speakers:
    request_data["num_speakers"] = str(num_speakers)
```
Sends the whole preprocessed file to `POST https://api.elevenlabs.io/v1/speech-to-text` in **one** request. Retries up to `MAX_TRANSCRIPTION_ATTEMPTS` (3) times with 5s/10s backoff on failure. On each failed attempt, logs elapsed time (a fast ~15-20s failure points at a network/proxy write-timeout; a failure near the full `ELEVENLABS_TIMEOUT` means the upload was progressing but genuinely too slow) and, for HTTP error responses, the response body itself — since a 4xx's exception message alone never explains *why* (bad key, quota/plan limit, unsupported param, file too long, etc.), only that it happened.

On success, the word-level `words` array from the response is passed to `_build_speaker_turns`, each turn is assigned a friendly display label (`"Speaker 1"`, `"Speaker 2"`, ... in order of first appearance, via `label_for()`), and both a structured `turns` list and a formatted `[HH:MM:SS] Speaker N: ...` transcript string (`full_text`) are built. Returns:

```python
{
    "full_text": "...",       # formatted, speaker-labeled transcript (fed to the summarizer)
    "plain_text": "...",      # ElevenLabs' own unlabeled transcript, kept as reference
    "turns": [...],           # structured {speaker, start, end, start_formatted, text} per turn
    "speaker_count": N,
    "language_code": "...",
    "errors": [],
}
```
If every attempt fails, raises a `RuntimeError` summarizing the last error after all retries are exhausted.

---

## 8. STEP 3 — Summarization (Groq `openai/gpt-oss-120b` via `langchain-groq`)

Long meetings (2-3 hours) produce transcripts of 25,000+ words — far too much for one call to summarize coherently. A map-reduce approach is used instead: split the transcript into ~15-minute sections, summarize each independently, then combine those section-summaries into one final structured report.

```python
WORDS_PER_SECTION = 2200
MAP_REDUCE_THRESHOLD_WORDS = 3000
```
~15 minutes of natural speech is roughly 2,000-2,500 words — splitting on word count (not time) keeps this correct even if a stretch of audio transcribed unusually densely or sparsely. Below `MAP_REDUCE_THRESHOLD_WORDS`, a normal 5-60 minute meeting goes straight through as a single call.

### `_split_into_sections(full_text, words_per_section)`
Splits the diarized transcript into sections of roughly `words_per_section` words each **without ever splitting in the middle of a `[HH:MM:SS] Speaker N: ...` line** — each line is one speaker turn, and breaking one apart would corrupt the speaker/timestamp attribution for whatever's on either side of the cut. Falls back to plain word-slicing only if the text has no line structure at all.

### System prompts (`SECTION_SYSTEM_PROMPT`, `SUMMARY_SYSTEM_PROMPT`, `REDUCE_SYSTEM_PROMPT`)
All three are written around the same required output structure (`REQUIRED_OUTPUT_FORMAT`): a Markdown `# Title` followed by exactly four `## N. ...` sections in order — **Main Theme**, **Key Discussion Points**, **Comprehensive Summary**, **Conclusion & Action Items** — with detailed formatting rules for each (e.g. Section 1 must be 2-3 full prose paragraphs; Section 4 must list action items as `"Owner: action"` pairs). `SECTION_SYSTEM_PROMPT` is the "map" step — it's told explicitly that its output is an *intermediate* input to a later combine pass, so it should extract dense detail rather than write a short recap, and skips the title/heading structure entirely (plain bullets only). `SUMMARY_SYSTEM_PROMPT` (single-pass) and `REDUCE_SYSTEM_PROMPT` (combining section summaries) both produce the final structured format and both instruct the model to attribute points to specific speakers wherever the transcript/section summaries support it, and to never invent content that isn't supported by the source.

### `_get_summary_llm(user_groq_api_key)`
```python
if not user_groq_api_key:
    raise RuntimeError(
        "No Groq API key was provided. Enter your Groq API key in the extension's "
        "settings panel (gear icon) before recording."
    )
return ChatGroq(
    model_name=SUMMARY_MODEL,
    temperature=LLM_TEMPERATURE,
    groq_api_key=user_groq_api_key,
    timeout=120.0,
    reasoning_effort="medium",
)
```
Every call must supply the requesting account's own Groq key — there is no server-side fallback. `reasoning_effort="medium"` is gpt-oss's balance point (better structure/attribution than `"low"`, without `"high"`'s latency hit on a prompt that's already tightly specified). **Important implementation detail:** `reasoning_effort` must be passed as a **top-level** `ChatGroq` constructor argument, not inside `model_kwargs` — current `langchain-groq` (1.x) added it as a first-class field, so stuffing it into `model_kwargs` instead collides with that field when request params are assembled. This is what broke summarization when the model was switched from `llama-3.3-70b-versatile` (which never touched this code path, since it doesn't support `reasoning_effort`) to `gpt-oss-120b`.

### `_extract_response_text(response)`
Pulls the plain-text answer out of a `ChatGroq` response, tolerating both shapes seen in the wild: `response.content` as a plain string, or as a list of content-block dicts (the newer langchain-core "standard content blocks" representation) — in which case every block of type `"text"` is joined. `gpt-oss-120b` also returns its chain-of-thought separately; as a defensive measure, if any raw Harmony-format channel markers (`<|channel|>...<|message|>`) ever leak into the content string, only the `"final"` channel's message is kept and any stray marker tokens are stripped with a regex — reasoning is never shown to the user as if it were the answer.

### `summarize_transcript(full_text, user_groq_api_key=None)`
- **Short transcript** (≤ `MAP_REDUCE_THRESHOLD_WORDS`): one call with `SUMMARY_SYSTEM_PROMPT`.
- **Long transcript:** split into sections (`_split_into_sections`), each summarized with `SECTION_SYSTEM_PROMPT` (individual section failures are caught and replaced with a placeholder note rather than aborting the whole pipeline), then all section summaries are joined and combined in one final call with `REDUCE_SYSTEM_PROMPT`.

Either path raises a `RuntimeError` with a truncated raw response for debugging if the model returns no usable text (e.g. an invalid/out-of-quota key, or an unexpected response format).

### `extract_meeting_title(summary_text)`
Pulls the title out of the summary's leading `# Title` Markdown H1 line via regex; falls back to `"Untitled meeting"` if the pattern isn't found.

---

## 9. STEP 3.5 — Local PDF Export

Nothing here touches HubSpot or any server-side storage — this just turns the summary (and, optionally, the full transcript) into a PDF and hands the bytes back to the extension, which saves it via `chrome.downloads`. The backend never keeps a copy.

### `sanitize_filename(title)`
Strips characters illegal (or just awkward) in Windows/Mac/Linux filenames, collapses whitespace, truncates to 120 chars, and falls back to a timestamped name (`meeting-<unix time>`) if nothing usable is left (e.g. a title that was only punctuation/emoji).

### `generate_summary_pdf(meeting_title, summary_text, full_transcript=None)`
Renders the structured Markdown summary as a readable PDF: the title is rendered once at the top (the summary's own leading `# Title` line is dropped so it isn't shown twice), `## ` lines get bold/larger text, everything else is body text. FPDF's core fonts are latin-1 only, so a `to_latin1()` helper replaces (rather than crashes on) characters like smart quotes/em-dashes that the LLM sometimes produces.

**New in this version:** when `full_transcript` is provided (the caller passes `transcript_data["full_text"]`), it's appended starting on a fresh page under a "Full Transcript" heading, at a smaller body font (10pt vs. the summary's 11pt) since a full transcript is much longer — this is purely additive; omitting the argument produces exactly the same PDF as before.

---

## 10. STEP 4 — CRM Push (per-account HubSpot Private App token)

```python
def push_to_crm(hubspot_api_key: Optional[str], summary_text: str, meeting_title: str) -> Dict[str, Any]:
    if not hubspot_api_key:
        return {"status": "skipped", "reason": "HubSpot API key not set for this account"}

    url = "https://api.hubapi.com/crm/v3/objects/notes"
    headers = {"Authorization": f"Bearer {hubspot_api_key}", "Content-Type": "application/json"}
    note_body = f"{meeting_title}\n\n{summary_text}"
    payload = {"properties": {"hs_note_body": note_body, "hs_timestamp": int(time.time() * 1000)}}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return {"status": "success", "crm_response": response.json()}
    except requests.exceptions.RequestException as e:
        return {"status": "failed", "error": str(e)}
```
No OAuth, no client ID/secret, no token refresh. The user generates their own **Private App** access token directly in their HubSpot account (Settings → Integrations → Private Apps → Create a private app → grant CRM notes read/write → copy the token) and pastes it into the settings panel; it's stored as `hubspot_api_key` and used here exactly as given, as a Bearer token — Private App tokens don't expire the way OAuth access tokens do, so there's nothing to refresh. If the account hasn't set one, the push is cleanly skipped (`status: "skipped"`) and the rest of the pipeline still succeeds. Network/HTTP failures are caught and reported as `status: "failed"` rather than raised, so a CRM outage never takes down an otherwise-successful transcription+summary.

---

## 11. The Main API Endpoint

### `POST /process-meeting`
```python
@app.post("/process-meeting")
async def process_meeting(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    save_locally: bool = Form(False),
    num_speakers: Optional[int] = Form(None),
):
```
Parameters:
- `file` — the recorded meeting audio (webm from the extension).
- `user_id` — **required**. Identifies the logged-in account (returned by `/auth/login` or `/auth/register`). Both the Groq key and the HubSpot key used for this request are looked up from that account in Supabase — the extension never sends either key value with the upload.
- `save_locally` — when `True`, response includes a base64 PDF (summary + full transcript) for the extension to save via `chrome.downloads`. Independent of the HubSpot push (either, both, or neither can happen per request).
- `num_speakers` — optional hint to improve diarization accuracy if the caller knows exactly how many people are in the recording.

```python
    user_row = _get_user_row(user_id)
    groq_api_key = (user_row.get("groq_api_key") or "").strip()
    hubspot_api_key = (user_row.get("hubspot_api_key") or "").strip() or None

    if not groq_api_key:
        raise HTTPException(status_code=400, detail="A Groq API key is required. Enter yours in the extension's settings panel (gear icon).")
```
**Fails fast, before any audio processing happens.** `_get_user_row` itself raises `404` if the account doesn't exist (e.g. a stale session after the account was deleted). Rejecting a missing Groq key immediately means the user doesn't wait through preprocessing + transcription (potentially many minutes for a long meeting) only to discover at the very end that summarization can't run.

```python
    input_path = None
    audio_work_dir = None
    try:
        suffix = os.path.splitext(file.filename or "")[1] or ".webm"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            content = await file.read()
            if not content:
                raise HTTPException(status_code=400, detail="Uploaded file is empty.")
            tmp_file.write(content)
            input_path = tmp_file.name

        processed_path, audio_work_dir = preprocess_audio(input_path)
        transcript_data = transcribe_audio_with_diarization(processed_path, num_speakers=num_speakers)

        if not transcript_data["full_text"]:
            error_detail = "Transcription produced no text — check audio quality or ElevenLabs API key/quota."
            if transcript_data.get("errors"):
                error_detail += " Underlying error(s): " + "; ".join(transcript_data["errors"])
            raise HTTPException(status_code=422, detail=error_detail)

        summary = summarize_transcript(transcript_data["full_text"], user_groq_api_key=groq_api_key)
        meeting_title = extract_meeting_title(summary)
        crm_result = push_to_crm(hubspot_api_key, summary, meeting_title)

        pdf_result = None
        if save_locally:
            try:
                pdf_bytes = generate_summary_pdf(meeting_title, summary, transcript_data["full_text"])
                pdf_result = {
                    "filename": f"{sanitize_filename(meeting_title)}.pdf",
                    "data_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                }
            except Exception as e:
                pdf_result = {"error": f"PDF generation failed: {e}"}

        return {
            "meeting_title": meeting_title,
            "transcript": transcript_data,
            "summary": summary,
            "crm_push": crm_result,
            "pdf": pdf_result,
        }
```
Step by step:
1. Saves the uploaded file to a temp file, preserving its original extension (defaulting to `.webm` if none — matches what the extension actually sends).
2. Rejects an empty upload with `400`.
3. `preprocess_audio()` → converts/compresses (Section 6).
4. `transcribe_audio_with_diarization()` → gets the diarized transcript (Section 7).
5. If transcription produced **no text at all**, raises `422 Unprocessable Entity` with a message suggesting likely causes (bad audio / bad ElevenLabs key or quota), appending any underlying error strings if present.
6. `summarize_transcript()` → runs Groq summarization using the account's own key (Section 8).
7. `extract_meeting_title()` → pulls the title out of the summary.
8. `push_to_crm()` → attempts the HubSpot push using the account's saved token; result (`success`/`skipped`/`failed`) is included in the response either way, never raises.
9. If `save_locally` was requested, generates a PDF (summary + full transcript) and base64-encodes it for the response — wrapped in its own `try/except` so a PDF-generation failure doesn't take down an otherwise-successful summary/CRM result; it's surfaced as `pdf_result = {"error": ...}` instead.
10. Returns everything in one JSON object: title, full transcript data, summary text, CRM result, and PDF (or `None` if not requested).

```python
    except HTTPException:
        raise
    except RuntimeError as e:
        logger.error(f"Pipeline configuration error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected pipeline failure.")
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {e}")
    finally:
        if input_path and os.path.exists(input_path):
            try:
                os.remove(input_path)
            except OSError as e:
                logger.warning(f"Could not delete temp file {input_path}: {e}")
        if audio_work_dir and os.path.isdir(audio_work_dir):
            shutil.rmtree(audio_work_dir, ignore_errors=True)
```
- `except HTTPException: raise` — HTTPExceptions raised deliberately above (400/404/422) are re-raised as-is, not swallowed or rewrapped.
- `except RuntimeError` — configuration/setup-style errors (e.g. missing ffmpeg, missing ElevenLabs key, missing Groq key) are logged and surfaced as `500` with the specific message.
- `except Exception` — anything else unexpected is logged with a full traceback (`logger.exception`) and returned as a generic `500`, so the client always gets *some* actionable detail without leaking a raw stack trace.
- `finally` — **always** cleans up: deletes the original uploaded temp file, and recursively deletes the entire preprocessing work directory (`shutil.rmtree(..., ignore_errors=True)` so a cleanup failure itself doesn't crash the response). This runs whether the request succeeded or failed.

### `GET /health`
```python
@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "groq_configured": "per-account (stored in Supabase, entered via settings)",
        "hubspot_configured": "per-account (Private App token stored in Supabase, no OAuth)",
        "supabase_configured": supabase is not None,
    }
```
A simple status endpoint showing *which* integrations are configured, without ever exposing actual key values — useful for quickly confirming the `.env` is set up correctly (particularly `supabase_configured`, since a missing Supabase config silently disables all account endpoints rather than crashing at import time).

### Startup
```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```
Only binds to `127.0.0.1` (localhost) — matches `manifest.json`'s `host_permissions` and is intentional: this backend is meant to run locally, not be exposed on the network. There is no `init_db()`-style startup step anymore — Supabase's table is created once manually via the SQL editor (Section 2), not by the app itself.

---

## 12. Test/Diagnostic Scripts (companion files, not part of the app itself)

- **`test_elevenlabs_audio.py`** — generates a tiny 2-second silent WAV directly with Python's `wave` module (no ffmpeg needed) and uploads it straight to ElevenLabs' `/v1/speech-to-text` endpoint (`model_id: "scribe_v2"`), printing the full exception/traceback on failure. Isolates ElevenLabs connectivity from every other part of the pipeline (audio format handling, ffmpeg, retries, etc).
- **`test_groq.py`** — bare Groq connectivity check: sends one trivial chat completion request (currently against `llama-3.3-70b-versatile` in this standalone script — note this is independent of, and hasn't been updated alongside, the app's own `SUMMARY_MODEL = "openai/gpt-oss-120b"`) and prints either the reply or the raw exception, bypassing the app's own retry/catch logic to see errors unfiltered.
- **`test_upload_speed.py`** — uploads a ~2MB random-bytes file to `httpbin.org` (a generic public test endpoint unrelated to ElevenLabs) to determine whether slow/broken uploads are a general network issue (antivirus HTTPS inspection, firewall, VPN, poor bandwidth) versus something specific to reaching ElevenLabs. Reports an approximate KB/s figure and interprets it (below ~100 KB/s is flagged as suspicious).

These three scripts were the actual diagnostic trail used to find and fix the `ELEVENLABS_TIMEOUT` tuple bug described in Section 7.

---

## 13. Files That No Longer Apply

Earlier versions of this backend used HubSpot OAuth (`HUBSPOT_CLIENT_ID`/`HUBSPOT_CLIENT_SECRET`, `/oauth/connect`, `/oauth/callback`, `/oauth/status`) and a local `users.db` SQLite file for token storage, with `sqlite3` and `secrets` as stdlib imports supporting that flow. **None of that exists in the current `main.py`** — HubSpot is now a user-pasted Private App token (Section 10), and all account/key persistence goes through Supabase (Section 5). If a `users.db` or npm log file still exist in the project folder from an earlier version, they're leftovers and unrelated to the current backend.
