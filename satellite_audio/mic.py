from __future__ import annotations

import re

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_track_state_change_event

from . import SatSession

_TOKEN_RE = re.compile(r"^([A-Za-z_0-9]+)=(\S+)$")

VAD_MODES = ("singleton", "per_pipeline")

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
    {
        "full": "speech_threshold",
        "short": "speech",
        "service": "set_speech_threshold",
        "cast": float,
        "min": 0.1,
        "max": 1.0,
        "step": 0.05,
        "unit": None,
        "icon": "mdi:account-voice",
        "name": "VAD порог речи",
        "comment": "Порог вероятности речи Silero VAD ВО ВРЕМЯ команды. Ниже (0.1-0.3) — распознаёт тихую речь, но и шум; выше (0.6-0.8) — нужен чёткий голос.",
    },
    {
        "full": "before_command_speech_threshold",
        "short": "bspeech",
        "service": "set_before_command_speech_threshold",
        "cast": float,
        "min": 0.05,
        "max": 0.9,
        "step": 0.05,
        "unit": None,
        "icon": "mdi:microphone-outline",
        "name": "VAD порог начала речи",
        "comment": "Порог для ОБНАРУЖЕНИЯ НАЧАЛА команды: обычно ниже основного, чтобы уловить начало тихой речи. Слишком низкий — срабатывает на шум.",
    },
    {
        "full": "silence_seconds",
        "short": "silence",
        "service": "set_silence_seconds",
        "cast": float,
        "min": 0.3,
        "max": 10.0,
        "step": 0.1,
        "unit": "s",
        "icon": "mdi:timer-outline",
        "name": "VAD тишина до обрыва с",
        "comment": "Сколько секунд тишины после речи, чтобы остановить запись команды. Меньше — быстрее ответ, больше — паузы в речи не обрывают команду.",
    },
    {
        "full": "command_seconds",
        "short": "cmd",
        "service": "set_command_seconds",
        "cast": float,
        "min": 0.3,
        "max": 10.0,
        "step": 0.1,
        "unit": "s",
        "icon": "mdi:clock-outline",
        "name": "VAD мин. команда с",
        "comment": "Минимальная длительность команды: запись не остановится раньше, даже если наступила тишина. Защита от обрыва коротких фраз.",
    },
    {
        "full": "vad_timeout_seconds",
        "short": "vadto",
        "service": "set_vad_timeout",
        "cast": float,
        "min": 1.0,
        "max": 60.0,
        "step": 1,
        "unit": "s",
        "icon": "mdi:file-clock-outline",
        "name": "VAD макс. запись с",
        "comment": "Максимальная длительность одной голосовой команды. Для коротких команд хватит 15, для диктовок — 30-60.",
    },
    {
        "full": "before_command_timeout_seconds",
        "short": "bct",
        "service": "set_before_command_timeout",
        "cast": float,
        "min": 1.0,
        "max": 30.0,
        "step": 0.5,
        "unit": "s",
        "icon": "mdi:hourglass-outline",
        "name": "VAD ожидание речи с",
        "comment": "Максимальное ожидание начала речи после активации (wake word). Если речи нет — сброс, ассистент не висит зря.",
    },
    {
        "full": "trigger_timeout_seconds",
        "short": "trigto",
        "service": "set_trigger_timeout",
        "cast": float,
        "min": 1.0,
        "max": 30.0,
        "step": 0.5,
        "unit": "s",
        "icon": "mdi:timer",
        "name": "VAD таймаут триггера с",
        "comment": "После начала речи: сколько слушать команду при паузах, затем принудительное распознавание и проверка триггера.",
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
        if key in _ALIASES or key == "vad_mode"
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
        if key == "vadmode":
            if raw not in VAD_MODES:
                errors.append(f"vadmode: {' или '.join(VAD_MODES)}, получено {raw}")
                continue
            calls.append(("set_vad_mode", raw))
            continue
        spec = next((s for s in _SPECS if s["short"] == key), None)
        if spec is None:
            errors.append(f"неизвестный ключ «{key}»")
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


class SatelliteAudioMicEntityMixin:
    def _assist_attributes(self) -> dict:
        return assist_attributes(self.hass, self._session.entity_id)

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

    @property
    def _base_attributes(self) -> dict:
        result = {"comment": self._spec["comment"]}
        firmware = firmware_settings(self.hass, self._session.entity_id)
        if firmware is not None:
            result["firmware"] = firmware
        return result


class SatelliteMicNumber(SatelliteAudioMicEntityMixin, NumberEntity):
    """One tunable coefficient of a satellite."""

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

    @property
    def native_value(self) -> float | None:
        return self._assist_attributes().get(self._spec["full"])

    @property
    def extra_state_attributes(self) -> dict:
        return self._base_attributes

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


class SatelliteVadModeSelect(SatelliteAudioMicEntityMixin, SelectEntity):
    """VAD mode selector of a satellite."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cog-outline"
    _attr_options = list(VAD_MODES)

    def __init__(self, session: SatSession) -> None:
        self._session = session
        self._remove_track = None
        self._spec = {
            "comment": "Режим Silero VAD: singleton — одна общая модель (экономит память), per_pipeline — своя копия у каждого pipeline (~10 МБ)."
        }
        self._attr_unique_id = f"satellite_audio_{session.mac}_vadmode"
        self._attr_name = f"{session.name} VAD режим"
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, session.mac)}
        )

    @property
    def available(self) -> bool:
        return self._session.available

    @property
    def current_option(self) -> str | None:
        value = self._assist_attributes().get("vad_mode")
        if value in VAD_MODES:
            return value
        return None

    @property
    def extra_state_attributes(self) -> dict:
        return self._base_attributes

    async def async_select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            "assist_satellite",
            "set_vad_mode",
            {"entity_id": self._session.entity_id, "value": option},
            blocking=True,
        )
        self.async_write_ha_state()


def build_mic_entities(session: SatSession) -> list:
    entities = [SatelliteMicNumber(session, spec) for spec in _SPECS]
    entities.append(SatelliteVadModeSelect(session))
    return entities
