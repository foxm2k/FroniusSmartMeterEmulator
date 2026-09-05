from __future__ import annotations

import asyncio
import struct

import pytest

from fronius_emulator.healthcheck import check
from fronius_emulator.modbus import ModbusTcpServer, RegisterBank, _describe_request
from fronius_emulator.probe import read_holding_registers


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


def test_request_description_rejects_invalid_documented_range() -> None:
    description = _describe_request(2, struct.pack(">BHH", 3, 0xFFFF, 2))
    assert "protocol_address=65535 count=2 documented_registers=invalid" in description


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


def test_live_server_logs_fc03_request(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="fronius_emulator.modbus")

    async def scenario() -> None:
        server = ModbusTcpServer(RegisterBank({40000: 0x5375, 40001: 0x6E53}), "127.0.0.1", 0, 2)
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        try:
            values = await asyncio.to_thread(
                read_holding_registers,
                "127.0.0.1",
                port,
                2,
                40000,
                2,
                transaction_id=17,
            )
            assert values == [0x5375, 0x6E53]
            repeated = await asyncio.to_thread(
                read_holding_registers,
                "127.0.0.1",
                port,
                2,
                40000,
                2,
                transaction_id=18,
            )
            assert repeated == values
        finally:
            await server.close()

    asyncio.run(scenario())

    assert any(
        "Modbus request peer=127.0.0.1:" in message
        and "tx=17 unit=2 fc=3 protocol_address=40000 count=2 "
        "documented_registers=40001-40002 result=ok"
        " since_same_ms=-"
        in message
        for message in caplog.messages
    )
    repeated_message = next(message for message in caplog.messages if "tx=18 " in message)
    interval_ms = repeated_message.rsplit("since_same_ms=", 1)[1]
    assert float(interval_ms) >= 0


def test_register_bank_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        RegisterBank({-1: 0})
    with pytest.raises(ValueError):
        RegisterBank({1: 0x10000})


def test_close_disconnects_existing_clients() -> None:
    async def scenario():
        server = ModbusTcpServer(RegisterBank({40000: 0x5375}), "127.0.0.1", 0, 2)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(struct.pack(">HHHB BHH", 1, 0, 6, 2, 3, 40000, 1))
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(11), 2)
            await asyncio.wait_for(server.close(), 2)
            assert await asyncio.wait_for(reader.read(), 2) == b""
        finally:
            writer.close()
            await writer.wait_closed()
            await server.close()

    asyncio.run(scenario())
