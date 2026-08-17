"""
Isolated test: uploads a tiny generated WAV file straight to ElevenLabs'
Scribe v2 transcription endpoint, with full traceback on failure and an
explicit generous timeout. No ffmpeg, no extension, no recording needed -
this removes every other variable so we can see the REAL error.
"""
import os
import wave
import struct
import traceback
import time

import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("ELEVENLABS_API_KEY")
print("Key loaded:", bool(api_key), "- starts with:", (api_key[:8] + "...") if api_key else None)

if not api_key:
    raise SystemExit("ELEVENLABS_API_KEY is not set in your .env - stopping here.")

# Generate a 2-second silent WAV file directly with Python's built-in
# wave module - no ffmpeg needed for this test.
test_path = "test_audio.wav"
sample_rate = 16000
duration_sec = 2
n_samples = sample_rate * duration_sec

with wave.open(test_path, "w") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sample_rate)
    silence_frame = struct.pack("<h", 0)
    wf.writeframes(silence_frame * n_samples)

print(f"Created test file: {test_path} ({os.path.getsize(test_path)} bytes)")

url = "https://api.elevenlabs.io/v1/speech-to-text"

print("Uploading to ElevenLabs Scribe v2 endpoint...")
start = time.time()
try:
    with open(test_path, "rb") as f:
        response = requests.post(
            url,
            headers={"xi-api-key": api_key},
            files={"file": (test_path, f, "audio/wav")},
            data={"model_id": "scribe_v2"},
            timeout=(10.0, 45.0),
        )
    elapsed = time.time() - start
    response.raise_for_status()
    text = response.json().get("text", "")
    print(f"SUCCESS in {elapsed:.1f}s. Transcript: {repr(text)}")
    # A silent file should come back with empty/near-empty text - that's
    # expected and fine. What matters here is that the request succeeded
    # at all (200 status), not what the transcript says.
except Exception as e:
    elapsed = time.time() - start
    print(f"FAILED after {elapsed:.1f}s")
    print("Exception type:", type(e).__name__)
    print("Exception repr:", repr(e))
    if hasattr(e, "response") and e.response is not None:
        print("Response status:", e.response.status_code)
        print("Response body:", e.response.text)
    print()
    print("FULL TRACEBACK:")
    traceback.print_exc()
