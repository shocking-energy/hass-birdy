"""Live 'imported at low rate' figure for the birdy_ha master — ACCOUNTING ONLY.

Port of the pi-daemon deriver (pi-daemon/src/runtime/low_rate_import.py) so
HA-master tenants (e.g. David) emit the same `grid_import.low_rate_today_kwh`
split that pi-daemon masters do. Splits today's grid import into the LOW-RATE
portion: the gate is true when the inverter's AC-charge window is active (on a
GivEnergy + Octopus tenant that window IS the cheap-rate window) OR a CT `ev`
channel shows the car charging (always Octopus-triggered, hence low rate).

**No control writes** — reads the already-derived `grid_import.energy_today_kwh`
and appends a sibling metric, so it can never affect the inverter/battery.

Peak-complement model (identical to pi-daemon): track the PEAK (high-rate)
portion, derive low-rate as the remainder —

    peak_accum   += max(0, today − last_today)   ONLY when NOT is_low_rate_now
    low_rate_today = max(0, today − peak_accum)

so a cold start / mid-day HA restart assumes all import so far was low rate
(peak_accum = 0 → low_rate = today), the correct default for a well-managed
tenant, and never under-reports. It is an ESTIMATE; the billing-exact figure
(Octopus off-peak + IOG dispatch) reconciles it in the daily view.

The store is **in-memory** (per master, reset on HA restart) — deliberately no
file writes, to avoid SD-card wear; the cold-start rule handles restarts.

Difference from pi-daemon: pi-daemon gates on the Victron planner's absolute-UTC
charge_plan window, which HA-master (GivEnergy) tenants don't have. Here the gate
is the inverter's own AC-charge window (local HH:MM, read every poll from the
SYSTEM `config` block), which is the right local signal for these tenants.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    _LONDON = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover - zoneinfo always present on HA
    _LONDON = timezone.utc

_LOGGER = logging.getLogger(__name__)

GRID_IMPORT_COMPONENT = "grid_import"
SOURCE_METRIC = "energy_today_kwh"      # the derived today-figure, not the lifetime total
LOW_RATE_METRIC = "low_rate_today_kwh"
LOW_RATE_EV_W = 1000.0                   # car draw that counts the whole house as low-rate


def _finite(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _london_date(recorded_at: Any) -> str:
    """London-local date (YYYY-MM-DD) for an ISO timestamp — gives the midnight
    reset for free. Falls back to 'now' in London on any parse failure."""
    try:
        dt = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_LONDON).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(_LONDON).strftime("%Y-%m-%d")


def now_in_local_window(start_hhmm: Optional[str], end_hhmm: Optional[str],
                        now_local: datetime) -> bool:
    """Is `now_local` inside the local HH:MM window [start, end)? Handles a
    window that wraps midnight (start > end, e.g. 23:30-05:30). A zero-length
    window (start == end) is treated as never-active."""
    if not start_hhmm or not end_hhmm:
        return False
    try:
        sh, sm = (int(x) for x in str(start_hhmm).split(":")[:2])
        eh, em = (int(x) for x in str(end_hhmm).split(":")[:2])
    except (ValueError, TypeError):
        return False
    s, e = sh * 60 + sm, eh * 60 + em
    cur = now_local.hour * 60 + now_local.minute
    if s == e:
        return False
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


class LowRateImportDeriver:
    """Derives `grid_import.low_rate_today_kwh` from `grid_import.energy_today_kwh`
    by tracking the peak (high-rate) portion and subtracting it. In-memory state
    per key `<device_id>|grid_import|low_rate_today_kwh`:
        { "date": "YYYY-MM-DD", "last_today": float, "peak_accum": float }
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._recs: Dict[str, Dict[str, Any]] = {}
        self._log = logger or _LOGGER

    @staticmethod
    def _key(device_id: str) -> str:
        return f"{device_id}|{GRID_IMPORT_COMPONENT}|{LOW_RATE_METRIC}"

    def derive(self, *, device_id: str, today_kwh: Any, recorded_at: Any,
               is_low_rate_now: bool) -> Optional[float]:
        """Return today's low-rate import (kWh), or None to skip (stale value)."""
        today = _finite(today_kwh)
        if today is None:
            return None

        key = self._key(device_id)
        date = _london_date(recorded_at)
        rec = self._recs.get(key)

        if rec is None or rec.get("date") != date:
            # Cold start / midnight rollover: assume all import so far was low
            # rate (peak_accum = 0). No mid-day restart zeroing; no under-report.
            self._recs[key] = {"date": date, "last_today": today, "peak_accum": 0.0}
            return round(max(0.0, today), 3)

        last_today = _finite(rec.get("last_today"))
        peak = _finite(rec.get("peak_accum")) or 0.0
        if last_today is None:
            last_today = today

        delta = today - last_today
        # today can dip on an intra-day counter re-baseline — add nothing then.
        if delta > 0 and not is_low_rate_now:
            peak += delta
        peak = min(peak, today)  # clamp so low-rate never goes negative
        self._recs[key] = {"date": date, "last_today": today, "peak_accum": peak}
        return round(max(0.0, today - peak), 3)

    def augment(self, rows: List[Dict[str, Any]], is_low_rate_now: bool) -> List[Dict[str, Any]]:
        """Append `grid_import.low_rate_today_kwh` for each
        `grid_import.energy_today_kwh` row present."""
        extra: List[Dict[str, Any]] = []
        for r in rows:
            if r.get("component") != GRID_IMPORT_COMPONENT or r.get("metric") != SOURCE_METRIC:
                continue
            val = self.derive(
                device_id=r.get("device_id"),
                today_kwh=r.get("value"),
                recorded_at=r.get("recorded_at"),
                is_low_rate_now=is_low_rate_now,
            )
            if val is None:
                continue
            extra.append({
                "device_id":   r.get("device_id"),
                "recorded_at": r.get("recorded_at"),
                "component":   GRID_IMPORT_COMPONENT,
                "metric":      LOW_RATE_METRIC,
                "value":       val,
                "unit":        "kWh",
                "quality":     r.get("quality", "good"),
                "source":      r.get("source"),
            })
        return list(rows) + extra
