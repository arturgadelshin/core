import json, time, jwt, asyncio, websockets

with open("/config/.storage/auth") as f:
    d = json.load(f)
for t in d["data"]["refresh_tokens"]:
    if t.get("client_name") == "for_test":
        token = jwt.encode({"iss": t["id"], "iat": int(time.time()), "exp": int(time.time())+315360000}, t["jwt_key"], algorithm="HS256")
        break

async def main():
    async with websockets.connect("ws://localhost:8123/api/websocket") as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        await ws.recv()
        await ws.send(json.dumps({"type": "assist_pipeline/pipeline/list", "id": 1}))
        resp = json.loads(await ws.recv())
        for p in resp.get("pipelines", []):
            print(f"  id={p['id'][:16]:16s}  name={p['name'][:40]:40s}  stt={p.get('stt_engine','?')[:60]}")
        print(f"\nTotal: {len(resp.get('pipelines', []))} pipelines")

asyncio.run(main())
