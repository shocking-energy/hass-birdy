"""Unit tests for the Planner Schedule → GE inverter reconciler.

No Home Assistant needed — ge_schedule + low_rate are HA-free (loaded under a
synthetic package, same as test_export_scheduler).

The key test simulates an inverter: start from a state, apply `next_step`
one write at a time, and assert (a) it converges on the schedule and (b) it
NEVER passes through a harmful state on the way:
  * a timed (export/discharge) mode while the export windows are wrong, or
  * grid charging enabled while the charge window is wrong.
"""
from __future__ import annotations

import asyncio
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
gs = _load("ge_schedule", "ge_schedule.py")

LON = ZoneInfo("Europe/London")


def lon(h, m=0):
    return datetime(2026, 10, 14, h, m, tzinfo=LON)


def inverter(**over):
    cfg = {
        "acCharge": False, "acChargeStart": "00:00", "acChargeEnd": "00:00",
        "acChargeUpperEnabled": False, "acChargeLimit": 100,
        "dcDischarge1Start": "00:00", "dcDischarge1End": "00:00",
        "dcDischarge2Start": "00:00", "dcDischarge2End": "00:00",
        "batteryMode": "eco",
    }
    cfg.update(over)
    return cfg


def apply(cfg, step):
    """What the inverter holds after one step lands (mirrors the library)."""
    c = dict(cfg)
    if step.kind == "mode":
        c["batteryMode"] = step.value
    elif step.kind == "ac_charge":
        c["acCharge"] = bool(step.value)
    elif step.kind == "charge_target":      # set_charge_target: enables charge; 100 = no limit
        c["acCharge"] = True
        if step.value >= 100:
            c["acChargeUpperEnabled"], c["acChargeLimit"] = False, 100
        else:
            c["acChargeUpperEnabled"], c["acChargeLimit"] = True, step.value
    elif step.kind == "charge_slot":
        c["acChargeStart"], c["acChargeEnd"] = step.start, step.end
    elif step.kind == "discharge_slot":
        c[f"dcDischarge{step.slot}Start"], c[f"dcDischarge{step.slot}End"] = step.start, step.end
    return c


def converge(desired, cfg, now_local, max_steps=20):
    # Zero-length windows are skipped (same as the cloud RPC and next_step).
    want_export = [gs._window(w) for w in desired.get("export", []) if gs._window(w) != gs.NO_WINDOW][:2]
    want_slots = [want_export[i] if i < len(want_export) else gs.NO_WINDOW for i in range(2)]
    want_cw = gs._window(desired["charge"]) if desired.get("charge") else None
    def bad_export(c):   # a timed mode while the export windows are wrong
        have = [gs._cfg_window(c, f"dcDischarge{n}Start", f"dcDischarge{n}End") for n in (1, 2)]
        return c["batteryMode"] in gs.TIMED_MODES and have != want_slots

    def bad_charge(c):   # grid charging enabled with the wrong charge window
        return c["acCharge"] and (want_cw is None
                                  or gs._cfg_window(c, "acChargeStart", "acChargeEnd") != want_cw)

    steps, prev = [], None
    for _ in range(max_steps):
        # Safety invariants at EVERY intermediate state: a step may leave a
        # pre-existing bad state for a later step to fix, but must never
        # CREATE one.
        if prev is not None:
            assert not (bad_export(cfg) and not bad_export(prev)), f"step created a bad export state: {steps[-1]} → {cfg}"
            assert not (bad_charge(cfg) and not bad_charge(prev)), f"step created a bad charge state: {steps[-1]} → {cfg}"
        step = gs.next_step(desired, cfg, now_local)
        if step is None:
            return cfg, steps
        steps.append(step)
        prev, cfg = cfg, apply(cfg, step)
    raise AssertionError(f"did not converge: {steps}")


KIM = {"version": "v1", "charge": {"start": "00:30", "end": "05:30", "target_pct": 80},
       "export": [{"start": "16:00", "end": "19:00"}]}


def test_none_means_leave_alone():
    assert gs.next_step(None, inverter(acCharge=True), lon(12)) is None


def test_fresh_inverter_converges_and_observed_matches():
    cfg, steps = converge(KIM, inverter(), lon(12))
    obs = gs.observed_from_config(cfg)
    assert obs["charge"] == KIM["charge"]
    assert obs["export"] == KIM["export"]
    assert obs["battery_mode"] == "eco"            # 12:00 is outside 16–19
    assert len(steps) <= 4


def test_inside_export_window_ends_in_timed_export():
    cfg, _ = converge(KIM, inverter(), lon(17))
    assert cfg["batteryMode"] == "timed_export"


def test_already_holding_it_does_nothing():
    cfg, _ = converge(KIM, inverter(), lon(12))
    assert gs.next_step(KIM, cfg, lon(12)) is None


def test_changing_export_window_while_exporting_goes_eco_first():
    held = inverter(acCharge=True, acChargeStart="00:30", acChargeEnd="05:30",
                    acChargeUpperEnabled=True, acChargeLimit=80,
                    dcDischarge1Start="16:00", dcDischarge1End="19:00",
                    dcDischarge2Start="20:00", dcDischarge2End="21:00",
                    batteryMode="timed_export")
    first = gs.next_step(KIM, held, lon(20, 30))
    assert first.kind == "mode" and first.value == "eco"
    cfg, _ = converge(KIM, apply(held, first), lon(20, 30))
    assert gs._cfg_window(cfg, "dcDischarge2Start", "dcDischarge2End") == gs.NO_WINDOW


def test_changing_charge_window_while_charging_disables_charging_first():
    held = inverter(acCharge=True, acChargeStart="01:30", acChargeEnd="05:30")
    first = gs.next_step(KIM, held, lon(2))
    assert first.kind == "ac_charge" and first.value is False
    cfg, _ = converge(KIM, apply(held, first), lon(2))
    assert gs.observed_from_config(cfg)["charge"] == KIM["charge"]


def test_target_100_means_no_upper_limit():
    full = {**KIM, "charge": {**KIM["charge"], "target_pct": 100}}
    cfg, _ = converge(full, inverter(acCharge=True, acChargeStart="00:30", acChargeEnd="05:30",
                                     acChargeUpperEnabled=True, acChargeLimit=80), lon(12))
    assert cfg["acChargeUpperEnabled"] is False
    assert gs.observed_from_config(cfg)["charge"]["target_pct"] == 100


def test_empty_schedule_switches_charging_and_export_off():
    empty = {"version": "v2", "charge": None, "export": []}
    held = inverter(acCharge=True, acChargeStart="00:30", acChargeEnd="05:30",
                    dcDischarge1Start="16:00", dcDischarge1End="19:00", batteryMode="timed_export")
    cfg, _ = converge(empty, held, lon(17))
    assert cfg["acCharge"] is False and cfg["batteryMode"] == "eco"
    assert gs.observed_from_config(cfg)["export"] == []


def test_unpadded_times_are_equal():
    assert gs._window({"start": "0:30", "end": "5:30"}) == ("00:30", "05:30")
    cfg, _ = converge(KIM, inverter(), lon(12))
    padded = {**KIM, "charge": {"start": "0:30", "end": "5:30", "target_pct": 80}}
    assert gs.next_step(padded, cfg, lon(12)) is None


def test_bad_payloads_are_ignored():
    assert gs._norm(None) is None
    assert gs._norm({"charge": None}) is None                       # no version
    n = gs._norm({"version": "x", "charge": {"start": "00:00", "end": "00:00"},
                  "export": [{"start": "1:00", "end": "2:00"}, "junk", {"start": "3:00", "end": "4:00"},
                             {"start": "5:00", "end": "6:00"}]})
    assert n["charge"] is None and len(n["export"]) == 2


# ── the async class: fetch, step cooldown, manual pause, reporting ──

class FakeCloud:
    def __init__(self, schedule):
        self.schedule, self.reports, self.fetches = schedule, [], 0

    async def rpc(self, name, body=None):
        if name == "get_my_ge_schedule":
            self.fetches += 1
            return self.schedule
        if name == "report_ge_schedule_applied":
            self.reports.append(body)
            return True
        raise AssertionError(name)


class FakeResult:
    def __init__(self, ok=True, error=None):
        self.accepted, self.error = ok, error


class FakeSettings:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def apply_setting(self, key, value, origin="user"):
        self.calls.append((key, value, origin))
        return FakeResult(not self.fail, "timeout" if self.fail else None)

    async def apply_slot(self, kind, n, start, end, origin="user"):
        self.calls.append((f"{kind}{n}", (start, end), origin))
        return FakeResult(not self.fail, "timeout" if self.fail else None)


class FakeMaster:
    def __init__(self, cloud, settings):
        self.cloud, self.settings = cloud, settings


def run(coro):
    return asyncio.run(coro)


T0 = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)


def test_tick_one_step_then_cooldown_then_reports_failure():
    cloud, st = FakeCloud(KIM), FakeSettings(fail=True)
    rec, m = gs.GeScheduleReconciler(), FakeMaster(cloud, st)
    run(rec.tick(m, inverter(), T0, T0.astimezone(LON)))
    assert len(st.calls) == 1 and st.calls[0][2] == "planner"
    assert cloud.reports[-1]["p_ok"] is False and "timeout" in cloud.reports[-1]["p_error"]
    run(rec.tick(m, inverter(), T0 + timedelta(seconds=15), (T0 + timedelta(seconds=15)).astimezone(LON)))
    assert len(st.calls) == 1                       # cooldown: no second write yet
    run(rec.tick(m, inverter(), T0 + timedelta(seconds=75), (T0 + timedelta(seconds=75)).astimezone(LON)))
    assert len(st.calls) == 2                       # retried after the cooldown


def test_tick_reports_ok_when_inverter_holds_it():
    cfg, _ = converge(KIM, inverter(), T0.astimezone(LON))
    cloud, st = FakeCloud(KIM), FakeSettings()
    rec = gs.GeScheduleReconciler()
    run(rec.tick(FakeMaster(cloud, st), cfg, T0, T0.astimezone(LON)))
    assert st.calls == [] and cloud.reports[-1]["p_ok"] is True
    assert cloud.reports[-1]["p_observed"]["charge"] == KIM["charge"]


def test_cloud_withdraws_schedule_leaves_inverter_alone():
    cloud, st = FakeCloud(KIM), FakeSettings()
    rec, m = gs.GeScheduleReconciler(), FakeMaster(cloud, st)
    run(rec.tick(m, inverter(), T0, T0.astimezone(LON)))
    cloud.schedule = None
    later = T0 + gs.FETCH_INTERVAL
    n = len(st.calls)
    run(rec.tick(m, inverter(), later, later.astimezone(LON)))
    assert rec.active is False and len(st.calls) == n


def test_fetch_failure_keeps_last_schedule():
    cloud, st = FakeCloud(KIM), FakeSettings()
    rec, m = gs.GeScheduleReconciler(), FakeMaster(cloud, st)
    run(rec.tick(m, inverter(), T0, T0.astimezone(LON)))

    async def boom(name, body=None):
        if name == "get_my_ge_schedule":
            raise RuntimeError("offline")
        return True
    cloud.rpc = boom
    later = T0 + gs.FETCH_INTERVAL
    run(rec.tick(m, inverter(), later, later.astimezone(LON)))
    assert rec.active is True                       # never clears on a fetch error


def test_manual_change_pauses_until_new_version():
    cloud, st = FakeCloud(KIM), FakeSettings()
    rec, m = gs.GeScheduleReconciler(), FakeMaster(cloud, st)
    run(rec.tick(m, inverter(), T0, T0.astimezone(LON)))
    rec.note_manual_write("acChargeEnd")
    n = len(st.calls)
    t = T0 + timedelta(minutes=2)
    run(rec.tick(m, inverter(), t, t.astimezone(LON)))
    assert len(st.calls) == n and "paused" in cloud.reports[-1]["p_error"]
    cloud.schedule = {**KIM, "version": "v2"}
    t2 = T0 + gs.FETCH_INTERVAL
    run(rec.tick(m, inverter(), t2, t2.astimezone(LON)))
    assert len(st.calls) == n + 1                   # new version → resumed


def test_random_states_converge_safely():
    import random
    rnd = random.Random(7)
    hh = lambda: f"{rnd.randrange(24):02d}:{rnd.choice([0, 30]):02d}"
    for _ in range(2000):
        start = inverter(
            acCharge=rnd.random() < 0.5, acChargeStart=hh(), acChargeEnd=hh(),
            acChargeUpperEnabled=rnd.random() < 0.5, acChargeLimit=rnd.choice([50, 80, 100]),
            dcDischarge1Start=hh(), dcDischarge1End=hh(),
            dcDischarge2Start=hh(), dcDischarge2End=hh(),
            batteryMode=rnd.choice(["eco", "timed_export", "timed_discharge"]))
        desired = {"version": "r",
                   "charge": rnd.choice([None, {"start": hh(), "end": hh(), "target_pct": rnd.choice([30, 80, 100])}]),
                   "export": [{"start": hh(), "end": hh()} for _ in range(rnd.randrange(3))]}
        now = lon(rnd.randrange(24), rnd.choice([0, 15, 45]))
        cfg, _ = converge(desired, start, now)      # asserts safety at every step
        assert gs.next_step(desired, cfg, now) is None
