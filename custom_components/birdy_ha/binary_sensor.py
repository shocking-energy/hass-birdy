"""HA binary_sensor entities — connected / publishing diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER

_STALE_THRESHOLD_S = 60


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    master = data["master"]
    device_info = _build_device_info(master)
    async_add_entities([
        ConnectedBinarySensor(master, device_info, entry.entry_id),
        PublishingBinarySensor(master, device_info, entry.entry_id),
    ])


def _build_device_info(master) -> dict[str, Any]:
    serial = ""
    if master.binding.inverter:
        serial = master.binding.inverter.inverter_serial
    return {
        "identifiers": {(DOMAIN, serial or master.binding.integration_id)},
        "name": "Birdy",
        "manufacturer": MANUFACTURER,
    }


class _DiagnosticBinaryBase(BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, master, device_info, entry_id):
        self._master = master
        self._attr_device_info = device_info


class ConnectedBinarySensor(_DiagnosticBinaryBase):
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, master, device_info, entry_id):
        super().__init__(master, device_info, entry_id)
        self._attr_unique_id = f"{entry_id}_connected"

    @property
    def is_on(self) -> bool:
        last = self._master.last_modbus_ok_at
        if last is None:
            return False
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < _STALE_THRESHOLD_S


class PublishingBinarySensor(_DiagnosticBinaryBase):
    _attr_name = "Publishing"
    _attr_icon = "mdi:upload"

    def __init__(self, master, device_info, entry_id):
        super().__init__(master, device_info, entry_id)
        self._attr_unique_id = f"{entry_id}_publishing"

    @property
    def is_on(self) -> bool:
        if not self._master.binding.can_publish:
            return False
        last = self._master.last_publish_ok_at
        if last is None:
            return False
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < _STALE_THRESHOLD_S
