#!/usr/bin/env python3
"""Run simulate_satellites.py with auto-generated HA token."""
import json
import time
import jwt
import subprocess
import sys
from pathlib import Path

AUTH_PATH = "/home/green/homeassistant/config/.storage/auth"
HA_URL = "ws://localhost:8123"

with open(AUTH_PATH) as f:
    d = json.load(f)

token = None
for t in d["data"]["refresh_tokens"]:
    if t.get("client_name") == "for_test":
        token = jwt.encode(
            {"iss": t["id"], "iat": int(time.time()), "exp": int(time.time()) + 315360000},
            t["jwt_key"],
            algorithm="HS256",
        )
        break

if not token:
    print("ERROR: No 'for_test' token found in HA auth")
    sys.exit(1)

print(f"Token generated: {token[:40]}...")

audio = sys.argv[1] if len(sys.argv) > 1 else "audio/я_думаю_можно_заставить_это_все_работать.wav"
ramp = sys.argv[2] if len(sys.argv) > 2 else "1,10,50"
extra = sys.argv[3:] if len(sys.argv) > 3 else []

cmd = [
    sys.executable, "simulate_satellites.py",
    "--ha-url", HA_URL,
    "--token", token,
    "--audio", audio,
    "--ramp", ramp,
] + extra

print(f"Running: {' '.join(cmd[:8])}... --ramp {ramp} {' '.join(extra)}")
subprocess.run(cmd)
