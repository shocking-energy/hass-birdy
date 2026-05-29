"""HA config flow for Birdy.

UX (happy path):
  1. User clicks "Add Integration → Birdy".
  2. Single screen explains: "After install, open shocking.energy on
     this LAN and click Adopt on the banner."
  3. Submit → integration_id generated, bootstrap called, sign-in,
     LAN scan (or fallback to manual host), discovery registered.
  4. Background polling waits for adopt; entities populate when
     claim_pi_by_serial succeeds.

Recovery paths:
  - LAN scan finds nothing: subsequent step asks for inverter host
    manually (item #12 gotcha).
  - Auto-adopt times out after 2 min: subsequent step offers
    "Enter claim code from dashboard" (item #5 gotcha).
  - Already-bound monitor: subsequent step shows "You're a monitor;
    open admin to demote the existing master." (item #6 gotcha).
  - HA clock skew > 5 min: surfaced as setup error during sign-in
    (item #7 gotcha; the cloud client raises AuthError).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .cloud import AuthError, CloudClient
from .const import (
    CONF_CLAIM_CODE,
    CONF_INTEGRATION_ID,
    CONF_INVERTER_HOST,
    DOMAIN,
)
from .driver.transport import async_scan_for_dongle

_LOGGER = logging.getLogger(__name__)

ADOPT_WAIT_TIMEOUT_S = 120
ADOPT_POLL_INTERVAL_S = 5


class BirdyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Walks the user through bootstrap → discover → adopt."""

    VERSION = 1

    def __init__(self) -> None:
        self._integration_id: Optional[str] = None
        self._inverter_host: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._cloud: Optional[CloudClient] = None
        self._inverter_serial: Optional[str] = None
        self._is_monitor: bool = False
        self._local_only: bool = False

    # ─── Step 1: intro + bootstrap ───────────────────────────────────

    async def async_step_user(self, user_input=None) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=self._user_schema(),
            )

        self._local_only = bool(user_input.get("local_only"))
        self._integration_id = str(uuid.uuid4())

        if self._local_only:
            # Local mode: no Shocking Energy account, no telemetry
            # leaves the LAN, no AI assistant or forecast values.
            # Skip bootstrap + adoption entirely; just find the
            # inverter and finalise the entry.
            host = await async_scan_for_dongle()
            if host:
                self._inverter_host = host
                return self._finalize_local()
            return await self.async_step_manual_host()

        # Cloud mode (default): bootstrap a per-install auth user,
        # sign in, then run the LAN scan + adopt-via-egress-IP flow.
        self._cloud = CloudClient()
        await self._cloud.async_start()

        try:
            email, password = await self._cloud.bootstrap_ha_master(
                self._integration_id,
            )
            await self._cloud.sign_in_with_password(email, password)
        except AuthError as exc:
            _LOGGER.error("bootstrap failed: %s", exc)
            return self.async_show_form(
                step_id="user",
                data_schema=self._user_schema(),
                errors={"base": "cannot_connect"},
                description_placeholders={"error": str(exc)},
            )

        self._refresh_token = self._cloud.refresh_token

        # LAN scan.
        host = await async_scan_for_dongle()
        if host:
            self._inverter_host = host
            return await self.async_step_wait_for_adopt()

        # LAN scan found nothing — fall through to manual host entry.
        return await self.async_step_manual_host()

    @staticmethod
    def _user_schema():
        """Schema for the first step.

        Single optional `local_only` checkbox. Default False → cloud
        mode (matches the existing onboarding experience for everyone
        who started before 0.11.0). Checking it skips every cloud
        round-trip; the integration runs from local Modbus only.
        """
        return vol.Schema(
            {vol.Optional("local_only", default=False): bool}
        )

    def _finalize_local(self) -> FlowResult:
        """Create the config entry for a local-only install.

        No bootstrap, no auth user, no tenant binding. The runtime
        reads `local_only` from entry.data and skips every cloud
        path. Modbus polling + control writes still go LAN-direct.
        """
        return self.async_create_entry(
            title="Birdy",
            data={
                CONF_INTEGRATION_ID: self._integration_id,
                CONF_INVERTER_HOST: self._inverter_host,
                "local_only": True,
            },
        )

    # ─── Step 2a: manual host (LAN scan failed) ──────────────────────

    async def async_step_manual_host(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="manual_host",
                data_schema=vol.Schema(
                    {vol.Required(CONF_INVERTER_HOST): str}
                ),
                description_placeholders={
                    "hint": (
                        "Couldn't auto-discover the GivEnergy dongle on "
                        "the LAN. Enter its IP address (e.g. "
                        "192.168.1.42). If HA is running in a Docker "
                        "container, ensure it has LAN access (network "
                        "mode: host)."
                    ),
                },
            )
        self._inverter_host = user_input[CONF_INVERTER_HOST].strip()
        if self._local_only:
            return self._finalize_local()
        return await self.async_step_wait_for_adopt()

    # ─── Step 3: wait for adopt (background) ─────────────────────────

    async def async_step_wait_for_adopt(self, user_input=None) -> FlowResult:
        """Block briefly while the customer adopts in the dashboard."""
        if self._cloud is None or self._inverter_host is None:
            return self.async_abort(reason="invalid_state")

        # Register a pending discovery, then poll claim_pi_by_serial.
        # We don't actually read the inverter here — the background
        # poll loop after setup does that. But we DO need the serial
        # for the discovery, so we connect briefly.
        from .driver.transport import ModbusTransport

        try:
            transport = ModbusTransport(self._inverter_host)
            plant = await transport.refresh()
            # 1.x renamed the register-derived serial to `serial_number`.
            # `inverter_serial_number` is now a PDU-header field on the
            # plant itself (not the inverter), populated even for partial
            # responses. Prefer the inverter register, fall back to the
            # plant header, fall back to empty.
            self._inverter_serial = (
                getattr(plant.inverter, "serial_number", None)
                or getattr(plant, "inverter_serial_number", None)
                or ""
            )
        except Exception as exc:
            _LOGGER.warning("inverter probe failed: %s", exc)
            return self.async_show_form(
                step_id="manual_host",
                data_schema=vol.Schema(
                    {vol.Required(CONF_INVERTER_HOST, default=self._inverter_host): str}
                ),
                errors={"base": "modbus_unreachable"},
                description_placeholders={"error": str(exc)},
            )

        if not self._inverter_serial:
            return self.async_show_form(
                step_id="manual_host",
                data_schema=vol.Schema(
                    {vol.Required(CONF_INVERTER_HOST, default=self._inverter_host): str}
                ),
                errors={"base": "no_serial"},
            )

        # Register the discovery so the dashboard banner can match.
        from .domain import InverterIdentity

        ident = InverterIdentity(
            inverter_serial=self._inverter_serial,
            lan_host=self._inverter_host,
        )
        try:
            await self._cloud.register_discovery(ident)
        except AuthError as exc:
            _LOGGER.warning("discovery register failed: %s", exc)

        # Poll for adoption.
        tenant_id = await self._wait_for_claim()
        if tenant_id:
            return await self._finalize(tenant_id)
        # Timed out — offer claim-code fallback.
        return await self.async_step_claim_code()

    async def _wait_for_claim(self) -> Optional[str]:
        elapsed = 0
        while elapsed < ADOPT_WAIT_TIMEOUT_S:
            tenant_id = await self._cloud.claim_pi_by_serial(
                self._inverter_serial,
            )
            if tenant_id:
                return tenant_id
            await asyncio.sleep(ADOPT_POLL_INTERVAL_S)
            elapsed += ADOPT_POLL_INTERVAL_S
        return None

    # ─── Step 4: claim-code fallback (egress-IP mismatch) ────────────

    # TODO(maintainer): claim-code is a non-functional stub.
    #
    # The form is wired up + presented, but submitting it always
    # returns `claim_code_not_implemented`. The README's CGNAT/VPN
    # recovery promise depends on this path. Either:
    #   (a) implement the claim-code exchange (server-side endpoint +
    #       client RPC + tests), or
    #   (b) remove the form + drop the CGNAT recovery promise from
    #       the README so users see a clean error path instead of a
    #       half-promise.
    #
    # Flagged by 2026-05-29 security review (finding I1).
    async def async_step_claim_code(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="claim_code",
                data_schema=vol.Schema({vol.Required(CONF_CLAIM_CODE): str}),
                description_placeholders={
                    "hint": (
                        "Auto-adopt didn't complete within 2 minutes. "
                        "This usually means the dashboard browser and "
                        "your HA host are on different networks (CGNAT, "
                        "VPN, mobile data). Open shocking.energy from "
                        "the same LAN as HA OR enter a claim code from "
                        "Settings → Integrations → Add HA master."
                    ),
                },
            )
        # Stub: claim-code RPC + endpoint are v2 work (see open
        # questions in the doc). For v1, the form is wired up but
        # exchange is a placeholder; user is told to use the LAN
        # browser instead.
        return self.async_show_form(
            step_id="claim_code",
            data_schema=vol.Schema({vol.Required(CONF_CLAIM_CODE): str}),
            errors={"base": "claim_code_not_implemented"},
            description_placeholders={
                "hint": (
                    "Claim code exchange is not yet implemented (v2 "
                    "feature). For now, please open shocking.energy "
                    "from a device on the same LAN as Home Assistant."
                ),
            },
        )

    # ─── Step 5: finalize / existing-master block ────────────────────

    async def _finalize(self, tenant_id: str) -> FlowResult:
        # Check role — if monitor, block the setup with a friendly
        # message instead of silently no-op'ing forever (item #6).
        try:
            role = await self._cloud.get_my_pi_role()
        except Exception:
            role = None

        if role and role.value == "monitor":
            self._is_monitor = True
            return self.async_show_form(
                step_id="existing_master",
                data_schema=vol.Schema(
                    {
                        # Default False → unchecked submit just re-checks
                        # the role (the original "demote then click
                        # Submit" path). Checking the box accepts the
                        # current monitor binding and finalises the
                        # config entry; the runtime then reads from
                        # cloud and stays out of Modbus contention.
                        vol.Optional("accept_monitor", default=False): bool,
                    }
                ),
                description_placeholders={
                    "hint": (
                        "Your account already has a master device (likely a "
                        "Pi or another HA install). Birdy has been adopted "
                        "as a monitor.\n\n"
                        "• To run as MASTER: open admin.shocking.energy → "
                        "Pis, demote the existing master, then click "
                        "Submit (leave the box unchecked).\n\n"
                        "• To stay as MONITOR: check the box below and "
                        "Submit. Birdy will read live data from the cloud "
                        "(no Modbus polling, no telemetry publish). You "
                        "can promote it to master later via the admin panel "
                        "and Birdy will pick up the role change within "
                        "60 s — no reinstall needed."
                    ),
                },
            )

        # Happy path — create the entry.
        return self.async_create_entry(
            title="Birdy",
            data={
                CONF_INTEGRATION_ID: self._integration_id,
                CONF_INVERTER_HOST: self._inverter_host,
                "refresh_token": self._refresh_token,
                "tenant_id": tenant_id,
            },
        )

    async def async_step_existing_master(self, user_input=None) -> FlowResult:
        # Two submit paths from the existing-master form:
        #   accept_monitor=True  → finalise the entry now as monitor
        #   accept_monitor=False → re-check role (user demoted; want master)
        if self._cloud is None:
            return self.async_abort(reason="invalid_state")

        if user_input and user_input.get("accept_monitor"):
            # Resolve tenant_id (best-effort) and finalise. Runtime
            # picks up any later promotion within 60 s via the role
            # refresh loop — no reinstall needed.
            tenant_id: Optional[str] = None
            if self._inverter_serial:
                try:
                    tenant_id = await self._cloud.claim_pi_by_serial(
                        self._inverter_serial,
                    )
                except Exception as exc:  # pragma: no cover - logged only
                    _LOGGER.warning(
                        "accept_monitor: tenant_id re-resolve failed: %s",
                        exc,
                    )
            entry_data: dict[str, Any] = {
                CONF_INTEGRATION_ID: self._integration_id,
                CONF_INVERTER_HOST: self._inverter_host,
                "refresh_token": self._refresh_token,
            }
            if tenant_id:
                entry_data["tenant_id"] = tenant_id
            return self.async_create_entry(title="Birdy", data=entry_data)

        try:
            role = await self._cloud.get_my_pi_role()
        except Exception as exc:
            return self.async_show_form(
                step_id="existing_master",
                data_schema=vol.Schema({}),
                errors={"base": "cannot_connect"},
                description_placeholders={"error": str(exc)},
            )
        if role and role.value == "master":
            # Re-resolve tenant_id for entry data parity with _finalize().
            # The binding already exists (we were adopted as monitor),
            # so claim_pi_by_serial returns the bound tenant immediately
            # without re-running adoption. If it fails (unusual — would
            # mean the binding got revoked between role-flip and now),
            # omit the key rather than persisting an empty string;
            # runtime.HaMaster re-resolves tenant_id from the binding
            # on first poll so this is correctness-only, not a hard
            # requirement for the integration to function.
            tenant_id: Optional[str] = None
            if self._inverter_serial:
                try:
                    tenant_id = await self._cloud.claim_pi_by_serial(
                        self._inverter_serial,
                    )
                except Exception as exc:  # pragma: no cover - logged only
                    _LOGGER.warning(
                        "existing_master: tenant_id re-resolve failed: %s",
                        exc,
                    )
            entry_data: dict[str, Any] = {
                CONF_INTEGRATION_ID: self._integration_id,
                CONF_INVERTER_HOST: self._inverter_host,
                "refresh_token": self._refresh_token,
            }
            if tenant_id:
                entry_data["tenant_id"] = tenant_id
            return self.async_create_entry(
                title="Birdy",
                data=entry_data,
            )
        # Still monitor — repeat the screen.
        return await self._finalize(tenant_id="")
