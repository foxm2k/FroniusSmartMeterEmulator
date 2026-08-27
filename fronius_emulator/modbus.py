from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Mapping
from contextlib import suppress

LOGGER = logging.getLogger(__name__)

_MBAP = struct.Struct(">HHHB")
_READ_REQUEST = struct.Struct(">BHH")
_MAX_PDU_LENGTH = 253
_MAX_INTERVAL_SIGNATURES = 1024


def _peer_label(peer: object) -> str:
    if isinstance(peer, tuple) and len(peer) >= 2:
        host, port = peer[0], peer[1]
        if isinstance(host, str) and ":" in host:
            return f"[{host}]:{port}"
        return f"{host}:{port}"
    return str(peer)


def _peer_host(peer: object) -> str:
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return str(peer)


def _request_signature(unit_id: int, pdu: bytes) -> tuple[int, int, int, int] | None:
    if len(pdu) != _READ_REQUEST.size:
        return None
    function_code, start, count = _READ_REQUEST.unpack(pdu)
    return unit_id, function_code, start, count


def _describe_request(unit_id: int, pdu: bytes) -> str:
    if len(pdu) == _READ_REQUEST.size:
        function_code, start, count = _READ_REQUEST.unpack(pdu)
        valid_range = 1 <= count <= 125 and start + count <= 0x10000
        documented = f"{start + 1}-{start + count}" if valid_range else "invalid"
        return (
            f"unit={unit_id} fc={function_code} protocol_address={start} "
            f"count={count} documented_registers={documented}"
        )
    function_code = pdu[0] if pdu else None
    return f"unit={unit_id} fc={function_code} malformed_pdu_length={len(pdu)}"


def _response_result(pdu: bytes) -> str:
    if pdu and pdu[0] & 0x80:
        exception_code = pdu[1] if len(pdu) > 1 else "missing"
        return f"exception={exception_code}"
    return "ok"


class RegisterBank:
    """Atomically replaceable read-only holding-register snapshot."""

    def __init__(self, registers: Mapping[int, int]) -> None:
        self.replace(registers)

    def replace(self, registers: Mapping[int, int]) -> None:
        checked: dict[int, int] = {}
        for address, value in registers.items():
            if not 0 <= address <= 0xFFFF:
                raise ValueError(f"register address out of range: {address}")
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"register value out of range at {address}: {value}")
            checked[int(address)] = int(value)
        self._registers = checked

    def read(self, start: int, count: int) -> list[int] | None:
        registers = self._registers
        values: list[int] = []
        for address in range(start, start + count):
            value = registers.get(address)
            if value is None:
                return None
            values.append(value)
        return values


class ModbusTcpServer:
    """Small read-only Modbus TCP server implementing holding-register reads (FC03)."""

    def __init__(self, bank: RegisterBank, host: str, port: int, unit_id: int) -> None:
        self.bank = bank
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self._server: asyncio.Server | None = None
        self._last_request_at: dict[tuple[str, int, int, int, int], float] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        LOGGER.info(
            "Modbus TCP listening on %s:%d with unit ID %d",
            self.host,
            self.port,
            self.unit_id,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        LOGGER.debug("Modbus client connected: %s", peer)
        peer_name = _peer_label(peer)
        peer_host = _peer_host(peer)
        try:
            while True:
                try:
                    header = await reader.readexactly(_MBAP.size)
                except asyncio.IncompleteReadError:
                    break
                transaction_id, protocol_id, length, unit_id = _MBAP.unpack(header)
                if protocol_id != 0 or not 2 <= length <= _MAX_PDU_LENGTH + 1:
                    LOGGER.warning("Invalid Modbus MBAP header from %s", peer)
                    break
                try:
                    pdu = await reader.readexactly(length - 1)
                except asyncio.IncompleteReadError:
                    break
                response_pdu = self._handle_pdu(unit_id, pdu)
                response_result = _response_result(response_pdu)
                request_now = time.monotonic()
                signature = _request_signature(unit_id, pdu)
                since_same = "-"
                if signature is not None and response_result == "ok":
                    request_key = (peer_host, *signature)
                    previous_request = self._last_request_at.pop(request_key, None)
                    if (
                        previous_request is None
                        and len(self._last_request_at) >= _MAX_INTERVAL_SIGNATURES
                    ):
                        oldest_key = next(iter(self._last_request_at))
                        self._last_request_at.pop(oldest_key)
                    self._last_request_at[request_key] = request_now
                    if previous_request is not None:
                        since_same = f"{(request_now - previous_request) * 1000:.1f}"
                LOGGER.info(
                    "Modbus request peer=%s tx=%d %s result=%s since_same_ms=%s",
                    peer_name,
                    transaction_id,
                    _describe_request(unit_id, pdu),
                    response_result,
                    since_same,
                )
                response = (
                    _MBAP.pack(transaction_id, 0, len(response_pdu) + 1, unit_id) + response_pdu
                )
                writer.write(response)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            LOGGER.debug("Modbus client disconnected: %s", peer)

    def _handle_pdu(self, unit_id: int, pdu: bytes) -> bytes:
        if not pdu:
            return bytes((0x80, 0x03))
        function_code = pdu[0]
        if unit_id != self.unit_id:
            return bytes((function_code | 0x80, 0x0B))
        if function_code != 3:
            return bytes((function_code | 0x80, 0x01))
        if len(pdu) != _READ_REQUEST.size:
            return bytes((function_code | 0x80, 0x03))

        _, start, count = _READ_REQUEST.unpack(pdu)
        if count < 1 or count > 125 or start + count > 0x10000:
            return bytes((function_code | 0x80, 0x03))
        values = self.bank.read(start, count)
        if values is None:
            return bytes((function_code | 0x80, 0x02))

        payload = struct.pack(f">{count}H", *values)
        return bytes((function_code, len(payload))) + payload
