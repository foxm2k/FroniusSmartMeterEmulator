from __future__ import annotations

import asyncio
import struct
import time
from pathlib import Path

import pytest

from fronius_emulator.aggregate import aggregate_readings
from fronius_emulator.app import _poll_sources
from fronius_emulator.config import AppConfig
from fronius_emulator.modbus import RegisterBank
from fronius_emulator.shelly import (
    ShellyConnectionError,
    ShellyReading,
    ShellySourceConfig,
)
from fronius_emulator.state import EnergyStateStore
from fronius_emulator.sunspec import build_registers


def _float_at(bank: RegisterBank, documented_register: int) -> float:
    values = bank.read(documented_register - 1, 2)
    assert values is not None
    return struct.unpack(">f", struct.pack(">HH", *values))[0]


def test_stale_source_zeroes_power_but_keeps_energy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="fronius_emulator.app")
    source = ShellySourceConfig("shelly_1", "example.test", "L1")

    class OneReadingClient:
        config = source

        def __init__(self) -> None:
            self.calls = 0

        def fetch(self) -> ShellyReading:
            self.calls += 1
            if self.calls > 1:
                raise ShellyConnectionError("offline")
            return ShellyReading(
                name="shelly_1",
                phase="L1",
                power_w=500.0,
                voltage_v=230.0,
                current_a=2.2,
                frequency_hz=50.0,
                apparent_power_va=506.0,
                power_factor=None,
                raw_energy_wh=1234.0,
                energy_field="aenergy",
                timestamp=time.time(),
            )

    config = AppConfig(
        sources=(source,),
        poll_interval_seconds=0.01,
        stale_after_seconds=0.03,
        state_save_interval_seconds=0.02,
        modbus_host="127.0.0.1",
        modbus_port=1502,
        modbus_unit_id=2,
        modbus_serial="TEST",
        sunspec_meter_model=213,
        grid_frequency_hz=50.0,
        fallback_voltage_v=230.0,
        state_file=tmp_path / "state.json",
        log_level="INFO",
    )
    initial = aggregate_readings(
        [],
        {},
        {"shelly_1": "L1"},
        fallback_voltage_v=230.0,
        grid_frequency_hz=50.0,
    )
    bank = RegisterBank(build_registers(initial, unit_id=2, serial="TEST"))
    state = EnergyStateStore(config.state_file)
    stop = asyncio.Event()

    async def scenario() -> None:
        poller = asyncio.create_task(_poll_sources(config, [OneReadingClient()], state, bank, stop))
        await asyncio.sleep(0.09)
        stop.set()
        await poller

    asyncio.run(scenario())

    assert _float_at(bank, 40098) == 0.0
    assert _float_at(bank, 40130) == 1234.0
    assert config.state_file.exists()
    assert "Shelly poll request sources=shelly_1" in caplog.messages
    assert any(
        "Shelly poll result " in message and "ok=1/1 shelly_1=500.0W/1234.0Wh[aenergy]" in message
        for message in caplog.messages
    )
