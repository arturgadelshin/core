"""Check pipeline audio settings (noise suppression, auto gain, VAD)."""
import json
import sys
import urllib.request

HA_URL = "http://localhost:8123"

def get_token():
    with open("/home/green/homeassistant/config/.storage/auth") as f:
        auth = json.load(f)
    import jwt
    refresh_token = None
    for rt in auth["data"].get("refresh_tokens", []):
        if rt.get("client_name") == "for_test":
            refresh_token = rt
            break
    if not refresh_token:
        print("ERROR: no for_test refresh token found")
        sys.exit(1)
    user_id = refresh_token["user_id"]
    jwt_key = refresh_token["jwt_key"]
    token = jwt.encode(
        {"iss": user_id, "iat": 1784870614, "exp": 2100230614},
        jwt_key,
        algorithm="HS256",
    )
    return token

token = get_token()

# Try API first
try:
    url = f"{HA_URL}/api/pipeline/assist_pipeline/pipeline_list"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    pipelines = data.get("pipelines", [])
    print(f"Total pipelines via API: {len(pipelines)}")
except Exception as e:
    print(f"API not available: {e}")
    pipelines = []

# Check the assist_pipeline storage for default settings
import os
storage_path = "/home/green/homeassistant/config/.storage/assist_pipeline"
if os.path.exists(storage_path):
    with open(storage_path) as f:
        ap_config = json.load(f)
    items = ap_config.get("data", {}).get("pipelines", [])
    if items:
        print(f"\nPipeline from storage ({len(items)} items):")
        p = items[0]
        for key in sorted(p.keys()):
            print(f"  {key}: {p[key]}")
