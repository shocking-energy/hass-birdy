"""Unit tests for the time-based battery-mode export scheduler.

No Home Assistant needed — export_scheduler + low_rate are HA-free. They're
loaded under a synthetic package so export_scheduler's `from .low_rate ...`
relative import resolves.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BIRDY = Path(__file__).resolve().parents[1] / "custom_components" / "birdy_ha"
_pkg = types.ModuleType("bh")
_pkg.__path__ = [str(BIRDY)]
sys.modules.setdefault("bh", _pkg)


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(f"bh.{modname}", BIRDY / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"bh.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("low_rate", "low_rate.py")
es = _load("export_scheduler", "export_scheduler.py")

LON = ZoneInfo("Europe/London")


def _lon(h, m=0):
    return datetime(2026, 7, 18, h, m, tzinfo=LON)


def _utc(h, m=0):
    return datetime(2026, 7, 18, h, m, tzinfo=timezone.utc)


# ── desired_mode (pure) ───────────────────────────────────────────────
def test_in_window_is_export():
    assert es.desired_mode(_lon(17), "16:00", "19:00") == "timed_export"


def test_out_of_window_is_eco():
    assert es.desired_mode(_lon(21), "16:00", "19:00") == "eco"
    assert es.desired_mode(_lon(15, 59), "16:00", "19:00") == "eco"


def test_no_or_degenerate_window_is_noop():
    assert es.desired_mode(_lon(17), None, "19:00") is None
    assert es.desired_mode(_lon(17), "16:00", None) is None
    assert es.desired_mode(_lon(17), "16:00", "16:00") is None


def test_custom_modes_respected():
    assert es.desired_mode(_lon(17), "16:00", "19:00", mode_in="timed_discharge") == "timed_discharge"
    assert es.desired_mode(_lon(21), "16:00", "19:00", mode_out="timed_discharge") == "timed_discharge"


# ── target (stateful) ─────────────────────────────────────────────────
def test_no_write_when_already_in_desired_mode():
    s = es.ExportScheduler()
    assert s.target(_lon(17), _utc(16), "timed_export", "16:00", "19:00") is None
    assert s.target(_lon(21), _utc(20), "eco", "16:00", "19:00") is None


def test_writes_on_transition():
    s = es.ExportScheduler()
    assert s.target(_lon(21), _utc(20), "timed_export", "16:00", "19:00") == "eco"
    assert s.target(_lon(17), _utc(16), "eco", "16:00", "19:00") == "timed_export"


def test_manual_override_pauses_then_expires():
    s = es.ExportScheduler()
    t = _utc(20)
    s.note_manual_mode_write(t)
    # within grace → scheduler stands down even though a transition is due
    assert s.target(_lon(21), t + timedelta(minutes=5), "timed_export", "16:00", "19:00") is None
    # after grace → resumes
    assert s.target(_lon(21), t + timedelta(hours=3), "timed_export", "16:00", "19:00") == "eco"


def test_rewrite_cooldown_suppresses_repeat_while_read_lags():
    s = es.ExportScheduler()
    t = _utc(20)
    s.note_scheduler_write("eco", t)
    # mode read still shows the old value inside the cooldown → no repeat write
    assert s.target(_lon(21), t + timedelta(seconds=30), "timed_export", "16:00", "19:00") is None
    # once the cooldown lapses and the read still hasn't caught up → write again
    assert s.target(_lon(21), t + timedelta(seconds=200), "timed_export", "16:00", "19:00") == "eco"
