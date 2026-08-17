# Backend Documentation — `main.py`

**Meeting Summarizer & CRM Sync Pipeline** — a single-file FastAPI service.

This document explains every section, function, and meaningful line of `main.py` in detail — what it does, why it's written that way, and how it connects to the rest of the pipeline.

---

## 1. High-Level Pipeline

```
audio upload
   -> preprocess (pydub: downsample to 16kHz mono, compress to 32kbps mp3)
   -> ElevenLabs Scribe v2 transcription WITH SPEAKER DIARIZATION
      (one single request for the WHOLE recording, not chunked)
   -> Groq Llama 3.3 70B summarization (map-reduce for long meetings)
   -> CRM push (HubSpot, per-user via OAuth)
   -> optional local PDF export
```

**Why the whole recording is sent as ONE request (not chunks):** ElevenLabs' diarization assigns speaker IDs (`speaker_0`, `speaker_1`, ...) that are only consistent *within a single API call*. If the recording were split into independent chunks and each diarized separately, "Speaker 1" in chunk 3 would not refer to the same person as "Speaker 1" in chunk 9. So the whole file goes out as one upload, and is only downsampled/compressed beforehand to keep that single upload as small as practical.

---

## 2. Setup Requirements (from the module docstring)

- Install: `fastapi uvicorn[standard] pydub langchain-groq python-dotenv requests python-multipart fpdf2`
- **ffmpeg** must be installed and on `PATH` — `pydub` shells out to it for audio conversion.
- `.env` file needs:
  - `ELEVENLABS_API_KEY` — server-side key used for transcription (shared, not per-user).
  - `HUBSPOT_CLIENT_ID` / `HUBSPOT_CLIENT_SECRET` — for the OAuth app that lets each user connect their own HubSpot account.
- **Groq is NOT server-side anymore.** Every request must supply its own `groq_api_key` — this is entered by the user in the extension's settings panel and travels with every upload. Summarization always runs against the *requesting user's own* Groq account/quota.
- `CRM_API_KEY` (an old single fixed token) is no longer used — each user now connects their own HubSpot account via OAuth instead. Safe to leave in `.env` unused or remove.
- Run with `python main.py` or `uvicorn main:app --host 127.0.0.1 --port 8000 --reload`.
- To manually test the OAuth flow before the extension has a "Connect" button: visit `http://localhost:8000/oauth/connect?user_id=test-user` in a browser. This creates `users.db` (SQLite) the first time it's run.

---

## 3. Imports

```python
import os, re, time, base64, shutil, logging, sqlite3, secrets, tempfile
from typing import List, Tuple, Dict, Any, Optional

import requests
from dotenv import load_dotenv
from pydub import AudioSegment
from fpdf import FPDF

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage
```

| Import | Purpose |
|---|---|
| `os`, `re`, `time`, `base64`, `shutil`, `tempfile` | Filesystem/temp-file handling, regex for parsing, timing, base64-encoding the PDF for the JSON response, deleting temp work directories. |
| `logging` | Structured logging (`logger.info`, `.warning`, `.error`) throughout the pipeline so failures are traceable. |
| `sqlite3` | Local per-user HubSpot token storage (`users.db`). |
| `secrets` | Generates a cryptographically random nonce for OAuth CSRF protection. |
| `requests` | All outbound HTTP calls: ElevenLabs transcription, HubSpot token exchange/refresh, HubSpot CRM push. |
| `dotenv.load_dotenv` | Loads `.env` into `os.environ`. |
| `pydub.AudioSegment` | Audio loading/resampling/exporting (via ffmpeg). |
| `fpdf.FPDF` | Renders the meeting summary as a downloadable PDF. |
| `fastapi.*` | Web framework: routing, file uploads, form fields, error responses, redirects, HTML responses. |
| `pydantic.BaseModel` | Used for `CrmPushResult`, a typed response shape. |
| `langchain_groq.ChatGroq` / `langchain_core.messages` | Wraps the Groq Llama 3.3 70B chat API for summarization calls. |

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
The server's own ElevenLabs key — this one IS shared across all users (unlike Groq).

```python
HUBSPOT_CLIENT_ID = os.getenv("HUBSPOT_CLIENT_ID")
HUBSPOT_CLIENT_SECRET = os.getenv("HUBSPOT_CLIENT_SECRET")
HUBSPOT_REDIRECT_URI = os.getenv("HUBSPOT_REDIRECT_URI", "http://localhost:8000/oauth/callback")
HUBSPOT_SCOPES = "oauth crm.objects.contacts.read crm.objects.contacts.write"
```
- `HUBSPOT_REDIRECT_URI` **must exactly match** a Redirect URL configured in the HubSpot app's Auth tab. Note it's `localhost`, not `127.0.0.1` — HubSpot only allows `https://` or exactly `http://localhost`, even though both point to the same machine.
- `HUBSPOT_SCOPES` must be kept in sync with `requiredScopes` in the HubSpot app's `app-hsmeta.json`.

```python
ELEVENLABS_MODEL = "scribe_v2"
SUMMARY_MODEL = "llama-3.3-70b-versatile"
DEFAULT_NUM_SPEAKERS: Optional[int] = None
LLM_TEMPERATURE = 0.2
```
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
CORS is wide open (`*`) — necessary because the request originates from a Chrome extension context, not a normal web origin, and `allow_credentials=False` means no cookies are involved (auth is via `user_id` in the request body/query, not cookies).

```python
class CrmPushResult(BaseModel):
    status: str
    detail: Dict[str, Any] = {}
```
A typed shape for CRM push results (declared but the actual `push_to_crm()` function returns plain dicts matching this shape informally).

---

## 5. STEP 0 — Per-User Token Storage (SQLite)

**Why this exists:** many different people can use the same backend instance. Each user gets their own row in `users.db`, keyed by a `user_id` the extension generates once (a UUID saved in `chrome.storage.local`) and sends with every request. This is what makes sure Person A's meeting notes land in Person A's HubSpot account, not Person B's.

```python
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
```
Database file lives next to `main.py`, regardless of the current working directory the script was launched from.

### `init_db()`
```python
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hubspot_tokens (
            user_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at REAL NOT NULL,
            hub_domain TEXT
        )
    """)
    conn.commit()
    conn.close()
```
Creates the `hubspot_tokens` table if it doesn't exist. Called once at module load time (bottom of the file, `init_db()` before `if __name__ == "__main__"`).

### `save_tokens(user_id, access_token, refresh_token, expires_in, hub_domain=None)`
```python
def save_tokens(user_id, access_token, refresh_token, expires_in, hub_domain=None) -> None:
    expires_at = time.time() + expires_in - 60  # refresh 60s early
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO hubspot_tokens (...)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            access_token=excluded.access_token, ...
    """, (user_id, access_token, refresh_token, expires_at, hub_domain))
    conn.commit()
    conn.close()
```
- `expires_at = now + expires_in - 60`: stores an absolute expiry timestamp, deliberately 60 seconds earlier than the real expiry, as a safety margin so a token isn't used right at the edge of expiring.
- `ON CONFLICT(user_id) DO UPDATE`: an "upsert" — if this `user_id` already has a row (e.g. re-connecting, or a token refresh), it updates in place rather than erroring on the `PRIMARY KEY` constraint.

### `get_tokens(user_id) -> Optional[Dict]`
```python
def get_tokens(user_id: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM hubspot_tokens WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
```
`row_factory = sqlite3.Row` lets the result be accessed by column name (`row["access_token"]`) and easily converted to a plain `dict`. Returns `None` if this user has never connected HubSpot.

### `get_valid_access_token(user_id) -> Optional[str]`
```python
def get_valid_access_token(user_id: str) -> Optional[str]:
    tokens = get_tokens(user_id)
    if not tokens:
        return None
    if time.time() < tokens["expires_at"]:
        return tokens["access_token"]
    # expired — refresh
    resp = requests.post("https://api.hubapi.com/oauth/v1/token", data={
        "grant_type": "refresh_token",
        "client_id": HUBSPOT_CLIENT_ID,
        "client_secret": HUBSPOT_CLIENT_SECRET,
        "refresh_token": tokens["refresh_token"],
    }, timeout=15)
    if not resp.ok:
        return None
    data = resp.json()
    save_tokens(user_id, access_token=data["access_token"],
                refresh_token=data.get("refresh_token", tokens["refresh_token"]),
                expires_in=data["expires_in"], hub_domain=tokens.get("hub_domain"))
    return data["access_token"]
```
This is the single "give me a usable token" entry point used elsewhere in the file:
1. No stored tokens at all → `None` (user never connected HubSpot).
2. Stored token still valid (`now < expires_at`) → return it directly, no network call.
3. Expired → silently exchange the long-lived `refresh_token` for a new `access_token`/`refresh_token` pair via HubSpot's token endpoint, save the new pair, and return the fresh access token. If the refresh call itself fails, returns `None` (caller treats this like "not connected").
- Note: HubSpot doesn't always return a new `refresh_token` on refresh, so it falls back to keeping the old one (`data.get("refresh_token", tokens["refresh_token"])`).

---

## 6. STEP 0b — OAuth Endpoints

### `GET /oauth/connect?user_id=...`
```python
@app.get("/oauth/connect")
def oauth_connect(user_id: str = Query(...)):
    if not HUBSPOT_CLIENT_ID:
        raise HTTPException(500, "HUBSPOT_CLIENT_ID is not configured.")
    nonce = secrets.token_urlsafe(16)
    state = f"{user_id}:{nonce}"
    params = {"client_id": HUBSPOT_CLIENT_ID, "redirect_uri": HUBSPOT_REDIRECT_URI,
              "scope": HUBSPOT_SCOPES, "state": state}
    query = "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
    authorize_url = f"https://app-na2.hubspot.com/oauth/authorize?{query}"
    return RedirectResponse(authorize_url)
```
- The starting point when the user clicks "Connect HubSpot" in the extension.
- `state = "{user_id}:{nonce}"` packs two things into HubSpot's generic `state` parameter: a random `nonce` (CSRF protection — proves the callback really originated from this specific `/oauth/connect` call) and the `user_id`, so `/oauth/callback` knows *whose* token it's about to receive.
- **`app-na2.hubspot.com` is hardcoded** — HubSpot splits accounts across regional data centers (na1, na2, eu1, etc.), and the authorize URL must use the SAME subdomain as the account itself. This was confirmed against the working `app-na2.hubspot.com` link in this HubSpot app's own Distribution tab. **If this backend is ever pointed at a different HubSpot account/data center, this subdomain must be updated to match, or HubSpot will fail with "No accounts match that search."**
- Redirects the browser (a real HubSpot login/consent screen) rather than returning JSON — this endpoint is meant to be opened directly in a tab.

### `GET /oauth/callback?code=...&state=...`
```python
@app.get("/oauth/callback")
def oauth_callback(code: str = Query(...), state: str = Query(...)):
    if not (HUBSPOT_CLIENT_ID and HUBSPOT_CLIENT_SECRET):
        raise HTTPException(500, "HubSpot OAuth is not configured.")
    user_id = state.split(":", 1)[0]
    resp = requests.post("https://api.hubapi.com/oauth/v1/token", data={
        "grant_type": "authorization_code", "client_id": HUBSPOT_CLIENT_ID,
        "client_secret": HUBSPOT_CLIENT_SECRET, "redirect_uri": HUBSPOT_REDIRECT_URI,
        "code": code,
    }, timeout=15)
    if not resp.ok:
        raise HTTPException(502, f"HubSpot token exchange failed: {resp.text}")
    data = resp.json()
    save_tokens(user_id, access_token=data["access_token"],
                refresh_token=data["refresh_token"], expires_in=data["expires_in"])
    return HTMLResponse("<html>...HubSpot connected ✅...</html>")
```
- HubSpot redirects the user's browser back here after they approve access, carrying a temporary `code`.
- `user_id = state.split(":", 1)[0]` extracts the original `user_id` back out of the `state` string set in `/oauth/connect`.
- Exchanges `code` for a real `access_token` + `refresh_token` pair, saves it, and shows a plain "connected, you can close this tab" HTML page (this endpoint is opened directly by the browser, not called via `fetch`, so it needs to render something human-readable).

### `GET /oauth/status?user_id=...`
```python
@app.get("/oauth/status")
def oauth_status(user_id: str = Query(...)):
    tokens = get_tokens(user_id)
    return {"connected": tokens is not None}
```
A cheap, no-side-effect check the extension popup calls on load/focus to show a "HubSpot connected ✅" / "not connected" indicator, without triggering any login flow.

---

## 7. STEP 1 — Audio Preprocessing

**Historical note (in the code comments):** this used to split recordings into many independent ~20s chunks so each upload stayed small. That's incompatible with diarization (see Section 1), so now the whole file is preprocessed and sent as one piece — still downsampled/compressed to keep that one upload as small as reasonably possible.

### `preprocess_audio(file_path) -> (processed_path, work_dir)`
```python
def preprocess_audio(file_path: str) -> Tuple[str, str]:
    try:
        audio = AudioSegment.from_file(file_path)
    except Exception as e:
        raise RuntimeError(f"Failed to load audio file (is ffmpeg installed and on PATH?): {e}")

    if len(audio) == 0:
        raise RuntimeError("Uploaded audio file has zero duration.")

    audio = audio.set_frame_rate(16000).set_channels(1)

    work_dir = tempfile.mkdtemp(prefix="meeting_audio_")
    out_path = os.path.join(work_dir, "meeting.mp3")
    audio.export(out_path, format="mp3", bitrate="32k")

    duration_sec = len(audio) / 1000
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    logger.info(f"Preprocessed audio: {duration_sec:.0f}s recording -> {size_mb:.1f}MB mp3 (mono, 16kHz, 32kbps)")
    return out_path, work_dir
```
Line by line:
- `AudioSegment.from_file(file_path)` — loads the uploaded file (webm from the browser) via ffmpeg. If ffmpeg isn't installed/on PATH, this raises, and the error message explicitly hints at that as the likely cause.
- Zero-duration check — catches a genuinely empty/corrupt recording early with a clear error instead of a confusing downstream failure.
- `set_frame_rate(16000).set_channels(1)` — downsamples to 16kHz mono. Speech models are trained on 16kHz mono audio; uploading full-quality stereo (often 48kHz from tab+mic capture) wastes 6-8x the bandwidth for zero accuracy benefit. Diarization doesn't need stereo separation either — it works from voice characteristics in the audio itself.
- `tempfile.mkdtemp(prefix="meeting_audio_")` — creates a fresh temp directory per request; the caller (the `/process-meeting` endpoint) is responsible for deleting it afterward (`finally` block, Section 12).
- `bitrate="32k"` — 32kbps mono is plenty for speech-to-text/diarization accuracy, and is half the size of a previous 64kbps setting. A smaller upload means less time spent in the vulnerable "write" phase of the HTTP request, where a slow/inspected connection is likely to time out (see Section 8's timeout note) — this matters more now that the whole meeting goes out as one upload instead of many small chunks.
- The final log line reports duration and resulting file size for diagnostics (and was recently fixed to correctly say "32kbps" instead of a stale "64kbps").

---

## 8. STEP 2 — Speech-to-Text with Speaker Diarization (ElevenLabs Scribe v2)

```python
ELEVENLABS_TIMEOUT = 1800.0  # seconds, applied to connect + write + read alike
MAX_TRANSCRIPTION_ATTEMPTS = 3
```

**Why `ELEVENLABS_TIMEOUT` is a single float, not a `(connect, read)` tuple:** `requests`/`urllib3` has a well-known quirk (most visible on Windows) where, for a large request body, the timeout actually enforced during the write/upload phase can end up being the *connect* timeout rather than the read timeout — the socket timeout isn't always re-armed before `sendall()`. With a tuple like `(15.0, 1800.0)`, a multi-minute upload of a whole meeting recording gets its write killed after ~15s on every attempt — this looked like a new failure mode in practice but was really the old "slow upload" problem (see `test_upload_speed.py`) hitting a much bigger single upload than before, once chunked uploads were replaced with one whole-meeting upload. A single float applies uniformly to connect AND read/write, closing that gap.

### `_format_timestamp(seconds) -> str`
```python
def _format_timestamp(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        seconds = 0
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
```
Converts a float seconds value into `HH:MM:SS` for display in the transcript (e.g. `[00:01:15] Speaker 1: ...`). Defensive: treats `None`/negative input as `0` rather than raising.

### `_build_speaker_turns(words) -> List[Dict]`
```python
def _build_speaker_turns(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    turns = []
    current = None
    for w in words:
        if w.get("type") == "spacing":
            continue
        text = (w.get("text") or "").strip()
        if not text:
            continue
        speaker_id = w.get("speaker_id") or "speaker_unknown"
        start, end = w.get("start"), w.get("end")
        if current is None or current["speaker_id"] != speaker_id:
            if current is not None:
                turns.append(current)
            current = {"speaker_id": speaker_id, "start": start, "end": end, "words": [text]}
        else:
            current["words"].append(text)
            if end is not None:
                current["end"] = end
    if current is not None:
        turns.append(current)
    return turns
```
ElevenLabs returns diarization as a flat **word-level** list, each word tagged with a `speaker_id`. This function collapses that into **speaker turns** — consecutive words from the same speaker merged into one block:
- `"spacing"`-type entries (pure whitespace between words in the raw response) are skipped entirely — words are rejoined with a single space later, so spacing tokens carry no needed information.
- Empty/whitespace-only text is skipped.
- `speaker_id` defaults to `"speaker_unknown"` if missing.
- A new turn starts whenever the speaker changes (`current["speaker_id"] != speaker_id`) — the just-finished turn is appended to `turns`, and a fresh `current` dict starts collecting words for the new speaker.
- Within a turn, `current["end"]` keeps advancing to the latest word's `end` timestamp, so the turn's `end` reflects when that speaker stopped talking.
- Audio event tags (like `(laughter)`) are just treated as words and stay inline within whichever speaker's turn they fall in.

### `transcribe_audio_with_diarization(file_path, num_speakers=None) -> Dict`
```python
def transcribe_audio_with_diarization(file_path, num_speakers=None) -> Dict[str, Any]:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not configured...")

    request_data = {
        "model_id": ELEVENLABS_MODEL,
        "diarize": "true",
        "timestamps_granularity": "word",
        "tag_audio_events": "true",
    }
    if num_speakers:
        request_data["num_speakers"] = str(num_speakers)
```
- Fails fast with a clear message if the server-side ElevenLabs key isn't configured at all.
- `diarize: "true"` turns on speaker separation.
- `timestamps_granularity: "word"` gets per-word start/end times (needed to build accurate turn boundaries and `[HH:MM:SS]` labels).
- `tag_audio_events: "true"` includes things like `(laughter)` inline in the output.
- `num_speakers` is only sent if the caller explicitly provided it — passing the exact known speaker count measurably improves diarization accuracy, but it's optional (ElevenLabs auto-detects otherwise).

```python
    last_error = None
    payload = None
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

    for attempt in range(1, MAX_TRANSCRIPTION_ATTEMPTS + 1):
        attempt_start = time.time()
        try:
            with open(file_path, "rb") as audio_file:
                response = requests.post(
                    ELEVENLABS_STT_URL,
                    headers={"xi-api-key": ELEVENLABS_API_KEY},
                    files={"file": (os.path.basename(file_path), audio_file, "audio/mpeg")},
                    data=request_data,
                    timeout=ELEVENLABS_TIMEOUT,
                )
            response.raise_for_status()
            payload = response.json()
            last_error = None
            logger.info(f"Diarized transcription succeeded on attempt {attempt}/{MAX_TRANSCRIPTION_ATTEMPTS} ({file_size_mb:.1f}MB in {time.time() - attempt_start:.1f}s).")
            break
        except Exception as e:
            last_error = e
            elapsed = time.time() - attempt_start
            logger.warning(f"Diarized transcription attempt {attempt}/{MAX_TRANSCRIPTION_ATTEMPTS} failed after {elapsed:.1f}s (file was {file_size_mb:.1f}MB) — {type(e).__name__}: {e}")
            if attempt < MAX_TRANSCRIPTION_ATTEMPTS:
                time.sleep(5 * attempt)  # 5s, then 10s
```
Retry loop, up to `MAX_TRANSCRIPTION_ATTEMPTS = 3` attempts:
- Opens the audio file fresh on every attempt (`with open(...)`) — a file handle can't be re-read after a failed `POST` without reopening.
- `headers={"xi-api-key": ELEVENLABS_API_KEY}` — ElevenLabs auth header.
- `response.raise_for_status()` — turns any non-2xx HTTP status into an exception, caught below.
- **Diagnostic logging is deliberate:** the elapsed time per failed attempt is the key signal for debugging — failing in ~15-20s points at a network/proxy write-timeout (upload never really got going), while failing near the full `ELEVENLABS_TIMEOUT` means the upload was progressing but genuinely too slow. This is exactly the pattern that was used to diagnose and fix the timeout-tuple bug described above.
- Backoff: `time.sleep(5 * attempt)` → 5s after attempt 1, 10s after attempt 2 (no sleep after the final attempt, since the loop just ends).

```python
    if last_error is not None or payload is None:
        raise RuntimeError(f"Transcription failed after {MAX_TRANSCRIPTION_ATTEMPTS} attempts: {type(last_error).__name__}: {last_error}")
```
If every attempt failed, raise with details of the *last* error (not all three) — enough to diagnose without an overwhelming message.

```python
    words = payload.get("words") or []
    raw_turns = _build_speaker_turns(words)

    speaker_labels: Dict[str, str] = {}
    def label_for(speaker_id: str) -> str:
        if speaker_id not in speaker_labels:
            speaker_labels[speaker_id] = f"Speaker {len(speaker_labels) + 1}"
        return speaker_labels[speaker_id]

    turns = []
    formatted_lines = []
    for t in raw_turns:
        text = " ".join(t["words"]).strip()
        if not text:
            continue
        speaker = label_for(t["speaker_id"])
        start_formatted = _format_timestamp(t["start"])
        turns.append({"speaker": speaker, "start": t["start"], "end": t["end"],
                      "start_formatted": start_formatted, "text": text})
        formatted_lines.append(f"[{start_formatted}] {speaker}: {text}")

    full_text = "\n".join(formatted_lines)
    plain_text = (payload.get("text") or "").strip()
```
- `label_for()` maps ElevenLabs' internal `speaker_0`, `speaker_1`, ... IDs to friendlier `Speaker 1`, `Speaker 2`, ... labels, assigned **in order of first appearance** — so "Speaker 1" is whoever spoke first chronologically, not an arbitrary internal ID.
- Builds both a structured `turns` list (for programmatic use / the JSON response) and a flat `full_text` string of lines like `[00:01:15] Speaker 1: some text` (this is what gets fed to the summarizer).
- `plain_text` is ElevenLabs' own undiarized transcript, kept as a fallback/reference in the response.

```python
    return {
        "full_text": full_text, "plain_text": plain_text, "turns": turns,
        "speaker_count": len(speaker_labels),
        "language_code": payload.get("language_code"), "errors": [],
    }
```
Final structured return value used both by the summarizer and included directly in the API response to the extension.

---

## 9. STEP 3 — Summarization (Groq Llama 3.3 70B via `langchain-groq`)

**Why map-reduce:** long meetings (2-3 hours) can produce 25,000+ word transcripts — too much for one model call to summarize coherently. The solution: split into ~15-minute sections, summarize each independently ("map"), then combine those section summaries into one final structured report ("reduce"). This keeps every individual call small and reliable regardless of total meeting length.

```python
WORDS_PER_SECTION = 2200
MAP_REDUCE_THRESHOLD_WORDS = 3000
```
- `WORDS_PER_SECTION = 2200` — ~15 minutes of natural speech is roughly 2,000-2,500 words. Splitting is done by **word count**, not time, because word count is what actually determines whether a call is too big for the model — this stays correct even if a section transcribed unusually densely or sparsely.
- `MAP_REDUCE_THRESHOLD_WORDS = 3000` — below this, there's no benefit to two-pass summarization; a normal 5-60 minute meeting goes through as a single call, same as the original (pre-map-reduce) behavior.

### Prompts

- `REQUIRED_OUTPUT_FORMAT` — the exact Markdown skeleton every summary must follow: an H1 title, then `## 1. Main Theme`, `## 2. Key Discussion Points`, `## 3. Comprehensive Summary`, `## 4. Conclusion & Action Items`.
- `SECTION_SYSTEM_PROMPT` — used for the "map" step. Tells the model it's summarizing ONE SECTION of a longer diarized transcript, that its output will later be merged with other sections, and to extract dense **detail** (not a short recap) with speaker attribution wherever the transcript supports it, without inventing content.
- `SUMMARY_SYSTEM_PROMPT` — used when a meeting is short enough for a single-pass summary. Instructs the model to produce a deep, thorough report (never a brief overview) in the exact `REQUIRED_OUTPUT_FORMAT` structure, cleaning up filler/false starts but never inventing content, and attributing statements to specific speakers.
- `REDUCE_SYSTEM_PROMPT` (truncated in the visible source, but referenced in `summarize_transcript`) — used for the final "reduce" pass over long meetings: merges/deduplicates repeated points across sections (e.g. a decision mentioned early and reconfirmed later) while preserving chronology, and must also follow the exact `REQUIRED_OUTPUT_FORMAT`. Notably: Section 1 must be 2-3 full paragraphs of prose (not bullets), Section 2 must be genuinely comprehensive across *all* sections, Section 3 is meant to be the longest section synthesizing the full arc, and Section 4 must separate decided items from open ones and list action items as `"Owner: action"` pairs (or `"Unassigned: action"` if no owner is identifiable).

### `_get_summary_llm(user_groq_api_key=None) -> ChatGroq`
```python
def _get_summary_llm(user_groq_api_key: Optional[str] = None) -> ChatGroq:
    if not user_groq_api_key:
        raise RuntimeError("No Groq API key was provided. Enter your Groq API key in the extension's settings panel (gear icon) before recording.")
    return ChatGroq(model_name=SUMMARY_MODEL, temperature=LLM_TEMPERATURE,
                     groq_api_key=user_groq_api_key, timeout=120.0)
```
Constructs a `ChatGroq` client **scoped to the calling user's own key** — there is no server-side fallback anymore. Raises immediately (caught upstream and surfaced as an HTTP error) if no key was supplied.

### `_split_into_sections(full_text, words_per_section) -> List[str]`
```python
def _split_into_sections(full_text: str, words_per_section: int) -> List[str]:
    lines = full_text.splitlines()
    if not lines:
        return []
    sections = []
    current_lines = []
    current_word_count = 0
    for line in lines:
        line_word_count = len(line.split())
        if current_lines and current_word_count + line_word_count > words_per_section:
            sections.append("\n".join(current_lines))
            current_lines = []
            current_word_count = 0
        current_lines.append(line)
        current_word_count += line_word_count
    if current_lines:
        sections.append("\n".join(current_lines))
    return sections
```
Splits the diarized transcript into ~`words_per_section`-word chunks **without ever splitting a `[HH:MM:SS] Speaker N: ...` line in half** — each line is a single speaker turn, so cutting one apart mid-line would corrupt the speaker/timestamp attribution on both sides of the cut. Instead, it accumulates whole lines until adding the next one would exceed the target word count, then starts a new section. (Falls back gracefully to producing a single "section" if the text has no line breaks at all — not expected with diarized output, but keeps this safe for plain-text input too.)

### `summarize_transcript(full_text, user_groq_api_key=None) -> str`
```python
def summarize_transcript(full_text: str, user_groq_api_key=None) -> str:
    word_count = len(full_text.split())

    if word_count <= MAP_REDUCE_THRESHOLD_WORDS:
        llm = _get_summary_llm(user_groq_api_key)
        messages = [SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
                    HumanMessage(content=f"Meeting transcript:\n\n{full_text}\n\nProduce the structured notes now.")]
        response = llm.invoke(messages)
        return response.content.strip()

    # Long meeting - map-reduce
    sections = _split_into_sections(full_text, WORDS_PER_SECTION)
    llm = _get_summary_llm(user_groq_api_key)
    section_summaries = []
    for index, section_text in enumerate(sections):
        messages = [SystemMessage(content=SECTION_SYSTEM_PROMPT),
                    HumanMessage(content=f"Meeting transcript - section {index + 1} of {len(sections)}:\n\n{section_text}")]
        try:
            response = llm.invoke(messages)
            section_summaries.append(f"[Section {index + 1}]\n{response.content.strip()}")
        except Exception as e:
            section_summaries.append(f"[Section {index + 1}]\n(This section could not be summarized: {e})")

    combined_sections = "\n\n".join(section_summaries)
    reduce_messages = [SystemMessage(content=REDUCE_SYSTEM_PROMPT),
                        HumanMessage(content=f"Section summaries, in order:\n\n{combined_sections}\n\nProduce the combined structured notes now.")]
    response = llm.invoke(reduce_messages)
    return response.content.strip()
```
- Short path (`word_count <= 3000`): one direct call using `SUMMARY_SYSTEM_PROMPT`.
- Long path: splits into sections, summarizes each with `SECTION_SYSTEM_PROMPT` **inside a `try/except` per section** — if one section's LLM call fails, it's replaced with an inline `"(This section could not be summarized: {e})"` placeholder rather than aborting the entire meeting's summary. All section summaries (each labeled `[Section N]`) are then joined and sent through one final `REDUCE_SYSTEM_PROMPT` call to produce the unified report.

### `extract_meeting_title(summary_text) -> str`
```python
def extract_meeting_title(summary_text: str) -> str:
    match = re.search(r"^#\s+(.+)$", summary_text, re.MULTILINE)
    if match:
        title = match.group(1).strip()
        if title:
            return title
    return "Untitled meeting"
```
Pulls the meeting title out of the summary's leading Markdown H1 line (`# Title`) via regex. `re.MULTILINE` lets `^`/`$` match at line boundaries rather than only string start/end. Falls back to `"Untitled meeting"` if no H1 is found or it's empty.

---

## 10. STEP 3.5 — Local PDF Export (optional)

Nothing here touches HubSpot or server-side storage — this turns the summary into PDF bytes, base64-encodes them into the API response, and the extension itself saves the file locally via `chrome.downloads`. The backend never keeps a copy.

### `sanitize_filename(title) -> str`
```python
def sanitize_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", title)  # illegal on Windows
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:120]
    if not name:
        name = f"meeting-{int(time.time())}"
    return name
```
- Strips characters illegal (or just awkward) in Windows/Mac/Linux filenames: `\ / : * ? " < > |`.
- Collapses runs of whitespace into single spaces and trims.
- Truncates to 120 characters (safely under OS filesystem path limits).
- If nothing usable remains (e.g. a title that was only emoji/punctuation), falls back to a timestamped name like `meeting-1755000000`.

### `generate_summary_pdf(meeting_title, summary_text) -> bytes`
```python
def generate_summary_pdf(meeting_title: str, summary_text: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def to_latin1(s: str) -> str:
        return s.encode("latin-1", "replace").decode("latin-1")

    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 10, to_latin1(meeting_title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    body_lines = summary_text.splitlines()
    if body_lines and body_lines[0].strip().startswith("# "):
        body_lines = body_lines[1:]

    pdf.set_font("Helvetica", size=11)
    for line in body_lines:
        stripped = to_latin1(line.strip())
        if stripped.startswith("## "):
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 13)
            pdf.multi_cell(0, 8, stripped[3:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=11)
        elif stripped.startswith("# "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(0, 8, stripped[2:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=11)
        elif stripped == "":
            pdf.ln(3)
        else:
            pdf.multi_cell(0, 6, stripped, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output(dest="S"))
```
- `to_latin1()`: FPDF's built-in core fonts only support Latin-1 encoding. LLM output sometimes contains smart quotes, em-dashes, or other Unicode characters that would otherwise raise an encoding error — `.encode("latin-1", "replace")` swaps any unsupported character for a safe placeholder instead of crashing.
- The title is rendered once, bold, at the top (16pt).
- The summary's own leading `"# Title"` line is stripped from `body_lines` before rendering the body — otherwise the title would appear twice (once from the dedicated title render, once from the body).
- Then it does lightweight **Markdown-aware** rendering line by line:
  - `## ` lines → rendered bold, 13pt, with a small leading gap (`pdf.ln(3)`), then font reset back to normal body size.
  - `# ` lines (any other H1 that might appear later, in theory) → bold, 14pt.
  - Blank lines → just add vertical spacing (`pdf.ln(3)`).
  - Everything else → plain 11pt body text via `multi_cell` (which wraps automatically).
- Returns raw PDF bytes (`dest="S"` = return as a string/bytes rather than saving to disk), wrapped in `bytes(...)` for correct typing.

---

## 11. STEP 4 — CRM Push (per-user HubSpot via OAuth)

### `push_to_crm(user_id, summary_text, meeting_title) -> Dict`
```python
def push_to_crm(user_id: str, summary_text: str, meeting_title: str) -> Dict[str, Any]:
    access_token = get_valid_access_token(user_id)
    if not access_token:
        logger.warning(f"No HubSpot connection for user_id={user_id} — skipping CRM push.")
        return {"status": "skipped", "reason": "HubSpot not connected for this user"}

    url = "https://api.hubapi.com/crm/v3/objects/notes"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    note_body = f"{meeting_title}\n\n{summary_text}"
    payload = {"properties": {"hs_note_body": note_body, "hs_timestamp": int(time.time() * 1000)}}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return {"status": "success", "crm_response": response.json()}
    except requests.exceptions.RequestException as e:
        logger.error(f"CRM push failed for user_id={user_id}: {e}")
        return {"status": "failed", "error": str(e)}
```
- If the user has no valid HubSpot token (`get_valid_access_token` returns `None` — never connected, or refresh failed), the push is **skipped, not fatal** — the rest of the pipeline (recording → transcript → summary) still succeeds standalone.
- Pushes the summary as a HubSpot **Note** object (`hs_note_body`), timestamped in milliseconds (HubSpot's expected format, hence `* 1000`).
- Three possible outcomes surfaced to the caller: `"skipped"` (not connected), `"success"` (with HubSpot's response), or `"failed"` (with the error string) — all non-fatal to the overall request.

---

## 12. The Main API Endpoint

### `POST /process-meeting`
```python
@app.post("/process-meeting")
async def process_meeting(
    file: UploadFile = File(...),
    user_id: str = Form("default-user"),
    save_locally: bool = Form(False),
    groq_api_key: str = Form(...),
    num_speakers: Optional[int] = Form(None),
):
```
Parameters:
- `file` — the recorded meeting audio (webm from the extension).
- `user_id` — identifies which HubSpot connection to use; defaults to `"default-user"` if not supplied.
- `save_locally` — when `True`, response includes a base64 PDF for the extension to save via `chrome.downloads`. Independent of the HubSpot push (either, both, or neither can happen per request).
- `groq_api_key` — **required**. The user's own key from the extension settings panel.
- `num_speakers` — optional hint to improve diarization accuracy.

```python
    groq_api_key = groq_api_key.strip() if groq_api_key else ""
    if not groq_api_key:
        raise HTTPException(status_code=400, detail="A Groq API key is required. Enter yours in the extension's settings panel (gear icon).")
```
**Fails fast, before any audio processing happens.** This is deliberate — rejecting a missing key immediately means the user doesn't wait through preprocessing + transcription (potentially many minutes for a long meeting) only to discover at the very end that summarization can't run.

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

        logger.info(f"Received upload: {file.filename} ({len(content)} bytes) for user_id={user_id}")

        processed_path, audio_work_dir = preprocess_audio(input_path)
        transcript_data = transcribe_audio_with_diarization(processed_path, num_speakers=num_speakers)

        if not transcript_data["full_text"]:
            error_detail = "Transcription produced no text — check audio quality or ElevenLabs API key/quota."
            if transcript_data.get("errors"):
                error_detail += " Underlying error(s): " + "; ".join(transcript_data["errors"])
            raise HTTPException(status_code=422, detail=error_detail)

        summary = summarize_transcript(transcript_data["full_text"], user_groq_api_key=groq_api_key)
        meeting_title = extract_meeting_title(summary)
        crm_result = push_to_crm(user_id, summary, meeting_title)

        pdf_result = None
        if save_locally:
            try:
                pdf_bytes = generate_summary_pdf(meeting_title, summary)
                pdf_result = {"filename": f"{sanitize_filename(meeting_title)}.pdf",
                              "data_base64": base64.b64encode(pdf_bytes).decode("ascii")}
            except Exception as e:
                pdf_result = {"error": f"PDF generation failed: {e}"}

        return {"meeting_title": meeting_title, "transcript": transcript_data,
                "summary": summary, "crm_push": crm_result, "pdf": pdf_result}
```
Step by step:
1. Saves the uploaded file to a temp file, preserving its original extension (defaulting to `.webm` if none — matches what the extension actually sends).
2. Rejects an empty upload with `400`.
3. `preprocess_audio()` → converts/compresses (Section 7).
4. `transcribe_audio_with_diarization()` → gets the diarized transcript (Section 8).
5. If transcription produced **no text at all**, raises `422 Unprocessable Entity` with a message suggesting likely causes (bad audio / bad ElevenLabs key or quota), and appends any underlying error strings if present.
6. `summarize_transcript()` → runs Groq summarization using the caller's own key (Section 9).
7. `extract_meeting_title()` → pulls the title out of the summary.
8. `push_to_crm()` → attempts the HubSpot push; result (success/skipped/failed) is included in the response either way, never raises.
9. If `save_locally` was requested, generates a PDF and base64-encodes it for the response — wrapped in its own `try/except` so a PDF-generation failure doesn't take down an otherwise-successful summary/CRM result; it's surfaced as `pdf_result = {"error": ...}` instead.
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
- `except HTTPException: raise` — HTTPExceptions raised deliberately above (400/422) are re-raised as-is, not swallowed or rewrapped.
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
        "groq_configured": "per-user (each request supplies its own key)",
        "hubspot_oauth_configured": bool(HUBSPOT_CLIENT_ID and HUBSPOT_CLIENT_SECRET),
    }
```
A simple status endpoint showing *which* integrations are configured, without ever exposing the actual key values — useful for quickly confirming the `.env` is set up correctly.

### Startup
```python
init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```
- `init_db()` runs at **import time** (module load), not just under `__main__` — so the table exists whether the app is launched via `python main.py` or via `uvicorn main:app --reload`.
- Only binds to `127.0.0.1` (localhost) — matches `manifest.json`'s `host_permissions` and is intentional: this backend is meant to run locally, not be exposed on the network.

---

## 13. Test/Diagnostic Scripts (companion files, not part of the app itself)

- **`test_elevenlabs_audio.py`** — generates a tiny 2-second silent WAV directly with Python's `wave` module (no ffmpeg needed) and uploads it straight to ElevenLabs' `/v1/speech-to-text` endpoint, printing the full exception/traceback on failure. Isolates ElevenLabs connectivity from every other part of the pipeline (audio format handling, ffmpeg, retries, etc).
- **`test_groq.py`** — bare Groq connectivity check: sends one trivial chat completion request and prints either the reply or the raw exception, bypassing the app's own retry/catch logic to see errors unfiltered.
- **`test_upload_speed.py`** — uploads a ~2MB random-bytes file to `httpbin.org` (a generic public test endpoint unrelated to ElevenLabs) to determine whether slow/broken uploads are a general network issue (antivirus HTTPS inspection, firewall, VPN, poor bandwidth) versus something specific to reaching ElevenLabs. Reports an approximate KB/s figure and interprets it (below ~100 KB/s is flagged as suspicious).

These three scripts were the actual diagnostic trail used to find and fix the `ELEVENLABS_TIMEOUT` tuple bug described in Section 8.

---

## 14. `users.db` and `npm-log.txt`

- **`users.db`** — the SQLite file created by `init_db()`. Contains exactly one table, `hubspot_tokens`, with one row per `user_id` that has connected HubSpot (columns: `user_id`, `access_token`, `refresh_token`, `expires_at`, `hub_domain`).
- **`npm-log.txt`** — an npm log file that ended up in the backend folder; not related to this Python service (no Node/npm involved in the backend itself).
