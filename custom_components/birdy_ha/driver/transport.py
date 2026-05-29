"""Async Modbus-YY transport for the HA master integration.

Wraps `givenergy_modbus.client.client.Client` (givenergy-modbus 1.x —
async-native). Reads via `refresh_plant()` populate `client.plant` in
place; writes go via `one_shot_command([requests])` with command lists
built from `givenergy_modbus.client.commands.*`.

This module is the boundary between the inverter LAN context and the
HA Master domain. Above it, everything is async + typed; below it, the
library's async API. Anti-corruption layer per R9.

DISCOVERY NOTE: `async_scan_for_dongle()` performs an active TCP
port-8899 sweep across all 254 hosts on the host's /24. This is
harmless on a home LAN but can trip an IDS/IPS on corporate or
monitored networks. Callers MUST short-circuit (pass the configured
inverter host) whenever one is already known — the function itself
will also early-return if `known_host` is supplied. See finding L4.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

try:
    from givenergy_modbus.client.client import Client
    from givenergy_modbus.client import commands
    from givenergy_modbus.model import TimeSlot
    from givenergy_modbus.model.plant import Plant
except ImportError:  # pragma: no cover - lib installed by HA at runtime
    Client = None  # type: ignore[assignment]
    commands = None  # type: ignore[assignment]
    TimeSlot = None  # type: ignore[assignment]
    Plant = Any  # type: ignore[assignment]


def _refresh_plant_requests(plant, full_refresh: bool):
    """Build the refresh_plant request list without going through
    Client.refresh_plant, so we can pass return_exceptions=True to
    execute() and tolerate shape_hash collisions.
    """
    if commands is None:
        return []
    return commands.refresh_plant_data(
        full_refresh, plant.number_batteries, max_batteries=5,
    )

from ..const import (
    LAN_SCAN_MAX_WORKERS,
    LAN_SCAN_TIMEOUT_S,
    MODBUS_INTER_CALL_GAP_S,
    MODBUS_PORT,
)

_LOGGER = logging.getLogger(__name__)


# ─── LAN discovery ───────────────────────────────────────────────────


async def async_scan_for_dongle(
    cidr: Optional[str] = None,
    timeout_s: float = LAN_SCAN_TIMEOUT_S,
    max_workers: int = LAN_SCAN_MAX_WORKERS,
    known_host: Optional[str] = None,
) -> Optional[str]:
    """Probe the local /24 for an open Modbus-YY port (8899).

    Passing `known_host` short-circuits the sweep — the function
    returns that host verbatim without touching the network. This is
    the preferred path for any caller that already has the inverter
    IP cached in entry data (R9 / finding L4). Sweep is reserved for
    first-time-install discovery only.
    """
    if known_host:
        return known_host
    candidates = _candidate_ips(cidr)
    if not candidates:
        return None
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        tasks = [
            loop.run_in_executor(pool, _probe_one, ip, MODBUS_PORT, timeout_s)
            for ip in candidates
        ]
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
            except Exception:
                continue
            if result:
                return result
    return None


def _candidate_ips(cidr: Optional[str]) -> list[str]:
    if cidr:
        try:
            base, mask = cidr.split("/")
            if mask != "24":
                return [base]
            parts = base.split(".")
            return [f"{parts[0]}.{parts[1]}.{parts[2]}.{i}" for i in range(1, 255)]
        except (ValueError, IndexError):
            return []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            host_ip = s.getsockname()[0]
    except OSError:
        return []
    parts = host_ip.split(".")
    if len(parts) != 4:
        return []
    return [f"{parts[0]}.{parts[1]}.{parts[2]}.{i}" for i in range(1, 255)]


def _probe_one(ip: str, port: int, timeout_s: float) -> Optional[str]:
    try:
        with socket.create_connection((ip, port), timeout=timeout_s):
            return ip
    except OSError:
        return None


# ─── Async transport ─────────────────────────────────────────────────


class ModbusTransport:
    """Owns one async `Client` bound to a host:port.

    All public methods are async and serialise through a per-instance
    asyncio.Lock so reads and writes never interleave on the
    single-shot Modbus-YY relay (S13 mitigation).
    """

    def __init__(self, host: str, port: int = MODBUS_PORT) -> None:
        if Client is None:
            raise RuntimeError(
                "givenergy-modbus not installed; "
                "HA installs it from manifest.requirements at startup"
            )
        self._host = host
        self._port = port
        self._client: Optional["Client"] = None
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def host(self) -> str:
        return self._host

    async def _ensure_connected(self) -> "Client":
        if self._client is None:
            self._client = Client(self._host, self._port, connect_timeout=2.0)
        if not self._connected:
            await self._client.connect()
            self._connected = True
        return self._client

    async def refresh(self, full: bool = True) -> Any:
        """Pull a fresh Plant snapshot from the inverter.

        The library mutates client.plant in place; we return that
        instance so downstream code can hold a reference.

        `timeout=10.0` (default 1.0) — `refresh_plant` fires every
        request in parallel via `asyncio.gather`; on a multi-pack
        system that's ~15 frames against a single-shot dongle that
        serialises responses at ~250 ms each. The last frames land
        well past the 1 s per-request default and the library gives
        up. 10 s is comfortably above the worst observed wall-clock
        and below our INITIAL_PROBE_TIMEOUT_S (30 s) so a real
        failure still surfaces promptly.

        On any error (including CancelledError from an outer
        wait_for), close the underlying Client so the next call
        forces a reconnect. The library's producer coroutine is
        started by `connect()` and stops on first cancellation —
        once dead, `refresh_plant` fails forever with
        "TX queue full — producer task has likely died" until the
        connection is rebuilt.
        """
        async with self._lock:
            client = await self._ensure_connected()
            try:
                # Bypass Client.refresh_plant — it calls execute()
                # with return_exceptions=False, so any one of the
                # ~15 requests raising CancelledError (which happens
                # routinely because shape_hash collides on every
                # request for the same slave_address — base_register
                # isn't part of the hash) bubbles up and we never
                # see the data. The consumer task still calls
                # plant.update() for every response that arrives,
                # so the plant is correctly populated regardless.
                reqs = _refresh_plant_requests(client.plant, full)
                await client.execute(
                    reqs, timeout=10.0, retries=0, return_exceptions=True,
                )
                await asyncio.sleep(MODBUS_INTER_CALL_GAP_S)
                return client.plant
            except BaseException:
                await self._close_quietly()
                raise

    async def _close_quietly(self) -> None:
        """Tear down the client without raising on a connection that
        was never fully established (or already in a bad state).

        `Client.close()` is an async def — calling it without await
        returns a coroutine that's never scheduled, so the underlying
        TCP socket + reader/writer streams + producer/consumer tasks
        leak. On the dongle side that surfaces as many half-open
        connections and no new requests get serviced. Always await.
        """
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self._client = None
        self._connected = False

    async def one_shot_command(self, command_requests: list) -> None:
        """Send a list of TransparentRequest commands. Used by the
        settings writer.

        Notably DOES NOT call `client.one_shot_command()` — that method
        in givenergy-modbus 1.3.0 always calls `await self.connect()`
        at the top, which opens a fresh TCP socket every time. With
        the single-shot dongle that races the existing read connection
        and both can hang. Instead we call `client.execute()` on the
        already-connected client, matching what refresh() does.

        Timeout is 10 s (vs the library default 1.5 s) because the
        dongle's response latency under our read+write contention
        regularly exceeds 1.5 s. return_exceptions=True so that if
        one write of an N-register settings command fails, the others
        still go out and any retry strategy can be decided at our
        layer rather than the library raising on the first miss.
        """
        async with self._lock:
            client = await self._ensure_connected()
            try:
                await client.execute(
                    command_requests, timeout=10.0, retries=0,
                    return_exceptions=True,
                )
                await asyncio.sleep(MODBUS_INTER_CALL_GAP_S)
            except BaseException:
                await self._close_quietly()
                raise

    async def close(self) -> None:
        if self._client is not None and self._connected:
            try:
                await self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._connected = False
