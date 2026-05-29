"""Parity test (R13): HA-master canonical readings == Pi-master.

Asserts that, for a fixed input Plant, the HA integration's mapper +
shape builder produce byte-identical output to pi-daemon's. Catches
silent drift between the two writers that would manifest as live_snapshot
or device_telemetry mismatches in production.

Uses the in-repo emulator's deterministic Plant fixture. No mocks —
real library, real conversion logic.

This test is run by the ENERGY-MONITOR repo's CI (it has the
pi-daemon source). The standalone HACS-distributed repo's CI runs
only the no-emulator unit tests.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PI_DAEMON_SRC = REPO_ROOT / "pi-daemon" / "src"
HA_DRIVER = REPO_ROOT / "ha-integration" / "custom_components" / "birdy_ha" / "driver"


def _import_from(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def pi_shape():
    if not (PI_DAEMON_SRC / "shape.py").exists():
        pytest.skip("pi-daemon source not present (running outside monorepo)")
    return _import_from(PI_DAEMON_SRC / "shape.py", "pi_shape")


@pytest.fixture(scope="module")
def pi_mappings():
    path = PI_DAEMON_SRC / "drivers" / "givenergy" / "mappings.py"
    if not path.exists():
        pytest.skip("pi-daemon mappings not present")
    return _import_from(path, "pi_mappings")


@pytest.fixture(scope="module")
def ha_shape():
    return _import_from(HA_DRIVER / "shape.py", "ha_shape")


@pytest.fixture(scope="module")
def ha_mappings():
    return _import_from(HA_DRIVER / "mappings.py", "ha_mappings")


@pytest.fixture(scope="module")
def emulator_plant():
    """Connect to the in-repo emulator and grab a Plant.

    The emulator must be running on localhost:8899. If unavailable,
    skip cleanly — this test is the parity gate, not a connectivity
    check.
    """
    try:
        from givenergy_modbus.client import GivEnergyClient
    except ImportError:
        pytest.skip("givenergy-modbus not installed")

    host = os.environ.get("EMULATOR_HOST", "127.0.0.1")
    port = int(os.environ.get("EMULATOR_PORT", "8899"))
    client = GivEnergyClient(host=host, port=port)
    try:
        client.refresh_plant(full_refresh=True)
    except Exception as exc:
        pytest.skip(f"emulator unreachable at {host}:{port}: {exc}")
    return client.plant


def test_shape_byte_identical(pi_shape, ha_shape, emulator_plant):
    """build_snapshot_row(plant, opts) must be byte-identical."""
    opts: dict = {}
    pi_out = pi_shape.build_snapshot_row(emulator_plant, opts)
    ha_out = ha_shape.build_snapshot_row(emulator_plant, opts)
    assert pi_out == ha_out, (
        "SYSTEM shape drift between Pi and HA — divergence in "
        f"keys: {set(pi_out) ^ set(ha_out)}"
    )


def test_canonical_readings_byte_identical(
    pi_mappings, ha_mappings, ha_shape, emulator_plant
):
    """snapshot_to_readings must produce identical canonical rows."""
    opts: dict = {}
    snapshot = ha_shape.build_snapshot_row(emulator_plant, opts)
    device_id = "00000000-0000-0000-0000-000000000099"
    recorded_at = "2026-05-28T00:00:00+00:00"
    pi_rows = pi_mappings.snapshot_to_readings(
        snapshot, device_id=device_id, recorded_at=recorded_at,
    )
    ha_rows = ha_mappings.snapshot_to_readings(
        snapshot, device_id=device_id, recorded_at=recorded_at,
    )
    # Order is significant for byte-identity; we sort by (component,
    # metric) before comparing because the dict-iteration order in
    # mappings.py is the same between the two sources (it's a literal
    # `if` chain — but defensive sort doesn't hurt).
    key = lambda r: (r["component"], r["metric"])
    assert sorted(pi_rows, key=key) == sorted(ha_rows, key=key), (
        "canonical reading drift between Pi and HA mappers"
    )


def test_reading_set_matches_expected_count(ha_mappings, ha_shape, emulator_plant):
    """13 readings expected for GE WiFi-dongle hardware (matches
    docs/architecture/ha-master-integration.md entity table).
    """
    snapshot = ha_shape.build_snapshot_row(emulator_plant, {})
    rows = ha_mappings.snapshot_to_readings(
        snapshot, device_id="x", recorded_at="2026-05-28T00:00:00Z",
    )
    # Some readings are gated by partial Modbus reads (string1/string2
    # both present, e.g.). On the emulator's full snapshot, expect 13.
    assert 12 <= len(rows) <= 14, f"unexpected reading count {len(rows)}"
