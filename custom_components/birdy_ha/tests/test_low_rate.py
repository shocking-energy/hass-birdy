"""Tests for the low-rate grid-import figure (birdy_ha/low_rate.py).

Mirrors pi-daemon/tests/test_low_rate_import.py — same peak-complement model
(low_rate = today − peak_accum; peak grows by the today-delta only on high-rate
ticks; cold start / London-midnight reset assume all-low-rate). Plus the local
HH:MM AC-charge-window gate that replaces pi-daemon's absolute-UTC window.

Run: cd custom_components/birdy_ha/tests && python3 test_low_rate.py
(imports low_rate.py directly — the birdy_ha package __init__ needs homeassistant.)
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# Import the module standalone (it's stdlib-only, no relative imports) so we
# don't trigger birdy_ha/__init__.py, which requires homeassistant.
sys.path.insert(0, os.path.join(HERE, ".."))

from low_rate import (  # noqa: E402
    LowRateImportDeriver,
    now_in_local_window,
    _LONDON,
)

DEV = "dev-1"
D = "2026-07-06T"  # BST day; times below stay on the same London day (< 23:00Z)


class DeriveTest(unittest.TestCase):
    def test_cold_start_returns_full_today(self):
        d = LowRateImportDeriver()
        self.assertEqual(d.derive(device_id=DEV, today_kwh=3.0,
                                  recorded_at=D + "10:00:00Z", is_low_rate_now=True), 3.0)

    def test_peak_ticks_reduce_low_rate(self):
        d = LowRateImportDeriver()
        d.derive(device_id=DEV, today_kwh=1.0, recorded_at=D + "10:00:00Z", is_low_rate_now=True)
        self.assertEqual(d.derive(device_id=DEV, today_kwh=3.0, recorded_at=D + "10:05:00Z", is_low_rate_now=True), 3.0)
        # +1 at HIGH rate → peak 1 → low_rate = 4 − 1 = 3.0
        self.assertEqual(d.derive(device_id=DEV, today_kwh=4.0, recorded_at=D + "10:10:00Z", is_low_rate_now=False), 3.0)
        # +2 at LOW rate → peak stays 1 → low_rate = 6 − 1 = 5.0
        self.assertEqual(d.derive(device_id=DEV, today_kwh=6.0, recorded_at=D + "10:15:00Z", is_low_rate_now=True), 5.0)

    def test_all_low_rate_tracks_total(self):
        d = LowRateImportDeriver()
        vals = [d.derive(device_id=DEV, today_kwh=t, recorded_at=D + f"1{i}:00:00Z", is_low_rate_now=True)
                for i, t in enumerate([0.5, 2.0, 5.0, 9.0])]
        self.assertEqual(vals, [0.5, 2.0, 5.0, 9.0])

    def test_midnight_reset(self):
        d = LowRateImportDeriver()
        d.derive(device_id=DEV, today_kwh=1.0, recorded_at="2026-07-06T22:00:00Z", is_low_rate_now=False)
        v = d.derive(device_id=DEV, today_kwh=2.0, recorded_at="2026-07-06T22:30:00Z", is_low_rate_now=False)
        self.assertEqual(v, 1.0)
        # London midnight = 23:00 UTC (BST) → new day, resets to all-low-rate
        v = d.derive(device_id=DEV, today_kwh=0.3, recorded_at="2026-07-06T23:30:00Z", is_low_rate_now=True)
        self.assertEqual(v, 0.3)

    def test_today_dip_adds_no_peak(self):
        d = LowRateImportDeriver()
        d.derive(device_id=DEV, today_kwh=5.0, recorded_at=D + "10:00:00Z", is_low_rate_now=True)
        v = d.derive(device_id=DEV, today_kwh=0.2, recorded_at=D + "10:05:00Z", is_low_rate_now=False)
        self.assertEqual(v, 0.2)

    def test_stale_skips(self):
        d = LowRateImportDeriver()
        self.assertIsNone(d.derive(device_id=DEV, today_kwh=None,
                                   recorded_at=D + "10:00:00Z", is_low_rate_now=True))


class AugmentTest(unittest.TestCase):
    def test_appends_low_rate_from_energy_today(self):
        d = LowRateImportDeriver()
        rows = [{"device_id": DEV, "component": "grid_import", "metric": "energy_today_kwh",
                 "value": 3.0, "recorded_at": D + "10:00:00Z", "unit": "kWh", "source": "ha:x"}]
        out = d.augment(rows, is_low_rate_now=True)
        low = [r for r in out if r["metric"] == "low_rate_today_kwh"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["value"], 3.0)
        self.assertEqual(low[0]["component"], "grid_import")
        self.assertEqual(low[0]["unit"], "kWh")

    def test_ignores_lifetime_total(self):
        d = LowRateImportDeriver()
        rows = [{"device_id": DEV, "component": "grid_import", "metric": "energy_total_kwh",
                 "value": 355.0, "recorded_at": D + "10:00:00Z"}]
        self.assertEqual(len(d.augment(rows, is_low_rate_now=True)), 1)


class WindowTest(unittest.TestCase):
    def _local(self, hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        return datetime(2026, 7, 6, h, m, tzinfo=timezone.utc).astimezone(_LONDON)

    def test_inside_simple_window(self):
        # David's window 02:00-05:00; 03:00 local is inside.
        self.assertTrue(now_in_local_window("02:00", "05:00", datetime(2026, 7, 6, 3, 0)))

    def test_outside_simple_window(self):
        self.assertFalse(now_in_local_window("02:00", "05:00", datetime(2026, 7, 6, 12, 0)))

    def test_wraps_midnight(self):
        # 23:30-05:30 wraps; 01:00 is inside, 06:00 is outside.
        self.assertTrue(now_in_local_window("23:30", "05:30", datetime(2026, 7, 6, 1, 0)))
        self.assertFalse(now_in_local_window("23:30", "05:30", datetime(2026, 7, 6, 6, 0)))

    def test_missing_or_zero_window(self):
        self.assertFalse(now_in_local_window(None, None, datetime(2026, 7, 6, 3, 0)))
        self.assertFalse(now_in_local_window("02:00", "02:00", datetime(2026, 7, 6, 2, 0)))


if __name__ == "__main__":
    unittest.main()
