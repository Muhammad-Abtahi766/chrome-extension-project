"""
Automated Audio Meeting Summarizer & CRM Sync Pipeline — single-file backend.

Pipeline:
    audio upload -> preprocess (pydub: downsample/mono/compress) ->
    ElevenLabs Scribe v2 transcription WITH SPEAKER DIARIZATION (one request
    for the whole recording) -> deep structured Groq GPT-OSS 120B
    summarization -> CRM push (HubSpot, per-user via OAuth)

DIARIZATION NOTE: unlike the old pipeline, audio is no longer split into many
small independent chunks for transcription. Speaker IDs from diarization are
only consistent *within a single ElevenLabs request* - splitting the meeting
into pieces and diarizing each independently would produce "Speaker 1" labels
that don't refer to the same person across chunks. So the whole recording is
sent as one transcription request (still downsampled/compressed first to
keep the upload as small as practical).

SETUP:
    pip install fastapi uvicorn[standard] pydub langchain-groq \
                python-dotenv requests python-multipart fpdf2

    You also need ffmpeg installed and on PATH (pydub shells out to it):
        Windows: winget install ffmpeg
        Mac:     brew install ffmpeg
        Linux:   sudo apt install ffmpeg

    Set these environment variables (create a ".env" file next to this script):
        ELEVENLABS_API_KEY=your_elevenlabs_api_key_here     # from elevenlabs.io/app/settings/api-keys
        SUPABASE_URL=https://your-project.supabase.co       # from your Supabase project's Settings -> API
        SUPABASE_SERVICE_KEY=your_supabase_service_role_key # same page - "service_role" secret, NEVER expose this client-side

    Groq and HubSpot are no longer configured server-side at all. Each user
    registers an account (username + password, see /auth/register) and
    pastes their own Groq API key and (optionally) their own HubSpot Private
    App token into the extension's settings panel. Both are stored per-
    account in the Supabase "users" table and looked up by user_id on every
    request — there is no shared server-side key for either, and no HubSpot
    OAuth client ID/secret anywhere in this file anymore.

    Before first run, create the accounts table once in the Supabase SQL
    editor (see the comment above the STEP 0 section below for the exact
    schema).

USAGE:
    python main.py
    # or: uvicorn main:app --host 127.0.0.1 --port 8000 --reload

Then load the extension/ folder as an unpacked Chrome extension. On first
open it'll ask the user to register or log in; after that, Start/Stop
records a meeting tab and POSTs the audio here along with their user_id.
"""

import os
import re
import time
import base64
import shutil
import logging
import tempfile
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

# =============================================================================
# CONFIG
# =============================================================================

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# NOTE: there is no server-side Groq key anymore. Every request must supply
# its own via the "groq_api_key" form field (entered by the user in the
# extension's settings panel) - summarization always runs against the
# requesting user's own Groq account.

# ElevenLabs is now used for transcription (Scribe v2) instead of Groq Whisper.
# Groq is still used below, but only for the GPT-OSS 120B summarization step.
ELEVENLABS_API_KEY = (os.getenv("ELEVENLABS_API_KEY") or "").strip().strip('"').strip("'")
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Diagnostic only — never logs the key itself, just its shape, so a bad
# Railway paste (trailing newline, wrapped quotes, stale value) is provable
# from the deploy logs instead of guessed at.
if ELEVENLABS_API_KEY:
    logger.info(
        f"ELEVENLABS_API_KEY loaded: length={len(ELEVENLABS_API_KEY)}, "
        f"starts_with={ELEVENLABS_API_KEY[:4]!r}, ends_with={ELEVENLABS_API_KEY[-4:]!r}"
    )
else:
    logger.warning("ELEVENLABS_API_KEY is empty or not set.")

# --- Supabase (accounts + per-user API keys) ---
# HubSpot no longer uses OAuth (no client ID/secret, no redirect flow). Each
# user pastes their own HubSpot "Private App" access token (generated from
# their HubSpot account: Settings -> Integrations -> Private Apps), the same
# simple paste-and-save pattern as the Groq key. It's stored per-account in
# Supabase and used directly as a Bearer token on every CRM push.
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # service_role key - server-side only, never ship this to the extension

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    logger.warning(
        "SUPABASE_URL / SUPABASE_SERVICE_KEY not set - account registration/login and "
        "per-user API key storage will not work until these are configured."
    )

ELEVENLABS_MODEL = "scribe_v2"
# GPT-OSS 120B (OpenAI's open-weight reasoning model, served on Groq) -
# swapped in from Llama 3.3 70B. Same ~131K context window as before, so the
# map-reduce chunking below (WORDS_PER_SECTION) doesn't need to change, but
# it reasons noticeably better over the multi-section synthesis this prompt
# asks for. Cheaper per-token than Llama 3.3 70B too.
SUMMARY_MODEL = "openai/gpt-oss-120b"

# Diarization defaults. num_speakers can optionally be overridden per-request
# (see /process-meeting) if the caller knows exactly how many people are in
# the recording - that measurably improves diarization accuracy. Left as
# None by default, in which case ElevenLabs auto-detects.
DEFAULT_NUM_SPEAKERS: Optional[int] = None
LLM_TEMPERATURE = 0.2

# SUMMARY_SYSTEM_PROMPT, SECTION_SYSTEM_PROMPT, and REDUCE_SYSTEM_PROMPT are
# defined further down, next to the summarization functions that use them
# (STEP 3), since the map-reduce approach for long meetings needs all three
# prompts together to make sense.

app = FastAPI(title="Meeting Summarizer & CRM Sync Pipeline")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CrmPushResult(BaseModel):
    status: str
    detail: Dict[str, Any] = {}


# =============================================================================
# STEP 0 — ACCOUNTS & PER-USER API KEYS (Supabase)
# =============================================================================
# Replaces the old per-device random user_id + SQLite HubSpot-OAuth-token
# table. Now there's a real "users" table in Supabase (username + bcrypt
# password hash + groq_api_key + hubspot_api_key), so a user's Groq/HubSpot
# keys follow their ACCOUNT, not one specific browser/computer - they can log
# into the same account from any machine and their keys are already there.
#
# Expected Supabase table (create this once in the Supabase SQL editor):
#
#   create table users (
#     id uuid primary key default gen_random_uuid(),
#     username text unique not null,
#     password_hash text not null,
#     groq_api_key text,
#     hubspot_api_key text,
#     created_at timestamptz default now()
#   );
#
# HubSpot: no client ID/secret, no OAuth redirect. The user generates a
# "Private App" access token themselves from their own HubSpot account
# (Settings -> Integrations -> Private Apps -> Create private app -> grant
# crm.objects.contacts read/write -> copy the token) and pastes it in,
# exactly like the Groq key. It's stored as hubspot_api_key and used
# directly as a Bearer token in push_to_crm() below - no refreshing needed,
# Private App tokens don't expire the way OAuth access tokens do.

def _require_supabase() -> Client:
    if supabase is None:
        raise HTTPException(
            status_code=500,
            detail="Backend is not configured with Supabase credentials (SUPABASE_URL / SUPABASE_SERVICE_KEY).",
        )
    return supabase


def _public_user(row: Dict[str, Any]) -> Dict[str, Any]:
    """Strips the password hash before a user row ever goes back to the extension."""
    return {
        "user_id": row["id"],
        "username": row["username"],
        "has_groq_key": bool(row.get("groq_api_key")),
        "has_hubspot_key": bool(row.get("hubspot_api_key")),
    }


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

    logger.info(f"Registered new account: username={username}")
    return _public_user(result.data[0])


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

    logger.info(f"Logged in: username={username}")
    return _public_user(row)


@app.get("/user/keys")
def get_user_keys(user_id: str = Query(...)):
    """
    Used by the settings panel to show whether Groq/HubSpot keys are already
    set (as booleans only - the actual key values are never sent back down
    to the extension once saved, only whether one exists).
    """
    db = _require_supabase()
    result = db.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found.")
    return _public_user(result.data[0])


@app.put("/user/keys")
def update_user_keys(payload: UpdateKeysRequest):
    """
    Lets the user edit/replace their saved Groq and/or HubSpot keys from the
    settings panel at any time - same account, from any computer. Only the
    fields actually provided (or explicitly flagged for clearing) are
    touched; omitted fields are left as-is.
    """
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
    logger.info(f"Updated keys for user_id={payload.user_id}: {list(updates.keys())}")
    return _public_user(result.data[0])


def _get_user_row(user_id: str) -> Dict[str, Any]:
    db = _require_supabase()
    result = db.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found. Please log in again.")
    return result.data[0]


# =============================================================================
# STEP 1 — AUDIO PREPROCESSING (single file, no chunking)
# =============================================================================
# Previously this split the recording into many independent ~20s chunks so
# each upload/transcription call stayed small and a slow connection lost as
# little work as possible per failure. That approach is incompatible with
# diarization: ElevenLabs assigns speaker_id values ("speaker_0", "speaker_1",
# ...) that are only consistent *within one request* - transcribing chunk 3
# and chunk 9 separately gives two unrelated "speaker_0"s, not the same
# person. So the whole file now goes out as a single request. We still
# downsample and compress here to keep that one upload as small as we
# reasonably can.

def preprocess_audio(file_path: str) -> Tuple[str, str]:
    """
    Loads the uploaded recording, converts it to 16kHz mono, and re-exports
    it as a compressed mp3. Returns (processed_file_path, work_dir) - the
    caller is responsible for cleaning up work_dir afterwards.
    Requires ffmpeg to be installed and on PATH (pydub shells out to it).
    """
    try:
        audio = AudioSegment.from_file(file_path)
    except Exception as e:
        raise RuntimeError(
            f"Failed to load audio file (is ffmpeg installed and on PATH?): {e}"
        )

    if len(audio) == 0:
        raise RuntimeError("Uploaded audio file has zero duration.")

    # Speech models are trained on 16kHz mono audio - uploading full-quality
    # stereo (often 48kHz from tab+mic capture) wastes 6-8x the bandwidth for
    # zero accuracy benefit, and diarization doesn't need stereo separation
    # either (it works from the voice characteristics in the audio itself).
    audio = audio.set_frame_rate(16000).set_channels(1)

    work_dir = tempfile.mkdtemp(prefix="meeting_audio_")
    out_path = os.path.join(work_dir, "meeting.mp3")
    # 32kbps mono is plenty for speech-to-text and diarization accuracy, and
    # is half the size of the previous 64kbps setting - on a slow or
    # bandwidth-constrained connection (see test_upload_speed.py), a smaller
    # upload means less total time spent in the write phase where a timeout
    # or connection drop can hit, which matters more now that the whole
    # meeting goes out as one upload instead of many small chunks.
    audio.export(out_path, format="mp3", bitrate="32k")

    duration_sec = len(audio) / 1000
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    logger.info(
        f"Preprocessed audio: {duration_sec:.0f}s recording -> {size_mb:.1f}MB mp3 "
        f"(mono, 16kHz, 32kbps)"
    )
    return out_path, work_dir


# =============================================================================
# STEP 2 — SPEECH-TO-TEXT WITH SPEAKER DIARIZATION (ElevenLabs Scribe v2)
# =============================================================================

# One call now covers the entire meeting (see STEP 1's note on why), so the
# timeout needs to comfortably cover a long recording upload, not just a
# small chunk.
#
# IMPORTANT: this is a single float, not a (connect, read) tuple. requests/
# urllib3 has a well-known quirk (most visible on Windows) where, for a
# large request body, the timeout actually enforced during the write/upload
# phase can end up being the *connect* timeout rather than the read timeout
# - the socket timeout isn't always re-armed before sendall(). With a tuple
# like (15.0, 1800.0), a multi-minute upload of a whole meeting recording
# gets its write killed after ~15s every time, which surfaces as
# "ConnectionError: The write operation timed out" on every retry - looking
# like a new/different failure, but it's really the old slow-upload problem
# (see test_upload_speed.py) hitting a much larger single upload than before.
# A single float applies uniformly to connect AND read/write, so there's no
# phase left exposed to a short window.
ELEVENLABS_TIMEOUT = 1800.0  # seconds, applied to connect + write + read alike
MAX_TRANSCRIPTION_ATTEMPTS = 3


def _format_timestamp(seconds: Optional[float]) -> str:
    """Formats a float seconds value as HH:MM:SS for display in the transcript."""
    if seconds is None or seconds < 0:
        seconds = 0
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _build_speaker_turns(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapses ElevenLabs' word-level diarization output into speaker turns:
    consecutive words from the same speaker_id are merged into one turn, with
    a turn boundary wherever the speaker changes. "spacing" entries (pure
    whitespace between words) are skipped - words are rejoined with a single
    space instead, so spacing tokens carry no information we need. Audio
    event tags (e.g. "(laughter)") are kept inline as part of whichever
    speaker's turn they fall within.
    """
    turns: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for w in words:
        w_type = w.get("type")
        if w_type == "spacing":
            continue

        text = (w.get("text") or "").strip()
        if not text:
            continue

        speaker_id = w.get("speaker_id") or "speaker_unknown"
        start = w.get("start")
        end = w.get("end")

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


def transcribe_audio_with_diarization(
    file_path: str,
    num_speakers: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Sends the WHOLE preprocessed recording to ElevenLabs Scribe v2 in a
    single request with diarize=True, then reshapes the word-level response
    into clean speaker turns and a formatted transcript of the form:

        [00:01:15] Speaker 1: ...
        [00:01:42] Speaker 2: ...

    Returns a dict with:
        full_text     - the formatted, speaker-labeled transcript (fed to the
                         summarizer)
        plain_text    - ElevenLabs' own unlabeled transcript, kept as a
                         fallback/reference
        turns         - structured list of {speaker, start, end,
                         start_formatted, text} per spoken turn
        speaker_count - how many distinct speakers were detected
        language_code - detected/declared language of the audio
    """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not configured. Set it as an environment variable.")

    request_data = {
        "model_id": ELEVENLABS_MODEL,
        "diarize": "true",
        "timestamps_granularity": "word",
        "tag_audio_events": "true",
    }
    if num_speakers:
        request_data["num_speakers"] = str(num_speakers)

    last_error: Optional[Exception] = None
    payload: Optional[Dict[str, Any]] = None
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
            logger.info(
                f"Diarized transcription succeeded on attempt {attempt}/{MAX_TRANSCRIPTION_ATTEMPTS} "
                f"({file_size_mb:.1f}MB in {time.time() - attempt_start:.1f}s)."
            )
            break
        except Exception as e:
            last_error = e
            elapsed = time.time() - attempt_start
            # Elapsed time is the key diagnostic here: failing in ~15-20s
            # points at a network/proxy write-timeout (upload never really
            # got going), while failing near the full ELEVENLABS_TIMEOUT
            # value means the upload was progressing but genuinely too slow.
            #
            # For 4xx errors specifically, the exception message alone
            # ("400 Client Error: Bad Request for url: ...") never tells you
            # WHY - ElevenLabs puts the real reason (bad key, quota/plan
            # limit, unsupported param, file too long, etc.) in the response
            # body. Log that body explicitly so failures are diagnosable
            # instead of just "something went wrong".
            response_body = None
            response_obj = getattr(e, "response", None)
            if response_obj is not None:
                try:
                    response_body = response_obj.text
                except Exception:
                    response_body = None
            logger.warning(
                f"Diarized transcription attempt {attempt}/{MAX_TRANSCRIPTION_ATTEMPTS} failed after "
                f"{elapsed:.1f}s (file was {file_size_mb:.1f}MB) — {type(e).__name__}: {e}"
                + (f" | Response body: {response_body}" if response_body else "")
            )
            if attempt < MAX_TRANSCRIPTION_ATTEMPTS:
                time.sleep(5 * attempt)  # 5s, then 10s backoff — calls are large now, give more room

    if last_error is not None or payload is None:
        raise RuntimeError(
            f"Transcription failed after {MAX_TRANSCRIPTION_ATTEMPTS} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        )

    words = payload.get("words") or []
    raw_turns = _build_speaker_turns(words)

    # Map ElevenLabs' internal speaker_id ("speaker_0", "speaker_1", ...) to
    # friendlier display labels ("Speaker 1", "Speaker 2", ...), assigned in
    # order of first appearance so the numbering matches who spoke first.
    speaker_labels: Dict[str, str] = {}

    def label_for(speaker_id: str) -> str:
        if speaker_id not in speaker_labels:
            speaker_labels[speaker_id] = f"Speaker {len(speaker_labels) + 1}"
        return speaker_labels[speaker_id]

    turns: List[Dict[str, Any]] = []
    formatted_lines: List[str] = []
    for t in raw_turns:
        text = " ".join(t["words"]).strip()
        if not text:
            continue
        speaker = label_for(t["speaker_id"])
        start_formatted = _format_timestamp(t["start"])
        turns.append({
            "speaker": speaker,
            "start": t["start"],
            "end": t["end"],
            "start_formatted": start_formatted,
            "text": text,
        })
        formatted_lines.append(f"[{start_formatted}] {speaker}: {text}")

    full_text = "\n".join(formatted_lines)
    plain_text = (payload.get("text") or "").strip()

    logger.info(
        f"Transcribed and diarized recording: {len(turns)} turn(s), "
        f"{len(speaker_labels)} speaker(s) detected."
    )

    return {
        "full_text": full_text,
        "plain_text": plain_text,
        "turns": turns,
        "speaker_count": len(speaker_labels),
        "language_code": payload.get("language_code"),
        "errors": [],
    }


# =============================================================================
# STEP 3 — SUMMARIZATION (Groq GPT-OSS 120B via langchain-groq)
# =============================================================================
# Long meetings (2-3 hours) produce transcripts of 25,000+ words - far too
# much to hand to the model in a single call and get a coherent summary
# back. Instead we use a map-reduce approach: split the transcript into
# ~15-minute sections, summarize each section on its own, then do one final
# pass that combines those section-summaries into the final structured
# output. Each individual call stays small and reliable regardless of how
# long the meeting actually was.

# ~15 min of natural speech is roughly 2,000-2,500 words. Splitting on word
# count (not time) since that's what actually determines whether a call is
# too big for the model - keeps this correct even if a chunk transcribed to
# unusually dense or sparse text.
WORDS_PER_SECTION = 2200
# Below this, there's no benefit to the two-pass approach - a normal
# 5-60 min meeting goes straight through as a single call, same as before.
MAP_REDUCE_THRESHOLD_WORDS = 3000

# The transcript fed into every prompt below is now diarized: each line looks
# like "[00:01:15] Speaker 1: ...". Both prompts are written with that in
# mind - the model is told explicitly that it can and should attribute
# points to specific speakers, not just to "the meeting" in the abstract.

REQUIRED_OUTPUT_FORMAT = """# [Generated Descriptive Title]

## 1. Main Theme
- Provide a clear statement of the overall topic/theme of the recording.
- Explain the core focus, overarching goals, and context in detail across 2-3 structured paragraphs.

## 2. Key Discussion Points
- Bulleted list of all primary topics discussed during the meeting.
- Include specific details, contextual nuances, decisions made, and individual contributions tagged by speaker where applicable.

## 3. Comprehensive Summary
- An extended, deep-dive section breaking down the conversation sequentially or topically.
- Expand on arguments, background reasoning, problems raised, and solutions proposed.

## 4. Conclusion & Action Items
- A synthesizing conclusion wrapping up the meeting's outcomes.
- Clear summary of final decisions, next steps, and assigned responsibilities."""

SECTION_SYSTEM_PROMPT = """You are an expert meeting analyst summarizing ONE SECTION of a \
longer, speaker-diarized meeting transcript. Each line of the transcript is formatted as \
"[HH:MM:SS] Speaker N: ..." - preserve that speaker attribution in your output whenever a \
point, decision, or claim can be tied to a specific speaker.

This is an intermediate step: your output will later be combined with summaries of the \
other sections of this same meeting into one final report, so your job here is to extract \
and preserve DETAIL, not to write a short recap. Do not rush and do not omit context that \
a later reader would need - capture specific topics discussed, arguments and reasoning \
given, decisions made, numbers/names/dates mentioned, problems raised, and any solutions \
or commitments proposed, with speaker attribution wherever the transcript supports it. Do \
not invent or infer content that isn't actually supported by the transcript.

Output plain, densely-detailed bullet points grouped loosely by sub-topic if the section \
covers more than one. No preamble, no headers, no markdown title - just the bullets \
themselves. It is fine and expected for this to run long if the section is dense."""

SUMMARY_SYSTEM_PROMPT = f"""You are an expert meeting analyst. You produce deep, thorough, \
comprehensive written reports from meeting transcripts - never brief overviews. You are \
given a speaker-diarized transcript where each line is formatted as \
"[HH:MM:SS] Speaker N: ...". The transcript may contain filler words, false starts, and \
transcription noise - clean that up in your writing, but never invent content that isn't \
actually supported by the transcript, and never omit a point, decision, disagreement, or \
piece of context because it seems minor. Take your time; do not rush toward a short answer. \
Attribute specific statements, positions, and contributions to the speaker who made them \
(e.g. "Speaker 2 raised concerns about...") wherever the transcript makes that clear.

You MUST respond in EXACTLY the following Markdown structure, with these exact headings, \
in this exact order, and nothing before, after, or outside it (no preamble, no closing \
remarks, no commentary about the transcript itself):

{REQUIRED_OUTPUT_FORMAT}

Formatting rules:
- The title on the first line must be a specific, descriptive title for THIS meeting (not \
a generic placeholder), written as a single Markdown H1 ("# ").
- Use the four "## N. ..." headings verbatim, in that exact order, every time.
- Section 1 (Main Theme) must be 2-3 full paragraphs of prose, not a bare bullet list.
- Section 2 (Key Discussion Points) must be a genuinely comprehensive bulleted list - every \
distinct topic raised, not just the two or three most obvious ones.
- Section 3 (Comprehensive Summary) is the longest section: a real deep-dive, not a repeat \
of section 2's bullets in sentence form. Walk through the discussion's reasoning, tensions, \
and turning points.
- Section 4 (Conclusion & Action Items) must clearly separate what was decided from what is \
still open, and list action items as "Owner: action" pairs where an owner is identifiable \
(or "Unassigned: action" if not)."""

# Same required structure, but the input is already a set of section
# summaries (not raw transcript) - the model is combining/deduplicating
# detail that's already been extracted, not summarizing noisy speech from
# scratch. Section summaries retain speaker attribution where the section
# pass captured it, so the reduce pass is told to preserve that too.
REDUCE_SYSTEM_PROMPT = f"""You are an expert meeting analyst combining detailed summaries \
from consecutive sections of ONE long, speaker-diarized meeting into a single deep, \
comprehensive report. The sections are in chronological order and were each summarized \
independently, so some points may repeat across sections (e.g. a decision mentioned early \
and reconfirmed later) - merge and deduplicate those into one point rather than repeating \
them, while preserving the overall chronology of the discussion. Preserve speaker \
attribution from the section summaries wherever it's present. Do not invent content that \
isn't supported by the section summaries, and do not compress away detail for the sake of \
brevity - your job is to produce one unified, thorough report, not a shorter one.

You MUST respond in EXACTLY the following Markdown structure, with these exact headings, \
in this exact order, and nothing before, after, or outside it (no preamble, no closing \
remarks, no commentary about the source material):

{REQUIRED_OUTPUT_FORMAT}

Formatting rules:
- The title on the first line must be a specific, descriptive title for THIS meeting (not \
a generic placeholder), written as a single Markdown H1 ("# ").
- Use the four "## N. ..." headings verbatim, in that exact order, every time.
- Section 1 (Main Theme) must be 2-3 full paragraphs of prose, not a bare bullet list.
- Section 2 (Key Discussion Points) must be a genuinely comprehensive bulleted list covering \
every distinct topic across all sections, not just the most prominent ones.
- Section 3 (Comprehensive Summary) is the longest section: synthesize the full arc of the \
meeting across all sections - reasoning, tensions, and turning points - not a restatement \
of section 2.
- Section 4 (Conclusion & Action Items) must clearly separate what was decided from what is \
still open, and list action items as "Owner: action" pairs where an owner is identifiable \
(or "Unassigned: action" if not)."""


def _get_summary_llm(user_groq_api_key: Optional[str] = None) -> ChatGroq:
    # Every user must supply their own Groq key now (entered in the
    # extension's settings panel) - there is no server-side fallback key.
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
        # gpt-oss models support a reasoning_effort knob (low/medium/high).
        # "medium" is the balance point for this task - noticeably better
        # structure/attribution than "low" without the latency hit of "high"
        # on a prompt that's already tightly specified.
        #
        # IMPORTANT: reasoning_effort must be passed as a TOP-LEVEL ChatGroq
        # constructor argument, not inside model_kwargs. Current langchain-groq
        # versions (1.x) added reasoning_effort as a first-class field of
        # ChatGroq itself - stuffing it into model_kwargs instead means it
        # collides with that first-class field when the request params are
        # assembled, which is what broke summarization after switching from
        # llama-3.3-70b (which never touched this code path at all, since it
        # doesn't support reasoning_effort) to gpt-oss-120b.
        reasoning_effort="medium",
    )


def _extract_response_text(response: Any) -> str:
    """
    Pulls the plain-text answer out of a ChatGroq response, tolerating both
    response shapes seen in the wild:
      - response.content is a plain string (the classic/expected case).
      - response.content is a list of content-block dicts (the newer
        langchain-core "standard content blocks" representation) - in which
        case we join every block of type "text".

    gpt-oss-120b models also return their chain-of-thought separately (in
    additional_kwargs["reasoning_content"] once langchain-groq parses it, or
    inline wrapped in "<|channel|>...<|message|>" markers if something
    upstream fails to strip it) - reasoning is never treated as the answer,
    and any stray channel markers are stripped defensively so they can never
    leak into the summary shown to the user.
    """
    content = getattr(response, "content", None)

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in (None, "text") and block.get("text"):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    else:
        text = ""

    # Defensive cleanup: if raw Harmony-format channel markers ever leak
    # through (seen intermittently with gpt-oss-120b on Groq), keep only the
    # "final" channel's message rather than showing the reasoning/analysis
    # channel content to the user.
    if "<|channel|>final<|message|>" in text:
        text = text.split("<|channel|>final<|message|>", 1)[1]
    text = re.sub(r"<\|[a-zA-Z_]+\|>", "", text)

    return text.strip()


def _split_into_sections(full_text: str, words_per_section: int) -> List[str]:
    """
    Splits the diarized transcript into sections of roughly `words_per_section`
    words each, WITHOUT ever splitting in the middle of a
    "[HH:MM:SS] Speaker N: ..." line - each line is one speaker turn, and
    breaking one apart would corrupt the speaker/timestamp attribution for
    whatever's on either side of the cut. Falls back to the old word-slicing
    behavior only if the text has no line structure at all (shouldn't happen
    with diarized output, but keeps this safe for plain-text input too).
    """
    lines = full_text.splitlines()
    if not lines:
        return []

    sections: List[str] = []
    current_lines: List[str] = []
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


def summarize_transcript(full_text: str, user_groq_api_key: Optional[str] = None) -> str:
    word_count = len(full_text.split())

    if word_count <= MAP_REDUCE_THRESHOLD_WORDS:
        # Short meeting - single call, exactly as before.
        llm = _get_summary_llm(user_groq_api_key)
        messages = [
            SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
            HumanMessage(content=f"Meeting transcript:\n\n{full_text}\n\nProduce the structured notes now."),
        ]
        response = llm.invoke(messages)
        summary_text = _extract_response_text(response)
        if not summary_text:
            raise RuntimeError(
                "The summarization model returned no usable text. This can happen if the "
                "Groq API key is invalid/out of quota, or if the model's response format "
                "changed. Raw response for debugging: " + repr(response)[:500]
            )
        logger.info(f"Summary generated in a single pass ({word_count} words).")
        return summary_text

    # Long meeting - map-reduce.
    sections = _split_into_sections(full_text, WORDS_PER_SECTION)
    logger.info(f"Transcript is {word_count} words - summarizing in {len(sections)} section(s) before combining.")

    llm = _get_summary_llm(user_groq_api_key)
    section_summaries: List[str] = []
    for index, section_text in enumerate(sections):
        messages = [
            SystemMessage(content=SECTION_SYSTEM_PROMPT),
            HumanMessage(content=f"Meeting transcript - section {index + 1} of {len(sections)}:\n\n{section_text}"),
        ]
        try:
            response = llm.invoke(messages)
            section_text = _extract_response_text(response)
            if not section_text:
                raise RuntimeError("Model returned no usable text for this section.")
            section_summaries.append(f"[Section {index + 1}]\n{section_text}")
            logger.info(f"  Section {index + 1}/{len(sections)} summarized.")
        except Exception as e:
            logger.error(f"  Section {index + 1}/{len(sections)} summarization failed: {e}")
            section_summaries.append(f"[Section {index + 1}]\n(This section could not be summarized: {e})")

    combined_sections = "\n\n".join(section_summaries)
    reduce_messages = [
        SystemMessage(content=REDUCE_SYSTEM_PROMPT),
        HumanMessage(content=f"Section summaries, in order:\n\n{combined_sections}\n\nProduce the combined structured notes now."),
    ]
    response = llm.invoke(reduce_messages)
    summary_text = _extract_response_text(response)
    if not summary_text:
        raise RuntimeError(
            "The summarization model returned no usable text on the final combine pass. "
            "Raw response for debugging: " + repr(response)[:500]
        )
    logger.info(f"Final combined summary generated from {len(sections)} section(s).")
    return summary_text


def extract_meeting_title(summary_text: str) -> str:
    """Pulls the title out of the summary's leading '# Title' Markdown H1 line."""
    match = re.search(r"^#\s+(.+)$", summary_text, re.MULTILINE)
    if match:
        title = match.group(1).strip()
        if title:
            return title
    return "Untitled meeting"


# =============================================================================
# STEP 3.5 — LOCAL PDF EXPORT (optional, user-triggered from the extension)
# =============================================================================
# Nothing here touches HubSpot or any server-side storage — this just turns
# the summary into a PDF and hands the bytes back to the extension, which
# saves it straight to the user's own computer via chrome.downloads. The
# backend never keeps a copy.

def sanitize_filename(title: str) -> str:
    """
    Turns a meeting title into a safe filename: strips characters that are
    illegal (or just awkward) in Windows/Mac/Linux filenames, collapses
    whitespace, and falls back to a timestamped name if nothing usable is
    left (e.g. a title that was only punctuation/emoji).
    """
    name = re.sub(r'[\\/:*?"<>|]', "", title)  # illegal on Windows
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:120]  # keep it well under filesystem limits
    if not name:
        name = f"meeting-{int(time.time())}"
    return name


def generate_summary_pdf(meeting_title: str, summary_text: str, full_transcript: Optional[str] = None) -> bytes:
    """
    Renders the structured Markdown summary as a readable PDF. Summaries now
    follow a strict "# Title" + "## 1. ... / ## 2. ... / ## 3. ... / ## 4. ..."
    Markdown structure (see SUMMARY_SYSTEM_PROMPT), so this does lightweight
    Markdown-aware rendering (H1/H2 get bold/larger text, everything else is
    body text) rather than dumping raw "#"/"##" characters onto the page.
    Kept intentionally plain otherwise (no logos/fancy layout) since the
    point is a readable local record, not a branded doc.

    full_transcript: OPTIONAL. The diarized, timestamped transcript text
    (transcript_data["full_text"]). When provided, it's appended after the
    summary as its own page - "Full Transcript" heading followed by the
    "[HH:MM:SS] Speaker N: ..." lines as-is, one per paragraph. This is
    purely additive: if it's omitted, the PDF looks exactly like before.
    """
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # FPDF's core fonts are latin-1 only, so replace characters (e.g. smart
    # quotes/em-dashes the LLM sometimes produces) that would otherwise raise.
    def to_latin1(s: str) -> str:
        return s.encode("latin-1", "replace").decode("latin-1")

    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 10, to_latin1(meeting_title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    body_lines = summary_text.splitlines()
    # The summary's own leading "# Title" line would just repeat the title
    # already rendered above - drop it so it isn't shown twice.
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

    if full_transcript and full_transcript.strip():
        # Fresh page so the transcript reads as a distinct section, not a
        # continuation crammed under the summary's last heading.
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.multi_cell(0, 8, "Full Transcript", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # Smaller body font than the summary (10 vs 11) since a full
        # transcript is much longer and this keeps page count reasonable.
        pdf.set_font("Helvetica", size=10)
        for line in full_transcript.splitlines():
            stripped = to_latin1(line.strip())
            if stripped == "":
                pdf.ln(2)
                continue
            pdf.multi_cell(0, 5, stripped, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output(dest="S"))


# =============================================================================
# STEP 4 — CRM PUSH (per-user HubSpot via OAuth)
# =============================================================================

def push_to_crm(hubspot_api_key: Optional[str], summary_text: str, meeting_title: str) -> Dict[str, Any]:
    """
    Pushes the summary as a Note/Engagement into the user's HubSpot CRM,
    using the Private App access token they generated in their own HubSpot
    account and pasted into the extension's settings panel (stored as
    hubspot_api_key on their account). If they haven't set one, the push is
    skipped (pipeline still succeeds — recording/transcription/summary all
    still work standalone).
    """
    if not hubspot_api_key:
        logger.info("No HubSpot key set for this account — skipping CRM push.")
        return {"status": "skipped", "reason": "HubSpot API key not set for this account"}

    url = "https://api.hubapi.com/crm/v3/objects/notes"
    headers = {
        "Authorization": f"Bearer {hubspot_api_key}",
        "Content-Type": "application/json",
    }
    note_body = f"{meeting_title}\n\n{summary_text}"
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": int(time.time() * 1000),
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        logger.info("Pushed summary note to HubSpot.")
        return {"status": "success", "crm_response": response.json()}
    except requests.exceptions.RequestException as e:
        logger.error(f"CRM push failed: {e}")
        return {"status": "failed", "error": str(e)}


# =============================================================================
# API ENDPOINT
# =============================================================================

@app.post("/process-meeting")
async def process_meeting(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    save_locally: bool = Form(False),
    num_speakers: Optional[int] = Form(None),
):
    """
    user_id: REQUIRED. Identifies the logged-in account (returned by
    /auth/login or /auth/register). Both the Groq key and the HubSpot key
    used for this request are looked up from that account in Supabase - the
    extension no longer sends key values with the upload at all.

    save_locally: when True, the response includes a base64-encoded PDF of
    the summary (filename = the meeting title) for the extension to save to
    the user's computer via chrome.downloads. This is independent of the
    HubSpot push — either, both, or neither can happen per request.

    num_speakers: OPTIONAL. If the caller knows exactly how many distinct
    people are in the recording, passing it measurably improves diarization
    accuracy. Left unset, ElevenLabs auto-detects the speaker count.
    """
    user_row = _get_user_row(user_id)
    groq_api_key = (user_row.get("groq_api_key") or "").strip()
    hubspot_api_key = (user_row.get("hubspot_api_key") or "").strip() or None

    if not groq_api_key:
        raise HTTPException(
            status_code=400,
            detail="A Groq API key is required. Enter yours in the extension's settings panel (gear icon).",
        )
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
        crm_result = push_to_crm(hubspot_api_key, summary, meeting_title)

        pdf_result = None
        if save_locally:
            try:
                pdf_bytes = generate_summary_pdf(meeting_title, summary, transcript_data["full_text"])
                pdf_result = {
                    "filename": f"{sanitize_filename(meeting_title)}.pdf",
                    "data_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                }
                logger.info(f"Generated local-save PDF for user_id={user_id}.")
            except Exception as e:
                logger.error(f"PDF generation failed for user_id={user_id}: {e}")
                pdf_result = {"error": f"PDF generation failed: {e}"}

        return {
            "meeting_title": meeting_title,
            "transcript": transcript_data,
            "summary": summary,
            "crm_push": crm_result,
            "pdf": pdf_result,
        }

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


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "groq_configured": "per-account (stored in Supabase, entered via settings)",
        "hubspot_configured": "per-account (Private App token stored in Supabase, no OAuth)",
        "supabase_configured": supabase is not None,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
