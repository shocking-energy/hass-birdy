"""Planner Schedule mode for a GivEnergy inverter — reconcile the cloud's
schedule onto the inverter.

The energy-monitor Planner page saves the owner's timed slots; the cloud RPC
`get_my_ge_schedule()` (energy-monitor migration 101) turns them into what the
inverter should hold:

    {"version": "...", "charge": {"start": "00:30", "end": "05:30",
     "target_pct": 80} | None, "export": [{"start": "16:00", "end": "19:00"}, …≤2]}

or None, meaning "leave the inverter alone". A GE inverter holds ONE charge
window + target and TWO export (discharge) windows. Plan:
energy-monitor docs/architecture/ge-schedule-via-ha-plan.md.

LEVEL-TRIGGERED, not fire-and-forget: every tick compares what the inverter
holds (the poll's config block) with the schedule and takes AT MOST ONE step
towards it. A failed or partial write is simply "still different" and is
retried on a later tick. After each step we wait for the poll to catch up
before judging again.

Step order is chosen so the inverter never passes through a harmful state:
  1. Export windows about to change while in a timed mode → switch to eco
     first (a half-written window would otherwise export at the wrong time).
  2. Charge window about to change while charging is enabled → disable grid
     charging first (same reason: no stray grid charge at the peak rate).
  3. Windows are written ATOMICALLY (start+end in one command) — see
     SettingsAdapter.apply_slot.
  4. Charge target last: `set_charge_target` also re-enables grid charging;
     100 % = no upper limit (the library disables the target).
  5. Battery mode follows the clock only once the windows are right:
     timed_export inside an export window, eco outside — eco and timed export
     are ONE register on GE Gen1/2 (see export_scheduler.py).

Off unless enabled per install (BIRDY_PLANNER_CONTROL=1 or a
`.birdy_planner_control` marker) AND the cloud returns a schedule (the home is
opted in AND in Schedule mode). A human changing one of these settings in HA
pauses reconciliation until the owner saves a new schedule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .low_rate import now_in_local_window

_LOGGER = logging.getLogger(__name__)

FETCH_INTERVAL = timedelta(minutes=5)
# After a write, let the confirm-read (~8 s) and the next poll land before
# judging again; also keeps us far below the 6-writes/min/setting limit.
STEP_COOLDOWN = timedelta(seconds=60)
# Re-report an unchanged status at least this often (the card's "last heard").
REPORT_HEARTBEAT = timedelta(minutes=5)

MODE_ECO = "eco"
MODE_TIMED_EXPORT = "timed_export"
TIMED_MODES = ("timed_export", "timed_discharge")
NO_WINDOW = ("00:00", "00:00")

# Settings this feature owns; a human writing any of them in HA pauses it.
OWNED_KEYS = frozenset({
    "acCharge", "acChargeStart", "acChargeEnd", "acChargeLimit",
    "acChargeUpperEnabled", "batteryMode",
    "dcDischarge1Start", "dcDischarge1End", "dcDischarge2Start", "dcDischarge2End",
})


@dataclass(frozen=True)
class Step:
    """One write towards the schedule."""
    kind: str                       # mode | ac_charge | charge_slot | discharge_slot | charge_target
    value: Any = None               # mode str / bool / target pct
    slot: int = 0                   # discharge slot number (1|2)
    start: Optional[str] = None     # "HH:MM"
    end: Optional[str] = None


def _hhmm(v: Any) -> Optional[str]:
    """'0:30' / '00:30' / '00:30:00' → '00:30'; None if unparseable."""
    try:
        h, m = (int(x) for x in str(v).split(":")[:2])
    except (ValueError, TypeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def _pair(s: Any, e: Any) -> tuple[str, str]:
    s, e = _hhmm(s), _hhmm(e)
    if s is None or e is None or s == e:
        return NO_WINDOW
    return (s, e)


def _window(w: Optional[dict]) -> tuple[str, str]:
    if not isinstance(w, dict):
        return NO_WINDOW
    return _pair(w.get("start"), w.get("end"))


def _cfg_window(cfg: dict, start_key: str, end_key: str) -> tuple[str, str]:
    return _pair(cfg.get(start_key), cfg.get(end_key))


def observed_from_config(cfg: dict) -> dict:
    """What the inverter holds, in the cloud schedule's shape."""
    charge = None
    if cfg.get("acCharge"):
        win = _cfg_window(cfg, "acChargeStart", "acChargeEnd")
        if win != NO_WINDOW:
            target = int(round(float(cfg.get("acChargeLimit") or 100))) if cfg.get("acChargeUpperEnabled") else 100
            charge = {"start": win[0], "end": win[1], "target_pct": target}
    export = []
    for n in (1, 2):
        win = _cfg_window(cfg, f"dcDischarge{n}Start", f"dcDischarge{n}End")
        if win != NO_WINDOW:
            export.append({"start": win[0], "end": win[1]})
    return {"charge": charge, "export": export, "battery_mode": cfg.get("batteryMode")}


def wanted_mode(export: list, now_local: datetime) -> str:
    return MODE_TIMED_EXPORT if any(
        now_in_local_window(*_window(w), now_local) for w in export
    ) else MODE_ECO


def next_step(desired: Optional[dict], cfg: dict, now_local: datetime) -> Optional[Step]:
    """The single next write towards `desired`, or None when the inverter
    already holds it (or there is nothing we may do). Pure."""
    if desired is None or not cfg:
        return None
    want_charge = desired.get("charge")
    want_export = [w for w in (desired.get("export") or []) if _window(w) != NO_WINDOW][:2]
    want_slots = [_window(want_export[i]) if i < len(want_export) else NO_WINDOW for i in range(2)]
    have_slots = [_cfg_window(cfg, f"dcDischarge{n}Start", f"dcDischarge{n}End") for n in (1, 2)]
    mode = cfg.get("batteryMode")

    # 1. Export windows changing → leave any timed mode first.
    if want_slots != have_slots and mode in TIMED_MODES:
        return Step("mode", MODE_ECO)

    # 2 + 3 (charge). Window first, with grid charging off while it changes.
    if want_charge is None:
        if cfg.get("acCharge"):
            return Step("ac_charge", False)
    else:
        want_cw = _window(want_charge)
        have_cw = _cfg_window(cfg, "acChargeStart", "acChargeEnd")
        if want_cw != have_cw:
            if cfg.get("acCharge"):
                return Step("ac_charge", False)
            return Step("charge_slot", start=want_cw[0], end=want_cw[1])

    # 3 (export). Atomic per-slot writes; an unused slot is cleared.
    for i, (want, have) in enumerate(zip(want_slots, have_slots), start=1):
        if want != have:
            return Step("discharge_slot", slot=i, start=want[0], end=want[1])

    # 4. Charge target (also re-enables grid charging).
    if want_charge is not None:
        target = max(10, min(100, int(want_charge.get("target_pct") or 100)))
        have_target = (int(round(float(cfg.get("acChargeLimit") or 100)))
                       if cfg.get("acChargeUpperEnabled") else 100)
        if not cfg.get("acCharge") or have_target != target:
            return Step("charge_target", target)

    # 5. Battery mode by the clock (windows are right by now).
    if mode is not None:
        want_mode = wanted_mode(want_export, now_local)
        if mode != want_mode:
            return Step("mode", want_mode)
    return None


def _norm(sched: Any) -> Optional[dict]:
    """Validate the RPC payload; None for anything unusable."""
    if not isinstance(sched, dict) or not sched.get("version"):
        return None
    charge = sched.get("charge")
    if charge is not None and (not isinstance(charge, dict) or _window(charge) == NO_WINDOW):
        charge = None
    export = [w for w in (sched.get("export") or []) if isinstance(w, dict) and _window(w) != NO_WINDOW][:2]
    return {"version": str(sched["version"]), "charge": charge, "export": export}


class GeScheduleReconciler:
    """Timing + state around `next_step`. One per HaMaster."""

    def __init__(self) -> None:
        self.desired: Optional[dict] = None
        self._fetched_at: Optional[datetime] = None
        self._step_at: Optional[datetime] = None
        self._paused_version: Optional[str] = None
        self._last_error: Optional[str] = None
        self._report_sig: Optional[tuple] = None
        self._reported_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        """True while the cloud has handed us a schedule to hold."""
        return self.desired is not None

    def note_manual_write(self, key: str) -> None:
        """A human changed an owned setting in HA → pause until the owner
        saves a new schedule (a new version)."""
        if key in OWNED_KEYS and self.desired is not None:
            self._paused_version = self.desired["version"]
            _LOGGER.info("GE schedule paused: %s changed by hand in Home Assistant", key)

    async def tick(self, master, cfg: dict, now_utc: datetime, now_local: datetime) -> None:
        cloud = master.cloud
        if self._fetched_at is None or now_utc - self._fetched_at >= FETCH_INTERVAL:
            self._fetched_at = now_utc
            try:
                fetched = _norm(await cloud.rpc("get_my_ge_schedule"))
                if fetched is None and self.desired is not None:
                    _LOGGER.info("GE schedule withdrawn by the cloud — leaving the inverter as it is")
                self.desired = fetched
                if fetched and self._paused_version and fetched["version"] != self._paused_version:
                    self._paused_version = None          # owner saved a new schedule
            except Exception as exc:                      # keep the last schedule; never clear
                self._last_error = f"couldn't fetch schedule: {exc}"[:200]
                _LOGGER.debug("get_my_ge_schedule failed: %s", exc)

        if self.desired is None:
            return

        error: Optional[str] = None
        if self._paused_version == self.desired["version"]:
            error = "paused — a setting was changed by hand in Home Assistant; save the schedule again to resume"
            step = None
        else:
            step = next_step(self.desired, cfg, now_local)
            if step is not None and (self._step_at is None or now_utc - self._step_at >= STEP_COOLDOWN):
                self._step_at = now_utc
                error = await self._execute(master.settings, step)
                self._last_error = error
            elif step is not None:
                error = self._last_error                  # waiting for the last step to land

        ok = step is None and error is None
        await self._report(cloud, ok, error, observed_from_config(cfg), now_utc)

    async def _execute(self, settings, step: Step) -> Optional[str]:
        from datetime import time as dtime

        def t(hhmm: str) -> dtime:
            h, m = hhmm.split(":", 1)
            return dtime(int(h), int(m))

        try:
            if step.kind == "mode":
                res = await settings.apply_setting("batteryMode", step.value, origin="planner")
            elif step.kind == "ac_charge":
                res = await settings.apply_setting("acCharge", bool(step.value), origin="planner")
            elif step.kind == "charge_target":
                res = await settings.apply_setting("acChargeLimit", int(step.value), origin="planner")
            elif step.kind == "charge_slot":
                res = await settings.apply_slot("charge", 1, t(step.start), t(step.end), origin="planner")
            elif step.kind == "discharge_slot":
                res = await settings.apply_slot("discharge", step.slot, t(step.start), t(step.end), origin="planner")
            else:
                return f"unknown step {step.kind}"
        except Exception as exc:
            _LOGGER.warning("GE schedule step %s failed: %s", step, exc)
            return f"{step.kind}: {exc}"[:200]
        _LOGGER.info("GE schedule step %s → %s", step, "ok" if res.accepted else f"rejected: {res.error}")
        return None if res.accepted else f"{step.kind}: {res.error}"[:200]

    async def _report(self, cloud, ok: bool, error: Optional[str], observed: dict, now_utc: datetime) -> None:
        sig = (self.desired["version"], ok, error, repr(observed))
        if sig == self._report_sig and self._reported_at and now_utc - self._reported_at < REPORT_HEARTBEAT:
            return
        try:
            await cloud.rpc("report_ge_schedule_applied", {
                "p_version": self.desired["version"], "p_ok": ok,
                "p_error": error, "p_observed": observed,
            })
            self._report_sig, self._reported_at = sig, now_utc
        except Exception as exc:
            _LOGGER.debug("report_ge_schedule_applied failed: %s", exc)
