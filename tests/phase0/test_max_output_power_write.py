"""Verify the maxOutputPowerPct write targets HR(50) not HR(27).

The 0.9.0 release fixed a register-number bug: the raw write for
maxOutputPowerPct had been targeting HR(27) (`battery_power_mode` —
the register the library mislabels "eco mode") instead of HR(50)
(`active_power_rate` — the real output-curtailment register).
Writing the output-power% to register 27 would have silently flipped
eco mode and left curtailment untouched.

This is the single riskiest write in the integration (raw register,
direct physical effect on the inverter), so finding L3 of the
2026-05-29 security review asked for a dedicated round-trip test
asserting the request shape.

Hardware isn't available in the test env; we mock the transport and
assert the exact `WriteHoldingRegisterRequest` shape settings.py
issues. The assertions also lock in the 0-100 clamp.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


try:
    from givenergy_modbus.pdu import WriteHoldingRegisterRequest  # type: ignore
except ImportError:  # pragma: no cover - environment guard
    WriteHoldingRegisterRequest = None  # type: ignore[assignment]

try:
    from custom_components.birdy_ha.settings import SettingsAdapter
except ImportError:  # pragma: no cover - environment guard
    SettingsAdapter = None  # type: ignore[assignment]


def _make_adapter():
    """Build a SettingsAdapter wired to a mock master + mock transport.

    Only the bits `_issue_write` touches are populated; we don't
    exercise the rate-limit / cache / confirm-read paths in this test.
    """
    if SettingsAdapter is None:
        pytest.skip("integration not importable in this environment")
    master = MagicMock()
    transport = MagicMock()
    transport.one_shot_command = AsyncMock()
    adapter = SettingsAdapter(master)
    return adapter, transport


@pytest.mark.parametrize(
    "input_value, expected_register, expected_clamped",
    [
        (0, 50, 0),
        (50, 50, 50),
        (100, 50, 100),
        # Below-range clamps up to 0
        (-1, 50, 0),
        (-9999, 50, 0),
        # Above-range clamps down to 100
        (101, 50, 100),
        (9999, 50, 100),
    ],
    ids=[
        "min", "mid", "max",
        "neg_one", "neg_huge",
        "above_max", "way_above_max",
    ],
)
def test_max_output_power_writes_hr50_clamped(
    input_value, expected_register, expected_clamped,
):
    """The write must target HR(50), and values must be clamped to 0-100.

    Anything else is a regression of the bug the 0.9.0 fix closed.
    """
    if WriteHoldingRegisterRequest is None:
        pytest.skip("givenergy-modbus not installed in this environment")

    adapter, transport = _make_adapter()

    asyncio.run(
        adapter._issue_write(transport, "maxOutputPowerPct", input_value)
    )

    transport.one_shot_command.assert_awaited_once()
    args, _ = transport.one_shot_command.call_args
    requests = args[0]
    assert isinstance(requests, list) and len(requests) == 1, (
        f"expected exactly 1 request, got {requests!r}"
    )
    req = requests[0]
    assert isinstance(req, WriteHoldingRegisterRequest), (
        f"expected WriteHoldingRegisterRequest, got {type(req).__name__}"
    )
    # Pin the register number — this is the whole point of the test.
    assert req.register == expected_register, (
        f"WRONG REGISTER: writing maxOutputPowerPct={input_value} sent "
        f"to HR({req.register}), expected HR({expected_register}). "
        f"HR(27) is battery_power_mode, NOT max output power."
    )
    assert req.value == expected_clamped, (
        f"clamp regression: input={input_value} → "
        f"observed={req.value}, expected={expected_clamped}"
    )


def test_max_output_power_does_not_touch_hr27():
    """Defensive regression — under no circumstances should the write
    land on HR(27).

    HR(27) is `battery_power_mode` (the library's mislabelled "eco
    mode"). Writing 0-100 there would flip eco mode unpredictably
    AND leave the actual curtailment register untouched. Belt + braces
    on top of the parametrised test above.
    """
    if WriteHoldingRegisterRequest is None:
        pytest.skip("givenergy-modbus not installed in this environment")

    adapter, transport = _make_adapter()
    asyncio.run(
        adapter._issue_write(transport, "maxOutputPowerPct", 75)
    )
    args, _ = transport.one_shot_command.call_args
    req = args[0][0]
    assert req.register != 27, (
        "maxOutputPowerPct must NEVER write to HR(27) "
        "— that is battery_power_mode, not max output power"
    )
