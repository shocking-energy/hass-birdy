"""HA number entities — 6 numeric inverter controls.

Loaded only when FEATURE_CONTROL_ENTITIES is True.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CHARGE_RATE_W_PER_RAW,
    CONTROL_ENTITY_KEYS,
    CONTROL_ENTITY_NAMES,
    CONTROL_KEY_NUMBER,
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
        if spec["type"] != CONTROL_KEY_NUMBER:
            continue
        entities.append(
            BirdyNumber(coord, master, key, spec, device_info, entry.entry_id)
        )
    async_add_entities(entities)


def _device_info(master) -> dict[str, Any]:
    serial = master.binding.inverter.inverter_serial if master.binding.inverter else ""
    return {
        "identifiers": {(DOMAIN, serial or master.binding.integration_id)},
        "name": "Birdy",
        "manufacturer": MANUFACTURER,
    }


class BirdyNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

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
        self._attr_unique_id = f"{entry_id}_number_{key}"
        self._attr_device_info = device_info
        self._attr_native_min_value = spec["min"]
        self._attr_native_max_value = spec["max"]
        self._attr_native_step = spec["step"]
        self._attr_native_unit_of_measurement = spec["unit"]
        self._desc = spec.get("desc")

    @property
    def native_value(self) -> Optional[float]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return None
        value = snapshot.values.get(self._key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> Optional[dict[str, Any]]:
        attrs: dict[str, Any] = {}
        # A per-control human description (shown in the entity's more-info
        # dialog), for controls that carry one — e.g. the export limit.
        if self._desc:
            attrs["description"] = self._desc
        # For the amp-based rate controls, surface the calculated watts
        # (amps x ~52 W/A nominal) alongside the native current value.
        if self._attr_native_unit_of_measurement == "A":
            v = self.native_value
            if v is not None:
                attrs["calculated_power_w"] = round(v * CHARGE_RATE_W_PER_RAW)
        return attrs or None

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

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("set %s = %s", self._key, value)
        if not self._master.binding.can_control:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(
                "This Home Assistant is bound as a monitor; only the "
                "tenant master can write to the inverter."
            )
        await self._master.settings.apply_setting(self._key, value)
