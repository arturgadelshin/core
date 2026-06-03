"""Number entities for Wyoming integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.components.number import NumberEntityDescription, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import WyomingSatelliteEntity

if TYPE_CHECKING:
    from .models import DomainDataItem

_MAX_AUTO_GAIN: Final = 31
_MIN_VOLUME_MULTIPLIER: Final = 0.1
_MAX_VOLUME_MULTIPLIER: Final = 10.0
_MIN_SPEECH_THRESHOLD: Final = 0.1
_MAX_SPEECH_THRESHOLD: Final = 1.0
_MIN_VAD_TIMEOUT: Final = 1.0
_MAX_VAD_TIMEOUT: Final = 60.0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Wyoming number entities."""
    item: DomainDataItem = hass.data[DOMAIN][config_entry.entry_id]

    # Setup is only forwarded for satellites
    assert item.device is not None

    async_add_entities(
        [
            WyomingSatelliteAutoGainNumber(item.device),
            WyomingSatelliteVolumeMultiplierNumber(item.device),
            WyomingSatelliteSpeechThresholdNumber(item.device),
            WyomingSatelliteVadTimeoutNumber(item.device),
        ]
    )


class WyomingSatelliteAutoGainNumber(WyomingSatelliteEntity, RestoreNumber):
    """Entity to represent auto gain amount."""

    entity_description = NumberEntityDescription(
        key="auto_gain",
        translation_key="auto_gain",
        entity_category=EntityCategory.CONFIG,
    )
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = _MAX_AUTO_GAIN
    _attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()

        state = await self.async_get_last_state()
        if state is not None:
            await self.async_set_native_value(float(state.state))

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        auto_gain = int(max(0, min(_MAX_AUTO_GAIN, value)))
        self._attr_native_value = auto_gain
        self.async_write_ha_state()
        self._device.set_auto_gain(auto_gain)


class WyomingSatelliteVolumeMultiplierNumber(WyomingSatelliteEntity, RestoreNumber):
    """Entity to represent microphone volume multiplier."""

    entity_description = NumberEntityDescription(
        key="volume_multiplier",
        translation_key="volume_multiplier",
        entity_category=EntityCategory.CONFIG,
    )
    _attr_should_poll = False
    _attr_native_min_value = _MIN_VOLUME_MULTIPLIER
    _attr_native_max_value = _MAX_VOLUME_MULTIPLIER
    _attr_native_step = 0.1
    _attr_native_value = 1.0

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        last_number_data = await self.async_get_last_number_data()
        if (last_number_data is not None) and (
            last_number_data.native_value is not None
        ):
            await self.async_set_native_value(last_number_data.native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        self._attr_native_value = float(
            max(_MIN_VOLUME_MULTIPLIER, min(_MAX_VOLUME_MULTIPLIER, value))
        )
        self.async_write_ha_state()
        self._device.set_volume_multiplier(self._attr_native_value)


class WyomingSatelliteSpeechThresholdNumber(WyomingSatelliteEntity, RestoreNumber):
    """Entity to represent Silero VAD speech threshold."""

    entity_description = NumberEntityDescription(
        key="speech_threshold",
        translation_key="speech_threshold",
        entity_category=EntityCategory.CONFIG,
    )
    _attr_should_poll = False
    _attr_native_min_value = _MIN_SPEECH_THRESHOLD
    _attr_native_max_value = _MAX_SPEECH_THRESHOLD
    _attr_native_step = 0.05
    _attr_native_value = 0.5

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()

        last_number_data = await self.async_get_last_number_data()
        if (last_number_data is not None) and (
            last_number_data.native_value is not None
        ):
            await self.async_set_native_value(last_number_data.native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        self._attr_native_value = float(
            max(_MIN_SPEECH_THRESHOLD, min(_MAX_SPEECH_THRESHOLD, value))
        )
        self.async_write_ha_state()
        self._device.set_speech_threshold(self._attr_native_value)


class WyomingSatelliteVadTimeoutNumber(WyomingSatelliteEntity, RestoreNumber):
    """Entity to represent VAD timeout in seconds."""

    entity_description = NumberEntityDescription(
        key="vad_timeout_seconds",
        translation_key="vad_timeout_seconds",
        entity_category=EntityCategory.CONFIG,
    )
    _attr_should_poll = False
    _attr_native_min_value = _MIN_VAD_TIMEOUT
    _attr_native_max_value = _MAX_VAD_TIMEOUT
    _attr_native_step = 1.0
    _attr_native_value = 15.0

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()

        last_number_data = await self.async_get_last_number_data()
        if (last_number_data is not None) and (
            last_number_data.native_value is not None
        ):
            await self.async_set_native_value(last_number_data.native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        self._attr_native_value = float(
            max(_MIN_VAD_TIMEOUT, min(_MAX_VAD_TIMEOUT, value))
        )
        self.async_write_ha_state()
        self._device.set_vad_timeout_seconds(self._attr_native_value)
