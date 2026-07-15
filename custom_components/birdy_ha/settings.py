"""Settings adapter — reads + writes the 16 inverter control settings.

Gated by FEATURE_CONTROL_ENTITIES (see const.py + __init__.py). The
read side runs even when the flag is off, populating diagnostic
sensors; only writes (apply_setting) are flag-gated.

Reads sequentially through the Modbus relay's single-shot constraint.
Writes go through the same lock the poll loop uses (transport.lock)
so a write can't interleave with a refresh and corrupt either.

Optimistic write strategy:
  1. Update the in-memory cache so HA entities reflect the new value.
  2. Issue the Modbus write.
  3. After SETTINGS_WRITE_CONFIRM_DELAY_S, re-read just that setting.
  4. If the re-read matches: log INFO.
  5. If it doesn't: revert the cache, log WARN, fire HA notification.

Rate-limited per setting to SETTINGS_WRITE_RATE_LIMIT_PER_MIN.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime, time, timezone
from typing import Any, Optional, TYPE_CHECKING

from .const import (
    CHARGE_RATE_W_PER_RAW,
    CONTROL_KEY_BOOLEAN,
    CONTROL_KEY_NUMBER,
    CONTROL_KEY_TIME,
    CONTROL_ENTITY_KEYS,
    SETTINGS_POLL_INTERVAL_S,
    SETTINGS_WRITE_CONFIRM_DELAY_S,
    SETTINGS_WRITE_RATE_LIMIT_PER_MIN,
)
from .domain import SettingsSnapshot, WriteResult

if TYPE_CHECKING:
    from .runtime import HaMaster

_LOGGER = logging.getLogger(__name__)


class SettingsAdapter:
    """Owns read + write of the 16 control settings.

    Read cache is populated by read_settings() on a slow background
    loop (default 300 s). Writes go via apply_setting() and trigger an
    immediate confirmation re-read at +5 s.
    """

    def __init__(self, master: "HaMaster") -> None:
        self._master = master
        self._cache: dict[str, Any] = {}
        self._cache_asof: Optional[datetime] = None
        self._write_history: dict[str, deque] = defaultdict(deque)
        self._read_lock = asyncio.Lock()

    @property
    def cache(self) -> dict[str, Any]:
        return dict(self._cache)

    @property
    def snapshot(self) -> Optional[SettingsSnapshot]:
        if self._cache_asof is None:
            return None
        return SettingsSnapshot(values=dict(self._cache), asof=self._cache_asof)

    # ─── Read ────────────────────────────────────────────────────────

    async def read_settings(self) -> SettingsSnapshot:
        """Populate the settings cache.

        Master mode: sequential Modbus read of all 16 registers.
        Monitor mode: extract from the master's last live_snapshot
        SYSTEM shape (cached by the telemetry poll path). Both modes
        return a SettingsSnapshot with the same value shape so the
        control entities don't care which path produced it.
        """
        # Avoid the import-cycle: domain module already exports the enum.
        from .domain import PublisherRole

        if self._master.binding.role == PublisherRole.MONITOR:
            return self._read_from_latest_shape()

        async with self._read_lock:
            transport = self._master.transport
            if transport is None:
                return SettingsSnapshot(values={}, asof=datetime.now(timezone.utc))

            try:
                plant = await transport.refresh()
            except (ValueError, KeyError) as exc:
                # A single corrupt Modbus frame can decode an out-of-range
                # enum (e.g. "40634 is not a valid BatteryCalibrationStage")
                # and blow up the entire plant refresh. The poll path already
                # tolerates this; do the same here so one bad frame doesn't
                # mark every control entity unavailable. Keep last-good.
                _LOGGER.warning(
                    "read_settings: corrupt frame (%s); keeping last-good", exc)
                if self._cache:
                    return SettingsSnapshot(
                        values=dict(self._cache),
                        asof=self._cache_asof or datetime.now(timezone.utc))
                return SettingsSnapshot(values={}, asof=datetime.now(timezone.utc))
            inv = plant.inverter
            values: dict[str, Any] = {}

            # Boolean states. Attribute names per givenergy-modbus 1.x
            # inverter model.
            values["acCharge"] = _safe_attr(inv, "enable_charge", bool)
            values["acChargeUpperEnabled"] = _safe_attr(
                inv, "enable_charge_target", bool,
            )
            values["dcDischarge"] = _safe_attr(inv, "enable_discharge", bool)
            # ecoMode: the library's `battery_power_mode` register
            # (HR27) is mislabelled "eco mode" — it actually
            # distinguishes "match demand" (1) from "max export" (0).
            # Both set_mode_dynamic AND set_mode_storage call
            # set_discharge_mode_to_match_demand (= r27=1), so r27
            # can't tell them apart. The real eco signal is
            # `enable_discharge` (r59) — False = dynamic (eco on),
            # True = timed discharge / storage (eco off).
            ed = _safe_attr(inv, "enable_discharge", bool)
            values["ecoMode"] = (not ed) if ed is not None else None

            # Numbers.
            values["batteryReserve"] = _safe_attr(
                inv, "battery_discharge_min_power_reserve", int,
            )
            values["batteryCutoff"] = _safe_attr(inv, "battery_soc_reserve", int)
            # Convert the raw 0-100 register tick to Watts (× 52 on
            # GIV-HY5.0). pi-daemon's shape.py emits the same Watts
            # value in live_snapshot.config.chargeRate so monitor +
            # master modes now agree, and the HA number entity shows
            # "Charge rate 2600 W" instead of the misleading "50 %".
            # chargeRate/dischargeRate are exposed in AMPS = the native 0-50
            # register value 1:1 (battery current limit; ~52 W/A nominal).
            # The number entity shows the calculated watts as an attribute.
            values["chargeRate"] = _safe_attr(inv, "battery_charge_limit", int)
            values["dischargeRate"] = _safe_attr(inv, "battery_discharge_limit", int)
            values["acChargeLimit"] = _safe_attr(inv, "charge_target_soc", int)
            # maxOutputPowerPct lives at HR(50) — exposed by the
            # library as `active_power_rate` (uint16, 0-100). Some
            # firmwares return None when curtailment was never set;
            # leave the key out of the snapshot in that case so the
            # entity stays "unknown" rather than reporting a fake 0.
            values["maxOutputPowerPct"] = _safe_attr(inv, "active_power_rate", int)

            # Times — 1.x exposes charge/discharge_slot_N as TimeSlot
            # objects (.start/.end) rather than the 0.x _start/_end
            # attribute pair.
            cs1 = getattr(inv, "charge_slot_1", None)
            if cs1 is not None:
                values["acChargeStart"] = _coerce_time(getattr(cs1, "start", None))
                values["acChargeEnd"] = _coerce_time(getattr(cs1, "end", None))
            ds1 = getattr(inv, "discharge_slot_1", None)
            if ds1 is not None:
                values["dcDischarge1Start"] = _coerce_time(getattr(ds1, "start", None))
                values["dcDischarge1End"] = _coerce_time(getattr(ds1, "end", None))
            ds2 = getattr(inv, "discharge_slot_2", None)
            if ds2 is not None:
                values["dcDischarge2Start"] = _coerce_time(getattr(ds2, "start", None))
                values["dcDischarge2End"] = _coerce_time(getattr(ds2, "end", None))

            # Drop None entries from the snapshot but keep keys in
            # cache so deletes don't churn the entity state.
            filtered = {k: v for k, v in values.items() if v is not None}
            self._cache = {**self._cache, **filtered}
            self._cache_asof = datetime.now(timezone.utc)
            return SettingsSnapshot(values=dict(self._cache), asof=self._cache_asof)

    def _read_from_latest_shape(self) -> SettingsSnapshot:
        """Monitor-mode settings read.

        Pulls the 16 control values out of the SYSTEM shape's `config`
        block — that block is populated by the master's Modbus sweep
        and rides through live_snapshot, so the monitor doesn't need
        to (and shouldn't) touch the dongle. Returns the same
        SettingsSnapshot shape the Modbus path produces, so the
        control entities don't branch.
        """
        shape = self._master.latest_shape
        if shape is None:
            _LOGGER.debug("monitor settings: no shape yet; cache_asof=%s, cache_size=%d", self._cache_asof, len(self._cache))
            return SettingsSnapshot(
                values=dict(self._cache),
                asof=self._cache_asof or datetime.now(timezone.utc),
            )
        config = shape.payload.get("config") if isinstance(shape.payload, dict) else None
        if not isinstance(config, dict):
            _LOGGER.debug("monitor settings: shape has no config block; payload keys=%s", list(shape.payload.keys()) if isinstance(shape.payload, dict) else type(shape.payload).__name__)
            return SettingsSnapshot(
                values=dict(self._cache),
                asof=self._cache_asof or datetime.now(timezone.utc),
            )
        _LOGGER.debug("monitor settings: projecting config keys=%s", list(config.keys()))

        values: dict[str, Any] = {}
        # Booleans + numbers map across verbatim.
        for key in (
            "acCharge", "acChargeUpperEnabled", "dcDischarge", "ecoMode",
            "batteryReserve", "batteryCutoff", "chargeRate", "dischargeRate",
            "acChargeLimit", "maxOutputPowerPct",
        ):
            if key in config:
                values[key] = config[key]
        # Time strings ("HH:MM") -> datetime.time objects for the time entity.
        for key in (
            "acChargeStart", "acChargeEnd",
            "dcDischarge1Start", "dcDischarge1End",
            "dcDischarge2Start", "dcDischarge2End",
        ):
            raw = config.get(key)
            if isinstance(raw, str) and ":" in raw:
                try:
                    hh, mm = raw.split(":", 1)
                    values[key] = time(int(hh), int(mm))
                except (ValueError, TypeError):
                    pass

        filtered = {k: v for k, v in values.items() if v is not None}
        self._cache = {**self._cache, **filtered}
        self._cache_asof = datetime.now(timezone.utc)
        _LOGGER.debug(
            "monitor settings: projected %d keys (raw_values_pre_filter=%d, cache_size=%d)",
            len(filtered), len(values), len(self._cache),
        )
        return SettingsSnapshot(values=dict(self._cache), asof=self._cache_asof)

    # ─── Write ───────────────────────────────────────────────────────

    async def apply_setting(self, key: str, value: Any) -> WriteResult:
        """Optimistic write with revert-on-mismatch.

        Caller is the HA entity's `async_turn_on` / `async_set_value` /
        `async_set_value` for switch/number/time respectively.
        """
        if key not in CONTROL_ENTITY_KEYS:
            return WriteResult(
                key=key, requested_value=value, observed_value=None,
                accepted=False, error="unknown setting key",
            )

        # Master-only — monitors can't write.
        if not self._master.binding.can_control:
            return WriteResult(
                key=key, requested_value=value, observed_value=None,
                accepted=False, error="not bound as master",
            )

        # Rate limit (S12 mitigation).
        if not self._check_rate_limit(key):
            return WriteResult(
                key=key, requested_value=value, observed_value=None,
                accepted=False, error="rate limit exceeded",
            )

        transport = self._master.transport
        if transport is None:
            return WriteResult(
                key=key, requested_value=value, observed_value=None,
                accepted=False, error="transport not connected",
            )

        # Snap charge/discharge rate writes to the nearest valid
        # 52 W tick BEFORE we cache or confirm. Otherwise a user
        # typing 2630 would cache 2630, the inverter would round to
        # 2652 W (raw 51), and the confirm-read would log a spurious
        # "reverted (expected=2630 observed=2652)" + restore the
        # previous value — even though the write was correct.
        if key in ("chargeRate", "dischargeRate"):
            # Value is amps = the native 0-50 register; clamp to a whole amp
            # so the cache and the confirm-read agree.
            value = max(0, min(50, int(round(float(value)))))

        # Update cache optimistically.
        previous = self._cache.get(key)
        self._cache[key] = value
        self._cache_asof = datetime.now(timezone.utc)
        self._fire_settings_listeners()

        try:
            await self._issue_write(transport, key, value)
        except Exception as exc:
            _LOGGER.warning("apply_setting %s write failed: %s", key, exc)
            self._cache[key] = previous
            self._cache_asof = datetime.now(timezone.utc)
            self._fire_settings_listeners()
            return WriteResult(
                key=key, requested_value=value, observed_value=previous,
                accepted=False, error=str(exc), reverted=True,
            )

        # Schedule the confirmation read. Don't block the caller.
        asyncio.create_task(self._confirm_write(key, value, previous))
        return WriteResult(
            key=key, requested_value=value, observed_value=value, accepted=True,
        )

    def _fire_settings_listeners(self) -> None:
        """Notify the master's registered settings listeners that the
        cache has changed. Triggers a `coordinator.async_set_updated_data`
        call (via _on_master_settings) which in turn refreshes every
        control entity's state — without this, the entity continues to
        report the old (pre-write) value until the next 5-min settings
        coordinator tick.
        """
        for cb in self._master._settings_listeners:
            try:
                cb()
            except Exception:  # pragma: no cover - log + continue
                _LOGGER.exception("settings listener crashed during write")

    async def _issue_write(self, transport, key: str, value: Any) -> None:
        """Translate (key, value) to a list of TransparentRequest
        commands from givenergy_modbus.client.commands, then send via
        transport.one_shot_command().
        """
        from givenergy_modbus.client import commands as cmd
        from givenergy_modbus.model import TimeSlot

        if key == "acCharge":
            requests = cmd.enable_charge() if value else cmd.disable_charge()
        elif key == "acChargeUpperEnabled":
            # In 1.x: set_charge_target(target_soc) replaces the old
            # enable_charge_target. When disabling we use the dedicated
            # disable_charge_target() command.
            if value:
                limit = int(self._cache.get("acChargeLimit") or 80)
                requests = cmd.set_charge_target(limit)
            else:
                requests = cmd.disable_charge_target()
        elif key == "dcDischarge":
            # DC discharge is timed EXPORT on GivEnergy, so enabling it must
            # also put the inverter in max-power mode (battery_power_mode
            # HR27=0 = "maximum power, exporting to the grid"). Enabling
            # only enable_discharge (HR59) left HR27 at match-demand (=1,
            # "avoid exporting"), so the battery merely covered house load
            # and never exported — the behaviour GE Cloud's Timed Export
            # did set. Disabling restores match-demand so normal timed
            # discharge doesn't export. Verified on real hardware (David's
            # GIV-HY5.0, 2026-07-15): ~2.6 kW export vs 0 W before.
            if value:
                requests = cmd.enable_discharge() + cmd.set_discharge_mode_max_power()
            else:
                requests = cmd.disable_discharge() + cmd.set_discharge_mode_to_match_demand()
        elif key == "ecoMode":
            # Dynamic = eco on; Storage = eco off, using the user's
            # configured discharge slots.
            if value:
                requests = cmd.set_mode_dynamic()
            else:
                d1s = self._cache.get("dcDischarge1Start") or time(16, 0)
                d1e = self._cache.get("dcDischarge1End") or time(7, 0)
                d2s = self._cache.get("dcDischarge2Start")
                d2e = self._cache.get("dcDischarge2End")
                slot1 = TimeSlot(start=d1s, end=d1e)
                slot2 = (
                    TimeSlot(start=d2s, end=d2e) if d2s and d2e else None
                )
                requests = cmd.set_mode_storage(
                    discharge_slot_1=slot1,
                    discharge_slot_2=slot2,
                    discharge_for_export=False,
                )
        elif key == "batteryReserve":
            requests = cmd.set_battery_power_reserve(int(value))
        elif key == "batteryCutoff":
            requests = cmd.set_shallow_charge(int(value))
        elif key == "chargeRate":
            # UI value is amps (0-50) = the native register directly.
            raw = max(0, min(50, int(round(float(value)))))
            requests = cmd.set_battery_charge_limit(raw)
        elif key == "dischargeRate":
            raw = max(0, min(50, int(round(float(value)))))
            requests = cmd.set_battery_discharge_limit(raw)
        elif key == "acChargeLimit":
            # In 1.x the same value is set via set_charge_target.
            requests = cmd.set_charge_target(int(value))
        elif key == "maxOutputPowerPct":
            # No dedicated 1.x command — fall back to a raw write.
            # HR(50) = active_power_rate (0-100% active-power
            # curtailment). NOT HR(27) — that's battery_power_mode
            # (mislabelled "eco mode" in the library); writing the
            # output-power% to register 27 would flip eco mode and
            # leave max-output untouched.
            from givenergy_modbus.pdu import WriteHoldingRegisterRequest
            pct = max(0, min(100, int(value)))
            requests = [WriteHoldingRegisterRequest(register=50, value=pct)]
        elif key == "acChargeStart":
            end = self._cache.get("acChargeEnd") or time(5, 0)
            requests = cmd.set_charge_slot_1(TimeSlot(start=value, end=end))
        elif key == "acChargeEnd":
            start = self._cache.get("acChargeStart") or time(0, 30)
            requests = cmd.set_charge_slot_1(TimeSlot(start=start, end=value))
        elif key == "dcDischarge1Start":
            end = self._cache.get("dcDischarge1End") or time(20, 0)
            requests = cmd.set_discharge_slot_1(TimeSlot(start=value, end=end))
        elif key == "dcDischarge1End":
            start = self._cache.get("dcDischarge1Start") or time(16, 0)
            requests = cmd.set_discharge_slot_1(TimeSlot(start=start, end=value))
        elif key == "dcDischarge2Start":
            end = self._cache.get("dcDischarge2End") or time(22, 0)
            requests = cmd.set_discharge_slot_2(TimeSlot(start=value, end=end))
        elif key == "dcDischarge2End":
            start = self._cache.get("dcDischarge2Start") or time(22, 0)
            requests = cmd.set_discharge_slot_2(TimeSlot(start=start, end=value))
        else:
            raise ValueError(f"unhandled setting key {key}")

        await transport.one_shot_command(requests)

    async def _confirm_write(self, key: str, expected: Any, previous: Any) -> None:
        await asyncio.sleep(SETTINGS_WRITE_CONFIRM_DELAY_S)
        try:
            snapshot = await self.read_settings()
        except Exception as exc:
            _LOGGER.warning("confirm-read after %s failed: %s", key, exc)
            return
        observed = snapshot.values.get(key)
        # read_settings() already fires listeners via its own write into
        # _cache. But the cache may have been re-written by a parallel
        # apply_setting between sleep and now — fire again on revert so
        # entities reflect the rollback.
        if observed == expected:
            _LOGGER.info("apply_setting %s confirmed (=%s)", key, expected)
            return
        _LOGGER.warning(
            "apply_setting %s reverted (expected=%s observed=%s) — restoring cache",
            key, expected, observed,
        )
        self._cache[key] = previous
        self._cache_asof = datetime.now(timezone.utc)
        self._fire_settings_listeners()
        # Surface via HA persistent notification — non-fatal.
        try:
            from homeassistant.components.persistent_notification import (
                async_create as _notify,
            )
            _notify(
                self._master._hass,  # type: ignore[attr-defined]
                f"Inverter refused {key}={expected!r}; reverted to {previous!r}.",
                title="Birdy",
                notification_id=f"birdy_ha.write_reverted.{key}",
            )
        except Exception:
            pass

    def _check_rate_limit(self, key: str) -> bool:
        """6 writes per minute per setting (S12 mitigation)."""
        history = self._write_history[key]
        now = datetime.now(timezone.utc)
        # Trim entries older than 60 s.
        cutoff = now.timestamp() - 60
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= SETTINGS_WRITE_RATE_LIMIT_PER_MIN:
            return False
        history.append(now.timestamp())
        return True


def _safe_attr(obj: Any, name: str, coerce) -> Optional[Any]:
    """Read an attribute with type coercion; return None on missing /
    unreadable / coerce-failure.
    """
    value = getattr(obj, name, None)
    if value is None:
        return None
    try:
        return coerce(value)
    except (TypeError, ValueError):
        return None


def _coerce_time(value: Any) -> Optional[time]:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            parts = value.split(":")
            return time(int(parts[0]), int(parts[1]))
        except (IndexError, ValueError):
            return None
    return None
