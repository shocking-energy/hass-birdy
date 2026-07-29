"""Domain types for the Birdy HA master integration.

Value objects (frozen dataclasses) and enums for the bounded HA-Master
context. These types do not leak out to HA's entity registry — the
`hass_adapter` translates them. They do not leak in to the Modbus
driver either — the driver translates raw registers to these types.

See docs/architecture/ha-master-integration.md § Domain model for the
design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from .const import READING_MAX_AGE_S


class BindingState(str, Enum):
    """Lifecycle of an InverterBinding aggregate."""

    UNPROVISIONED = "unprovisioned"  # no auth user yet
    PROVISIONED = "provisioned"      # auth user but no tenant
    ADOPTED = "adopted"              # bound, role known
    PUBLISHING = "publishing"        # adopted + master, actively writing


class PublisherRole(str, Enum):
    """Role within the bound tenant."""

    MASTER = "master"
    MONITOR = "monitor"


@dataclass(frozen=True)
class InverterIdentity:
    """What the LAN-scan / Modbus discovery returned about the inverter."""

    inverter_serial: str
    inverter_model: Optional[str] = None
    battery_serial: Optional[str] = None
    dongle_serial: Optional[str] = None
    lan_host: Optional[str] = None  # IP we found it at


@dataclass(frozen=True)
class AuthCredentials:
    """Output of bootstrap_ha_master().

    Refresh token is the only thing that gets persisted; email +
    password are discarded after the initial token exchange.
    """

    user_id: str
    email: str
    refresh_token: str
    access_token: str
    access_token_expires_at: datetime


@dataclass(frozen=True)
class CanonicalReading:
    """One canonical reading row.

    Matches the schema of supabase.device_telemetry. Sign conventions:
      grid:    + import / - export
      battery: + charging / - discharging
      solar:   always +
      house:   always +
    The driver layer handles the sign flip from GE's native convention.
    """

    device_id: str
    component: str
    metric: str
    value: float
    unit: str
    recorded_at: datetime
    quality: str = "good"

    def __post_init__(self):
        # S10 client-side defense — recorded_at must be UTC and recent.
        # Server-side ±5 min clamp lands in migration 064.
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware (UTC)")
        if self.recorded_at.tzinfo.utcoffset(self.recorded_at) != \
                timezone.utc.utcoffset(self.recorded_at):
            raise ValueError("recorded_at must be UTC")
        age_s = (datetime.now(timezone.utc) - self.recorded_at).total_seconds()
        if age_s > READING_MAX_AGE_S:
            raise ValueError(
                f"recorded_at is {age_s:.0f}s old; must be <= "
                f"{READING_MAX_AGE_S}s"
            )

    def to_payload(self) -> dict[str, Any]:
        """Shape for publish_device_telemetry RPC."""
        return {
            "device_id": self.device_id,
            "component": self.component,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "quality": self.quality,
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True)
class SystemShape:
    """The live_snapshot.snapshot JSONB shape.

    Documented in docs/architecture/ge-dashboard-live-data-guide.md.
    This is the SAME shape the Pi writes; HA must match byte-for-byte
    so the existing frontend continues to work unchanged.
    """

    payload: dict[str, Any]  # opaque dict to keep parity with shape.py


@dataclass(frozen=True)
class SettingsSnapshot:
    """All 16 controllable settings (or as many as the round-trip
    test confirmed). Keys match CONTROL_ENTITY_KEYS in const.py.
    """

    values: dict[str, Any]
    asof: datetime


@dataclass
class InverterBinding:
    """Aggregate root for the integration's identity + role.

    Mutable across the integration's lifetime (refresh_role updates
    in place). All other domain types are immutable.
    """

    integration_id: str  # UUID v4
    state: BindingState = BindingState.UNPROVISIONED
    credentials: Optional[AuthCredentials] = None
    tenant_id: Optional[str] = None
    role: Optional[PublisherRole] = None
    inverter: Optional[InverterIdentity] = None
    last_role_check: Optional[datetime] = None
    last_published_at: Optional[datetime] = None
    # Opt-in: this HA is the SOLE local controller of its own inverter even
    # when it is a tenant MONITOR — i.e. a different agent (e.g. a Victron
    # pi-daemon) is the tenant master for a DIFFERENT inverter, but HA remains
    # the only thing writing this GivEnergy dongle. Resolved once at startup in
    # runtime (BIRDY_HA_LOCAL_CONTROL=1 or a `<config>/.birdy_local_control`
    # marker). Default False → control stays master-only, so GE-only /
    # HA-master installs are byte-for-byte unchanged. Only relaxes can_control;
    # can_publish (live_snapshot) stays master-only regardless.
    local_control: bool = False

    @property
    def is_master(self) -> bool:
        return self.role == PublisherRole.MASTER

    @property
    def can_publish(self) -> bool:
        return (
            self.state in {BindingState.ADOPTED, BindingState.PUBLISHING}
            and self.is_master
        )

    @property
    def can_control(self) -> bool:
        """Whether HA-initiated writes to the inverter should fire.

        Adopted + master always may. An adopted MONITOR may too, but ONLY
        when `local_control` is set (opt-in, default off) — the case where a
        different agent is the tenant master for a different inverter while
        HA stays the sole local controller of its own dongle. There is no
        write race in that topology (the tenant master never touches this
        inverter). Unprovisioned / provisioned states never control.

        NOTE: this deliberately does NOT gate on can_publish — a monitor with
        local_control controls its inverter but still must NOT write
        live_snapshot (that stays master-only via can_publish), so the tenant
        master keeps the single authoritative live view.
        """
        if self.state not in {BindingState.ADOPTED, BindingState.PUBLISHING}:
            return False
        return self.is_master or self.local_control


@dataclass
class WriteResult:
    """Result of apply_setting()."""

    key: str
    requested_value: Any
    observed_value: Optional[Any]
    accepted: bool
    error: Optional[str] = None
    reverted: bool = False
