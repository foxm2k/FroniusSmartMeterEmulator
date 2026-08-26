from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Mapping
from contextlib import suppress

LOGGER = logging.getLogger(__name__)

_MBAP = struct.Struct(">HHHB")
_READ_REQUEST = struct.Struct(">BHH")
_MAX_PDU_LENGTH = 253


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
