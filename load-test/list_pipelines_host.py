#!/usr/bin/env python3
"""List HA pipelines using auto-generated token."""
import json
import time
import jwt
import asyncio
import websockets

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

async def main():
    async with websockets.connect(f"{HA_URL}/api/websocket") as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        await ws.recv()
        await ws.send(json.dumps({"type": "assist_pipeline/pipeline/list", "id": 1}))
        resp = json.loads(await ws.recv())
        pipelines = resp.get("result", {}).get("pipelines", resp.get("pipelines", []))
        for p in pipelines:
            print(f"  id={p['id'][:16]:16s}  name={p['name'][:40]:40s}  stt={p.get('stt_engine','?')[:60]}")
        print(f"\nTotal: {len(pipelines)} pipelines")

asyncio.run(main())
