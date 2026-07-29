"""Unit tests for InverterBinding.can_control / can_publish.

Covers the local-control opt-in (Phase 1 of the Victron-master swap): an
adopted MONITOR may control its own inverter when `local_control` is set,
WITHOUT gaining the right to write live_snapshot (can_publish stays
master-only). Default off → GE-only / HA-master installs are unchanged.

domain.py is HA-free — loaded standalone under a synthetic package so its
`from .const import ...` relative import resolves (mirrors
test_export_scheduler.py)."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

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


_load("const", "const.py")
domain = _load("domain", "domain.py")

InverterBinding = domain.InverterBinding
BindingState = domain.BindingState
PublisherRole = domain.PublisherRole


def _binding(role, state, local_control=False):
    return InverterBinding(
        integration_id="00000000-0000-0000-0000-000000000000",
        state=state,
        role=role,
        local_control=local_control,
    )


# ── backward-compat: existing GE-only / HA-master behaviour unchanged ──
def test_master_adopted_controls_and_publishes():
    b = _binding(PublisherRole.MASTER, BindingState.PUBLISHING)
    assert b.can_control is True
    assert b.can_publish is True


def test_monitor_without_flag_cannot_control():
    # The default. A GE-only monitor (no opt-in) must NOT control — unchanged.
    b = _binding(PublisherRole.MONITOR, BindingState.ADOPTED)
    assert b.local_control is False   # default
    assert b.can_control is False
    assert b.can_publish is False


# ── new: local-control opt-in ──
def test_monitor_with_flag_controls_but_never_publishes():
    b = _binding(PublisherRole.MONITOR, BindingState.ADOPTED, local_control=True)
    assert b.can_control is True          # <- the enabler
    assert b.can_publish is False         # <- SAFETY: live_snapshot stays master-only


def test_flag_ignored_before_adoption():
    # No claim yet → never control, flag or not.
    for st in (BindingState.UNPROVISIONED, BindingState.PROVISIONED):
        b = _binding(None, st, local_control=True)
        assert b.can_control is False
        assert b.can_publish is False


def test_master_unaffected_by_flag():
    # A master with the flag still behaves exactly like a master.
    b = _binding(PublisherRole.MASTER, BindingState.PUBLISHING, local_control=True)
    assert b.can_control is True
    assert b.can_publish is True
