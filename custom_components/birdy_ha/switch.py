"""HA switch entities — 4 boolean inverter controls.

Loaded only when FEATURE_CONTROL_ENTITIES is True (gate evaluated in
__init__._platforms()).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONTROL_ENTITY_KEYS,
    CONTROL_ENTITY_NAMES,
    CONTROL_KEY_BOOLEAN,
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
        if spec["type"] != CONTROL_KEY_BOOLEAN:
            continue
        entities.append(
            BirdySwitch(coord, master, key, spec, device_info, entry.entry_id)
        )
    async_add_entities(entities)


def _device_info(master) -> dict[str, Any]:
    serial = master.binding.inverter.inverter_serial if master.binding.inverter else ""
    return {
        "identifiers": {(DOMAIN, serial or master.binding.integration_id)},
        "name": "Birdy",
        "manufacturer": MANUFACTURER,
    }


class BirdySwitch(CoordinatorEntity, SwitchEntity):
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
        self._attr_unique_id = f"{entry_id}_switch_{key}"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> Optional[bool]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return None
        return bool(snapshot.values.get(self._key))

    @property
    def available(self) -> bool:
        # Available iff we have a current reading. Writes are gated
        # separately in async_turn_on/off so monitors can still
        # display the current setting even though they can't change
        # it (apply_setting raises HomeAssistantError on monitor).
        if not super().available:
            return False
        snapshot = self.coordinator.data
        if snapshot is None or self._key not in snapshot.values:
            return False
        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._raise_if_readonly()
        await self._master.settings.apply_setting(self._key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._raise_if_readonly()
        await self._master.settings.apply_setting(self._key, False)

    def _raise_if_readonly(self) -> None:
        if not self._master.binding.can_control:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(
                "This Home Assistant is bound as a monitor for the "
                "tenant; only the master can write to the inverter. "
                "Promote this HA to master from the admin panel to "
                "enable controls."
            )
