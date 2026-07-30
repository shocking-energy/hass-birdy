"""Runtime patches for upstream library bugs.

Each patch:
  - Is idempotent (safe to call multiple times)
  - Documents the upstream bug + verification evidence
  - Should be removed once a released library version contains the fix

Patches are applied at integration module import time so they're in
place before any HaMaster instance runs its first poll.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)
_APPLIED: set[str] = set()


def _patch_givenergy_modbus_multi_battery() -> None:
    """Fix givenergy-modbus 1.3.0's `refresh_plant_data()`.

    Bug: the function only requests input-register block 60-119 on
    each battery slave (0x33+). Block 0-59 — which holds SOC, temp,
    cells, serial, health, totalKwh, capacity — is never requested,
    so those fields come through as zero on any inverter with one or
    more battery packs.

    Verified against:
      - GIV-HY5.0 with 4 battery packs (David Bird tenant), 2026-05-28
      - Library version: givenergy-modbus 1.3.0

    Fix: append an extra ReadInputRegistersRequest(base_register=0)
    per battery slave inside the same loop. The library's Plant
    handler already knows how to merge block-0 responses; the bug is
    purely in the request enumeration.

    Drop this patch when the upstream library releases the fix.
    """
    key = "givenergy_modbus_multi_battery"
    if key in _APPLIED:
        return
    try:
        from givenergy_modbus.client import commands
        from givenergy_modbus.pdu.read_registers import (
            ReadInputRegistersRequest,
        )
    except ImportError:
        _LOGGER.debug("givenergy-modbus not importable yet; skipping %s", key)
        return

    original = commands.refresh_plant_data

    def patched(
        complete: bool,
        number_batteries: int = 1,
        max_batteries: int = 5,
    ):
        reqs = original(complete, number_batteries, max_batteries)
        # Mirror the original's effective battery count: `complete`
        # forces it to max_batteries, otherwise number_batteries.
        n = max_batteries if complete else number_batteries
        # Skip i=0 — that slave (0x32) is the INVERTER and the
        # original function already reads its block 0 at line 48.
        # The loop iterates i in [1, n) so we only add reads for
        # the actual battery slaves (0x33 onwards). Duplicating the
        # inverter request collided with the library's request-
        # dedup table (`expected_responses`) — the duplicate's
        # shape_hash matched the original's pending future, the
        # library logged "Cancelling existing in-flight request",
        # killed the original, and the response (when it came)
        # had no waiter. refresh_plant raised on every cycle.
        for i in range(1, n):
            reqs.append(
                ReadInputRegistersRequest(
                    base_register=0,
                    register_count=60,
                    slave_address=0x32 + i,
                )
            )
        return reqs

    commands.refresh_plant_data = patched
    _APPLIED.add(key)
    _LOGGER.info(
        "Applied givenergy-modbus multi-battery refresh patch "
        "(reads battery input regs 0-59 + 60-119 per slave)"
    )


def _patch_givenergy_modbus_shape_hash_collision() -> None:
    """Make Read{Input,Holding}Registers{Request,Response}.shape_hash()
    distinguish on `base_register` (and `register_count`), not only
    on `slave_address`.

    Bug: TransparentMessage._extra_shape_hash_keys() returns
    `(slave_address,)`. Concrete Read register requests don't
    override it, so every read against a given slave (regardless of
    which register block) produces the same shape_hash. The
    Client's `expected_responses` dict keys on that hash — when
    refresh_plant_data emits 4 input-register reads for the
    inverter (bases 0, 60, 120, 180), each new send_request_and_
    await_response sees the previous one's pending future, calls
    `existing_response_future.cancel()`, and the prior await raises
    CancelledError. gather() inside execute() propagates the first
    exception and refresh_plant raises — even though
    _task_network_consumer happily called plant.update() for every
    response that arrived.

    Symptom on a working dongle: telemetry mostly populates BUT a
    handful of inverter / battery registers (the ones whose reads
    lost the dedup race) come through as zero — battery.soc,
    inverter.serial, inverter.temp, etc.

    Fix: override _extra_shape_hash_keys on all four classes so
    every (type, function_code, slave_address, base_register,
    register_count) tuple hashes uniquely. Idempotent.
    """
    key = "givenergy_modbus_shape_hash_collision"
    if key in _APPLIED:
        return
    try:
        from givenergy_modbus.pdu.read_registers import (
            ReadInputRegistersRequest,
            ReadInputRegistersResponse,
            ReadHoldingRegistersRequest,
            ReadHoldingRegistersResponse,
        )
    except ImportError:
        _LOGGER.debug("givenergy-modbus not importable yet; skipping %s", key)
        return

    def _extra_shape_hash_keys(self):
        return (self.slave_address, self.base_register, self.register_count)

    for cls in (
        ReadInputRegistersRequest,
        ReadInputRegistersResponse,
        ReadHoldingRegistersRequest,
        ReadHoldingRegistersResponse,
    ):
        cls._extra_shape_hash_keys = _extra_shape_hash_keys  # type: ignore[assignment]
    _APPLIED.add(key)
    _LOGGER.info(
        "Applied givenergy-modbus shape_hash patch (base_register + "
        "register_count now part of read-register dedup hash)"
    )


def _patch_allow_export_limit_write() -> None:
    """Allow writing HR(26) grid_port_max_power_output (the "Export Limit").

    givenergy-modbus gates raw holding-register writes behind
    `WRITE_SAFE_REGISTERS` (checked in WriteHoldingRegisterRequest.
    ensure_valid_state); HR(26) is not on that list, so a plain
    WriteHoldingRegisterRequest(register=26, ...) raises InvalidPduState
    ("HR(26) is not safe to write to"). HR(26) IS a legitimate, documented
    settings register — GivTCP reads it as `Export_Limit`, the original
    library annotates it `P_GRID_PORT_MAX_OUTPUT # Export limit`, and it is
    what the GivEnergy portal's Export Limitation writes. We add it to the
    allowlist so the `exportPowerLimit` control can write it.

    Verified: read HR(26)=6000 then wrote 0 with read-back on David Bird's
    GIV-HY5.0 (library 1.3.0), 2026-07-30. Value is a plain uint16 watts.
    Idempotent. Drop when the library adds HR(26) to its own allowlist.
    """
    key = "allow_export_limit_write"
    if key in _APPLIED:
        return
    try:
        from givenergy_modbus.pdu import write_registers
    except ImportError:
        _LOGGER.debug("givenergy-modbus not importable yet; skipping %s", key)
        return
    write_registers.WRITE_SAFE_REGISTERS.add(26)
    _APPLIED.add(key)
    _LOGGER.info("Applied export-limit write patch (HR26 added to WRITE_SAFE_REGISTERS)")


def apply_all() -> None:
    """Apply every patch. Safe to call multiple times."""
    _patch_givenergy_modbus_multi_battery()
    _patch_givenergy_modbus_shape_hash_collision()
    _patch_allow_export_limit_write()
