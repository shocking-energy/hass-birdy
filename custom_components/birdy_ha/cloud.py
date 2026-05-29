"""Async Supabase / Vercel cloud adapter.

Anti-corruption layer between the HA Master domain and the cloud
(Supabase Auth + RPCs + the Vercel /api/ endpoints). All HTTP is
aiohttp; all responses are translated to domain types before
returning.

Pins TLS verification ON regardless of HA's global verify_ssl
setting (R15).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

from .const import (
    CLOCK_SKEW_FAIL_S,
    CLOCK_SKEW_WARN_S,
    HA_BOOTSTRAP_PATH,
    PI_DISCOVERY_REGISTER_PATH,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    VERCEL_BASE_URL,
)
from .domain import (
    AuthCredentials,
    CanonicalReading,
    InverterIdentity,
    PublisherRole,
    SystemShape,
)

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when Supabase rejects our token or the refresh-grant fails."""


class CloudClient:
    """One HTTP client for all cloud interactions.

    Holds the access token + refresh token in memory. Persisting them
    to HA's config entry is the integration's responsibility (the
    config_flow stores the refresh token; access tokens are
    short-lived and re-derived from the refresh on each cold start).
    """

    def __init__(
        self,
        anon_key: str = SUPABASE_ANON_KEY,
        supabase_url: str = SUPABASE_URL,
        vercel_base: str = VERCEL_BASE_URL,
    ) -> None:
        self._anon_key = anon_key
        self._supabase_url = supabase_url.rstrip("/")
        self._vercel_base = vercel_base.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._access_token: Optional[str] = None
        self._access_token_expires_at: Optional[datetime] = None
        self._refresh_token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._last_clock_skew_s: Optional[float] = None

    # ─── Lifecycle ───────────────────────────────────────────────────

    async def __aenter__(self) -> "CloudClient":
        await self.async_start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.async_close()

    async def async_start(self) -> None:
        if self._session is None:
            # ssl=True forces certificate verification; ignores HA's
            # global verify_ssl setting per R15.
            connector = aiohttp.TCPConnector(ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)

    async def async_close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ─── Auth ────────────────────────────────────────────────────────

    async def bootstrap_ha_master(self, integration_id: str) -> tuple[str, str]:
        """Call /api/ha-bootstrap. Returns (email, password).

        Idempotent on integration_id — re-bootstrap rotates the
        password and returns the new value.
        """
        url = self._vercel_base + HA_BOOTSTRAP_PATH
        async with self._post(url, body={"integration_id": integration_id}) as resp:
            payload = await self._json_or_raise(resp, "ha-bootstrap")
        return payload["email"], payload["password"]

    async def sign_in_with_password(
        self, email: str, password: str
    ) -> AuthCredentials:
        """Exchange (email, password) for tokens, stash them on self."""
        url = f"{self._supabase_url}/auth/v1/token?grant_type=password"
        async with self._post(
            url,
            body={"email": email, "password": password},
            headers={"apikey": self._anon_key},
        ) as resp:
            payload = await self._json_or_raise(resp, "sign_in")
            await self._check_clock_skew(resp)
        return self._store_auth_payload(payload)

    async def refresh_access_token(self) -> AuthCredentials:
        if not self._refresh_token:
            raise AuthError("no refresh token")
        url = f"{self._supabase_url}/auth/v1/token?grant_type=refresh_token"
        async with self._post(
            url,
            body={"refresh_token": self._refresh_token},
            headers={"apikey": self._anon_key},
        ) as resp:
            if resp.status == 400:
                # Token rotated or revoked.
                raise AuthError(f"refresh rejected (status={resp.status})")
            payload = await self._json_or_raise(resp, "refresh")
        return self._store_auth_payload(payload)

    def load_refresh_token(self, refresh_token: str) -> None:
        """Used by the integration on cold start to resume an existing
        session before calling refresh_access_token().
        """
        self._refresh_token = refresh_token

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    @property
    def refresh_token(self) -> Optional[str]:
        return self._refresh_token

    @property
    def last_clock_skew_s(self) -> Optional[float]:
        return self._last_clock_skew_s

    def _store_auth_payload(self, payload: dict[str, Any]) -> AuthCredentials:
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        expires_in = int(payload.get("expires_in", 3600))
        self._access_token_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        )
        user = payload.get("user") or {}
        self._user_id = user.get("id")
        return AuthCredentials(
            user_id=self._user_id or "",
            email=user.get("email", ""),
            refresh_token=self._refresh_token or "",
            access_token=self._access_token or "",
            access_token_expires_at=self._access_token_expires_at,
        )

    async def _check_clock_skew(self, resp: aiohttp.ClientResponse) -> None:
        """Clock-skew sanity check (gotcha #7).

        Compares the Date header from a Supabase response against the
        HA host's clock. Logs WARN above CLOCK_SKEW_WARN_S, raises
        AuthError above CLOCK_SKEW_FAIL_S (5 min — the same window the
        server-side recorded_at clamp uses).
        """
        server_date = resp.headers.get("Date")
        if not server_date:
            return
        try:
            from email.utils import parsedate_to_datetime
            server_dt = parsedate_to_datetime(server_date)
            if server_dt.tzinfo is None:
                server_dt = server_dt.replace(tzinfo=timezone.utc)
            skew_s = (datetime.now(timezone.utc) - server_dt).total_seconds()
            self._last_clock_skew_s = skew_s
            abs_skew = abs(skew_s)
            if abs_skew > CLOCK_SKEW_FAIL_S:
                raise AuthError(
                    f"HA host clock is off by {skew_s:.0f}s vs Supabase; "
                    f"telemetry will be rejected by the server-side "
                    f"recorded_at clamp. Fix NTP and restart."
                )
            if abs_skew > CLOCK_SKEW_WARN_S:
                _LOGGER.warning(
                    "HA host clock skew %.0fs vs Supabase — telemetry "
                    "may be rejected; recommend NTP", skew_s,
                )
        except (TypeError, ValueError) as exc:
            _LOGGER.debug("could not parse server Date header: %s", exc)

    # ─── RPC helpers ─────────────────────────────────────────────────

    async def rpc(self, name: str, body: Optional[dict[str, Any]] = None) -> Any:
        """Call a Supabase RPC by name. Auto-refresh on 401."""
        url = f"{self._supabase_url}/rest/v1/rpc/{name}"
        try:
            async with self._post(
                url,
                body=body or {},
                headers=self._auth_headers(),
            ) as resp:
                if resp.status == 401:
                    # Try a token refresh and retry once.
                    await self.refresh_access_token()
                    async with self._post(
                        url,
                        body=body or {},
                        headers=self._auth_headers(),
                    ) as resp2:
                        return await self._json_or_raise(resp2, f"rpc:{name}")
                return await self._json_or_raise(resp, f"rpc:{name}")
        except aiohttp.ClientError as exc:
            raise AuthError(f"network error calling rpc:{name}: {exc}") from exc

    # ─── Specific RPCs the integration uses ──────────────────────────

    async def publish_lan_snapshot(self, shape: SystemShape) -> Any:
        return await self.rpc("publish_lan_snapshot", {"p_snapshot": shape.payload})

    async def publish_device_telemetry(
        self, readings: list[CanonicalReading]
    ) -> int:
        rows = [r.to_payload() for r in readings]
        result = await self.rpc("publish_device_telemetry", {"p_rows": rows})
        if isinstance(result, int):
            return result
        return 0

    async def get_my_pi_role(self) -> Optional[PublisherRole]:
        result = await self.rpc("get_my_pi_role")
        # RPC returns a table; depending on PostgREST shape this can be
        # a list of dicts, a dict, or a scalar.
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                value = first.get("role") or first.get("get_my_pi_role")
                return self._coerce_role(value)
        if isinstance(result, dict):
            value = result.get("role") or result.get("get_my_pi_role")
            return self._coerce_role(value)
        return self._coerce_role(result)

    async def fetch_my_binding(self) -> Optional[dict[str, Any]]:
        """Return the caller's pi_publisher_bindings summary.

        Uses the SECURITY DEFINER RPC `get_my_pi_binding()` (migration
        065) because the `pi_bindings_owner_select` RLS policy from
        migration 033 restricts SELECT on the table to tenant owners
        — publisher users (Pi / HA kind) cannot read their own row
        directly. The RPC returns the minimal fields the runtime
        needs (tenant_id, role, serial_at_bind, bound_at), following
        the same pattern as `get_my_pi_role()` from migration 046.

        Returns None when no binding exists for this user yet
        (pre-claim state — e.g. a freshly-bootstrapped HA user awaiting
        adoption).
        """
        result = await self.rpc("get_my_pi_binding")
        if isinstance(result, list) and result:
            row = result[0]
            if isinstance(row, dict):
                return row
        if isinstance(result, dict):
            return result
        return None

    async def fetch_live_snapshot(self, tenant_id: str) -> Optional[dict[str, Any]]:
        """Fetch the tenant's live_snapshot row. Monitor's data source.

        Returns the row dict (snapshot, updated_at, source) or None
        if there is no row yet. Raises AuthError on transport / auth
        problems.
        """
        rows = await self._rest_get_params(
            "live_snapshot",
            {
                "tenant_id": f"eq.{tenant_id}",
                "select": "snapshot,updated_at,source",
                "limit": "1",
            },
        )
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def claim_pi_by_serial(self, serial: str) -> Optional[str]:
        """Returns tenant_id on claim, None on 'no tenant claims yet'.

        Raises on other errors.
        """
        try:
            result = await self.rpc("claim_pi_by_serial", {"p_serial": serial})
        except AuthError as exc:
            if "no tenant claims" in str(exc):
                return None
            raise
        if isinstance(result, list) and result:
            return result[0]
        if isinstance(result, str):
            return result
        return None

    async def update_pi_diagnostics(
        self,
        installed_versions: Optional[dict[str, Any]] = None,
        client_info: Optional[dict[str, Any]] = None,
    ) -> Any:
        return await self.rpc(
            "update_pi_diagnostics",
            {
                "p_versions": installed_versions,
                "p_client_info": client_info,
            },
        )

    async def register_discovery(self, ident: InverterIdentity) -> None:
        url = self._vercel_base + PI_DISCOVERY_REGISTER_PATH
        body = {
            "inverter_serial": ident.inverter_serial,
            "inverter_model": ident.inverter_model,
            "battery_serial": ident.battery_serial,
            "dongle_serial": ident.dongle_serial,
        }
        async with self._post(
            url, body=body, headers=self._auth_headers()
        ) as resp:
            await self._json_or_raise(resp, "pi-discovery-register")

    # ─── Internal helpers ────────────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        if not self._access_token:
            raise AuthError("no access token; sign in first")
        return {
            "apikey": self._anon_key,
            "Authorization": f"Bearer {self._access_token}",
        }

    def _coerce_role(self, value: Any) -> Optional[PublisherRole]:
        if value in {"master", PublisherRole.MASTER}:
            return PublisherRole.MASTER
        if value in {"monitor", PublisherRole.MONITOR}:
            return PublisherRole.MONITOR
        return None

    def _post(self, url: str, *, body: dict[str, Any], headers: Optional[dict] = None):
        if self._session is None:
            raise RuntimeError("CloudClient not started")
        merged_headers = {"Content-Type": "application/json"}
        if headers:
            merged_headers.update(headers)
        return self._session.post(url, data=json.dumps(body), headers=merged_headers)

    def _get(self, url: str, *, headers: Optional[dict] = None):
        if self._session is None:
            raise RuntimeError("CloudClient not started")
        return self._session.get(url, headers=headers or {})

    async def _rest_get(self, path: str, query: str) -> Any:
        """PostgREST GET with auto-refresh on 401.

        `path` is the table or RPC name (e.g. 'live_snapshot');
        `query` is the URL-encoded selector + filters
        (e.g. 'tenant_id=eq.UUID&select=*&limit=1').

        Prefer `_rest_get_params` for new callers — it URL-encodes the
        values via aiohttp. This raw-query variant is retained for
        cases where the filter syntax can't be expressed as a flat
        dict (e.g. `or=(...)`).
        """
        url = f"{self._supabase_url}/rest/v1/{path}?{query}"
        try:
            async with self._get(url, headers=self._auth_headers()) as resp:
                if resp.status == 401:
                    await self.refresh_access_token()
                    async with self._get(url, headers=self._auth_headers()) as resp2:
                        return await self._json_or_raise(resp2, f"rest_get:{path}")
                return await self._json_or_raise(resp, f"rest_get:{path}")
        except aiohttp.ClientError as exc:
            raise AuthError(f"network error fetching {path}: {exc}") from exc

    async def _rest_get_params(
        self, path: str, params: dict[str, str]
    ) -> Any:
        """PostgREST GET with auto-refresh on 401, params-based.

        Same as `_rest_get` but URL-encodes the query parameters via
        aiohttp `params={...}` instead of taking a pre-built query
        string. Preferred for any filter whose value originates from
        external data (tenant UUIDs, LAN-sourced serials, etc.) to
        prevent characters in the value from being interpreted as
        PostgREST operators.
        """
        if self._session is None:
            raise RuntimeError("CloudClient not started")
        url = f"{self._supabase_url}/rest/v1/{path}"
        try:
            async with self._session.get(
                url, params=params, headers=self._auth_headers(),
            ) as resp:
                if resp.status == 401:
                    await self.refresh_access_token()
                    async with self._session.get(
                        url, params=params, headers=self._auth_headers(),
                    ) as resp2:
                        return await self._json_or_raise(resp2, f"rest_get:{path}")
                return await self._json_or_raise(resp, f"rest_get:{path}")
        except aiohttp.ClientError as exc:
            raise AuthError(f"network error fetching {path}: {exc}") from exc

    async def _json_or_raise(
        self, resp: aiohttp.ClientResponse, label: str
    ) -> Any:
        if resp.status >= 400:
            # Don't dump 500 chars of the server's response body into
            # the raised exception (callers log it at WARNING). Some
            # PostgREST errors include row data; an HA log scraper or
            # a copy-pasted bug report shouldnt carry it.
            #
            # Keep a SHORT snippet (~80 chars of the first line) in the
            # exception itself — claim_pi_by_serial() pattern-matches
            # the substring "no tenant claims" out of the message to
            # tell "not adopted yet" from a real error, and breaking
            # that match would break new-Pi onboarding. 80 chars fits
            # PostgRESTs `message` field but cant fit a row dump.
            # Full body still goes to DEBUG.
            body = await resp.text()
            _LOGGER.debug(
                "%s HTTP %s body (truncated): %s",
                label, resp.status, body[:500],
            )
            first_line = body.strip().splitlines()[0] if body.strip() else ""
            snippet = first_line[:80]
            if snippet:
                raise AuthError(f"{label} HTTP {resp.status}: {snippet}")
            raise AuthError(f"{label} HTTP {resp.status}")
        try:
            return await resp.json()
        except aiohttp.ContentTypeError:
            text = await resp.text()
            if not text:
                return None
            return json.loads(text)
