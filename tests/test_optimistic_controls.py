"""The export-limit control must never fabricate a reading.

HR26 ("Export Limit") cannot be read back through the plant model, so the
control is write-only: its value is knowable only if we set it ourselves this
process. It used to seed 0 — which on this inverter means "no grid export at
all" — and because the cache is in-memory that fabricated 0 came back on every
HA restart. On a tenant whose HR26 is set by an external script (David: the
16:00-19:00 export coordinator) the field was therefore permanently wrong,
reporting "export disabled" while the register held 2500.

That is not merely cosmetic: an export limit genuinely stuck at 0 wedges the
GE's MPPT at dawn, so the fabricated 0 pointed anyone diagnosing that straight
at a non-existent cause.

Correct behaviour: stay AVAILABLE (so it remains settable) but report None, so
HA renders "unknown". Home Assistant is stubbed — the logic under test touches
neither HA nor Modbus.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

BIRDY = Path(__file__).resolve().parents[1] / "custom_components" / "birdy_ha"


def _subscriptable(name):
    """A stub base class that tolerates `Base[T]` generic subscripting."""
    return type(name, (object,), {
        "__init__": lambda self, *a, **k: None,
        "__class_getitem__": classmethod(lambda cls, item: cls),
    })


def _load():
    stubs = (
        "homeassistant", "homeassistant.components", "homeassistant.components.number",
        "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers",
        "homeassistant.helpers.entity_platform", "homeassistant.helpers.update_coordinator",
        "homeassistant.helpers.entity", "aiohttp",
        "givenergy_modbus", "givenergy_modbus.client", "givenergy_modbus.client.client",
        "givenergy_modbus.model", "givenergy_modbus.model.plant", "givenergy_modbus.pdu",
    )
    for n in stubs:
        m = types.ModuleType(n)
        m.__path__ = []
        sys.modules.setdefault(n, m)
    num = sys.modules["homeassistant.components.number"]
    num.NumberEntity = _subscriptable("NumberEntity")
    num.NumberMode = types.SimpleNamespace(BOX="box", SLIDER="slider")
    uc = sys.modules["homeassistant.helpers.update_coordinator"]
    uc.CoordinatorEntity = _subscriptable("CoordinatorEntity")
    uc.DataUpdateCoordinator = _subscriptable("DataUpdateCoordinator")
    sys.modules["homeassistant.config_entries"].ConfigEntry = _subscriptable("ConfigEntry")
    sys.modules["homeassistant.core"].HomeAssistant = _subscriptable("HomeAssistant")
    sys.modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
    sys.modules["homeassistant.helpers.entity"].EntityCategory = types.SimpleNamespace(CONFIG="config")

    pkg = types.ModuleType("birdy_ha")
    pkg.__path__ = [str(BIRDY)]
    sys.modules.setdefault("birdy_ha", pkg)

    loaded = {}
    for mod, fname in (("const", "const.py"), ("coordinator", "coordinator.py"), ("number", "number.py")):
        path = BIRDY / fname
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location(f"birdy_ha.{mod}", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[f"birdy_ha.{mod}"] = m
        try:
            spec.loader.exec_module(m)
            loaded[mod] = m
        except Exception as exc:  # pragma: no cover - surfaced by the tests below
            loaded[mod] = exc
    return loaded


MODS = _load()
const = MODS["const"]


# ── the contract, testable without HA at all ────────────────────────────────

def test_export_limit_is_declared_write_only():
    spec = const.CONTROL_ENTITY_KEYS["exportPowerLimit"]
    assert spec.get("optimistic") is True, (
        "exportPowerLimit must be marked optimistic — it cannot be read back, "
        "so the entity must not invent a value"
    )


def test_readable_controls_are_not_marked_optimistic():
    """Only genuinely unreadable controls get the flag — otherwise a real
    read failure would silently render as 'unknown' instead of unavailable."""
    for key, spec in const.CONTROL_ENTITY_KEYS.items():
        if key == "exportPowerLimit":
            continue
        assert not spec.get("optimistic"), f"{key} should not be optimistic"


def test_zero_is_a_meaningful_value_for_this_control():
    """Guards the reasoning: 0 is in range and means 'no export', which is
    exactly why seeding it as a placeholder was dangerous."""
    spec = const.CONTROL_ENTITY_KEYS["exportPowerLimit"]
    assert spec["min"] == 0
    assert "0 to stop all grid export" in spec["desc"] or "0 = no export" in spec["desc"]


# ── the entity behaviour ────────────────────────────────────────────────────

class _Coord:
    def __init__(self, values):
        self.data = types.SimpleNamespace(values=values) if values is not None else None


def _make(key, values):
    number = MODS["number"]
    if isinstance(number, Exception):
        raise AssertionError(f"number.py failed to load: {number!r}")
    spec = const.CONTROL_ENTITY_KEYS[key]
    ent = number.BirdyNumber.__new__(number.BirdyNumber)
    ent.coordinator = _Coord(values)
    ent._key = key
    ent._desc = spec.get("desc")
    ent._optimistic = bool(spec.get("optimistic"))
    ent._attr_native_unit_of_measurement = spec["unit"]
    # super().available on a real Entity is True by default
    ent.__class__.__bases__[0].available = property(lambda self: True)
    return ent


def test_unwritten_export_limit_is_available_but_unknown():
    """The whole point: settable, but honest that we don't know the value."""
    ent = _make("exportPowerLimit", {})       # never written this process
    assert ent.available is True, "must stay settable — otherwise we remove control"
    assert ent.native_value is None, "must be unknown, NOT a fabricated 0"


def test_written_export_limit_reports_what_we_set():
    ent = _make("exportPowerLimit", {"exportPowerLimit": 2500})
    assert ent.available is True
    assert ent.native_value == 2500


def test_export_limit_present_as_none_is_still_unknown():
    """A failed write restores `previous`, which may be None."""
    ent = _make("exportPowerLimit", {"exportPowerLimit": None})
    assert ent.available is True
    assert ent.native_value is None


def test_a_zero_we_actually_wrote_is_reported_as_zero():
    """Once WE set 0, 0 is the truth and must display."""
    ent = _make("exportPowerLimit", {"exportPowerLimit": 0})
    assert ent.native_value == 0


def test_a_readable_control_still_goes_unavailable_when_missing():
    """Non-optimistic controls keep the old behaviour — a missing value means
    the read failed, and unavailable is the honest signal for that."""
    ent = _make("batteryReserve", {})
    assert ent.available is False


def test_no_coordinator_data_is_unavailable_even_when_optimistic():
    ent = _make("exportPowerLimit", None)
    assert ent.available is False
