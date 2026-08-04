"""
Port of the original shape.js — maps the Python givenergy-modbus library's
Plant/Inverter model onto the `live_snapshot.snapshot` JSONB shape the
energy-monitor frontend expects (see frontend/api/_ge-client.js#assembleSystem
and EnergyFlow.jsx#snapshotToState).

Sign conventions match the GivEnergy cloud API:
  +ve gridPower    = export
  +ve batteryPower = discharge

The Python library uses the *same* convention (verified by power-balance
against live device: solar - load - grid_export - battery_charge ≈ 0 when
p_grid_out is treated as +ve=export and p_battery as +ve=discharge), so
no inversion is needed. Don't "fix" the cloud convention — the frontend
already inverts once to Shelly convention and relies on this.
"""

import datetime
import math
from typing import Any, Dict


def _num(v, default=0):
    if v is None:
        return default
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except (TypeError, ValueError):
        return default


# Raw-register garbage backstop. A garbled or partial Modbus read can
# return uninitialised register windows that decode to astronomically
# large powers (raw uint16/uint32 bit patterns). This ceiling sits FAR
# above any real inverter — including future large commercial units — so
# it only ever rejects garbage, never a legitimately big installation.
# It is deliberately NOT an inverter-rating limit: the size-independent
# integrity check is the SOC gate below.
_RAW_GARBAGE_POWER_W = 1_000_000.0


def corrupt_snapshot_reason(snapshot):
    """Return a reason string if `snapshot` looks like a corrupt Modbus
    read, else None.

    The integrity signal is battery SOC: a percentage with a hard
    physical range [0, 100] regardless of inverter size, so a value
    outside that range means the register block decoded to garbage.
    Empirically (David's GIV-HY-5.0, 2026-06-05) a single garbled poll
    corrupts battery SOC, battery power and grid power together — they
    share one Modbus read block — while solar/house (a different block)
    stay valid. Gating the whole frame on SOC therefore drops the
    correlated garbage without bounding power by inverter rating.
    """
    if not isinstance(snapshot, dict):
        return "snapshot is not a dict"
    bat = snapshot.get("battery") or {}
    soc = bat.get("soc") if isinstance(bat, dict) else None
    if isinstance(soc, (int, float)) and not (0 <= soc <= 100):
        return f"battery SOC {soc} outside physical [0,100] — corrupt read"
    for comp, key in (("battery", "power"), ("grid", "power"),
                      ("solar", "power"), ("house", "load")):
        block = snapshot.get(comp) or {}
        v = block.get(key) if isinstance(block, dict) else None
        if isinstance(v, (int, float)) and abs(v) >= _RAW_GARBAGE_POWER_W:
            return f"{comp}.{key} {v} W exceeds raw-register backstop — corrupt read"
    return None


def _model_name(code: Any) -> str:
    c = int(_num(code, 0))
    # GivEnergy legacy (YY-wrapped) model codes (Hi-Flying dongle path).
    if 0x0800 <= c <= 0x08FF:
        return "GIV-HY (gen3)"
    if 0x0300 <= c <= 0x03FF:
        return "GIV-HY (gen2)"
    if 0x0400 <= c <= 0x04FF:
        return "GIV-AC"
    if 0x0600 <= c <= 0x06FF:
        return "GIV-3PH"
    # Solis-OEM model codes (Gen3 / AIO units that GE rebadges; reached
    # via the raw Modbus-TCP transport on port 502, register 33000).
    # Liz Gallop's inverter reports 0x4102 — empirically a single-phase
    # hybrid storage unit. Without a vendor-published mapping for the
    # full 0x41xx-0x42xx range, group them under one friendly name.
    if 0x4100 <= c <= 0x42FF:
        return "Solis Hybrid (Gen3)"
    if 0x4300 <= c <= 0x44FF:
        return "Solis 3-phase Hybrid"
    return f"GivEnergy (0x{c:04x})"


def _status(battery_power: float) -> str:
    if battery_power > 50:
        return "DISCHG"
    if battery_power < -50:
        return "CHARGING"
    return "NORMAL"


def _slot_hhmm(t) -> str:
    return t.strftime("%H:%M") if hasattr(t, "strftime") else "00:00"


def _eco_mode_from(enable_discharge) -> bool:
    """`enable_discharge == False` = dynamic (eco on);
    `enable_discharge == True` = storage / timed (eco off).

    NOT `battery_power_mode` — that register (HR27) is mislabelled
    "eco mode" in the library; in practice both set_mode_dynamic and
    set_mode_storage write `match_demand` (=1) to it, so r27 can't
    distinguish them. The differentiator is r59 (enable_discharge).
    Mirrors settings.SettingsAdapter so live_snapshot and the
    eco_mode switch agree.
    """
    if enable_discharge is None:
        return False
    return not bool(enable_discharge)


def _scrub_nuls(obj):
    """Postgres can't store \\u0000 in a TEXT/JSONB column — it raises
    `22P05: \\u0000 cannot be converted to text`. The Modbus seed-and-retry
    path can return string registers (firmware versions, serials) with
    embedded NUL bytes when a partial read is recovered. Walk the snapshot
    once after build and strip them. Cheap (<10 keys deep, <100 leaf nodes)
    and saves the entire poll cycle from being rejected by Supabase."""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _scrub_nuls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_nuls(v) for v in obj]
    return obj


def build_snapshot_row(plant, opts: Dict[str, Any]) -> Dict[str, Any]:
    inv = plant.inverter
    try:
        bats = list(plant.batteries) if plant.batteries else []
    except (ValueError, KeyError):
        # A corrupt battery frame (e.g. BatteryCalibrationStage=40634)
        # would otherwise abort the whole snapshot. Degrade to no-battery
        # detail for this cycle rather than also losing solar/grid/house.
        bats = []
    first_bat = bats[0] if bats else None

    battery_power = _num(getattr(inv, "p_battery", 0))
    grid_power = _num(getattr(inv, "p_grid_out", 0))
    solar_power = _num(getattr(inv, "p_pv1", 0)) + _num(getattr(inv, "p_pv2", 0))

    # In givenergy-modbus 1.x these are TimeSlot objects with .start/.end;
    # in 0.x they were (start, end) tuples. Normalise to (start, end).
    def _slot_pair(get):
        # `get` is a thunk so the library property access — which decodes
        # HHMM registers into time objects and RAISES ValueError on a
        # corrupt register (e.g. time(hour=176)) — happens INSIDE the
        # guard. getattr(inv, ..., None) alone does NOT catch this; its
        # default only suppresses AttributeError, not the getter's raise.
        try:
            value = get()
        except (ValueError, KeyError, TypeError):
            return (None, None)
        if value is None:
            return (None, None)
        if isinstance(value, tuple):
            return value
        return (getattr(value, "start", None), getattr(value, "end", None))

    charge_slot = _slot_pair(lambda: getattr(inv, "charge_slot_1", None))
    discharge_slot_1 = _slot_pair(lambda: getattr(inv, "discharge_slot_1", None))
    discharge_slot_2 = _slot_pair(lambda: getattr(inv, "discharge_slot_2", None))

    arm_fw = getattr(inv, "arm_firmware_version", "") or ""
    dsp_fw = getattr(inv, "dsp_firmware_version", "") or ""
    firmware = f"{arm_fw}/{dsp_fw}" if (arm_fw or dsp_fw) else ""

    # In givenergy-modbus 1.x the per-battery capacity registers are
    # `cap_design` (IR 86-87) and `cap_calibrated` (IR 84-85) — values
    # returned in Ah (centi converter divides by 100). The 0.x names
    # `battery_design_capacity` / `battery_full_capacity` are kept as a
    # fallback so an older library would still populate (currently
    # nothing in this repo pins to 0.x, but the cost of the fallback
    # is one extra getattr per battery).
    def _bat_cap(b, primary: str, fallback: str) -> float:
        v = getattr(b, primary, None)
        if v is None:
            v = getattr(b, fallback, 0)
        return _num(v)

    capacity_design = sum(_bat_cap(b, "cap_design", "battery_design_capacity") for b in bats)
    capacity_full = sum(_bat_cap(b, "cap_calibrated", "battery_full_capacity") for b in bats)
    health = round((capacity_full / capacity_design) * 1000) / 10 if capacity_design > 0 else 0
    # Nominal 51.2V per HV module; matches shape.js total_kwh estimation.
    total_kwh = sum((_bat_cap(b, "cap_design", "battery_design_capacity") * 51.2) / 1000 for b in bats)

    system_time = getattr(inv, "system_time", None)
    if isinstance(system_time, datetime.datetime):
        system_time_iso = system_time.isoformat()
    else:
        system_time_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    e_pv_today = _num(getattr(inv, "e_pv1_day", 0)) + _num(getattr(inv, "e_pv2_day", 0))
    work_seconds = _num(getattr(inv, "work_time_total", 0))

    return _scrub_nuls({
        "inverter": {
            # 1.x renames: inverter_serial_number → serial_number;
            # temp_inverter_heatsink → t_charger; p_inverter_out doesn't
            # exist in 1.x, derive from p_pv + p_battery + p_grid_out.
            "serial": getattr(inv, "serial_number", "") or "",
            "model": _model_name(getattr(inv, "device_type_code", 0)),
            "status": _status(battery_power),
            "firmware": firmware,
            "temp": _num(getattr(inv, "t_charger", 0)),
            "power": _num(
                getattr(inv, "p_inverter_out", None)
                or (
                    _num(getattr(inv, "p_pv1", 0))
                    + _num(getattr(inv, "p_pv2", 0))
                    + _num(getattr(inv, "p_battery", 0))
                    - _num(getattr(inv, "p_grid_out", 0))
                )
            ),
            "outputV": _num(getattr(inv, "v_ac1", 0)),
            "outputHz": _num(getattr(inv, "f_ac1", 0)),
            "commissioned": opts.get("commissioned_date") or "",
            "warrantyExpiry": opts.get("warranty_expiry") or "",
        },
        "battery": {
            # 1.x renames: battery_percent → battery_soc;
            # temp_battery → t_battery; battery_serial_number on Battery
            # → serial_number; battery_num_cells → num_cells; for the
            # inverter's aggregate first_battery_serial_number lives.
            "serial": (
                (getattr(first_bat, "serial_number", "")
                 or getattr(first_bat, "battery_serial_number", ""))
                if first_bat else ""
            ) or getattr(inv, "first_battery_serial_number", "") or "",
            "soc": _num(getattr(inv, "battery_soc", 0)),
            "power": battery_power,
            "voltage": _num(getattr(inv, "v_battery", 0)),
            "temp": _num(getattr(inv, "t_battery", 0)),
            "health": health,
            "capacityFull": capacity_full,
            "capacityDesign": capacity_design,
            "cells": int(_num(
                (getattr(first_bat, "num_cells", 0)
                 or getattr(first_bat, "battery_num_cells", 0))
                if first_bat else 0
            )),
            "firmware": str(
                (getattr(first_bat, "bms_firmware_version", "")
                 or getattr(first_bat, "firmware_version", ""))
                if first_bat else ""
            ),
            "totalKwh": total_kwh,
        },
        "solar": {
            "power": solar_power,
            "string1": {
                "v": _num(getattr(inv, "v_pv1", 0)),
                "a": _num(getattr(inv, "i_pv1", 0)),
                "w": _num(getattr(inv, "p_pv1", 0)),
            },
            "string2": {
                "v": _num(getattr(inv, "v_pv2", 0)),
                "a": _num(getattr(inv, "i_pv2", 0)),
                "w": _num(getattr(inv, "p_pv2", 0)),
            },
            "todayKwh": e_pv_today,
            "lifetimeKwh": _num(getattr(inv, "e_pv_total", 0)),
        },
        "grid": {
            "power": grid_power,
            "voltage": _num(getattr(inv, "v_ac1", 0)),
            "current": _num(getattr(inv, "i_grid_port", 0)),
            "frequency": _num(getattr(inv, "f_ac1", 0)),
            "importTodayKwh": _num(getattr(inv, "e_grid_in_day", 0)),
            "exportTodayKwh": _num(getattr(inv, "e_grid_out_day", 0)),
            "importLifetimeKwh": _num(getattr(inv, "e_grid_in_total", 0)),
            "exportLifetimeKwh": _num(getattr(inv, "e_grid_out_total", 0)),
            # Peak / off-peak splits are computed cloud-side against tariff
            # windows. The Pi doesn't have Supabase access, so leave null.
            "importPeakTodayKwh": None,
            "importOffpeakTodayKwh": None,
            "exportPeakTodayKwh": None,
        },
        "house": {
            "load": _num(getattr(inv, "p_load_demand", 0)),
        },
        "meter": {
            # House consumption = (inverter AC output - grid export) + grid import.
            # The GivEnergy Modbus library doesn't expose a direct house-consumption
            # counter — the inverter doesn't carry that register. Compute it from
            # the AC-port energy balance:
            #   e_inverter_out_day = energy leaving the inverter's AC port (→ house or grid)
            #   e_grid_out_day     = energy exported to grid (subtract: not house)
            #   e_grid_in_day      = energy imported from grid (add: also reaches house)
            # Verified against the GE Cloud API's reported meter.consumptionTodayKwh
            # on David's GIV-HY-5.0: formula matches within ~0.1 kWh after a full day.
            # Before this, the Pi was writing e_inverter_out_day directly, which
            # overstated house by the day's grid export (~10 kWh on a sunny day).
            "consumptionTodayKwh": (
                _num(getattr(inv, "e_inverter_out_day", 0))
                - _num(getattr(inv, "e_grid_out_day", 0))
                + _num(getattr(inv, "e_grid_in_day", 0))
            ),
            "consumptionLifetimeKwh": (
                _num(getattr(inv, "e_inverter_out_total", 0))
                - _num(getattr(inv, "e_grid_out_total", 0))
                + _num(getattr(inv, "e_grid_in_total", 0))
            ),
            "batteryChargeTodayKwh": _num(getattr(inv, "e_battery_charge_day", 0)),
            "batteryDischargeTodayKwh": _num(getattr(inv, "e_battery_discharge_day", 0)),
            "batteryThroughputLifetimeKwh": _num(
                getattr(inv, "e_battery_throughput_total", None)
                or getattr(inv, "e_battery_throughput", 0)
            ),
            "acChargeTodayKwh": 0,
            "acChargeLifetimeKwh": 0,
        },
        "config": {
            # battery_power_mode == 1 → dynamic (eco on); 2 → storage
            # (eco off). Same logic the settings adapter uses to drive
            # the eco_mode switch so live_snapshot and the entity agree.
            "ecoMode": _eco_mode_from(getattr(inv, "enable_discharge", None)),
            # Single mutually-exclusive mode (matches settings.py's derivation):
            # r59 enable_discharge gates timed discharge; r27 battery_power_mode
            # picks export (0) vs match-demand (1). Rides live_snapshot so the
            # monitor + cloud (and Birdy's LAN settings fallback) see the real
            # mode, not the coupled ecoMode/dcDischarge pair.
            "batteryMode": (
                None if getattr(inv, "enable_discharge", None) is None
                else "eco" if not getattr(inv, "enable_discharge")
                else "timed_export"
                if _num(getattr(inv, "battery_power_mode", 1)) == 0
                else "timed_discharge"
            ),
            "acCharge": bool(getattr(inv, "enable_charge", False)),
            "acChargeStart": _slot_hhmm(charge_slot[0]),
            "acChargeEnd": _slot_hhmm(charge_slot[1]),
            "acChargeLimit": _num(getattr(inv, "charge_target_soc", 0)),
            "acChargeUpperEnabled": bool(getattr(inv, "enable_charge_target", False)),
            "dcDischarge": bool(getattr(inv, "enable_discharge", False)),
            "dcDischarge1Start": _slot_hhmm(discharge_slot_1[0]),
            "dcDischarge1End": _slot_hhmm(discharge_slot_1[1]),
            "dcDischarge2Start": _slot_hhmm(discharge_slot_2[0]),
            "dcDischarge2End": _slot_hhmm(discharge_slot_2[1]),
            # Same mapping as settings.py (fixed 2026-08-04 — batteryCutoff was
            # a hardcoded 0 and settings.py had the two registers crossed, so
            # the "Battery reserve" entity read HR110 here but WROTE HR114):
            #   batteryReserve = HR110 battery_soc_reserve (eco floor)
            #   batteryCutoff  = HR114 battery_discharge_min_power_reserve
            #                    (timed-discharge/export floor)
            "batteryReserve": _num(getattr(inv, "battery_soc_reserve", 0)),
            "batteryCutoff": _num(
                getattr(inv, "battery_discharge_min_power_reserve", 0)
            ),
            # The Modbus registers hold an integer 0-50 that maps to inverter
            # power at ~100 W/unit (raw 50 = 5 kW = the HY-5.0's rating).
            # Verified on David's HY-5.0 (2026-07-17): raw 10 → ~1 kW. A single
            # GIV-BAT-9.5 saturates at its ~2.6 kW cell-current ceiling around
            # raw 26 (that's a battery limit, not the register's). The old
            # 52 W/unit wrongly assumed raw 50 = 2.6 kW, reading ~1.9x too low.
            "chargeRate": _num(getattr(inv, "battery_charge_limit", 0)) * 100,
            "dischargeRate": _num(getattr(inv, "battery_discharge_limit", 0)) * 100,
            # `active_power_rate` is HR(50) on this inverter — 0-100%
            # active-power output curtailment. Pass through None when
            # the register isn't readable so the monitor-mode settings
            # projection in settings.py treats it as "unknown" rather
            # than masking it with a fabricated 100% default.
            "maxOutputPowerPct": (
                int(_num(getattr(inv, "active_power_rate"), 0))
                if getattr(inv, "active_power_rate", None) is not None
                else None
            ),
        },
        "site": {
            "name": opts.get("site_name") or "",
            "id": opts.get("site_id") or 0,
            "lat": _num(opts.get("latitude"), 0),
            "lng": _num(opts.get("longitude"), 0),
            "daysOnline": int(work_seconds // 86400),
        },
        "dongle": {
            "serial": opts.get("dongle_serial") or "",
            "type": opts.get("dongle_type") or "wifi",
        },
        # Forecast block. Populated from the response of
        # publish_lan_snapshot (migration 034 onwards) — kiosk reads
        # these to render the "<actual> / <expected>" format on the
        # SOLAR + HOUSE panels. Both keys are None until the first
        # successful publish round-trip; kiosk falls back to actual-
        # only display in that window.
        "meta": {
            "solarForecastTodayKwh": _num((opts.get("forecast") or {}).get("solar_today_kwh"), None),
            "houseExpectedDailyKwh": _num((opts.get("forecast") or {}).get("house_daily_kwh"), None),
        },
        "snapshot": system_time_iso,
    })
