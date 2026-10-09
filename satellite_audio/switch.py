from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from . import LISTEN, RECORD, SatSession

_KIND_NAME = {LISTEN: "Прослушивание", RECORD: "Запись"}
_KIND_ICON = {LISTEN: "mdi:ear", RECORD: "mdi:record-rec"}


class SatelliteAudioSwitch(SwitchEntity):
    """Live listen or recording switch attached to the satellite device."""

    _attr_should_poll = False

    def __init__(self, session: SatSession, kind: str) -> None:
        self._session = session
        self._kind = kind
        self._attr_unique_id = f"satellite_audio_{session.mac}_{kind}"
        self._attr_name = f"{session.name} {_KIND_NAME[kind]}"
        self._attr_icon = _KIND_ICON[kind]
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, session.mac)}
        )

    @property
    def available(self) -> bool:
        return self._session.available

    @property
    def is_on(self) -> bool:
        if self._kind == LISTEN:
            return self._session.listening
        return self._session.recording

    @property
    def extra_state_attributes(self) -> dict:
        if self._kind == LISTEN:
            if self._session.listening and self._session.live_token:
                return {
                    "live_url": f"/api/satellite_audio/live/{self._session.live_token}"
                }
            return {}
        attributes = {}
        if self._session.recording and self._session.writer is not None:
            attributes["recording_file"] = str(self._session.writer.path)
        if self._session.last_recording_path is not None:
            attributes["last_recording_file"] = str(self._session.last_recording_path)
            attributes["last_recording_seconds"] = round(
                self._session.last_recording_seconds, 1
            )
        return attributes

    async def async_turn_on(self, **kwargs) -> None:
        if self._kind == LISTEN:
            await self._session.set_listen(True)
        else:
            await self._session.set_record(True)

    async def async_turn_off(self, **kwargs) -> None:
        if self._kind == LISTEN:
            await self._session.set_listen(False)
        else:
            await self._session.set_record(False)
