from __future__ import annotations

import asyncio
import struct

import pytest

from fronius_emulator.healthcheck import check
from fronius_emulator.modbus import ModbusTcpServer, RegisterBank


def test_register_bank_replaces_snapshot_atomically() -> None:
    bank = RegisterBank({10: 1, 11: 2})
    assert bank.read(10, 2) == [1, 2]
    assert bank.read(9, 1) is None

    bank.replace({10: 3})
    assert bank.read(10, 1) == [3]
    assert bank.read(11, 1) is None


def test_fc03_response_and_exceptions() -> None:
    server = ModbusTcpServer(RegisterBank({40000: 0x5375, 40001: 0x6E53}), "", 0, 2)

    assert server._handle_pdu(2, struct.pack(">BHH", 3, 40000, 2)) == bytes(
        (3, 4, 0x53, 0x75, 0x6E, 0x53)
    )
    assert server._handle_pdu(2, struct.pack(">BHH", 4, 40000, 2)) == bytes((0x84, 1))
    assert server._handle_pdu(2, struct.pack(">BHH", 3, 39999, 2)) == bytes((0x83, 2))
    assert server._handle_pdu(3, struct.pack(">BHH", 3, 40000, 2)) == bytes((0x83, 0x0B))


def test_healthcheck_reads_live_server() -> None:
    async def scenario() -> None:
        server = ModbusTcpServer(RegisterBank({40000: 0x5375, 40001: 0x6E53}), "127.0.0.1", 0, 2)
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        try:
            await asyncio.to_thread(check, "127.0.0.1", port, 2)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_register_bank_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        RegisterBank({-1: 0})
    with pytest.raises(ValueError):
        RegisterBank({1: 0x10000})
