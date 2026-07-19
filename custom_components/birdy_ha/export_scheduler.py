"""Time-based battery-mode scheduler for GE HA-master tenants.

Flips `batteryMode` between an in-window mode (default `timed_export`) and
an out-of-window mode (default `eco`) so the tenant force-exports during
the window and self-consumes the rest of the day.

Why it must exist: on GE Gen1/2 `eco` and `timed_discharge/export` are the
SAME register (r59 `enable_discharge`), so a static config cannot do both
"export at peak" and "self-consume the evening". GE Cloud's "Timed Export"
preset flipped the mode by the clock server-side; that's withdrawn for
Pi-managed inverters, so we do the switching locally here. Full model +
design: docs/battery-mode-and-export-scheduler.md.

CONTROL — writes `batteryMode` via the master's SettingsAdapter. Gated by
the caller: master + can_control + `BIRDY_EXPORT_SCHEDULER=1` (OFF by
default). Inert unless a valid export window (the inverter's DC-discharge
slot) is configured. Backs off after a human changes the mode in HA.

The decision core (`desired_mode`) is pure and unit-tested; the class holds
only the manual-override + rewrite-cooldown state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from .low_rate import now_in_local_window

_LOGGER = logging.getLogger(__name__)

MODE_IN_DEFAULT = "timed_export"
MODE_OUT_DEFAULT = "eco"
# Back off this long after a human changes the mode in HA, so the clock
# doesn't immediately fight a manual override.
MANUAL_OVERRIDE_GRACE = timedelta(hours=2)
# Don't re-issue the same target while the poll's mode read still lags our
# last write (the confirm-read + next Modbus sweep can take a few seconds).
REWRITE_COOLDOWN = timedelta(seconds=120)


def desired_mode(
    now_local: datetime,
    window_start: Optional[str],
    window_end: Optional[str],
    mode_in: str = MODE_IN_DEFAULT,
    mode_out: str = MODE_OUT_DEFAULT,
) -> Optional[str]:
    """The batteryMode the clock wants.

    Returns None when there is no usable window (missing or degenerate
    DC-discharge slot) — in that case we do NOT touch the mode, so an
    un-configured inverter is never forced out of whatever the user set.
    """
    if not window_start or not window_end or window_start == window_end:
        return None
    return mode_in if now_in_local_window(window_start, window_end, now_local) else mode_out


class ExportScheduler:
    """Per-master scheduler state. Pure decision in `target`."""

    def __init__(self) -> None:
        self._override_until: Optional[datetime] = None
        self._last_written: Optional[str] = None
        self._last_written_at: Optional[datetime] = None

    def note_manual_mode_write(self, now_utc: datetime) -> None:
        """A human (HA select) changed batteryMode — pause the scheduler
        for the grace period so it doesn't stomp the manual choice."""
        self._override_until = now_utc + MANUAL_OVERRIDE_GRACE

    def note_scheduler_write(self, mode: str, now_utc: datetime) -> None:
        self._last_written = mode
        self._last_written_at = now_utc

    def override_active(self, now_utc: datetime) -> bool:
        return bool(self._override_until and now_utc < self._override_until)

    def target(
        self,
        now_local: datetime,
        now_utc: datetime,
        current_mode: Optional[str],
        window_start: Optional[str],
        window_end: Optional[str],
        mode_in: str = MODE_IN_DEFAULT,
        mode_out: str = MODE_OUT_DEFAULT,
    ) -> Optional[str]:
        """The mode to write this tick, or None to do nothing."""
        if self.override_active(now_utc):
            return None
        want = desired_mode(now_local, window_start, window_end, mode_in, mode_out)
        if want is None or want == current_mode:
            return None
        # Suppress a repeat write while the mode read lags our last write.
        if (
            self._last_written == want
            and self._last_written_at is not None
            and now_utc - self._last_written_at < REWRITE_COOLDOWN
        ):
            return None
        return want
