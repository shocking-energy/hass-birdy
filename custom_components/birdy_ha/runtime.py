"""HaMaster runtime — orchestrates the full lifecycle.

Owns the InverterBinding aggregate, the Modbus transport, the cloud
client, the poll loop, and the role-refresh + diagnostics tasks.

State machine (see ha-master-integration.md § InverterBinding):

  Unprovisioned → Provisioned: bootstrap_ha_master + sign-in
  Provisioned   → Adopted:     claim_pi_by_serial OR adopt_pending_pi
                               (the latter is driven by the customer's
                                browser; we just keep retrying claim)
  Adopted       → Publishing:  first successful publish cycle
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .cloud import AuthError, CloudClient
from .const import (
    CONF_CLAIM_CODE,
    CONF_INTEGRATION_ID,
    CONF_INVERTER_HOST,
    DOMAIN,
    FORBIDDEN_TENANT_IDS,
    INITIAL_PROBE_TIMEOUT_S,
    ROLE_REFRESH_INTERVAL_S,
    SETTINGS_POLL_INTERVAL_S,
    TELEMETRY_POLL_INTERVAL_S,
)
from .domain import (
    BindingState,
    CanonicalReading,
    InverterBinding,
    InverterIdentity,
    PublisherRole,
    SettingsSnapshot,
    SystemShape,
)
from .driver.mappings import snapshot_to_readings
from .driver.shape import build_snapshot_row, corrupt_snapshot_reason
from .driver.transport import ModbusTransport, async_scan_for_dongle
from .settings import SettingsAdapter
from .low_rate import (
    LowRateImportDeriver,
    now_in_local_window,
    LOW_RATE_EV_W,
    _LONDON,
)

_LOGGER = logging.getLogger(__name__)


def _read_manifest_version() -> str:
    """Read manifest.json at module-import time so we never hit the disk
    inside the HA event loop (HA flags any sync file I/O in async code).
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "manifest.json")) as f:
            return json.load(f).get("version", "unknown")
    except Exception:
        return "unknown"


_MANIFEST_VERSION = _read_manifest_version()


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort float — passes through numeric values, parses
    numeric strings, returns None for everything else (including the
    JSONB null that Postgres emits as Python None).
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> Optional[datetime]:
    """Best-effort parse of Supabase's ISO8601 strings (handles the
    trailing `Z` plus PG fractional-second forms like `+00:00`).
    Returns None on failure rather than raising.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value.replace("Z", "+00:00")
    # Postgres / PostgREST emit microsecond fractions wider than 6
    # digits sometimes; trim to 6 to stay within fromisoformat's spec.
    if "." in s and "+" in s:
        head, _, rest = s.partition(".")
        frac, _, tz = rest.partition("+")
        frac = (frac + "000000")[:6]
        s = f"{head}.{frac}+{tz}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None

# Refresh interval for diagnostics. (ROLE_REFRESH_INTERVAL_S lives in
# const.py so the setup-time discovery + the background loop share it.)
DIAGNOSTICS_INTERVAL_S = 300

# How long to wait between claim retries while in Provisioned state.
CLAIM_RETRY_INTERVAL_S = 10


class HaMaster:
    """The integration's master object — one per config entry.

    Held in `hass.data[DOMAIN][entry.entry_id]['master']`. Coordinators
    + entities reference this through that handle.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

        # Local mode skips every cloud interaction: no bootstrap, no
        # tenant binding, no publish, no role refresh, no diagnostics
        # push. The runtime synthesises a "master" role locally so the
        # poll loop always runs the Modbus path, and write entities
        # treat the install as authorised to control. Flag set by the
        # config_flow's local_only checkbox (commit 1 of 0.11.0).
        self._local_only: bool = bool(entry.data.get("local_only"))

        integration_id = entry.data.get(CONF_INTEGRATION_ID) or str(uuid.uuid4())
        self.binding = InverterBinding(integration_id=integration_id)
        # Restore the inverter identity from entry.data if a previous
        # run persisted it. This way `binding.inverter.inverter_serial`
        # is available at HA platform-setup time, which means every
        # entity's _device_info can return a consistent
        # `(DOMAIN, serial)` identifier from the FIRST setup pass
        # instead of falling back to integration_id and creating a
        # second device record that has to be cleaned up manually.
        stored_serial = entry.data.get("inverter_serial")
        stored_lan_host = entry.data.get(CONF_INVERTER_HOST)
        if stored_serial:
            self.binding.inverter = InverterIdentity(
                inverter_serial=stored_serial,
                inverter_model=entry.data.get("inverter_model"),
                battery_serial=entry.data.get("battery_serial"),
                dongle_serial=None,
                lan_host=stored_lan_host,
            )

        self._cloud = CloudClient()
        self._transport: Optional[ModbusTransport] = None
        self._settings = SettingsAdapter(self)
        # Live low-rate grid-import split (parity with the pi-daemon deriver).
        self._low_rate = LowRateImportDeriver()
        self._latest_readings: list[CanonicalReading] = []
        self._latest_shape: Optional[SystemShape] = None
        self._latest_settings: Optional[SettingsSnapshot] = None
        self._last_modbus_ok_at: Optional[datetime] = None
        self._last_publish_ok_at: Optional[datetime] = None
        # Forecast values returned by publish_lan_snapshot (migration 034,
        # 045) — solar generation forecast for today + the tenant's stated
        # daily house consumption. Captured on every successful publish
        # and threaded back into the next snapshot's `meta` block so the
        # 3.5" kiosk's `<actual> / <expected>` slots populate, plus
        # surfaced as two HA sensor entities for automations.
        self._forecast: dict[str, Optional[float]] = {
            "solar_today_kwh": None,
            "house_daily_kwh": None,
        }

        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

        self._reading_listeners: list[Callable[[], None]] = []
        self._settings_listeners: list[Callable[[], None]] = []

    # ─── Lifecycle ───────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Initialise the master.

        Cloud mode: bootstrap (if no refresh token) → sign-in →
        discover → claim. Schedules background tasks.

        Local mode (`local_only=True`): skip every cloud step. Synthesise
        a "master" binding (no tenant, no auth user) and just connect
        the Modbus transport. Only the poll loop is scheduled — the
        role-refresh and diagnostics loops would have nothing to talk
        to.
        """
        await self._ensure_transport()

        if self._local_only:
            self._synthesise_local_binding()
            self._tasks.append(asyncio.create_task(self._poll_loop()))
            return

        await self._cloud.async_start()
        await self._ensure_credentials()
        await self._initial_discovery_and_claim()

        # Background tasks.
        self._tasks.append(asyncio.create_task(self._poll_loop()))
        self._tasks.append(asyncio.create_task(self._role_refresh_loop()))
        self._tasks.append(asyncio.create_task(self._diagnostics_loop()))

    def _synthesise_local_binding(self) -> None:
        """Set the in-memory binding to a self-declared master.

        The cloud server normally tells us our role; in local mode the
        cloud isn't reachable. We declare master so the poll loop runs
        the Modbus path, settings writes accept, and the role
        diagnostic shows "local" (handled in sensor.py). tenant_id
        stays None — there's no tenant to belong to.
        """
        self.binding.tenant_id = None
        self.binding.role = PublisherRole.MASTER
        self.binding.state = BindingState.PUBLISHING

    async def async_stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._cloud.async_close()

    # ─── Public API for coordinators + entities ──────────────────────

    @property
    def latest_readings(self) -> list[CanonicalReading]:
        return list(self._latest_readings)

    @property
    def latest_shape(self) -> Optional[SystemShape]:
        """Most recent SYSTEM shape (from Modbus in master mode, or
        from live_snapshot in monitor mode). The settings adapter's
        monitor-mode read pulls the `config` block out of this.
        """
        return self._latest_shape

    @property
    def latest_settings(self) -> Optional[SettingsSnapshot]:
        return self._latest_settings

    @property
    def forecast(self) -> dict[str, Optional[float]]:
        """Most recent forecast values from `publish_lan_snapshot`'s
        response. `solar_today_kwh` = cloud-generated forecast for
        today; `house_daily_kwh` = customer-stated daily consumption
        target. Both may be None when no forecast row exists for
        today or the tenant hasn't set a daily target. Used by the
        two forecast sensor entities + threaded into the next
        SYSTEM shape's `meta` block for the kiosk.
        """
        return dict(self._forecast)

    @property
    def cloud(self) -> CloudClient:
        return self._cloud

    @property
    def transport(self) -> Optional[ModbusTransport]:
        return self._transport

    @property
    def settings(self) -> SettingsAdapter:
        return self._settings

    @property
    def last_modbus_ok_at(self) -> Optional[datetime]:
        return self._last_modbus_ok_at

    @property
    def last_publish_ok_at(self) -> Optional[datetime]:
        return self._last_publish_ok_at

    def register_reading_listener(self, callback: Callable[[], None]) -> None:
        self._reading_listeners.append(callback)

    def register_settings_listener(self, callback: Callable[[], None]) -> None:
        self._settings_listeners.append(callback)

    # ─── Setup steps ─────────────────────────────────────────────────

    async def _ensure_credentials(self) -> None:
        """Bootstrap on first run; restore on subsequent runs."""
        stored_refresh = self._entry.data.get("refresh_token")
        if stored_refresh:
            self._cloud.load_refresh_token(stored_refresh)
            try:
                await self._cloud.refresh_access_token()
                self.binding.state = BindingState.PROVISIONED
                # Save the rotated token immediately.
                self._persist_refresh_token()
                return
            except AuthError as exc:
                _LOGGER.warning(
                    "stored refresh token rejected (%s); re-bootstrapping", exc
                )

        # First-run bootstrap (or recovery after a rotated/revoked token).
        email, password = await self._cloud.bootstrap_ha_master(
            self.binding.integration_id,
        )
        await self._cloud.sign_in_with_password(email, password)
        self.binding.state = BindingState.PROVISIONED
        self._persist_refresh_token()
        # Wipe email + password from memory (they're not stored anywhere).
        del email, password

    def _persist_refresh_token(self) -> None:
        new_data = {
            **self._entry.data,
            CONF_INTEGRATION_ID: self.binding.integration_id,
            "refresh_token": self._cloud.refresh_token,
        }
        self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    def _persist_inverter_identity(self, ident: InverterIdentity) -> None:
        """Stash the discovered identity in entry.data so the next HA
        start can reconstruct `binding.inverter` before platform setup
        runs. Without this, the first poll cycle wins the device_info
        race for the integration_id-keyed fallback identifier and HA
        creates two device-registry rows that have to be cleaned up
        manually.
        """
        new_data = {
            **self._entry.data,
            "inverter_serial": ident.inverter_serial,
            "inverter_model": ident.inverter_model,
            "battery_serial": ident.battery_serial,
        }
        self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    async def _ensure_transport(self) -> None:
        """Resolve inverter host (config override or LAN scan) and
        connect the Modbus transport.
        """
        host = self._entry.data.get(CONF_INVERTER_HOST)
        if not host:
            host = await async_scan_for_dongle()
            if host:
                _LOGGER.info("found inverter at %s via LAN scan", host)
                new_data = {**self._entry.data, CONF_INVERTER_HOST: host}
                self._hass.config_entries.async_update_entry(
                    self._entry, data=new_data,
                )
        if not host:
            raise RuntimeError(
                "No inverter found on the LAN. Set inverter_host in "
                "config flow options or check that the dongle is "
                "reachable from this HA host on port 8899."
            )
        self._transport = ModbusTransport(host)

    async def _initial_discovery_and_claim(self) -> None:
        """First Modbus read → identify inverter → claim.

        Sets binding.inverter on success. If claim returns
        'no tenant claims yet', register a pending discovery and
        leave binding.state at Provisioned; the poll loop will retry.

        Never fails setup: monitor role skips Modbus entirely (the
        master owns the single-client dongle), and bounded probe
        timeouts swallow library cancellations so HA's setup
        completes even when the dongle is contested or offline.
        """
        if self._transport is None:
            return

        # Role check first — monitors mustn't touch the dongle.
        # The poll loop's role refresh will re-probe periodically; if
        # we later become master it'll pick up Modbus then.
        try:
            role = await self._cloud.get_my_pi_role()
        except Exception as exc:
            _LOGGER.debug("role probe failed (%s); proceeding as master", exc)
            role = None
        role_value = getattr(role, "value", None) if role is not None else None
        if role_value == "monitor":
            # Monitor: resolve tenant_id + inverter identity from the
            # existing binding row, then return. The poll loop's
            # monitor branch reads live_snapshot for telemetry.
            await self._setup_monitor_from_binding()
            return

        # Bounded probe — library-side cancellations or hangs must not
        # escape and fail HA's setup. The poll loop retries every cycle.
        try:
            plant = await asyncio.wait_for(
                self._transport.refresh(), timeout=INITIAL_PROBE_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            # Only swallow self-cancellation (library timeout or our
            # wait_for). If HA is cancelling the setup task itself,
            # propagate so the harness can clean up.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            _LOGGER.warning(
                "initial Modbus probe cancelled (library timeout); "
                "poll loop will retry"
            )
            return
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "initial Modbus probe timed out after %ds (dongle busy "
                "or unreachable); poll loop will retry",
                INITIAL_PROBE_TIMEOUT_S,
            )
            return
        except Exception as exc:
            _LOGGER.warning("initial Modbus read failed: %s", exc)
            return

        inv = plant.inverter
        bats = list(plant.batteries) if plant.batteries else []
        # 1.x: `serial_number` on the inverter register block; the
        # plant-level `inverter_serial_number` is set from the PDU header.
        serial = (
            getattr(inv, "serial_number", None)
            or getattr(plant, "inverter_serial_number", None)
            or ""
        )
        if not serial:
            _LOGGER.warning("inverter serial unknown on first read; will retry")
            return
        ident = InverterIdentity(
            inverter_serial=serial,
            inverter_model=str(getattr(inv, "device_type_code", "")),
            battery_serial=(
                # 1.x: serial_number on battery object; 0.x called it
                # battery_serial_number.
                (
                    getattr(bats[0], "serial_number", None)
                    or getattr(bats[0], "battery_serial_number", None)
                )
                if bats else None
            ),
            dongle_serial=None,  # not read separately today
            lan_host=self._transport.host,
        )
        self.binding.inverter = ident
        self._persist_inverter_identity(ident)

        # Try to claim immediately.
        tenant_id = await self._cloud.claim_pi_by_serial(serial)
        if tenant_id:
            self._assert_not_forbidden_tenant(tenant_id)
            self.binding.tenant_id = tenant_id
            self.binding.state = BindingState.ADOPTED
            await self._refresh_role()
        else:
            # Push a pending discovery so the customer can adopt from
            # the dashboard.
            try:
                await self._cloud.register_discovery(ident)
            except AuthError as exc:
                _LOGGER.warning("discovery register failed: %s", exc)

    def _assert_not_forbidden_tenant(self, tenant_id: str) -> None:
        """R2 defense: if somehow we end up bound to Kevin Home, refuse
        to publish anything.
        """
        if tenant_id in FORBIDDEN_TENANT_IDS:
            raise RuntimeError(
                f"Bound to forbidden tenant {tenant_id} — "
                "Kevin Home is externally managed. "
                "Please contact support."
            )

    async def _setup_monitor_from_binding(self) -> None:
        """Resolve tenant_id + inverter identity from the existing
        binding row in the cloud, so monitor mode has the metadata it
        needs for tenant-scoped live_snapshot reads.

        Master mode learns these via Modbus + claim_pi_by_serial; a
        monitor never touches Modbus and may not have first-bound the
        tenant, so we read the binding directly.
        """
        try:
            row = await self._cloud.fetch_my_binding()
        except Exception as exc:
            _LOGGER.warning(
                "monitor: fetch_my_binding failed (%s); poll loop will retry",
                exc,
            )
            return
        if not row:
            _LOGGER.warning(
                "monitor: no pi_publisher_bindings row for this user yet"
            )
            return
        tenant_id = row.get("tenant_id")
        serial = (row.get("serial_at_bind") or "").strip()
        if not tenant_id:
            return
        self._assert_not_forbidden_tenant(tenant_id)
        self.binding.tenant_id = tenant_id
        self.binding.role = PublisherRole.MONITOR
        # We don't know the LAN host (monitors didn't LAN-scan); leave
        # transport-host fields blank. Identity is enough for entity
        # device-grouping by serial.
        ident = InverterIdentity(
            inverter_serial=serial,
            inverter_model=None,
            battery_serial=None,
            dongle_serial=None,
            lan_host=None,
        )
        self.binding.inverter = ident
        self._persist_inverter_identity(ident)
        self.binding.state = BindingState.ADOPTED
        _LOGGER.info(
            "monitor: bound to tenant %s (inverter %s); cloud-read poll active",
            tenant_id,
            serial or "<unknown>",
        )

    # ─── Background loops ────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main telemetry poll. Drives binding state from Adopted →
        Publishing on first successful publish; survives transient
        failures.
        """
        while not self._stop_event.is_set():
            await self._poll_once_safe()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=TELEMETRY_POLL_INTERVAL_S,
                )
            except asyncio.TimeoutError:
                pass

    async def _poll_once_safe(self) -> None:
        try:
            await self._poll_once()
        except Exception as exc:  # broad on purpose — the loop must survive
            _LOGGER.warning("poll cycle failed: %s", exc, exc_info=False)

    async def _poll_once(self) -> None:
        # Monitor branch: no Modbus, read live_snapshot from cloud.
        if self.binding.role == PublisherRole.MONITOR:
            await self._poll_once_monitor()
            return

        if self._transport is None:
            return

        # If still Provisioned (no tenant yet), retry the claim
        # opportunistically.
        if self.binding.state == BindingState.PROVISIONED:
            if self.binding.inverter is None:
                await self._initial_discovery_and_claim()
                return
            tenant_id = await self._cloud.claim_pi_by_serial(
                self.binding.inverter.inverter_serial,
            )
            if tenant_id:
                self._assert_not_forbidden_tenant(tenant_id)
                self.binding.tenant_id = tenant_id
                self.binding.state = BindingState.ADOPTED
                await self._refresh_role()
                # If we became monitor after adopting, route this cycle
                # through the cloud-read path.
                if self.binding.role == PublisherRole.MONITOR:
                    await self._poll_once_monitor()
                    return
            else:
                # Refresh the pending-discovery row so it doesn't TTL.
                try:
                    await self._cloud.register_discovery(self.binding.inverter)
                except AuthError:
                    pass
                # Skip publish — not adopted yet.
                return

        plant = await self._transport.refresh()
        now = datetime.now(timezone.utc)
        self._last_modbus_ok_at = now

        # Build SYSTEM shape + canonical readings from the same plant.
        # Thread the forecast values from the previous publish response
        # into `meta` so the kiosk's `<actual> / <expected>` slots can
        # render (shape.py reads them out of opts["forecast"]).
        opts = {"forecast": dict(self._forecast)}
        shape_payload = build_snapshot_row(plant, opts)
        # Drop corrupt Modbus frames before they reach the local SSE
        # cache (_latest_shape), live_snapshot, or device_telemetry. A
        # garbled/partial read decodes to physically-impossible values
        # (SOC 28949%, grid 27.2 kW on a 5 kW inverter — David,
        # 2026-06-05); skipping the cycle keeps the previous good
        # snapshot and the next ~15 s poll recovers. Size-independent
        # gate (SOC range + 1 MW backstop) — see corrupt_snapshot_reason.
        corrupt = corrupt_snapshot_reason(shape_payload)
        if corrupt:
            _LOGGER.warning(
                "dropping corrupt Modbus frame (%s); keeping last good snapshot",
                corrupt,
            )
            return
        # Stamp source so dashboard knows the writer kind. Cloud schema
        # supports 'lan_push'; HA writes via the same RPC, same value.
        device_id = await self._resolve_inverter_device_id()
        recorded_at_iso = now.isoformat()
        # Build CanonicalReadings unconditionally so local-mode entities
        # have values to render. _resolve_inverter_device_id() returns
        # None whenever tenant_id is None — which is always-true in
        # local mode — so gating reading_dicts on a truthy device_id
        # leaves every read sensor permanently Unavailable. The cloud
        # telemetry publish that actually needs device_id is gated
        # separately by `if not self._local_only` below, so the local
        # path is safe with a placeholder. self._cloud.user_id is None
        # in local mode (we never sign in), hence the guard.
        _src = (
            f"ha:{self._cloud.user_id}"
            if (self._cloud and getattr(self._cloud, "user_id", None))
            else "ha:local"
        )
        reading_dicts = snapshot_to_readings(
            shape_payload,
            device_id=device_id or "local",
            recorded_at=recorded_at_iso,
            source=_src,
        )
        # Low-rate grid-import split (parity with the pi-daemon deriver):
        # gate on the inverter's AC-charge window (= the Octopus cheap window
        # on a GivEnergy tenant), plus a CT 'ev' channel if one exists.
        # Accounting only — appends grid_import.low_rate_today_kwh, never
        # touches control.
        try:
            cfg = shape_payload.get("config") or {}
            now_local = now.astimezone(_LONDON)
            is_low = now_in_local_window(
                cfg.get("acChargeStart"), cfg.get("acChargeEnd"), now_local)
            if not is_low:
                ev = next(
                    (r.get("value") for r in reading_dicts
                     if r.get("component") == "ev" and r.get("metric") == "power_w"),
                    None)
                if ev is not None and float(ev) >= LOW_RATE_EV_W:
                    is_low = True
            reading_dicts = self._low_rate.augment(reading_dicts, is_low)
        except Exception:
            _LOGGER.debug("low_rate augment failed", exc_info=True)
        readings = [
            CanonicalReading(
                device_id=r["device_id"],
                component=r["component"],
                metric=r["metric"],
                value=r["value"],
                unit=r["unit"] or "",
                recorded_at=now,
                quality=r.get("quality", "good"),
            )
            for r in reading_dicts
        ]

        self._latest_shape = SystemShape(payload=shape_payload)
        self._latest_readings = readings
        for cb in self._reading_listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("reading listener crashed")

        # Publish if master + adopted. Local mode never publishes —
        # by definition nothing leaves the LAN.
        if not self._local_only and self.binding.can_publish:
            await self._publish_safe(self._latest_shape, readings)

    async def _poll_once_monitor(self) -> None:
        """Cloud-read path for monitor mode.

        Fetches live_snapshot from Supabase (RLS-scoped to the bound
        tenant) and runs the same canonical mapper the master path
        runs. Updates _latest_shape, _latest_readings, and the
        reading listeners — entities populate identically to master,
        but `Publishing` stays off.

        If the master goes offline, updated_at drifts; entities still
        show last-known values and the dashboard diagnostic exposes
        the staleness.
        """
        tenant_id = self.binding.tenant_id
        if not tenant_id:
            # Setup didn't complete (no binding row yet); try again.
            await self._setup_monitor_from_binding()
            tenant_id = self.binding.tenant_id
            if not tenant_id:
                return

        row = await self._cloud.fetch_live_snapshot(tenant_id)
        if not row:
            return
        shape_payload = row.get("snapshot")
        if not isinstance(shape_payload, dict):
            return

        # The master Pi stamps publish_lan_snapshot's response forecast
        # values into snapshot.meta.{solarForecastTodayKwh,
        # houseExpectedDailyKwh} for the kiosk's <actual>/<expected>
        # panels. Lift them into _forecast so the same two HA sensors
        # populate on the monitor side too — same data, no separate
        # RPC round-trip needed.
        meta = shape_payload.get("meta") if isinstance(shape_payload, dict) else None
        if isinstance(meta, dict):
            self._forecast["solar_today_kwh"] = _coerce_float(
                meta.get("solarForecastTodayKwh")
            )
            self._forecast["house_daily_kwh"] = _coerce_float(
                meta.get("houseExpectedDailyKwh")
            )

        now = datetime.now(timezone.utc)
        # Track the source-of-truth age separately on _last_modbus_ok_at
        # so the freshness sensor still reflects when the master wrote.
        # But recorded_at on each CanonicalReading must be `now` — the
        # post-init age check rejects anything > READING_MAX_AGE_S old,
        # and live_snapshot can lag (ge-poll's 60s cadence, transient
        # cloud staleness) which would crash the whole poll cycle.
        published_at = _parse_iso(row.get("updated_at")) or now
        self._last_modbus_ok_at = published_at

        device_id = await self._resolve_inverter_device_id()
        recorded_at_iso = now.isoformat()
        reading_dicts = snapshot_to_readings(
            shape_payload,
            device_id=device_id or "",
            recorded_at=recorded_at_iso,
            source=f"ha-monitor:{self._cloud.user_id}",
        ) if device_id else []
        readings = [
            CanonicalReading(
                device_id=r["device_id"],
                component=r["component"],
                metric=r["metric"],
                value=r["value"],
                unit=r["unit"] or "",
                recorded_at=now,
                quality=r.get("quality", "good"),
            )
            for r in reading_dicts
        ]

        self._latest_shape = SystemShape(payload=shape_payload)
        self._latest_readings = readings
        for cb in self._reading_listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("reading listener crashed")

        # Monitor mode: project the settings out of the same shape so
        # control entities stay in sync with telemetry instead of
        # lagging by up to SETTINGS_POLL_INTERVAL_S (5 min) waiting for
        # the settings coordinator's slow timer to fire.
        try:
            await self._settings.read_settings()
        except Exception:
            _LOGGER.debug("monitor settings projection failed", exc_info=True)
        for cb in self._settings_listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("settings listener crashed")

    async def _publish_safe(
        self,
        shape: SystemShape,
        readings: list[CanonicalReading],
    ) -> None:
        try:
            response = await self._cloud.publish_lan_snapshot(shape)
            # Response (migration 045) is `{solar_today_kwh,
            # house_daily_kwh, asof, role, wrote}`. Capture the two
            # forecast values for the next poll's snapshot + the
            # forecast sensor entities. Both may be null (no forecast
            # row for today, or no `daily_consumption_kwh` in tenant
            # prefs) — pass through verbatim.
            if isinstance(response, dict):
                self._forecast["solar_today_kwh"] = _coerce_float(
                    response.get("solar_today_kwh")
                )
                self._forecast["house_daily_kwh"] = _coerce_float(
                    response.get("house_daily_kwh")
                )
        except Exception as exc:
            _LOGGER.warning("publish_lan_snapshot failed: %s", exc)
        if readings:
            try:
                await self._cloud.publish_device_telemetry(readings)
            except Exception as exc:
                _LOGGER.warning("publish_device_telemetry failed: %s", exc)
        self._last_publish_ok_at = datetime.now(timezone.utc)
        if self.binding.state == BindingState.ADOPTED:
            self.binding.state = BindingState.PUBLISHING

    async def _role_refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=ROLE_REFRESH_INTERVAL_S,
                )
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                return
            try:
                await self._refresh_role()
            except Exception as exc:
                _LOGGER.debug("role refresh failed: %s", exc)

    async def _refresh_role(self) -> None:
        if self.binding.state == BindingState.UNPROVISIONED:
            return
        role = await self._cloud.get_my_pi_role()
        self.binding.role = role
        self.binding.last_role_check = datetime.now(timezone.utc)

    async def _diagnostics_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=DIAGNOSTICS_INTERVAL_S,
                )
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                return
            try:
                await self._report_diagnostics()
            except Exception as exc:
                _LOGGER.debug("diagnostics report failed: %s", exc)

    async def _report_diagnostics(self) -> None:
        if not self.binding.can_publish and self.binding.state != BindingState.ADOPTED:
            return
        versions = {
            "integration": self._integration_version(),
            "ha_core": self._ha_version(),
        }
        client_info = {
            "lan_ip": self._transport.host if self._transport else None,
            "role_observed": self.binding.role.value if self.binding.role else None,
            "kind": "ha",
        }
        await self._cloud.update_pi_diagnostics(
            installed_versions=versions, client_info=client_info,
        )

    def _integration_version(self) -> str:
        # Read at module import time (before the event loop exists)
        # so we never hit the disk inside an async context.
        return _MANIFEST_VERSION

    def _ha_version(self) -> str:
        try:
            from homeassistant.const import __version__ as ha_version
            return ha_version
        except Exception:
            return "unknown"

    async def _resolve_inverter_device_id(self) -> Optional[str]:
        """Look up the inverter's devices.id row for the bound tenant.

        Returns None when unbound or the row isn't seeded yet — the
        publisher just skips device_telemetry in that case (snapshot
        still publishes).
        """
        if self.binding.tenant_id is None or self.binding.inverter is None:
            return None
        # Tenant scoping handled by RLS via get_my_tenant_id() — no
        # explicit tenant_id filter needed. The inverter serial is
        # LAN-sourced (whatever the Modbus device returns) so we pass
        # it via aiohttp `params={...}` to get proper URL-encoding
        # rather than hand-building the query string — a malformed or
        # hostile dongle that returned a serial containing `&` or a
        # PostgREST operator could otherwise tamper with the query.
        try:
            url = f"{self._cloud._supabase_url}/rest/v1/devices"
            params = {
                "device_type": "eq.inverter",
                "manufacturer": "eq.givenergy",
                "serial_number": (
                    f"eq.{self.binding.inverter.inverter_serial.upper()}"
                ),
                "select": "id",
            }
            async with self._cloud._session.get(  # type: ignore[union-attr]
                url, params=params, headers=self._cloud._auth_headers(),
            ) as resp:
                if resp.status >= 400:
                    return None
                rows = await resp.json()
                if rows and isinstance(rows, list):
                    return rows[0].get("id")
        except Exception:
            return None
        return None
