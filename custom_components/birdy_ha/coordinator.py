"""HA DataUpdateCoordinator subclasses.

Two coordinators — telemetry (15 s) and settings (configurable, default
300 s). Both are CONSUMERS of HaMaster's caches, not drivers of polling:
HaMaster owns the poll loops and pushes updates to the coordinators via
async_set_updated_data when fresh data arrives.

See ha-master-integration.md § hass_adapter for the design rationale.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    SETTINGS_POLL_INTERVAL_S,
    TELEMETRY_POLL_INTERVAL_S,
)
from .domain import SettingsSnapshot

if TYPE_CHECKING:
    from .runtime import HaMaster

_LOGGER = logging.getLogger(__name__)


class BirdyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Telemetry coordinator — consumes HaMaster.latest_readings.

    update_interval is set so HA will still trigger periodic updates
    if the master's poll loop ever stops pushing; under normal
    operation the master pushes via async_set_updated_data and the
    coordinator's own loop is a backup.
    """

    def __init__(self, hass: HomeAssistant, master: "HaMaster") -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_telemetry",
            update_interval=timedelta(seconds=TELEMETRY_POLL_INTERVAL_S * 2),
        )
        self._master = master
        master.register_reading_listener(self._on_master_readings)

    async def _async_update_data(self) -> dict[str, Any]:
        readings = self._master.latest_readings
        return _readings_to_dict(readings)

    def _on_master_readings(self) -> None:
        """Called by HaMaster after every successful poll cycle."""
        readings = self._master.latest_readings
        self.async_set_updated_data(_readings_to_dict(readings))


class BirdySettingsCoordinator(
    DataUpdateCoordinator[Optional[SettingsSnapshot]]
):
    """Settings coordinator — consumes HaMaster.settings.snapshot."""

    def __init__(self, hass: HomeAssistant, master: "HaMaster") -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_settings",
            update_interval=timedelta(seconds=SETTINGS_POLL_INTERVAL_S),
        )
        self._master = master
        master.register_settings_listener(self._on_master_settings)

    async def _async_update_data(self) -> Optional[SettingsSnapshot]:
        snapshot = await self._master.settings.read_settings()
        return snapshot

    def _on_master_settings(self) -> None:
        self.async_set_updated_data(self._master.settings.snapshot)


def _readings_to_dict(readings) -> dict[str, Any]:
    """Flatten canonical readings to a {(component, metric): value}
    dict for fast entity lookup.
    """
    out: dict[str, Any] = {}
    for r in readings:
        out[f"{r.component}.{r.metric}"] = {
            "value": r.value,
            "unit": r.unit,
            "recorded_at": r.recorded_at,
        }
    return out
