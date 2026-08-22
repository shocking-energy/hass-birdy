"""Regression tests for the device_telemetry publish guard.

Covers the 22P02 data-loss bug found on David's tenant 2026-08-22:
`_resolve_inverter_device_id()` returns None on any transient cloud
failure, the master poll path stamped every row `device_id="local"`, and
publish_device_telemetry's `(r->>'device_id')::uuid` cast then made
Postgres reject the ENTIRE cycle — ~18-20 readings/day silently lost
(297 over 16 days), each confirmed as a matching hole in device_telemetry.

runtime.py needs Home Assistant, which isn't a test dependency, so HA and
the Modbus library are stubbed in sys.modules before it's loaded — the
functions under test are pure and touch neither.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

BIRDY = Path(__file__).resolve().parents[1] / "custom_components" / "birdy_ha"

_STUBS = (
    "homeassistant", "homeassistant.config_entries", "homeassistant.core",
    "homeassistant.helpers", "homeassistant.helpers.entity",
    "homeassistant.helpers.update_coordinator", "homeassistant.const",
    "homeassistant.exceptions", "homeassistant.util", "homeassistant.util.dt",
    "aiohttp", "givenergy_modbus", "givenergy_modbus.client",
    "givenergy_modbus.client.client", "givenergy_modbus.model",
    "givenergy_modbus.model.plant",
)


def _load_runtime():
    for name in _STUBS:
        mod = types.ModuleType(name)
        mod.__path__ = []
        sys.modules.setdefault(name, mod)
    for attr, mod in (("ConfigEntry", "homeassistant.config_entries"),
                      ("HomeAssistant", "homeassistant.core")):
        setattr(sys.modules[mod], attr, type(attr, (object,), {}))
    pkg = types.ModuleType("birdy_ha")
    pkg.__path__ = [str(BIRDY)]
    sys.modules.setdefault("birdy_ha", pkg)
    spec = importlib.util.spec_from_file_location(
        "birdy_ha.runtime", BIRDY / "runtime.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["birdy_ha.runtime"] = mod
    spec.loader.exec_module(mod)
    return mod


rt = _load_runtime()

REAL_ID = "563486e3-4756-4a61-ba70-4e6f6c5b20ad"   # David's GE inverter


# ── _is_uuid ────────────────────────────────────────────────────────────

def test_accepts_a_real_device_id():
    assert rt._is_uuid(REAL_ID) is True


def test_rejects_the_local_placeholder():
    """The exact value that caused the outage."""
    assert rt._is_uuid("local") is False


def test_rejects_empty_and_non_strings():
    for bad in ("", None, 0, 123, [], {}, object()):
        assert rt._is_uuid(bad) is False, bad


def test_rejects_a_truncated_uuid():
    assert rt._is_uuid(REAL_ID[:-4]) is False


def test_accepts_uppercase_and_braced_forms():
    # uuid.UUID() tolerates both; Postgres casts both fine too.
    assert rt._is_uuid(REAL_ID.upper()) is True
    assert rt._is_uuid("{%s}" % REAL_ID) is True


# ── the filter as _publish_safe applies it ───────────────────────────────

class _Reading:
    def __init__(self, device_id):
        self.device_id = device_id


def _publishable(readings):
    """Mirror of the comprehension in _publish_safe."""
    return [r for r in readings if rt._is_uuid(r.device_id)]


def test_a_local_stamped_cycle_is_held_back_entirely():
    """Pre-fix this batch went to the RPC and Postgres threw it all out.
    Post-fix we skip it ourselves and wait for the next cycle."""
    cycle = [_Reading("local") for _ in range(13)]
    assert _publishable(cycle) == []


def test_a_resolved_cycle_publishes_every_row():
    cycle = [_Reading(REAL_ID) for _ in range(13)]
    assert len(_publishable(cycle)) == 13


def test_a_mixed_batch_keeps_only_the_valid_rows():
    cycle = [_Reading(REAL_ID), _Reading("local"), _Reading(REAL_ID)]
    assert len(_publishable(cycle)) == 2


# ── the device_id cache ─────────────────────────────────────────────────

class _FakeBinding:
    def __init__(self, serial, tenant_id="t"):
        self.tenant_id = tenant_id
        self.inverter = types.SimpleNamespace(inverter_serial=serial)


def _resolver_host(serial, cache=None):
    """A bare object carrying just what _resolve_inverter_device_id reads."""
    host = types.SimpleNamespace()
    host.binding = _FakeBinding(serial)
    host._device_id_cache = cache if cache is not None else {}
    host._cloud = None          # must never be touched on a cache hit
    return host


def test_cache_hit_short_circuits_before_any_cloud_call():
    """_cloud is None here, so reaching the network would raise. A hit
    must return without touching it — that's what removes ~4,300
    needless round-trips a day and with them the failure window."""
    host = _resolver_host("SD2246G165", {"SD2246G165": REAL_ID})
    got = asyncio.run(
        rt.HaMaster._resolve_inverter_device_id(host))    # type: ignore[arg-type]
    assert got == REAL_ID


def test_cache_is_keyed_by_serial_so_swapped_kit_refetches():
    """A different serial must NOT be served the old inverter's id."""
    host = _resolver_host("NEWSERIAL1", {"SD2246G165": REAL_ID})
    host._cloud = types.SimpleNamespace(_supabase_url="http://x", _session=None)
    got = asyncio.run(
        rt.HaMaster._resolve_inverter_device_id(host))    # type: ignore[arg-type]
    # Falls through to the lookup, which fails on the stubbed cloud →
    # None. The point is it did not return REAL_ID.
    assert got != REAL_ID


def test_missing_serial_resolves_to_none():
    host = _resolver_host("")
    assert asyncio.run(
        rt.HaMaster._resolve_inverter_device_id(host)) is None   # type: ignore[arg-type]


def test_unbound_tenant_resolves_to_none():
    host = _resolver_host("SD2246G165")
    host.binding.tenant_id = None
    assert asyncio.run(
        rt.HaMaster._resolve_inverter_device_id(host)) is None   # type: ignore[arg-type]
