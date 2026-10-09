from __future__ import annotations

import asyncio
import struct
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

from . import DOMAIN, LISTEN, RECORD, RECORDINGS_DIR, SatelliteAudioManager

WAV_HEADER = struct.pack(
    "<4sI4s4sIHHIIHH4sI",
    b"RIFF",
    0xFFFFFFFF,
    b"WAVE",
    b"fmt ",
    16,
    1,
    1,
    16000,
    32000,
    2,
    16,
    b"data",
    0xFFFFFFFF,
)

_SILENCE_BYTES = 3200
SILENT_WAV = (
    struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + _SILENCE_BYTES,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        16000,
        32000,
        2,
        16,
        b"data",
        _SILENCE_BYTES,
    )
    + b"\x00" * _SILENCE_BYTES
)

PANEL_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Спутники — аудио</title>
<style>
body{font-family:sans-serif;background:#111418;color:#e8e8e8;margin:0;padding:16px}
h1{font-size:18px}
.card{background:#1c2128;border-radius:10px;padding:14px;margin-bottom:12px}
.name{font-weight:bold;margin-bottom:8px}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;margin-left:8px}
.on{background:#2e7d32}.off{background:#454c56}
button{border:0;border-radius:8px;padding:10px 16px;font-size:15px;margin-right:8px;cursor:pointer}
.l-on{background:#4caf50}.l-off{background:#607080}
.r-on{background:#e53935}.r-off{background:#607080}
.meter{height:8px;background:#2a313a;border-radius:4px;margin-top:12px;overflow:hidden}
.meter div{height:100%;width:0;background:#4caf50;border-radius:4px}
.meta{font-size:12px;color:#9aa4b2;margin-top:8px}
.state{font-size:12px;margin-top:8px}
.state.ok{color:#81c784}.state.err{color:#e57373}
</style>
</head>
<body>
<h1>Спутники — живое прослушивание и запись</h1>
<div id="list">Загрузка…</div>
<script>
const players = {};
let audioCtx = null;

function ensureCtx() {
  if (!audioCtx) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    try { audioCtx = new Ctor({sampleRate: 16000}); }
    catch (e) { audioCtx = new Ctor(); }
  }
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

function connectStream(sat, card) {
  const key = sat.entity_id;
  if (players[key] && players[key].url === sat.live_url && players[key].open) return;
  disconnectStream(key);
  const ctx = ensureCtx();
  const wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + sat.live_url.replace('/live/', '/ws/');
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  const st = {url: sat.live_url, ws: ws, open: true, nextTime: 0, meter: card.querySelector('.meter div'), state: card.querySelector('.state')};
  players[key] = st;
  const setState = (text, cls) => { st.state.textContent = text; st.state.className = 'state ' + cls; };
  setState('подключение к эфиру…', '');
  ws.onopen = () => setState('эфир идёт', 'ok');
  ws.onmessage = (ev) => {
    const i16 = new Int16Array(ev.data);
    let peak = 0;
    for (let i = 0; i < i16.length; i++) { const a = Math.abs(i16[i]); if (a > peak) peak = a; }
    if (st.meter) st.meter.style.width = Math.min(100, peak / 200).toFixed(0) + '%';
    let buf;
    if (ctx.sampleRate === 16000) {
      buf = ctx.createBuffer(1, i16.length, 16000);
      const f32 = buf.getChannelData(0);
      for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    } else {
      const ratio = 16000 / ctx.sampleRate;
      const outLen = Math.max(1, Math.floor(i16.length * ratio));
      buf = ctx.createBuffer(1, outLen, ctx.sampleRate);
      const f32 = buf.getChannelData(0);
      for (let i = 0; i < outLen; i++) {
        const j = i / ratio, j0 = Math.floor(j), j1 = Math.min(j0 + 1, i16.length - 1), t = j - j0;
        f32[i] = (i16[j0] * (1 - t) + i16[j1] * t) / 32768;
      }
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if (st.nextTime < now + 0.02) st.nextTime = now + 0.06;
    src.start(st.nextTime);
    st.nextTime += buf.duration;
    if (st.nextTime - now > 1.5) st.nextTime = now + 0.4;
  };
  ws.onclose = () => {
    if (players[key] === st) {
      st.open = false;
      if (st.meter) st.meter.style.width = '0%';
      setState('эфир остановлен', '');
    }
  };
  ws.onerror = () => setState('ошибка соединения', 'err');
}

function disconnectStream(key) {
  const p = players[key];
  if (p) {
    p.open = false;
    try { p.ws.close(); } catch (e) {}
    delete players[key];
  }
}

async function refresh() {
  const resp = await fetch('/api/satellite_audio/status');
  const state = await resp.json();
  render(state);
}

function fmt(s) { return s ? ('' + Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0')) : ''; }

function render(state) {
  const root = document.getElementById('list');
  if (!state.satellites || !state.satellites.length) {
    root.textContent = 'Спутники не найдены';
    return;
  }
  for (const sat of state.satellites) {
    const cardId = 'card-' + sat.entity_id.replace(/\./g, '-');
    let card = document.getElementById(cardId);
    if (!card) {
      card = document.createElement('div');
      card.className = 'card';
      card.id = cardId;
      card.innerHTML = '<div class="name"></div>' +
        '<button class="listen"></button>' +
        '<button class="record"></button>' +
        '<div class="meter"><div></div></div>' +
        '<div class="state"></div>' +
        '<div class="meta"></div>';
      card.querySelector('.listen').onclick = () => control(sat.entity_id, 'listen', !sat.listening);
      card.querySelector('.record').onclick = () => control(sat.entity_id, 'record', !sat.recording);
      root.appendChild(card);
    }
    card.querySelector('.name').innerHTML = sat.name +
      (sat.listening ? '<span class="badge on">эфир</span>' : '<span class="badge off">тишина</span>') +
      (sat.recording ? '<span class="badge on">REC</span>' : '');
    const lb = card.querySelector('.listen');
    lb.textContent = sat.listening ? 'Остановить прослушивание' : 'Слушать';
    lb.className = 'listen ' + (sat.listening ? 'l-on' : 'l-off');
    const rb = card.querySelector('.record');
    rb.textContent = sat.recording ? 'Остановить запись' : 'Записать';
    rb.className = 'record ' + (sat.recording ? 'r-on' : 'r-off');
    const meta = [];
    if (sat.recording_seconds) meta.push('пишется ' + fmt(sat.recording_seconds));
    if (sat.last_recording_file) meta.push('последняя: ' + sat.last_recording_file.split('/').pop() + ' (' + fmt(sat.last_recording_seconds) + ')');
    card.querySelector('.meta').textContent = meta.join(' · ');
    if (sat.listening && sat.live_url) {
      connectStream(sat, card);
    } else {
      disconnectStream(sat.entity_id);
      card.querySelector('.state').textContent = '';
      card.querySelector('.meter div').style.width = '0%';
    }
  }
}

async function control(entity_id, action, enabled) {
  if (action === 'listen' && enabled) ensureCtx();
  await fetch('/api/satellite_audio/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_id: entity_id, action: action, enabled: enabled})
  });
  refresh();
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


def _manager(request: web.Request) -> SatelliteAudioManager:
    return request.app["hass"].data[DOMAIN]


class StatusView(HomeAssistantView):
    url = "/api/satellite_audio/status"
    name = "api:satellite_audio:status"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        manager = _manager(request)
        satellites = []
        for session in manager.sessions.values():
            entry = {
                "entity_id": session.entity_id,
                "name": session.name,
                "listening": session.listening,
                "recording": session.recording,
                "last_recording_file": (
                    str(session.last_recording_path)
                    if session.last_recording_path is not None
                    else None
                ),
                "last_recording_seconds": (
                    round(session.last_recording_seconds, 1)
                    if session.last_recording_path is not None
                    else None
                ),
            }
            if session.listening and session.live_token:
                entry["live_url"] = f"/api/satellite_audio/live/{session.live_token}"
            if session.recording and session.writer is not None:
                entry["recording_file"] = str(session.writer.path)
            satellites.append(entry)
        return self.json({"satellites": satellites})


class ControlView(HomeAssistantView):
    url = "/api/satellite_audio/control"
    name = "api:satellite_audio:control"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        manager = _manager(request)
        data = await request.json()
        entity_id = data.get("entity_id")
        action = data.get("action")
        enabled = bool(data.get("enabled"))
        if action not in (LISTEN, RECORD):
            return self.json_message("unknown action", status_code=400)
        session = manager.sessions.get(entity_id)
        if session is None:
            return self.json_message("unknown satellite", status_code=404)
        try:
            if action == LISTEN:
                await session.set_listen(enabled)
            else:
                await session.set_record(enabled)
        except Exception:
            return self.json_message("action failed", status_code=409)
        return self.json({"ok": True})


class LiveView(HomeAssistantView):
    url = "/api/satellite_audio/live/{token}"
    name = "api:satellite_audio:live"
    requires_auth = False

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        manager = _manager(request)
        session = None
        for candidate in manager.sessions.values():
            if candidate.listening and candidate.live_token == token:
                session = candidate
                break
        if session is None:
            return self.json_message("stream not found", status_code=404)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/wav",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        await response.write(WAV_HEADER)
        live_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=100)
        session.live_queues.add(live_queue)
        try:
            while True:
                chunk = await live_queue.get()
                if chunk is None:
                    break
                await response.write(chunk)
        finally:
            session.live_queues.discard(live_queue)
        return response


class LiveSocketView(HomeAssistantView):
    url = "/api/satellite_audio/ws/{token}"
    name = "api:satellite_audio:ws"
    requires_auth = False

    async def get(self, request: web.Request, token: str) -> web.WebSocketResponse:
        manager = _manager(request)
        session = None
        for candidate in manager.sessions.values():
            if candidate.listening and candidate.live_token == token:
                session = candidate
                break
        websocket = web.WebSocketResponse(heartbeat=30)
        await websocket.prepare(request)
        if session is None:
            await websocket.close(code=4004)
            return websocket
        live_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=300)
        session.live_queues.add(live_queue)
        try:
            while True:
                chunk = await live_queue.get()
                if chunk is None:
                    await websocket.close()
                    break
                await websocket.send_bytes(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            session.live_queues.discard(live_queue)
        return websocket


class PanelView(HomeAssistantView):
    url = "/api/satellite_audio/panel"
    name = "api:satellite_audio:panel"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=PANEL_HTML, content_type="text/html")


class SilentWavView(HomeAssistantView):
    url = "/api/satellite_audio/silent.wav"
    name = "api:satellite_audio:silent"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(
            body=SILENT_WAV,
            content_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )


class FileView(HomeAssistantView):
    url = "/api/satellite_audio/file/{key}/{filename}"
    name = "api:satellite_audio:file"
    requires_auth = False

    async def get(self, request: web.Request, key: str, filename: str) -> web.Response:
        hass = request.app["hass"]
        base = (Path(hass.config.config_dir) / RECORDINGS_DIR / key).resolve()
        if not filename.endswith(".wav"):
            return self.json_message("not a recording", status_code=400)
        path = (base / Path(filename).name).resolve()
        if path.parent != base or not path.is_file():
            return self.json_message("recording not found", status_code=404)
        return web.FileResponse(
            path,
            headers={"Content-Type": "audio/wav", "Cache-Control": "no-store"},
        )
