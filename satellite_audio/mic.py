from __future__ import annotations

import re

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

from . import SatSession

_TOKEN_RE = re.compile(r"^([A-Za-z_0-9]+)=(\S+)$")

_PARAMS = {
    "ns": ("noise_suppression_level", "set_noise_suppression", int, 0, 4),
    "ag": ("auto_gain_dbfs", "set_auto_gain", int, 0, 31),
    "vol": ("volume_multiplier", "set_volume_multiplier", float, 0.1, 30.0),
    "speech": ("speech_threshold", "set_speech_threshold", float, 0.1, 1.0),
    "bspeech": (
        "before_command_speech_threshold",
        "set_before_command_speech_threshold",
        float,
        0.05,
        0.9,
    ),
    "silence": ("silence_seconds", "set_silence_seconds", float, 0.3, 10.0),
    "cmd": ("command_seconds", "set_command_seconds", float, 0.3, 10.0),
    "vadto": ("vad_timeout_seconds", "set_vad_timeout", float, 1.0, 60.0),
    "bct": (
        "before_command_timeout_seconds",
        "set_before_command_timeout",
        float,
        1.0,
        30.0,
    ),
    "trigto": ("trigger_timeout_seconds", "set_trigger_timeout", float, 1.0, 30.0),
}

_ALIASES = {full: short for short, (full, _, _, _, _) in _PARAMS.items()}
_DISPLAY_ORDER = [
    "ns",
    "ag",
    "vol",
    "speech",
    "bspeech",
    "silence",
    "cmd",
    "vadto",
    "bct",
    "trigto",
]


def _format_settings(attributes: dict) -> str:
    parts = []
    for short in _DISPLAY_ORDER:
        full = _PARAMS[short][0]
        if full not in attributes:
            continue
        value = attributes[full]
        if isinstance(value, float):
            parts.append(f"{short}={value:g}")
        else:
            parts.append(f"{short}={value}")
    if "vad_mode" in attributes:
        parts.append(f"vadmode={attributes['vad_mode']}")
    return " ".join(parts)


class SatelliteAudioMic(TextEntity):
    """Mic and VAD coefficients of a satellite in one text field."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:tune"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = 255

    def __init__(self, session: SatSession) -> None:
        self._session = session
        self._attr_unique_id = f"satellite_audio_{session.mac}_mic"
        self._attr_name = f"{session.name} Микрофон"
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, session.mac)}
        )
        self._remove_track = None
        self._last_applied = None

    @property
    def available(self) -> bool:
        return self._session.available

    def _assist_attributes(self) -> dict:
        state = self.hass.states.get(self._session.entity_id)
        if state is None:
            return {}
        return dict(state.attributes)

    @property
    def native_value(self) -> str | None:
        attributes = self._assist_attributes()
        if not attributes:
            return None
        return _format_settings(attributes)

    @property
    def extra_state_attributes(self) -> dict:
        result = {
            key: value
            for key, value in self._assist_attributes().items()
            if key in _ALIASES or key == "vad_mode"
        }
        firmware = self._firmware_settings()
        if firmware is not None:
            result["firmware"] = firmware
        if self._last_applied is not None:
            result["last_applied"] = self._last_applied
        return result

    def _firmware_settings(self) -> str | None:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(self._session.entity_id)
        if entry is None or entry.device_id is None:
            return None
        for candidate in ent_reg.entities.values():
            if candidate.domain != "sensor" or candidate.device_id != entry.device_id:
                continue
            if "прошивк" not in (candidate.original_name or "").lower():
                continue
            state = self.hass.states.get(candidate.entity_id)
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                return None
            return state.state
        return None

    def _parse(self, value: str) -> list[tuple[str, object]]:
        calls = []
        errors = []
        tokens = [token for token in re.split(r"[;,\s]+", value.strip()) if token]
        for token in tokens:
            match = _TOKEN_RE.match(token)
            if match is None:
                errors.append(f"не понял «{token}», формат k=v")
                continue
            key = match.group(1).lower()
            raw = match.group(2)
            if key in _ALIASES:
                key = _ALIASES[key]
            if key == "vadmode":
                if raw not in ("singleton", "per_pipeline"):
                    errors.append(f"vadmode: singleton или per_pipeline, получено {raw}")
                    continue
                calls.append(("set_vad_mode", raw))
                continue
            full, service, cast, low, high = _PARAMS.get(
                key, (None, None, None, None, None)
            )
            if full is None:
                errors.append(f"неизвестный ключ «{key}»")
                continue
            try:
                parsed = cast(raw)
            except ValueError:
                errors.append(f"{key}: нужно число, получено {raw}")
                continue
            if not low <= parsed <= high:
                errors.append(f"{key}: диапазон {low:g}–{high:g}, получено {raw}")
                continue
            calls.append((service, parsed))
        if errors:
            raise HomeAssistantError("; ".join(errors))
        return calls

    async def async_set_value(self, value: str) -> None:
        calls = self._parse(value)
        for service, payload in calls:
            await self.hass.services.async_call(
                "assist_satellite",
                service,
                {"entity_id": self._session.entity_id, "value": payload},
                blocking=True,
            )
        self._last_applied = value.strip()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._remove_track = async_track_state_change_event(
            self.hass, [self._session.entity_id], self._on_sat_event
        )

    @callback
    def _on_sat_event(self, event) -> None:
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_track is not None:
            self._remove_track()
            self._remove_track = None
