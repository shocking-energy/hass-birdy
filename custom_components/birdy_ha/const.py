"""Constants for the Birdy HA master integration.

`Birdy` is the product name; `Shocking Energy` is the company. The
HA domain slug `birdy_ha` is suffixed to disambiguate from the cloud
`birdy` AI assistant that lives under the same brand on the web
dashboard.
"""

from __future__ import annotations

# Integration domain. Must match manifest.json's `domain`.
DOMAIN = "birdy_ha"

# Display strings.
MANUFACTURER = "GivEnergy"   # hardware maker — unchanged.
DEFAULT_DEVICE_NAME = "Birdy"

# ─── Network / discovery ─────────────────────────────────────────────

MODBUS_PORT = 8899
LAN_SCAN_TIMEOUT_S = 1.5
LAN_SCAN_MAX_WORKERS = 64

# ─── Polling cadence ─────────────────────────────────────────────────

TELEMETRY_POLL_INTERVAL_S = 15
SETTINGS_POLL_INTERVAL_S = 300  # 5 min; configurable via HA options
SETTINGS_WRITE_CONFIRM_DELAY_S = 8
SETTINGS_WRITE_RATE_LIMIT_PER_MIN = 6  # per setting

# Background role-refresh cadence + setup-time bounded Modbus probe.
# Keep INITIAL_PROBE_TIMEOUT_S strictly below HA's per-entry setup
# budget (240 s) so a contested dongle never fails async_setup_entry,
# but high enough that a slow first refresh (the multi-pack
# `refresh_plant(full_refresh=True)` requests ~15 frames, each with
# ~250-400 ms response latency, totalling 4-6 s on a clean dongle and
# more after a connection thrash) actually completes. The poll loop
# retries within TELEMETRY_POLL_INTERVAL_S anyway if it times out.
ROLE_REFRESH_INTERVAL_S = 60
INITIAL_PROBE_TIMEOUT_S = 30

# ─── Inter-Modbus-call gap ────────────────────────────────────────────
# The Modbus-YY relay is single-shot; concurrent calls return the
# -1 sentinel. Matches _ge-settings-fetch.js's 80 ms gap.
MODBUS_INTER_CALL_GAP_S = 0.08

# ─── Reading freshness ───────────────────────────────────────────────
# CanonicalReading rejects readings older than this (S10 client-side
# defense; server now also clamps to ±5 min per migration 064).
READING_MAX_AGE_S = 60

# Clock-skew sanity check threshold (gotcha #7). Logged + surfaced as
# persistent_notification if HA host clock differs from the Supabase
# server-time-header by more than this.
CLOCK_SKEW_WARN_S = 30
CLOCK_SKEW_FAIL_S = 300

# ─── Cloud endpoints ─────────────────────────────────────────────────

SUPABASE_URL = "https://intgpyoqfazxaciuiito.supabase.co"
# Anon key is intentionally embedded — it's public-by-design (used by
# the browser too). Server-side RLS does the real protection.
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImludGdweW9xZmF6eGFjaXVpaXRvIiwi"
    "cm9sZSI6ImFub24iLCJpYXQiOjE3NzYzNTE0MDYsImV4cCI6MjA5MTkyNzQwNn0."
    "fthKB7F8OnLR3vkjfqaJOEl4bDsVzT1xDCnO-XI62YA"
)

VERCEL_BASE_URL = "https://energy-monitor-six.vercel.app"
HA_BOOTSTRAP_PATH = "/api/ha-bootstrap"
PI_DISCOVERY_REGISTER_PATH = "/api/pi-discovery-register"

# ─── Feature flags ───────────────────────────────────────────────────

# FEATURE_CONTROL_ENTITIES — gates the 16 inverter control entities
# (switches, numbers, times). Default True per the 2026-05-28
# scoping pass. If Modbus writes fail on real hardware (A13
# falsified), flip this to False, re-release, read sensors keep
# working. Env override BIRDY_HA_CONTROLS_ENABLED lets users
# disable without waiting for a release.
FEATURE_CONTROL_ENTITIES = True
CONTROL_ENTITY_OVERRIDE_ENV = "BIRDY_HA_CONTROLS_ENABLED"

# ─── Config flow keys ────────────────────────────────────────────────

CONF_INTEGRATION_ID = "integration_id"
CONF_INVERTER_HOST = "inverter_host"
CONF_CLAIM_CODE = "claim_code"
CONF_TENANT_ID = "tenant_id"

# ─── Allowed brand ───────────────────────────────────────────────────

INVERTER_BRAND = "givenergy"

# Tenants this integration must NEVER write to (R2 — Kevin Home).
# Defensive: if a misconfigured install somehow ended up bound to
# Kevin's tenant, every cloud write is short-circuited.
#
# TODO(maintainer): confirm real home tenant UUID. The value below
# looks like a placeholder (sequential nibbles). If Kevin Home's real
# tenant_id is something else, _assert_not_forbidden_tenant protects
# nothing and the actual UUID should replace this. If R2 is being
# enforced server-side instead (RLS / publisher binding scope), this
# whole guard can be removed.
#
# Flagged by 2026-05-29 security review (finding L1).
FORBIDDEN_TENANT_IDS = frozenset(
    {"00000000-0000-0000-0000-000000000001"}
)

# ─── HA Energy Dashboard mapping ─────────────────────────────────────
# Entity keys that get device_class=energy + state_class=total_increasing
# so they plug directly into HA's Energy dashboard.
ENERGY_DASHBOARD_KEYS = frozenset({
    "solar_energy_today",
    "grid_import_energy_today",
    "grid_export_energy_today",
    "house_energy_today",
    "battery_charge_energy_today",
    "battery_discharge_energy_today",
})

# ─── The 16 controllable settings ────────────────────────────────────
# Maps canonical setting key → metadata. Sample values for the
# round-trip phase-0 test; min/max for the HA NumberEntity. See
# ha-integration/tests/phase0/test_modbus_write_roundtrip.py for the
# library-method bindings.
#
# The boolean for ecoMode is one-way-tested in phase 0 because the
# library's set_mode_storage requires slot args; production
# integration handles both directions cleanly using the user's
# configured slots.

CONTROL_KEY_BOOLEAN = "boolean"
CONTROL_KEY_NUMBER = "number"
CONTROL_KEY_TIME = "time"

# GE inverter charge/discharge rate registers (`battery_charge_limit`,
# `battery_discharge_limit`) are set as an integer 0-50 (the library's
# valid range). On the GIV-HY5.0 (5.0 kW inverter) the register maps
# linearly to inverter power at ~100 W per unit — raw 50 = 5000 W.
# VERIFIED on David's GIV-HY5.0 (2026-07-17): raw 10 → 995 W discharge /
# 968 W charge. The previous 52 W/unit was WRONG — it assumed a 0-100
# range (5200/100); the real range is 0-50 → ~100 W/unit, so the shown
# watts read ~1.9x too LOW and a "1 kW" setting actually pushed ~2 kW.
# Above ~raw 26 a single GIV-BAT-9.5 saturates at its ~2.6 kW cell-current
# ceiling (a battery limit, not the inverter's). Display-only: the write
# path sends the raw register value unchanged. For non-HY5.0 models the
# multiplier differs; derive from device_type_code when one appears.
CHARGE_RATE_W_PER_RAW = 100
CHARGE_RATE_MAX_W = 50 * CHARGE_RATE_W_PER_RAW  # 5000 W (inverter rating)

CONTROL_ENTITY_KEYS: dict[str, dict] = {
    # ── Switches (boolean) ──
    "acCharge": {"type": CONTROL_KEY_BOOLEAN, "icon": "mdi:battery-arrow-up"},
    "acChargeUpperEnabled": {
        "type": CONTROL_KEY_BOOLEAN,
        "icon": "mdi:battery-charging-100",
    },
    "dcDischarge": {
        "type": CONTROL_KEY_BOOLEAN,
        "icon": "mdi:battery-arrow-down",
    },
    "ecoMode": {"type": CONTROL_KEY_BOOLEAN, "icon": "mdi:leaf"},
    # ── Numbers ──
    "batteryReserve": {
        "type": CONTROL_KEY_NUMBER,
        "min": 4,
        "max": 100,
        "step": 1,
        "unit": "%",
        "icon": "mdi:battery-50",
    },
    "batteryCutoff": {
        "type": CONTROL_KEY_NUMBER,
        "min": 4,
        "max": 100,
        "step": 1,
        "unit": "%",
        "icon": "mdi:battery-alert",
    },
    # Exposed in amps (0-50) = the native battery current-limit register 1:1
    # (also the library's valid range; ~52 W/A nominal). The number entity
    # surfaces the calculated watts as a `calculated_power_w` attribute.
    "chargeRate": {
        "type": CONTROL_KEY_NUMBER,
        "min": 0,
        "max": 50,
        "step": 1,
        "unit": "A",
        "icon": "mdi:battery-arrow-up",
    },
    "dischargeRate": {
        "type": CONTROL_KEY_NUMBER,
        "min": 0,
        "max": 50,
        "step": 1,
        "unit": "A",
        "icon": "mdi:battery-arrow-down",
    },
    "acChargeLimit": {
        "type": CONTROL_KEY_NUMBER,
        "min": 4,
        "max": 100,
        "step": 1,
        "unit": "%",
        "icon": "mdi:battery-charging-high",
    },
    "maxOutputPowerPct": {
        "type": CONTROL_KEY_NUMBER,
        "min": 0,
        "max": 100,
        "step": 1,
        "unit": "%",
        "icon": "mdi:flash",
    },
    # ── Times ──
    "acChargeStart": {"type": CONTROL_KEY_TIME, "icon": "mdi:clock-start"},
    "acChargeEnd": {"type": CONTROL_KEY_TIME, "icon": "mdi:clock-end"},
    "dcDischarge1Start": {"type": CONTROL_KEY_TIME, "icon": "mdi:clock-start"},
    "dcDischarge1End": {"type": CONTROL_KEY_TIME, "icon": "mdi:clock-end"},
    "dcDischarge2Start": {"type": CONTROL_KEY_TIME, "icon": "mdi:clock-start"},
    "dcDischarge2End": {"type": CONTROL_KEY_TIME, "icon": "mdi:clock-end"},
}

# Human-readable names for HA UI. Match the casing the dashboard's
# Installation page uses for consistency.
CONTROL_ENTITY_NAMES = {
    "acCharge": "AC charge enabled",
    "acChargeUpperEnabled": "AC charge target enabled",
    "acChargeStart": "AC charge start",
    "acChargeEnd": "AC charge end",
    "acChargeLimit": "AC charge limit",
    "dcDischarge": "DC discharge enabled",
    "dcDischarge1Start": "DC discharge 1 start",
    "dcDischarge1End": "DC discharge 1 end",
    "dcDischarge2Start": "DC discharge 2 start",
    "dcDischarge2End": "DC discharge 2 end",
    "batteryReserve": "Battery reserve",
    "batteryCutoff": "Battery cutoff",
    # Renamed in 0.8.8 — these registers (HR111/HR112) only govern
    # charging/discharging during the AC charge window, NOT general
    # solar charging or DC discharge. Old name "Charge rate"
    # implied a universal cap and confused users.
    "chargeRate": "AC charge rate",
    "dischargeRate": "AC discharge rate",
    "ecoMode": "Eco mode",
    "maxOutputPowerPct": "Max output power",
}

# ─── Read sensor catalogue ───────────────────────────────────────────
# Maps (component, metric) → HA entity descriptor. Driven by the
# canonical readings the driver emits (see drivers/givenergy/mappings.py
# in pi-daemon for the original 13-row list).

READ_SENSOR_KEYS: list[dict] = [
    {"component": "grid", "metric": "power_w", "name": "Grid power",
     "device_class": "power", "unit": "W", "icon": "mdi:transmission-tower"},
    {"component": "battery", "metric": "power_w", "name": "Battery power",
     "device_class": "power", "unit": "W", "icon": "mdi:battery-charging"},
    {"component": "battery", "metric": "soc_pct", "name": "Battery SOC",
     "device_class": "battery", "unit": "%", "icon": "mdi:battery"},
    {"component": "solar", "metric": "power_w", "name": "Solar power",
     "device_class": "power", "unit": "W", "icon": "mdi:solar-power"},
    {"component": "house", "metric": "power_w", "name": "House power",
     "device_class": "power", "unit": "W", "icon": "mdi:home-lightning-bolt"},
    {"component": "solar_string_1", "metric": "power_w",
     "name": "Solar string 1", "device_class": "power", "unit": "W",
     "icon": "mdi:solar-panel"},
    {"component": "solar_string_2", "metric": "power_w",
     "name": "Solar string 2", "device_class": "power", "unit": "W",
     "icon": "mdi:solar-panel"},
    {"component": "solar", "metric": "energy_today_kwh",
     "name": "Solar energy today", "device_class": "energy",
     "unit": "kWh", "icon": "mdi:solar-power"},
    {"component": "grid_import", "metric": "energy_today_kwh",
     "name": "Grid import energy today", "device_class": "energy",
     "unit": "kWh", "icon": "mdi:transmission-tower-import"},
    {"component": "grid_export", "metric": "energy_today_kwh",
     "name": "Grid export energy today", "device_class": "energy",
     "unit": "kWh", "icon": "mdi:transmission-tower-export"},
    {"component": "house", "metric": "energy_today_kwh",
     "name": "House energy today", "device_class": "energy",
     "unit": "kWh", "icon": "mdi:home"},
    {"component": "battery_charge", "metric": "energy_today_kwh",
     "name": "Battery charge energy today", "device_class": "energy",
     "unit": "kWh", "icon": "mdi:battery-plus"},
    {"component": "battery_discharge", "metric": "energy_today_kwh",
     "name": "Battery discharge energy today", "device_class": "energy",
     "unit": "kWh", "icon": "mdi:battery-minus"},
]
