from __future__ import annotations

import re

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

from . import SatSession

_TOKEN_RE = re.compile(r"^([A-Za-z_0-9]+)=(\S+)$")

_SPECS = [
    {
        "full": "noise_suppression_level",
        "short": "ns",
        "service": "set_noise_suppression",
        "cast": int,
        "min": 0,
        "max": 4,
        "step": 1,
        "unit": None,
        "icon": "mdi:noise-control-off",
        "name": "Шумоподавление 0-4",
        "comment": "Шумоподавление Speex DSP: убирает фоновый шум перед распознаванием. 0 — выкл, 1 — мин, 4 — макс. Высокие уровни могут подавлять тихую речь.",
    },
    {
        "full": "auto_gain_dbfs",
        "short": "ag",
        "service": "set_auto_gain",
        "cast": int,
        "min": 0,
        "max": 31,
        "step": 1,
        "unit": "dB",
        "icon": "mdi:trending-up",
        "name": "Автоусиление дБ 0-31",
        "comment": "Автоусиление сигнала в дБFS: полезно при тихом микрофоне. Рекомендуется 10-20 дБ; выше 25 — искажения. 0 — выключено.",
    },
    {
        "full": "volume_multiplier",
        "short": "vol",
        "service": "set_volume_multiplier",
        "cast": float,
        "min": 0.1,
        "max": 30.0,
        "step": 0.1,
        "unit": "×",
        "icon": "mdi:volume-plus",
        "name": "Множитель громкости 0.1-30",
        "comment": "Умножает громкость микрофона перед распознаванием. 1.0 — без изменений, 1.5-2.0 — умеренно, выше 10 — возможны искажения.",
    },
]

_ALIASES = {spec["full"]: spec["short"] for spec in _SPECS}


def firmware_settings(hass, entity_id: str) -> str | None:
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    for candidate in ent_reg.entities.values():
        if candidate.domain != "sensor" or candidate.device_id != entry.device_id:
            continue
        if "прошивк" not in (candidate.original_name or "").lower():
            continue
        state = hass.states.get(candidate.entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return state.state
    return None


def assist_attributes(hass, entity_id: str) -> dict:
    state = hass.states.get(entity_id)
    if state is None:
        return {}
    return dict(state.attributes)


def mic_status(hass, entity_id: str) -> dict:
    attributes = assist_attributes(hass, entity_id)
    result = {
        key: value
        for key, value in attributes.items()
        if key in _ALIASES
    }
    firmware = firmware_settings(hass, entity_id)
    if firmware is not None:
        result["firmware"] = firmware
    return result


def parse_value(value: str) -> list[tuple[str, object]]:
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
        spec = next((s for s in _SPECS if s["short"] == key), None)
        if spec is None:
            errors.append(f"неизвестный ключ «{key}» (только ns, ag, vol)")
            continue
        try:
            parsed = spec["cast"](raw)
        except ValueError:
            errors.append(f"{key}: нужно число, получено {raw}")
            continue
        if not spec["min"] <= parsed <= spec["max"]:
            errors.append(
                f"{key}: диапазон {spec['min']:g}–{spec['max']:g}, получено {raw}"
            )
            continue
        calls.append((spec["service"], parsed))
    if errors:
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError("; ".join(errors))
    return calls


class SatelliteMicNumber(NumberEntity):
    """One tunable mic coefficient of a satellite."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, session: SatSession, spec: dict) -> None:
        self._session = session
        self._spec = spec
        self._remove_track = None
        self._attr_unique_id = f"satellite_audio_{session.mac}_{spec['short']}"
        self._attr_name = f"{session.name} {spec['name']}"
        self._attr_icon = spec["icon"]
        self._attr_native_min_value = spec["min"]
        self._attr_native_max_value = spec["max"]
        self._attr_native_step = spec["step"]
        self._attr_native_unit_of_measurement = spec["unit"]
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, session.mac)}
        )

    @property
    def available(self) -> bool:
        return self._session.available

    def _assist_attributes(self) -> dict:
        return assist_attributes(self.hass, self._session.entity_id)

    @property
    def native_value(self) -> float | None:
        return self._assist_attributes().get(self._spec["full"])

    @property
    def extra_state_attributes(self) -> dict:
        result = {"comment": self._spec["comment"]}
        firmware = firmware_settings(self.hass, self._session.entity_id)
        if firmware is not None:
            result["firmware"] = firmware
        return result

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

    async def async_set_native_value(self, value: float) -> None:
        value = max(self._spec["min"], min(self._spec["max"], value))
        if self._spec["cast"] is int:
            value = int(round(value))
        await self.hass.services.async_call(
            "assist_satellite",
            self._spec["service"],
            {"entity_id": self._session.entity_id, "value": value},
            blocking=True,
        )
        self.async_write_ha_state()


def build_mic_entities(session: SatSession) -> list[SatelliteMicNumber]:
    return [SatelliteMicNumber(session, spec) for spec in _SPECS]
