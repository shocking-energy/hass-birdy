# Battery mode model + time-based export scheduler (spec)

Status: **mode model shipped (0.11.12); scheduler NOT built** — this doc
specs the scheduler.

## 1. The register model (verified 2026-07-17, David's GIV-HY5.0)

On GivEnergy Gen1/2 there is no independent "eco" register on the Modbus
path. Two registers define the battery's discharge behaviour:

| Register | Name | Meaning |
|---|---|---|
| **r59** | `enable_discharge` | 0 = eco/self-consume (no timed discharge) · 1 = timed discharge active (slots) |
| **r27** | `battery_power_mode` | 1 = match demand (cover load, no export) · 0 = max power (export surplus) |

`givenergy_modbus` presets confirm it:
- `set_mode_dynamic()` (Eco) = r27=1, **r59=0**, r110(reserve)=4
- `set_mode_storage(discharge_for_export=…)` = **r59=1** + slots, r27 = 0 (export) or 1 (match)

So there are exactly **three mutually-exclusive modes**, now surfaced as
the single `batteryMode` select (0.11.12) instead of the old `ecoMode` +
`dcDischarge` switches (which both wrote r59 and silently cancelled each
other):

| `batteryMode` | r59 | r27 | Behaviour |
|---|---|---|---|
| `eco` | 0 | 1 | Self-consume 24/7 — battery covers load whenever solar is short, down to reserve. **No forced export.** |
| `timed_discharge` | 1 | 1 | Discharge to cover load **only inside** the discharge slot(s); holds outside them. |
| `timed_export` | 1 | 0 | Discharge at **max, exporting** surplus, only inside the slot(s); holds outside them. |

**Key consequence:** because r59 is one bit, a *static* config cannot give
both "force-export at peak" **and** "self-consume the rest of the day". In
`timed_export`, outside the slot the battery **holds** and the house is fed
from the grid (this is exactly the David 21:13 evening-import case). AC
**charge** windows are a *separate* register set (`enable_charge` + charge
slots) and are unaffected — eco + overnight AC-charge coexist fine.

## 2. Why a scheduler

To get both peak export **and** evening self-consumption you must **switch
r59/r27 by time of day**: `timed_export` during the export window, `eco`
the rest of the time. GE Cloud's "Timed Export" preset does this server-side
— but GE Cloud is withdrawn for Pi-managed inverters (e.g. David), and the
cloud control-intent plane isn't wired into `birdy_ha` yet. So the switch
must run **locally in `birdy_ha`**, which already holds the Modbus write path.

## 3. Design

A small scheduler in the master runtime that, each tick, computes the
**intended** `batteryMode` from the clock + config and writes it **only on
transition** (idempotent).

### 3.1 Config (`devices.meta.export_schedule`, per tenant, read via cloud)
```jsonc
{
  "enabled": true,
  "window_start": "16:00",   // London local
  "window_end":   "19:00",
  "mode_in":  "timed_export",     // eco | timed_discharge | timed_export
  "mode_out": "eco"               // mode outside the window
}
```
Absent / `enabled:false` → scheduler is a no-op (today's behaviour).

### 3.2 Loop
- Runs in the master publisher loop (same place as the settings coordinator),
  evaluated once per tick (~15 s is plenty; the transition only matters to
  the minute).
- Compute `want = mode_in if now∈[start,end) else mode_out` (London local,
  DST via `zoneinfo`, wrap-around windows allowed like the existing slot logic).
- Read the **current** `batteryMode` from the settings cache.
- If `want != current` → `settings.apply_setting("batteryMode", want)` (goes
  through the existing rate-limit + confirm-read path). Otherwise do nothing.
- The discharge-slot **times** the mode uses come from the `dcDischarge1/2`
  time entities, as today — keep them aligned with `window_start/end`.

### 3.3 Manual-override coexistence
- If the user picks a mode in HA that disagrees with the schedule, respect it
  for a grace period: stamp `manual_override_until` (e.g. now + 2 h) whenever
  a mode write arrives that the scheduler didn't originate, and skip scheduler
  writes until it expires. Prevents a fight between the human and the clock.
- Distinguish scheduler writes from human writes via a flag on the write call
  (extend `apply_setting` with an internal `origin="scheduler"` kwarg, or a
  private setter that bypasses the override stamp).

### 3.4 Safety
- **Kill-switch env** `BIRDY_EXPORT_SCHEDULER_DISABLED=1` → never writes.
- Only ever writes `batteryMode` (never reserve/cutoff/slots) → can't deepen
  the discharge floor. (The `eco` write already preserves reserve — it does
  NOT call `set_mode_dynamic`'s reserve=4 reset; see settings.py.)
- Master-only (monitors already can't write).
- Bounded by the existing per-key write rate-limit.

### 3.5 Edge cases
- **Pi restart mid-window:** first tick recomputes `want` and converges — no
  state needed beyond the config + the live read.
- **Confirm-read churn:** the mode write's confirm-read re-derives `batteryMode`
  from r59/r27; a corrupt-frame read returns None and must NOT trigger a revert
  (the 0.11.10 supersede-guard already handles this).
- **DST / wrap-around windows:** reuse the slot time helpers.

## 4. Verification (before enabling on a live tenant)
1. On the emulator: drive the clock across the window boundary, assert one
   `apply_setting("batteryMode", …)` per transition and none in between.
2. On David's HY-5.0 (a real evening): confirm at `window_end` the mode flips
   to `eco`, the battery starts covering house load, and grid import drops to
   ~0 while SOC > reserve; at `window_start` it flips to `timed_export` and
   exports. Watch `device_telemetry` battery/grid power around 16:00 and 19:00.
3. Confirm a manual mode change in HA sticks for the grace period.

## 5. Alternative considered
Cloud planner → `control_intents` → a `birdy_ha` intent executor. Cleaner
long-term (one control plane for GE + Victron) but needs the intent executor
built in `birdy_ha` first (see energy-monitor `docs/architecture/ha-control-intents-executor.md`).
The local scheduler above is the pragmatic near-term path and is independent
of that work.
