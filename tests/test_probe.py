from __future__ import annotations

import asyncio

import pytest

from fronius_emulator.aggregate import aggregate_readings
from fronius_emulator.modbus import ModbusTcpServer, RegisterBank
from fronius_emulator.probe import probe
from fronius_emulator.sunspec import build_registers


def test_probe_validates_complete_live_register_chain() -> None:
    async def scenario() -> None:
        snapshot = aggregate_readings(
            [],
            {"shelly_1": 1234.5},
            {"shelly_1": "L1"},
            fallback_voltage_v=230.0,
            grid_frequency_hz=50.0,
        )
        server = ModbusTcpServer(
            RegisterBank(build_registers(snapshot, unit_id=2, serial="TEST-1")),
            "127.0.0.1",
            0,
            2,
        )
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        try:
            result = await asyncio.to_thread(probe, "127.0.0.1", port, 2)
        finally:
            await server.close()

        assert result["signature"] == "SunS"
        assert result["manufacturer"] == "Fronius"
        assert result["meter_model"] == {"id": 213, "length": 124}
        assert result["exported_energy_wh"] == 1234.5
        assert result["end_model"] == {"id": 0xFFFF, "length": 0}

    asyncio.run(scenario())


def test_probe_rejects_wrong_model_chain() -> None:
    async def scenario() -> None:
        snapshot = aggregate_readings(
            [],
            {},
            {"shelly_1": "L1"},
            fallback_voltage_v=230.0,
            grid_frequency_hz=50.0,
        )
        registers = build_registers(snapshot, unit_id=2, serial="TEST-2")
        registers[40069] = 203
        server = ModbusTcpServer(RegisterBank(registers), "127.0.0.1", 0, 2)
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(RuntimeError, match="meter_model"):
                await asyncio.to_thread(probe, "127.0.0.1", port, 2)
        finally:
            await server.close()

    asyncio.run(scenario())


def test_probe_discovers_and_decodes_model_203() -> None:
    async def scenario() -> None:
        snapshot = aggregate_readings(
            [],
            {"shelly_1": 1234.5},
            {"shelly_1": "L1"},
            fallback_voltage_v=230.0,
            grid_frequency_hz=50.0,
        )
        server = ModbusTcpServer(
            RegisterBank(
                build_registers(
                    snapshot,
                    unit_id=2,
                    serial="TEST-203",
                    meter_model=203,
                )
            ),
            "127.0.0.1",
            0,
            2,
        )
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        try:
            result = await asyncio.to_thread(
                probe,
                "127.0.0.1",
                port,
                2,
                expected_model=203,
            )
        finally:
            await server.close()

        assert result["meter_model"] == {"id": 203, "length": 105}
        assert result["power_w"] == 0
        assert result["exported_energy_wh"] == 1234
        assert result["power_factor"] is None
        assert result["end_model"] == {"id": 0xFFFF, "length": 0}

    asyncio.run(scenario())
