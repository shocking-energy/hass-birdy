"""HA sensor entities — 13 read sensors + 4 diagnostic sensors.

See docs/architecture/ha-master-integration.md § hass_adapter for the
full entity table. Catalogue is driven by READ_SENSOR_KEYS in const.py
so adding/removing a metric is a one-line change.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CHARGE_RATE_W_PER_RAW,
    DOMAIN,
    ENERGY_DASHBOARD_KEYS,
    MANUFACTURER,
    READ_SENSOR_KEYS,
)
from .coordinator import BirdyCoordinator

_LOGGER = logging.getLogger(__name__)


_DEVICE_CLASS_MAP = {
    "power": SensorDeviceClass.POWER,
    "energy": SensorDeviceClass.ENERGY,
    "battery": SensorDeviceClass.BATTERY,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coord: BirdyCoordinator = data["telemetry_coordinator"]
    master = data["master"]
    device_info = _build_device_info(master)

    entities: list[SensorEntity] = []
    for spec in READ_SENSOR_KEYS:
        entities.append(
            BirdyReadingSensor(coord, spec, device_info, entry.entry_id)
        )

    # Diagnostic sensors.
    entities.extend([
        DiagnosticRoleSensor(master, device_info, entry.entry_id),
        DiagnosticLastPublishedSensor(master, device_info, entry.entry_id),
        DiagnosticIntegrationVersionSensor(master, device_info, entry.entry_id),
        DiagnosticTenantIdSensor(master, device_info, entry.entry_id),
        BatteryShapeSensor(
            master, device_info, entry.entry_id,
            key="health", name="Battery health", unit="%", icon="mdi:battery-heart",
        ),
        BatteryShapeSensor(
            master, device_info, entry.entry_id,
            key="totalKwh", name="Battery total capacity", unit="kWh",
            icon="mdi:battery-high", device_class=SensorDeviceClass.ENERGY_STORAGE,
        ),
        BatteryShapeSensor(
            master, device_info, entry.entry_id,
            key="capacityFull", name="Battery capacity (calibrated)",
            unit="Ah", icon="mdi:battery",
        ),
        BatteryShapeSensor(
            master, device_info, entry.entry_id,
            key="capacityDesign", name="Battery capacity (design)",
            unit="Ah", icon="mdi:battery-outline",
        ),
        BatteryShapeSensor(
            master, device_info, entry.entry_id,
            key="voltage", name="Battery voltage", unit="V",
            icon="mdi:flash-triangle", device_class=SensorDeviceClass.VOLTAGE,
        ),
        BatteryShapeSensor(
            master, device_info, entry.entry_id,
            key="temp", name="Battery temperature", unit="°C",
            icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE,
        ),
        ForecastSensor(
            master, device_info, entry.entry_id,
            key="solar_today_kwh",
            name="Solar forecast today",
            icon="mdi:weather-sunny",
        ),
        ForecastSensor(
            master, device_info, entry.entry_id,
            key="house_daily_kwh",
            name="House expected daily",
            icon="mdi:home-lightning-bolt-outline",
        ),
    ])

    # Calculated-watts readouts for the amp-based rate controls, so the
    # watts are visible on the dashboard (the number controls are in amps).
    settings_coord = data["settings_coordinator"]
    entities.extend([
        BirdyRateWattsSensor(settings_coord, "chargeRate", "AC charge rate (W)",
                             "mdi:battery-arrow-up", device_info, entry.entry_id),
        BirdyRateWattsSensor(settings_coord, "dischargeRate", "AC discharge rate (W)",
                             "mdi:battery-arrow-down", device_info, entry.entry_id),
    ])

    async_add_entities(entities)


def _build_device_info(master) -> dict[str, Any]:
    serial = ""
    if master.binding.inverter:
        serial = master.binding.inverter.inverter_serial
    return {
        "identifiers": {(DOMAIN, serial or master.binding.integration_id)},
        "name": "Birdy",
        "manufacturer": MANUFACTURER,
        "model": "GivEnergy hybrid inverter",
        "serial_number": serial or None,
        "configuration_url": "https://app.shocking.energy",
    }


class BirdyReadingSensor(CoordinatorEntity, SensorEntity):
    """One sensor entity per canonical reading."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BirdyCoordinator,
        spec: dict[str, Any],
        device_info: dict[str, Any],
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._key = f"{spec['component']}.{spec['metric']}"
        self._attr_name = spec["name"]
        self._attr_native_unit_of_measurement = spec["unit"]
        self._attr_icon = spec.get("icon")
        self._attr_unique_id = f"{entry_id}_{self._key.replace('.', '_')}"
        self._attr_device_info = device_info
        dc = _DEVICE_CLASS_MAP.get(spec["device_class"])
        if dc:
            self._attr_device_class = dc
        # Energy-dashboard-eligible metrics: total_increasing.
        # Others: measurement.
        key_short = spec["component"].replace(".", "_") + "_" + spec["metric"]
        if any(k in key_short for k in ENERGY_DASHBOARD_KEYS):
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        data = self.coordinator.data or {}
        entry = data.get(self._key)
        if not entry:
            return None
        return entry["value"]

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        data = self.coordinator.data or {}
        return self._key in data


class BirdyRateWattsSensor(CoordinatorEntity, SensorEntity):
    """Calculated watts for an amp-based rate control (amps x ~52 W/A),
    driven by the settings coordinator so the watts show on the dashboard
    next to the amps number control."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, key, name, icon, device_info, entry_id):
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_{key}_watts"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> Optional[float]:
        snap = self.coordinator.data
        if snap is None:
            return None
        v = snap.values.get(self._key)
        if v is None:
            return None
        try:
            return round(float(v) * CHARGE_RATE_W_PER_RAW)
        except (TypeError, ValueError):
            return None


# ─── Diagnostic sensors ──────────────────────────────────────────────


class _DiagnosticBase(SensorEntity):
    """Diagnostic sensors derive from HaMaster state directly — not
    from a coordinator — because they update on a different cadence.
    HA will poll them at its default interval (~30 s) which is fine
    for slowly-changing diagnostics.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, master, device_info: dict[str, Any], entry_id: str) -> None:
        self._master = master
        self._attr_device_info = device_info
        self._entry_id = entry_id


# Master-state diagnostics get the Birdy entity_picture so the device
# card carries the brand mark next to the integration's identity rows.
# Battery + forecast diagnostics deliberately don't — they have
# semantic mdi icons (battery-heart, weather-sunny, etc.) and replacing
# those with the same Birdy face on every row would hide the meaning.
_BIRDY_ICON_URL = "/birdy_ha/icon.png"


class DiagnosticRoleSensor(_DiagnosticBase):
    _attr_name = "Role"
    _attr_icon = "mdi:account-key"
    _attr_entity_picture = _BIRDY_ICON_URL

    def __init__(self, master, device_info, entry_id):
        super().__init__(master, device_info, entry_id)
        self._attr_unique_id = f"{entry_id}_role"

    @property
    def native_value(self) -> Optional[str]:
        # Local installs have no server-side role; the runtime
        # synthesises master internally but "local" is what the user
        # should see — there's no tenant/master/monitor concept here.
        if getattr(self._master, "_local_only", False):
            return "local"
        role = self._master.binding.role
        return role.value if role else None


class DiagnosticLastPublishedSensor(_DiagnosticBase):
    _attr_name = "Last published"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:upload"
    _attr_entity_picture = _BIRDY_ICON_URL

    def __init__(self, master, device_info, entry_id):
        super().__init__(master, device_info, entry_id)
        self._attr_unique_id = f"{entry_id}_last_published"

    @property
    def native_value(self) -> Optional[datetime]:
        return self._master.last_publish_ok_at


class DiagnosticIntegrationVersionSensor(_DiagnosticBase):
    _attr_name = "Integration version"
    _attr_icon = "mdi:tag"
    _attr_entity_picture = _BIRDY_ICON_URL

    def __init__(self, master, device_info, entry_id):
        super().__init__(master, device_info, entry_id)
        self._attr_unique_id = f"{entry_id}_integration_version"

    @property
    def native_value(self) -> str:
        return self._master._integration_version()


class DiagnosticTenantIdSensor(_DiagnosticBase):
    # User-facing label uses customer-friendly framing ("account"); the
    # underlying class + unique_id + entity_id stay `tenant_id` so the
    # entity registry row doesn't churn (renaming unique_id orphans
    # existing recorder history). README + UI consistently say
    # "Account ID"; only the internal Python name reveals the tenant
    # vocabulary used in the backend (pi_publisher_bindings.tenant_id).
    _attr_name = "Account ID"
    _attr_icon = "mdi:identifier"
    _attr_entity_picture = _BIRDY_ICON_URL

    def __init__(self, master, device_info, entry_id):
        super().__init__(master, device_info, entry_id)
        self._attr_unique_id = f"{entry_id}_tenant_id"

    @property
    def native_value(self) -> Optional[str]:
        # Local mode has no tenant. Show a label that explains why
        # rather than just "unknown" — saves a forum thread.
        if getattr(self._master, "_local_only", False):
            return "local install"
        return self._master.binding.tenant_id


class BatteryShapeSensor(_DiagnosticBase):
    """Diagnostic sensor sourced from `latest_shape.payload["battery"][key]`.

    The Modbus refresh cycle populates the SYSTEM shape's battery
    block with health/capacity/voltage/temp every 15 s, but these
    fields aren't part of the canonical readings stream (no value in
    publishing them as time-series — they change slowly). Reading
    them off the master directly avoids growing device_telemetry by
    ~6 metrics × 5760 rows/day/tenant.
    """

    def __init__(
        self,
        master,
        device_info: dict[str, Any],
        entry_id: str,
        *,
        key: str,
        name: str,
        unit: str,
        icon: str,
        device_class: Optional[SensorDeviceClass] = None,
    ) -> None:
        super().__init__(master, device_info, entry_id)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_battery_{key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = SensorStateClass.MEASUREMENT
        if device_class is not None:
            self._attr_device_class = device_class

    @property
    def native_value(self) -> Optional[float]:
        shape = self._master.latest_shape
        if shape is None:
            return None
        battery = shape.payload.get("battery") if isinstance(shape.payload, dict) else None
        if not isinstance(battery, dict):
            return None
        value = battery.get(self._key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return self.native_value is not None


class ForecastSensor(_DiagnosticBase):
    """Today-forecast sensor sourced from `master.forecast[key]`.

    Two of these: `solar_today_kwh` (cloud-computed Open-Meteo +
    learning-factor forecast for today's solar generation) and
    `house_daily_kwh` (the customer's stated daily consumption target
    from `tenants.meta.planner.daily_consumption_kwh`).

    Both values are returned by `publish_lan_snapshot` on every
    master poll (migration 045) — the runtime captures them and
    exposes via `master.forecast`. Returns None until the first
    successful publish.
    """

    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        master,
        device_info: dict[str, Any],
        entry_id: str,
        *,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(master, device_info, entry_id)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_forecast_{key}"
        self._attr_icon = icon

    @property
    def native_value(self) -> Optional[float]:
        return self._master.forecast.get(self._key)

    @property
    def available(self) -> bool:
        return self.native_value is not None
