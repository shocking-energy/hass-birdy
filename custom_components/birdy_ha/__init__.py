"""Birdy — Home Assistant master integration.

Acts as a tenant master for Birdy: polls the customer's
GivEnergy inverter via Modbus-YY at ~15 s cadence, publishes
canonical telemetry + the SYSTEM snapshot to Supabase, and exposes
read sensors + (optionally) 16 inverter control entities to HA.

Design doc: docs/architecture/ha-master-integration.md in the
energy-monitor repo. Server-side contract lives in supabase/
migrations 020/030/035/041/042/044/045/046/047/064.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import _lib_patches
from .const import (
    DOMAIN,
    FEATURE_CONTROL_ENTITIES,
    CONTROL_ENTITY_OVERRIDE_ENV,
)
from .coordinator import (
    BirdyCoordinator,
    BirdySettingsCoordinator,
)
from .runtime import HaMaster

_LOGGER = logging.getLogger(__name__)

# Apply upstream library patches before any HaMaster instance polls.
_lib_patches.apply_all()


def _controls_enabled() -> bool:
    """Resolve the FEATURE_CONTROL_ENTITIES flag with env override.

    Default is True per the 2026-05-28 scoping decision; users who
    hit a Modbus-write failure in the field can disable controls
    without a rerelease by setting:
        BIRDY_HA_CONTROLS_ENABLED=false
    on the HA host's environment.
    """
    import os
    raw = os.environ.get(CONTROL_ENTITY_OVERRIDE_ENV, "").strip().lower()
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return FEATURE_CONTROL_ENTITIES


def _platforms() -> list[Platform]:
    """Which HA platforms this integration exposes.

    Always includes sensor + binary_sensor (read-only telemetry +
    diagnostics). Adds switch/number/time only when controls are
    enabled — the feature flag gates the entire control surface.
    """
    platforms = [Platform.SENSOR, Platform.BINARY_SENSOR]
    if _controls_enabled():
        platforms.extend([Platform.SWITCH, Platform.NUMBER, Platform.TIME])
    return platforms


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Birdy from a config entry."""
    _LOGGER.info(
        "Setting up Birdy master (controls=%s)",
        _controls_enabled(),
    )

    # Serve the package's icon at /birdy_ha/icon.png so the entity
    # platforms can reference it as `entity_picture` and have HA
    # render the hand-drawn Birdy face on the device card. The brands
    # repo (brands.home-assistant.io) is the proper home for the
    # Add-Integration list icon — that needs a separate PR — but
    # this in-package serve covers the device + entity surface that
    # users actually look at day-to-day.
    await _register_icon_static_path(hass)

    master = HaMaster(hass, entry)
    try:
        await master.async_start()
    except Exception as exc:  # pragma: no cover - logged + re-raised for HA
        _LOGGER.error("HaMaster startup failed: %s", exc, exc_info=True)
        raise

    telemetry_coordinator = BirdyCoordinator(hass, master)
    await telemetry_coordinator.async_config_entry_first_refresh()

    settings_coordinator = BirdySettingsCoordinator(hass, master)
    # Settings load asynchronously after entity setup so we don't block
    # async_setup_entry on a 2-3 s Modbus sweep.
    asyncio.create_task(settings_coordinator.async_config_entry_first_refresh())

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "master": master,
        "telemetry_coordinator": telemetry_coordinator,
        "settings_coordinator": settings_coordinator,
        "controls_enabled": _controls_enabled(),
    }

    _migrate_entity_names(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, _platforms())
    return True


def _migrate_entity_names(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push the latest `CONTROL_ENTITY_NAMES` into HA's entity registry.

    HA stores `original_name` at first registration and doesn't update
    it on subsequent reloads — so renaming a const between versions
    leaves existing installs stuck with the old name in their UI. Walk
    the registry once on startup and update any entity whose stored
    name is out of date.
    """
    from homeassistant.helpers import entity_registry as er
    from .const import CONTROL_ENTITY_NAMES

    ent_reg = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for entity in list(ent_reg.entities.values()):
        if entity.config_entry_id != entry.entry_id:
            continue
        if not entity.unique_id or not entity.unique_id.startswith(prefix):
            continue
        # unique_id is "<entry_id>_<platform>_<key>"
        tail = entity.unique_id[len(prefix):]
        try:
            _platform, key = tail.split("_", 1)
        except ValueError:
            continue
        new_name = CONTROL_ENTITY_NAMES.get(key)
        if not new_name:
            continue
        if entity.original_name == new_name and (entity.name in (None, new_name)):
            continue
        ent_reg.async_update_entity(
            entity.entity_id,
            original_name=new_name,
            name=new_name if entity.name == entity.original_name else entity.name,
        )
        _LOGGER.info(
            "migrated entity name: %s = %r → %r",
            entity.entity_id, entity.original_name, new_name,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, _platforms()
    )
    if unloaded:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data:
            await data["master"].async_stop()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry,
) -> bool:
    """Allow the user to remove stale device registry entries.

    HA only shows the ⋮ → Delete option when the integration opts in
    via this hook. Stale devices accumulate when the device's
    identifier changes (e.g. the 0.8.1 fix that swapped from
    integration_id-keyed to serial_number-keyed identifiers left
    an orphan behind). Anything that points to this config entry
    is fair game — the active device's identifier will keep
    repopulating via the runtime's _device_info on each setup, so
    a wrongful delete is self-healing.
    """
    return True


_ICON_STATIC_PATH = "/birdy_ha/icon.png"
_ICON_REGISTERED = False


async def _register_icon_static_path(hass: HomeAssistant) -> None:
    """Mount the package-bundled icon at /birdy_ha/icon.png.

    Done once per HA process. The path is hardcoded so all entities
    + the device card can point at it deterministically. Cache headers
    are on because the icon never changes between releases except
    when we bump the integration version (HACS clears the cache then).
    """
    global _ICON_REGISTERED
    if _ICON_REGISTERED:
        return
    import os
    icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
    if not os.path.exists(icon_path):
        _LOGGER.warning("icon.png missing at %s — device card falls back to default", icon_path)
        return
    try:
        from homeassistant.components.http import StaticPathConfig
        await hass.http.async_register_static_paths([
            StaticPathConfig(_ICON_STATIC_PATH, icon_path, True),
        ])
    except ImportError:  # pragma: no cover - older HA core
        # 2023.11 fallback — sync register_static_path was the only API.
        hass.http.register_static_path(_ICON_STATIC_PATH, icon_path, True)
    _ICON_REGISTERED = True
    _LOGGER.debug("registered Birdy icon at %s", _ICON_STATIC_PATH)
