"""Live listen and recording controls for ESPHome assist satellites."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from pathlib import Path
import queue
import re
import secrets
import threading
import time
import wave

import voluptuous as vol

from aioesphomeapi import VoiceAssistantEventType

from homeassistant.components import switch
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.event import async_track_entity_registry_updated_event
from homeassistant.helpers.network import get_url

DOMAIN = "satellite_audio"

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

RECORDINGS_DIR = "recordings"
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
FIRST_AUDIO_TIMEOUT = 12.0
LIVE_QUEUE_SIZE = 100
ANNOUNCE_TIMEOUT = 12.0
RETRIGGER_LIMIT = 8
STALE_AUDIO_TIMEOUT = 4.0

_TERMINAL_EVENTS = frozenset(
    {
        VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END,
        VoiceAssistantEventType.VOICE_ASSISTANT_ERROR,
        VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
    }
)

LISTEN = "listen"
RECORD = "record"

_LOGGER = logging.getLogger(__name__)


def _sanitize(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", name).strip("_")
    return cleaned or "unknown"


class WavWriter:
    """Stream audio chunks to a wav file from a background thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started_at = time.monotonic()
        self._thread.start()

    def _run(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(self.path), "wb") as wav_file:
                wav_file.setnchannels(CHANNELS)
                wav_file.setsampwidth(SAMPLE_WIDTH)
                wav_file.setframerate(SAMPLE_RATE)
                while True:
                    item = self._queue.get()
                    if item is None:
                        break
                    wav_file.writeframes(item)
        except Exception:
            _LOGGER.exception("WavWriter failed for %s", self.path)

    def write(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def close(self) -> float:
        self._queue.put_nowait(None)
        self._thread.join(timeout=5)
        return time.monotonic() - self._started_at


class _TeeQueue:
    """Queue replacement that tees audio to a session and the real queue."""

    def __init__(self, real_queue: asyncio.Queue, session: "SatSession") -> None:
        self._real_queue = real_queue
        self._session = session
        self._active = True
        self._waiting = 0

    def detach(self) -> None:
        self._active = False

    def put_nowait(self, item) -> None:
        if self._active and item is not None:
            self._session._on_audio(item)
        if self._waiting > 0:
            self._real_queue.put_nowait(item)

    async def get(self):
        self._waiting += 1
        try:
            return await self._real_queue.get()
        finally:
            self._waiting -= 1

    def empty(self) -> bool:
        return self._real_queue.empty()


@dataclass
class SatSession:
    """Mic stream state of a single satellite."""

    manager: SatelliteAudioManager
    entity_id: str
    esphome_entry_id: str
    mac: str
    name: str
    listening: bool = False
    recording: bool = False
    live_token: str | None = None
    live_queues: set[asyncio.Queue[bytes | None]] = field(default_factory=set)
    writer: WavWriter | None = None
    last_recording_path: Path | None = None
    last_recording_seconds: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    first_audio: asyncio.Event = field(default_factory=asyncio.Event)
    _retrigger_count: int = 0
    _last_audio_at: float = 0.0
    _last_retrigger_at: float = 0.0
    _installed: bool = False
    _real_queue: asyncio.Queue | None = None
    _orig_send_event = None
    _monitor_task: asyncio.Task | None = None

    @property
    def client(self):
        entry = self.manager.hass.config_entries.async_get_entry(self.esphome_entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return None
        runtime_data = getattr(entry, "runtime_data", None)
        if runtime_data is None or getattr(runtime_data, "client", None) is None:
            return None
        return runtime_data.client

    @property
    def available(self) -> bool:
        return self.client is not None

    async def set_listen(self, enabled: bool) -> None:
        async with self.lock:
            if enabled == self.listening:
                return
            if enabled:
                if not await self._install():
                    raise HomeAssistantError(
                        f"Плата {self.name} недоступна, прослушивание не включено"
                    )
                self.live_token = secrets.token_urlsafe(16)
                self.listening = True
                _LOGGER.info("Listen enabled for %s", self.entity_id)
                if not await self._wait_first_audio():
                    await self._revert(LISTEN)
                    raise HomeAssistantError(
                        f"Плата {self.name} не отдаёт аудио (микрофон занят или плата занята), прослушивание выключено"
                    )
            else:
                self.listening = False
                self._close_live_queues()
                if not self.recording:
                    await self._uninstall()
                _LOGGER.info("Listen disabled for %s", self.entity_id)
        self.manager.notify_change(self)

    async def set_record(self, enabled: bool) -> None:
        async with self.lock:
            if enabled == self.recording:
                return
            if enabled:
                recordings_root = Path(self.manager.hass.config.config_dir) / RECORDINGS_DIR
                stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
                self.writer = WavWriter(recordings_root / _sanitize(self.name) / f"{stamp}.wav")
                if not await self._install():
                    await self._close_writer()
                    raise HomeAssistantError(
                        f"Плата {self.name} недоступна, запись не включена"
                    )
                self.recording = True
                if not await self._wait_first_audio():
                    await self._revert(RECORD)
                    raise HomeAssistantError(
                        f"Плата {self.name} не отдаёт аудио (микрофон занят или плата занята), запись выключена"
                    )
            else:
                self.recording = False
                await self._close_writer()
                if not self.listening:
                    await self._uninstall()
        self.manager.notify_change(self)

    async def _close_writer(self) -> None:
        if self.writer is None:
            return
        writer = self.writer
        self.writer = None
        duration = await self.manager.hass.async_add_executor_job(writer.close)
        self.last_recording_path = writer.path
        self.last_recording_seconds = duration
        _LOGGER.info(
            "Recording saved: %s (%.1fs)", writer.path, duration
        )

    async def _revert(self, kind: str) -> None:
        if kind == LISTEN:
            self.listening = False
            self._close_live_queues()
        else:
            self.recording = False
            await self._close_writer()
        if not self.listening and not self.recording:
            await self._uninstall()

    async def _wait_first_audio(self) -> bool:
        if self.first_audio.is_set():
            return True
        try:
            await asyncio.wait_for(self.first_audio.wait(), timeout=FIRST_AUDIO_TIMEOUT)
        except TimeoutError:
            return False
        return True

    def _satellite_entity(self):
        component = self.manager.hass.data[DATA_INSTANCES].get(Platform.ASSIST_SATELLITE)
        if component is None:
            return None
        return component.get_entity(self.entity_id)

    def _media_player_entity_id(self) -> str | None:
        satellite_entry = er.async_get(self.manager.hass).async_get(self.entity_id)
        if satellite_entry is None or satellite_entry.device_id is None:
            return None
        ent_reg = er.async_get(self.manager.hass)
        for entry in ent_reg.entities.values():
            if entry.domain == "media_player" and entry.device_id == satellite_entry.device_id:
                return entry.entity_id
        return None

    async def _install(self) -> bool:
        if self._installed:
            return True
        client = self.client
        entity = self._satellite_entity()
        if client is None or entity is None:
            _LOGGER.warning("Satellite %s is not ready", self.entity_id)
            return False
        self.first_audio.clear()
        self._last_audio_at = 0.0
        self._real_queue = entity._audio_queue
        entity._audio_queue = _TeeQueue(self._real_queue, self)
        self._orig_send_event = client.send_voice_assistant_event
        client.send_voice_assistant_event = self._wrap_send_event(self._orig_send_event)
        self._installed = True
        self._monitor_task = self.manager.hass.async_create_background_task(
            self._monitor(), f"satellite_audio_monitor_{self.entity_id}"
        )
        await self._start_conversation()
        return True

    def _wrap_send_event(self, orig_send_event):
        session = self

        def wrapped_send_event(event_type, data=None):
            if session._installed and event_type in _TERMINAL_EVENTS:
                _LOGGER.debug(
                    "Dropped terminal event %s for %s during audio session",
                    event_type.name,
                    session.entity_id,
                )
                return
            return orig_send_event(event_type, data)

        return wrapped_send_event

    async def _uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        monitor_task = self._monitor_task
        self._monitor_task = None
        if monitor_task is not None:
            monitor_task.cancel()
        entity = self._satellite_entity()
        if entity is not None and self._real_queue is not None:
            tee = entity._audio_queue
            entity._audio_queue = self._real_queue
            if isinstance(tee, _TeeQueue):
                tee.detach()
            self._real_queue = None
        client = self.client
        if client is not None:
            if self._orig_send_event is not None:
                client.send_voice_assistant_event = self._orig_send_event
                self._orig_send_event = None
            try:
                client.send_voice_assistant_event(
                    VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, {}
                )
            except Exception:
                _LOGGER.debug("Failed to send run end for %s", self.entity_id)

    async def _monitor(self) -> None:
        try:
            while self._installed and (self.listening or self.recording):
                await asyncio.sleep(2.0)
                if not self._installed or not (self.listening or self.recording):
                    break
                now = time.monotonic()
                if self._last_audio_at <= 0:
                    continue
                if now - self._last_retrigger_at > 30.0:
                    self._retrigger_count = 0
                if now - self._last_audio_at > STALE_AUDIO_TIMEOUT:
                    self._retrigger_count += 1
                    self._last_retrigger_at = now
                    if self._retrigger_count > RETRIGGER_LIMIT:
                        _LOGGER.error(
                            "Audio stream for %s keeps stalling, stopping listen/record",
                            self.entity_id,
                        )
                        self.manager.hass.async_create_task(self._abort_after_churn())
                        return
                    _LOGGER.info(
                        "Stale audio for %s, restarting conversation (attempt %d)",
                        self.entity_id,
                        self._retrigger_count,
                    )
                    self._last_audio_at = 0.0
                    await self._start_conversation()
        except asyncio.CancelledError:
            pass

    async def _abort_after_churn(self) -> None:
        try:
            await self.set_listen(False)
            await self.set_record(False)
        except Exception:
            _LOGGER.exception("Failed to stop %s after stream churn", self.entity_id)

    def _on_audio(self, data: bytes) -> None:
        self.first_audio.set()
        self._last_audio_at = time.monotonic()
        if self.writer is not None:
            self.writer.write(data)
        for live_queue in list(self.live_queues):
            try:
                live_queue.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    live_queue.get_nowait()
                    live_queue.put_nowait(data)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def _start_conversation(self) -> None:
        client = self.client
        if client is None:
            return
        hass = self.manager.hass
        media_url = f"{get_url(hass, allow_cloud=False, allow_ip=True)}/api/satellite_audio/silent.wav"
        mp_entity_id = self._media_player_entity_id()
        original_volume = None
        if mp_entity_id is not None:
            state = hass.states.get(mp_entity_id)
            if state is not None:
                original_volume = state.attributes.get("volume_level")
                try:
                    await hass.services.async_call(
                        "media_player",
                        "volume_set",
                        {"entity_id": mp_entity_id, "volume_level": 0.0},
                        blocking=True,
                    )
                    await asyncio.sleep(0.15)
                except Exception:
                    _LOGGER.debug("Could not mute %s for trigger", mp_entity_id)
        try:
            await asyncio.wait_for(
                client.send_voice_assistant_announcement_await_response(
                    media_url, ANNOUNCE_TIMEOUT, start_conversation=True
                ),
                timeout=ANNOUNCE_TIMEOUT,
            )
        except Exception:
            _LOGGER.debug(
                "Conversation trigger for %s did not confirm, relying on audio watchdog",
                self.entity_id,
            )
        finally:
            if mp_entity_id is not None and original_volume is not None:
                try:
                    await hass.services.async_call(
                        "media_player",
                        "volume_set",
                        {"entity_id": mp_entity_id, "volume_level": original_volume},
                        blocking=True,
                    )
                except Exception:
                    _LOGGER.debug("Could not restore volume for %s", mp_entity_id)

    def _close_live_queues(self) -> None:
        for live_queue in list(self.live_queues):
            live_queue.put_nowait(None)
        self.live_queues.clear()

    async def shutdown(self) -> None:
        async with self.lock:
            self.listening = False
            self.recording = False
            self._close_live_queues()
            await self._close_writer()
            await self._uninstall()


class SatelliteAudioManager:
    """Discover satellites and own sessions, entities and services."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.sessions: dict[str, SatSession] = {}
        self.entities: dict[str, list[switch.SwitchEntity]] = {}
        self.platform: EntityPlatform | None = None
        self._remove_track: Callable[[], None] | None = None

    async def async_setup(self) -> None:
        component = self.hass.data[DATA_INSTANCES].get(switch.DOMAIN)
        if component is None:
            raise HomeAssistantError("switch component is not loaded")
        self.platform = EntityPlatform(
            hass=self.hass,
            logger=_LOGGER,
            domain=switch.DOMAIN,
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        self._register_views()
        self._register_services()
        self._remove_track = async_track_entity_registry_updated_event(
            self.hass, [Platform.ASSIST_SATELLITE], self._on_registry_event
        )
        await self._reconcile()
        self.hass.async_create_background_task(self._poll_reconcile(), "satellite_audio_reconcile")

    async def _poll_reconcile(self) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await self._reconcile()
            except Exception:
                _LOGGER.exception("reconcile failed")

    async def async_shutdown(self) -> None:
        if self._remove_track is not None:
            self._remove_track()
            self._remove_track = None
        for session in list(self.sessions.values()):
            await session.shutdown()

    def _register_views(self) -> None:
        from .views import (
            ControlView,
            FileView,
            LiveSocketView,
            LiveView,
            PanelView,
            SilentWavView,
            StatusView,
        )

        for view in (
            StatusView(),
            ControlView(),
            LiveView(),
            LiveSocketView(),
            PanelView(),
            SilentWavView(),
            FileView(),
        ):
            self.hass.http.register_view(view)

    def _register_services(self) -> None:
        service_schema = vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("enabled"): cv.boolean,
            }
        )

        async def handle_service(call):
            entity_id = call.data["entity_id"]
            enabled = call.data["enabled"]
            session = self.sessions.get(entity_id)
            if session is None:
                raise HomeAssistantError(f"Unknown satellite {entity_id}")
            if call.service == LISTEN:
                await session.set_listen(enabled)
            else:
                await session.set_record(enabled)

        self.hass.services.async_register(
            DOMAIN, LISTEN, handle_service, schema=service_schema
        )
        self.hass.services.async_register(
            DOMAIN, RECORD, handle_service, schema=service_schema
        )

    @callback
    def _on_registry_event(self, event) -> None:
        self.hass.async_create_task(self._reconcile())

    async def _satellite_registry_entries(self) -> list:
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        found = []
        for entry in ent_reg.entities.values():
            if entry.domain != Platform.ASSIST_SATELLITE or entry.platform != "esphome":
                continue
            if entry.config_entry_id is None:
                continue
            esphome_entry = self.hass.config_entries.async_get_entry(entry.config_entry_id)
            if esphome_entry is None or esphome_entry.domain != "esphome":
                continue
            runtime_data = getattr(esphome_entry, "runtime_data", None)
            device_info = getattr(runtime_data, "device_info", None)
            mac = getattr(device_info, "mac_address", None)
            if mac is None:
                continue
            name = esphome_entry.title
            device = dev_reg.async_get(entry.device_id) if entry.device_id else None
            if device is not None:
                name = device.name_by_user or device.name or name
            found.append((entry.entity_id, esphome_entry.entry_id, mac, name, entry.device_id))
        return found

    async def _reconcile(self) -> None:
        from .switch import SatelliteAudioSwitch

        found = await self._satellite_registry_entries()
        found_map = {item[0]: item for item in found}

        for entity_id in list(self.sessions):
            if entity_id not in found_map:
                session = self.sessions.pop(entity_id)
                await session.shutdown()
                for entity in self.entities.pop(entity_id, []):
                    await entity.async_remove()

        new_sessions = []
        new_entities = []
        for entity_id, entry_id, mac, name, device_id in found:
            if entity_id in self.sessions:
                continue
            session = SatSession(
                manager=self,
                entity_id=entity_id,
                esphome_entry_id=entry_id,
                mac=mac,
                name=name,
            )
            self.sessions[entity_id] = session
            new_sessions.append((session, device_id))
            listen_entity = SatelliteAudioSwitch(session, LISTEN)
            record_entity = SatelliteAudioSwitch(session, RECORD)
            self.entities[entity_id] = [listen_entity, record_entity]
            new_entities.extend((listen_entity, record_entity))

        if new_entities and self.platform is not None:
            await self.platform.async_add_entities(new_entities)
            ent_reg = er.async_get(self.hass)
            for session, device_id in new_sessions:
                if not device_id:
                    continue
                for entity in self.entities.get(session.entity_id, []):
                    if entity.entity_id:
                        ent_reg.async_update_entity(entity.entity_id, device_id=device_id)

    def notify_change(self, session: SatSession) -> None:
        for entity in self.entities.get(session.entity_id, []):
            entity.async_write_ha_state()


class SatelliteAudioMediaSource(MediaSource):
    """Expose live streams and recordings to HA media browser."""

    name = "Спутники — аудио"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _manager(self) -> SatelliteAudioManager | None:
        return self.hass.data.get(DOMAIN)

    def _session_by_key(self, key: str) -> SatSession | None:
        manager = self._manager()
        if manager is None:
            return None
        for session in manager.sessions.values():
            if _sanitize(session.name) == key:
                return session
        return None

    def _browse_satellite(self, session: SatSession) -> BrowseMediaSource:
        key = _sanitize(session.name)
        children = []
        if session.listening and session.live_token:
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{key}/live",
                    media_class=MediaClass.MUSIC,
                    media_content_type=MediaType.MUSIC,
                    title="Живой эфир",
                    can_play=True,
                    can_expand=False,
                )
            )
        recordings_dir = (
            Path(self.hass.config.config_dir) / RECORDINGS_DIR / key
        )
        if recordings_dir.is_dir():
            files = sorted(
                (f for f in recordings_dir.iterdir() if f.suffix == ".wav"),
                key=lambda f: f.name,
                reverse=True,
            )
            for recording in files[:200]:
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{key}/{recording.name}",
                        media_class=MediaClass.MUSIC,
                        media_content_type=MediaType.MUSIC,
                        title=recording.stem,
                        can_play=True,
                        can_expand=False,
                    )
                )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=key,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=session.name,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.MUSIC,
        )

    async def async_browse_media(
        self, item: MediaSourceItem
    ) -> BrowseMediaSource:
        manager = self._manager()
        if manager is None or not manager.sessions:
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=None,
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=self.name,
                can_play=False,
                can_expand=False,
            )
        if item.identifier:
            parts = item.identifier.split("/", 1)
            session = self._session_by_key(parts[0])
            if session is not None:
                return self._browse_satellite(session)
        children = [
            self._browse_satellite(session)
            for session in sorted(
                manager.sessions.values(), key=lambda s: s.name
            )
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=self.name,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        if not item.identifier:
            raise ValueError("Nothing to play")
        parts = item.identifier.split("/", 1)
        if len(parts) < 2:
            raise ValueError("Nothing to play")
        key, leaf = parts
        session = self._session_by_key(key)
        if session is None:
            raise ValueError(f"Unknown satellite {key}")
        base_url = get_url(self.hass, allow_cloud=False, allow_ip=True)
        if leaf == "live":
            if not session.listening or not session.live_token:
                raise ValueError("Live stream is not active")
            return PlayMedia(
                url=f"{base_url}/api/satellite_audio/live/{session.live_token}",
                mime_type="audio/wav",
            )
        filename = Path(leaf).name
        if not filename.endswith(".wav"):
            raise ValueError("Not a recording")
        path = (
            Path(self.hass.config.config_dir) / RECORDINGS_DIR / key / filename
        )
        if not path.is_file():
            raise ValueError("Recording not found")
        return PlayMedia(
            url=f"{base_url}/api/satellite_audio/file/{key}/{filename}",
            mime_type="audio/wav",
        )


async def async_get_media_source(hass: HomeAssistant) -> SatelliteAudioMediaSource:
    return SatelliteAudioMediaSource(hass)


async def async_setup(hass: HomeAssistant, config) -> bool:
    manager = SatelliteAudioManager(hass)
    hass.data[DOMAIN] = manager
    await manager.async_setup()
    return True
