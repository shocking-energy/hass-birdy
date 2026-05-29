"""HA time entities — 6 time-of-day inverter controls.

Charge/discharge slot start/end times.
Loaded only when FEATURE_CONTROL_ENTITIES is True.
"""

from __future__ import annotations

import logging
from datetime import time as time_type
from typing import Any, Optional

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONTROL_ENTITY_KEYS,
    CONTROL_ENTITY_NAMES,
    CONTROL_KEY_TIME,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import BirdySettingsCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    master = data["master"]
    coord: BirdySettingsCoordinator = data["settings_coordinator"]
    device_info = _device_info(master)

    entities = []
    for key, spec in CONTROL_ENTITY_KEYS.items():
        if spec["type"] != CONTROL_KEY_TIME:
            continue
        entities.append(
            BirdyTime(coord, master, key, spec, device_info, entry.entry_id)
        )
    async_add_entities(entities)


def _device_info(master) -> dict[str, Any]:
    serial = master.binding.inverter.inverter_serial if master.binding.inverter else ""
    return {
        "identifiers": {(DOMAIN, serial or master.binding.integration_id)},
        "name": "Birdy",
        "manufacturer": MANUFACTURER,
    }


class BirdyTime(CoordinatorEntity, TimeEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BirdySettingsCoordinator,
        master,
        key: str,
        spec: dict[str, Any],
        device_info: dict[str, Any],
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._master = master
        self._key = key
        self._attr_name = CONTROL_ENTITY_NAMES[key]
        self._attr_icon = spec.get("icon")
        self._attr_unique_id = f"{entry_id}_time_{key}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> Optional[time_type]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return None
        value = snapshot.values.get(self._key)
        if value is None or not isinstance(value, time_type):
            return None
        return value

    @property
    def available(self) -> bool:
        # Read-availability is decoupled from write-availability so
        # monitors can display values; writes are gated below.
        if not super().available:
            return False
        snapshot = self.coordinator.data
        if snapshot is None or self._key not in snapshot.values:
            return False
        return True

    async def async_set_value(self, value: time_type) -> None:
        if not self._master.binding.can_control:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(
                "This Home Assistant is bound as a monitor; only the "
                "tenant master can write to the inverter."
            )
        await self._master.settings.apply_setting(self._key, value)
