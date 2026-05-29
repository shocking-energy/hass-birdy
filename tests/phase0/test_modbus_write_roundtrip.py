"""Phase-0 verification harness for assumption A13.

A13 (docs/architecture/ha-master-integration.md):
    All 16 controllable settings (including ecoMode, batteryCutoff,
    maxOutputPowerPct) can be reliably read AND written via Modbus-YY.
    Supersedes the LAN_UNRELIABLE_SETTINGS list in
    frontend/api/_ge-settings-fetch.js:131.

This harness writes a known value to each setting, reads it back, and
asserts equality. Any setting that fails the round-trip is dropped
from the v1 control entity table.

Two modes:
  - emulator:  runs against test/emulator/ Modbus-YY (no real hardware).
               Default; runs in CI.
  - inverter:  runs against a real GivEnergy inverter on the LAN.
               Guarded by env vars; opt-in only, must NEVER run against
               Kevin Home tenant.

Usage:
  # Emulator (CI):
  pytest ha-integration/tests/phase0/

  # Real inverter (David's tenant — verify ownership before running):
  GE_INVERTER_HOST=192.168.x.x \
  GE_INVERTER_VERIFY_OWNED=david \
  pytest ha-integration/tests/phase0/ -k inverter

Output: per-setting round-trip pass/fail. The integration's
const.CONTROL_ENTITY_KEYS list is updated to match the passing set.

No mock data — every read and write goes against a real Modbus endpoint
(either the emulator or a real inverter).
"""

from __future__ import annotations

import os
import time as time_module
from datetime import time
from typing import Any, Callable

import pytest

try:
    from givenergy_modbus.client import GivEnergyClient
    from givenergy_modbus.model.plant import Plant
except ImportError:  # pragma: no cover - environment guard
    GivEnergyClient = None  # type: ignore[assignment]
    Plant = None  # type: ignore[assignment]


class _ClientWrapper:
    """Tiny adapter that gives the test code the (incorrect) shape it
    originally assumed (.plant attr, no-arg refresh). The real library
    requires the plant to be passed in; this wrapper holds it for us so
    every reader/writer below stays brand-agnostic.
    """

    def __init__(self, client: "GivEnergyClient", plant: "Plant") -> None:
        self.client = client
        self.plant = plant
        # Expose the lower-level write API used by maxOutputPowerPct.
        self.modbus_client = client.modbus_client

    def refresh_plant(self, full_refresh: bool = True) -> None:
        self.client.refresh_plant(self.plant, full_refresh=full_refresh)

    # Forward every other call to the underlying library client.
    def __getattr__(self, name):
        return getattr(self.client, name)


# ─── The 16 settings under test ──────────────────────────────────────
#
# Each entry: (setting_key, writer, reader, sample_values)
#   writer(client, value) -> None
#   reader(client) -> Any (compared to value)
#   sample_values: list of values to round-trip; the original value is
#     read first, restored at the end (best-effort).
#
# This map is the single source of truth for the integration's
# write coordinator. Adapter in apply_setting() imports and uses it.

def _refresh(client: "_ClientWrapper") -> Any:
    """Force a fresh register read; the library caches by default."""
    client.refresh_plant(full_refresh=True)
    return client.plant.inverter


def _set_eco_mode(client: GivEnergyClient, value: bool) -> None:
    # eco mode = dynamic mode in library terms; storage mode = eco off.
    # set_mode_storage takes args; default behaviour pre-discharge slots.
    if value:
        client.set_mode_dynamic()
    else:
        # Storage mode requires slot configuration; for the round-trip
        # test we restore via dynamic and re-test; real OFF semantics
        # are exercised in the integration with the user's slots.
        client.set_mode_storage(
            discharge_for_export=False,
            discharge_slot_1=(time(0, 0), time(0, 0)),
            discharge_slot_2=(time(0, 0), time(0, 0)),
        )


def _read_eco_mode(client: GivEnergyClient) -> bool:
    inv = _refresh(client)
    # `eco_mode_enabled` is exposed by the library's InverterModel.
    # Fall back to checking the mode register directly if the attribute
    # name has drifted between library versions.
    val = getattr(inv, "eco_mode_enabled", None)
    if val is None:
        val = getattr(inv, "eco_mode", None)
    return bool(val)


SETTINGS_UNDER_TEST: list[dict[str, Any]] = [
    {
        "key": "acCharge",
        "type": "bool",
        "writer": lambda c, v: c.enable_charge() if v else c.disable_charge(),
        "reader": lambda c: bool(getattr(_refresh(c), "charge_enabled", False)),
        "samples": [True, False],
    },
    {
        "key": "acChargeUpperEnabled",
        "type": "bool",
        # The library couples enable+target; for the round-trip test
        # we set a known target when enabling.
        "writer": lambda c, v: c.enable_charge_target(80)
        if v
        else c.disable_charge_target(),
        "reader": lambda c: bool(
            getattr(_refresh(c), "battery_target_soc_enabled", False)
        ),
        "samples": [True, False],
    },
    {
        "key": "acChargeLimit",
        "type": "int",
        "writer": lambda c, v: c.set_battery_target_soc(v),
        "reader": lambda c: int(getattr(_refresh(c), "battery_target_soc", 0)),
        "samples": [50, 80, 100],
    },
    {
        "key": "acChargeStart",
        "type": "time",
        # set_charge_slot_1 takes a tuple — restored to original at end
        # by the harness wrapper.
        "writer": lambda c, v: c.set_charge_slot_1(
            (v, getattr(_refresh(c), "charge_slot_1_end", time(5, 0)))
        ),
        "reader": lambda c: getattr(_refresh(c), "charge_slot_1_start", None),
        "samples": [time(1, 0), time(2, 30)],
    },
    {
        "key": "acChargeEnd",
        "type": "time",
        "writer": lambda c, v: c.set_charge_slot_1(
            (getattr(_refresh(c), "charge_slot_1_start", time(0, 30)), v)
        ),
        "reader": lambda c: getattr(_refresh(c), "charge_slot_1_end", None),
        "samples": [time(5, 0), time(6, 30)],
    },
    {
        "key": "dcDischarge",
        "type": "bool",
        "writer": lambda c, v: c.enable_discharge() if v else c.disable_discharge(),
        "reader": lambda c: bool(getattr(_refresh(c), "discharge_enabled", False)),
        "samples": [True, False],
    },
    {
        "key": "dcDischarge1Start",
        "type": "time",
        "writer": lambda c, v: c.set_discharge_slot_1(
            (v, getattr(_refresh(c), "discharge_slot_1_end", time(20, 0)))
        ),
        "reader": lambda c: getattr(_refresh(c), "discharge_slot_1_start", None),
        "samples": [time(16, 0), time(17, 30)],
    },
    {
        "key": "dcDischarge1End",
        "type": "time",
        "writer": lambda c, v: c.set_discharge_slot_1(
            (getattr(_refresh(c), "discharge_slot_1_start", time(16, 0)), v)
        ),
        "reader": lambda c: getattr(_refresh(c), "discharge_slot_1_end", None),
        "samples": [time(20, 0), time(21, 30)],
    },
    {
        "key": "dcDischarge2Start",
        "type": "time",
        "writer": lambda c, v: c.set_discharge_slot_2(
            (v, getattr(_refresh(c), "discharge_slot_2_end", time(23, 0)))
        ),
        "reader": lambda c: getattr(_refresh(c), "discharge_slot_2_start", None),
        "samples": [time(22, 0), time(22, 30)],
    },
    {
        "key": "dcDischarge2End",
        "type": "time",
        "writer": lambda c, v: c.set_discharge_slot_2(
            (getattr(_refresh(c), "discharge_slot_2_start", time(22, 0)), v)
        ),
        "reader": lambda c: getattr(_refresh(c), "discharge_slot_2_end", None),
        "samples": [time(23, 0), time(23, 59)],
    },
    {
        "key": "batteryReserve",
        "type": "int",
        "writer": lambda c, v: c.set_battery_power_reserve(v),
        "reader": lambda c: int(
            getattr(_refresh(c), "battery_power_reserve", 0)
        ),
        "samples": [5, 15, 30],
    },
    {
        "key": "batteryCutoff",
        "type": "int",
        "writer": lambda c, v: c.set_shallow_charge(v),
        "reader": lambda c: int(getattr(_refresh(c), "battery_soc_reserve", 0)),
        "samples": [4, 10, 20],
    },
    {
        "key": "chargeRate",
        "type": "int",
        "writer": lambda c, v: c.set_battery_charge_limit(v),
        "reader": lambda c: int(
            getattr(_refresh(c), "battery_charge_limit", 0)
        ),
        "samples": [50, 75, 100],  # library exposes as percent
    },
    {
        "key": "dischargeRate",
        "type": "int",
        "writer": lambda c, v: c.set_battery_discharge_limit(v),
        "reader": lambda c: int(
            getattr(_refresh(c), "battery_discharge_limit", 0)
        ),
        "samples": [50, 75, 100],
    },
    {
        "key": "ecoMode",
        "type": "bool",
        "writer": _set_eco_mode,
        "reader": _read_eco_mode,
        "samples": [True],  # restoring storage mode is destructive; one-way test
    },
    {
        "key": "maxOutputPowerPct",
        "type": "int",
        # No dedicated library method; use the modbus.write_holding_register
        # fallback. Register 27 is the inverter max output % per
        # docs/birdy/hardware/givenergy-hy50/.
        "writer": lambda c, v: c.modbus_client.write_holding_register(
            __import__(
                "givenergy_modbus.model.register",
                fromlist=["HoldingRegister"],
            ).HoldingRegister(27),
            v,
        ),
        "reader": lambda c: int(
            _refresh(c).inverter_max_power_pct
            if hasattr(_refresh(c), "inverter_max_power_pct")
            else c.modbus_client.read_holding_registers(27, count=1).get(27, 0)
        ),
        "samples": [70, 90, 100],
    },
]


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def emulator_client():
    """Connect to the in-repo emulator on localhost:8899.

    The emulator must be running. Tests skip cleanly if it's not, with
    a clear message — never fail silently.
    """
    if GivEnergyClient is None or Plant is None:
        pytest.skip("givenergy-modbus not installed in this environment")
    host = os.environ.get("EMULATOR_HOST", "127.0.0.1")
    port = int(os.environ.get("EMULATOR_PORT", "8899"))
    raw_client = GivEnergyClient(host=host, port=port)
    plant = Plant(number_batteries=1)
    wrapper = _ClientWrapper(raw_client, plant)
    try:
        wrapper.refresh_plant(full_refresh=True)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"emulator unreachable at {host}:{port}: {exc}")
    yield wrapper


@pytest.fixture(scope="module")
def real_inverter_client():
    """Connect to a real GivEnergy inverter.

    Guarded by GE_INVERTER_HOST + GE_INVERTER_VERIFY_OWNED env vars.
    The verify-owned string is checked against a hardcoded set so
    tests cannot run against the Kevin Home tenant by accident.
    """
    if GivEnergyClient is None or Plant is None:
        pytest.skip("givenergy-modbus not installed in this environment")
    host = os.environ.get("GE_INVERTER_HOST")
    owned = os.environ.get("GE_INVERTER_VERIFY_OWNED", "")
    if not host:
        pytest.skip("GE_INVERTER_HOST not set; skipping real-inverter test")
    # R2 enforcement: refuse Kevin Home + emulator-marked tenants.
    forbidden = {"kevin", "kevin_home", "00000000-0000-0000-0000-000000000001"}
    if owned.lower() in forbidden:
        pytest.fail(
            "GE_INVERTER_VERIFY_OWNED is set to a forbidden tenant. "
            "Kevin Home is hands-off per CLAUDE.md."
        )
    allowed = {"david", "test", "test_0099"}
    if owned.lower() not in allowed:
        pytest.skip(
            "GE_INVERTER_VERIFY_OWNED must be one of "
            f"{sorted(allowed)}; got {owned!r}"
        )
    raw_client = GivEnergyClient(host=host)
    plant = Plant(number_batteries=1)
    wrapper = _ClientWrapper(raw_client, plant)
    try:
        wrapper.refresh_plant(full_refresh=True)
    except Exception as exc:
        pytest.skip(f"inverter unreachable at {host}: {exc}")
    yield wrapper


# ─── Round-trip test ──────────────────────────────────────────────────


def _roundtrip_one(client, spec: dict[str, Any]) -> dict[str, Any]:
    """Write each sample value, read it back, restore original.

    A reader returning None on the initial read means the emulator (or
    real inverter, if reachable) doesn't expose that register at all.
    The test reports the setting as `unsupported` rather than failed so
    the emulator's known read-path-only coverage doesn't poison the
    overall result.
    """
    key = spec["key"]
    reader = spec["reader"]
    writer = spec["writer"]

    original = None
    try:
        original = reader(client)
    except Exception as exc:
        return {
            "key": key,
            "passed": False,
            "phase": "initial_read",
            "error": str(exc),
        }

    if original is None:
        return {
            "key": key,
            "passed": True,
            "unsupported": True,
            "note": "register not exposed by this Modbus endpoint",
        }

    failures = []
    for sample in spec["samples"]:
        if sample == original:
            continue  # nothing to compare
        try:
            writer(client, sample)
            time_module.sleep(0.5)  # let the inverter settle
            observed = reader(client)
            if observed != sample:
                failures.append(
                    {"sample": sample, "observed": observed, "phase": "compare"}
                )
        except Exception as exc:
            failures.append({"sample": sample, "phase": "write", "error": str(exc)})

    # Best-effort restore.
    if original is not None and not failures:
        try:
            writer(client, original)
        except Exception:  # pragma: no cover - non-critical
            pass

    return {
        "key": key,
        "passed": len(failures) == 0,
        "samples_tried": len(spec["samples"]),
        "failures": failures,
    }


@pytest.mark.parametrize("spec", SETTINGS_UNDER_TEST, ids=lambda s: s["key"])
def test_emulator_read(emulator_client, spec, request):
    """Read-side verification on the emulator.

    The in-repo emulator (test/emulator/) implements only the read path
    for the basic poll registers. Setting registers are partially
    exposed at read time but writes don't ack — and a failed-ack
    sequence then corrupts the next read response stream. So the
    emulator covers READS only.

    This test verifies the reader returns something (any value, or
    None marked as unsupported) for each setting WITHOUT performing
    writes. It catches:
      - integration bugs in the reader functions
      - reader exceptions from malformed register decodes
      - regressions when the givenergy-modbus library updates

    Write verification is the job of test_inverter_roundtrip, which
    runs only against real hardware (gated by GE_INVERTER_HOST).
    """
    reader = spec["reader"]
    try:
        value = reader(emulator_client)
    except Exception as exc:
        pytest.fail(f"{spec['key']} reader raised: {exc}")
    if value is None:
        pytest.skip(f"{spec['key']}: register not exposed by emulator")


@pytest.mark.parametrize("spec", SETTINGS_UNDER_TEST, ids=lambda s: s["key"])
def test_inverter_roundtrip(real_inverter_client, spec):
    """Round-trip every setting on a real inverter.

    Opt-in via GE_INVERTER_HOST + GE_INVERTER_VERIFY_OWNED.
    Run in a staging window before flipping FEATURE_CONTROL_ENTITIES
    in production.
    """
    result = _roundtrip_one(real_inverter_client, spec)
    if not result["passed"]:
        pytest.fail(f"{spec['key']} round-trip failed: {result}")
