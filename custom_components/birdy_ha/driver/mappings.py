"""GivEnergy → canonical reading mapping (Python side).

Mirrors frontend/api/drivers/givenergy/mappings.js. Single source of the
sign-flip + canonical-row construction logic on the Pi.

The library's `Plant.inverter` model uses GivEnergy's native API
conventions:
  - p_grid_out:  +ve = export
  - p_battery:   +ve = discharging

This file flips both at the boundary so everything downstream
(publish_device_telemetry RPC, App API consumers, Birdy) sees the
canonical contract:
  - grid:    +import / -export
  - battery: +charging / -discharging
  - solar:   always +

Phase 0 had this logic inlined in daemon.py#_publish_device_telemetry;
Phase 1 moves it here. Behaviour must remain bit-identical.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def snapshot_to_readings(
    snapshot: Dict[str, Any],
    *,
    device_id: str,
    recorded_at: str,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build canonical reading dicts from a SYSTEM-shape snapshot.

    The Pi calls build_snapshot_row(plant) to produce a SYSTEM-shape
    dict (matching what the cloud's fetchSystem returns), then this
    function converts it to canonical (component, metric) rows.

    Returns rows ready for publish_device_telemetry. Missing fields
    are skipped — not zero-filled — so a partial Modbus read doesn't
    publish a row of zeros that pollute the latest-cache.
    """
    if not isinstance(snapshot, dict):
        return []

    grid = snapshot.get("grid") or {}
    battery = snapshot.get("battery") or {}
    solar = snapshot.get("solar") or {}
    house = snapshot.get("house") or {}
    meter = snapshot.get("meter") or {}

    grid_w = _num(grid.get("power"))        # GE +ve = export → flip
    battery_w = _num(battery.get("power"))  # GE +ve = discharging → flip
    soc_pct = _num(battery.get("soc"))
    solar_w = _num(solar.get("power"))
    house_w = _num(house.get("load"))       # always +ve (consumption)
    string1_w = _num((solar.get("string1") or {}).get("w"))
    string2_w = _num((solar.get("string2") or {}).get("w"))

    # Today-counters (reset at local midnight; see CLAUDE.md sign quirks
    # for the 5-10 min post-midnight lag — readers handle via
    # trimToAfterDailyReset, not the publisher).
    solar_today = _num(solar.get("todayKwh"))
    grid_import_today = _num(grid.get("importTodayKwh"))
    grid_export_today = _num(grid.get("exportTodayKwh"))
    house_today = _num(meter.get("consumptionTodayKwh"))
    batt_charge_today = _num(meter.get("batteryChargeTodayKwh"))
    batt_discharge_today = _num(meter.get("batteryDischargeTodayKwh"))

    rows: List[Dict[str, Any]] = []

    def push(component: str, metric: str, value: float, unit: Optional[str]) -> None:
        rows.append({
            "device_id": device_id,
            "recorded_at": recorded_at,
            "component": component,
            "metric": metric,
            "value": value,
            "unit": unit,
            "quality": "good",
            "source": source,
        })

    # ── Instantaneous power (W) — sign conventions per §4.2.1 ──
    if grid_w is not None:
        push("grid", "power_w", -grid_w, "W")
    if battery_w is not None:
        push("battery", "power_w", -battery_w, "W")
    if soc_pct is not None:
        push("battery", "soc_pct", soc_pct, "%")
    if solar_w is not None:
        push("solar", "power_w", solar_w, "W")
    if house_w is not None:
        push("house", "power_w", house_w, "W")
    if string1_w is not None:
        push("solar_string_1", "power_w", string1_w, "W")
    if string2_w is not None:
        push("solar_string_2", "power_w", string2_w, "W")

    # ── Today-energy counters (kWh) — direction encoded in the
    #    component, never folded into the metric name ──
    if solar_today is not None:
        push("solar", "energy_today_kwh", solar_today, "kWh")
    if grid_import_today is not None:
        push("grid_import", "energy_today_kwh", grid_import_today, "kWh")
    if grid_export_today is not None:
        push("grid_export", "energy_today_kwh", grid_export_today, "kWh")
    if house_today is not None:
        push("house", "energy_today_kwh", house_today, "kWh")
    if batt_charge_today is not None:
        push("battery_charge", "energy_today_kwh", batt_charge_today, "kWh")
    if batt_discharge_today is not None:
        push("battery_discharge", "energy_today_kwh", batt_discharge_today, "kWh")

    return rows


def pick_inverter_device(
    devices: Iterable[Dict[str, Any]],
    inverter_serial: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Find the `devices` row corresponding to this Pi's inverter.

    Matches by serial when known; falls back to the first GE inverter row
    when the snapshot didn't include a serial yet (rare, only on the
    very first poll after a fresh boot).
    """
    candidates = [
        d for d in devices or []
        if d.get("device_type") == "inverter"
        and d.get("manufacturer") == "givenergy"
    ]
    if not candidates:
        return None
    if inverter_serial:
        for d in candidates:
            if d.get("serial_number") == inverter_serial:
                return d
    return candidates[0]
