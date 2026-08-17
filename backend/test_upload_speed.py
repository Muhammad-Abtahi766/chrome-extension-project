"""
Diagnostic: uploads a ~2MB dummy file to httpbin.org (a generic public test
endpoint, nothing to do with ElevenLabs or Groq) to find out whether large
uploads are slow/broken in general on this machine/network, or whether it's
something specific to the ElevenLabs endpoint.

Run this the same way as the other test scripts:
    python test_upload_speed.py
"""
import os
import time
import traceback

import requests

test_path = "test_upload_dummy.bin"
size_mb = 2
with open(test_path, "wb") as f:
    f.write(os.urandom(size_mb * 1024 * 1024))

print(f"Created dummy file: {test_path} ({os.path.getsize(test_path) / (1024*1024):.1f} MB)")
print("Uploading to https://httpbin.org/post (generic test server, unrelated to ElevenLabs)...")

start = time.time()
try:
    with open(test_path, "rb") as f:
        response = requests.post(
            "https://httpbin.org/post",
            files={"file": (test_path, f, "application/octet-stream")},
            timeout=(10.0, 90.0),
        )
    elapsed = time.time() - start
    response.raise_for_status()
    speed_kbps = (size_mb * 1024) / elapsed
    print(f"SUCCESS in {elapsed:.1f}s  (~{speed_kbps:.0f} KB/s upload speed)")
    print()
    if speed_kbps < 100:
        print("That upload speed is very slow for a 2MB file. This points to a")
        print("network-level issue (antivirus HTTPS inspection, firewall, VPN,")
        print("or genuinely poor upload bandwidth) rather than anything specific")
        print("to ElevenLabs or your app's code.")
    else:
        print("That's a normal upload speed. If ElevenLabs uploads are still")
        print("timing out but this succeeds quickly, the issue is specific to")
        print("reaching ElevenLabs's servers - possibly regional routing, or")
        print("something blocking that specific domain.")
except Exception as e:
    elapsed = time.time() - start
    print(f"FAILED after {elapsed:.1f}s")
    print("Exception type:", type(e).__name__)
    print("Exception repr:", repr(e))
    print()
    print("This failing on a completely generic, unrelated test server strongly")
    print("suggests the problem is on this machine/network - not ElevenLabs and")
    print("not the app's code. Most common causes: antivirus doing HTTPS")
    print("inspection on outbound traffic, a firewall/VPN throttling uploads,")
    print("or genuinely poor upload bandwidth.")
    print()
    print("FULL TRACEBACK:")
    traceback.print_exc()
finally:
    if os.path.exists(test_path):
        os.remove(test_path)
