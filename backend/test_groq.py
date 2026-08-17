"""
Standalone Groq connectivity test — run this directly to see the raw
error without any of the app's retry/catch logic hiding details.
"""
import os
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
api_key = os.getenv("GROQ_API_KEY")
print("Key loaded:", bool(api_key), "- starts with:", (api_key[:8] + "...") if api_key else None)

client = Groq(api_key=api_key)

try:
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": "Say 'connection ok' and nothing else."}],
    )
    print("SUCCESS:", response.choices[0].message.content)
except Exception as e:
    print("FAILED WITH FULL ERROR:")
    print(repr(e))
