"""Register fixtures captured from the unmodified 4a31a45 implementation."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from fronius_emulator import app
from fronius_emulator.aggregate import aggregate_readings
from fronius_emulator.config import load_config
from fronius_emulator.modbus import RegisterBank
from fronius_emulator.shelly import ShellyClient, ShellySourceConfig
from fronius_emulator.state import EnergyStateStore
from fronius_emulator.sunspec import build_registers

CASES = json.loads((Path(__file__).parent / "fixtures/registers_4a31a45.json").read_text())


def case_id(case):
    return f"{case['model']}-{case.get('label', case['configs'][1]['phase'])}"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Session:
    payload = None

    def get(self, *args, **kwargs):
        return Response(self.payload)


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_complete_register_images_match_previous_release(case, tmp_path):
    state = EnergyStateStore(tmp_path / "state.json")
    phases = {source["name"]: source["phase"] for source in case["configs"]}
    sessions = [Session() for _ in case["configs"]]
    clients = [
        ShellyClient(ShellySourceConfig(**source), session)
        for source, session in zip(case["configs"], sessions, strict=True)
    ]
    for payloads, expected in zip(case["steps"], case["registers"], strict=True):
        readings = []
        for client, session, payload in zip(clients, sessions, payloads, strict=True):
            session.payload = payload
            reading = client.fetch(now=1.0)
            state.update(reading.name, reading.raw_energy_wh, reading.energy_field)
            readings.append(reading)
        snapshot = aggregate_readings(
            readings, state.values, phases, fallback_voltage_v=230, grid_frequency_hz=50
        )
        registers = build_registers(snapshot, unit_id=2, serial="COMPAT", meter_model=case["model"])
        assert list(registers.values()) == expected


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_async_transport_produces_the_same_complete_register_images(case, tmp_path):
    async def scenario():
        state = EnergyStateStore(tmp_path / "state.json")
        phases = {source["name"]: source["phase"] for source in case["configs"]}
        clients = [ShellyClient(ShellySourceConfig(**source)) for source in case["configs"]]
        cfg = replace(
            load_config({"MODBUS_SERIAL": "COMPAT", "SUNSPEC_METER_MODEL": str(case["model"])}),
            sources=tuple(client.config for client in clients),
            state_file=state.path,
            stale_after_seconds=60,
        )
        state.set_phases(phases, {})
        bank = RegisterBank(app._registers(cfg, [], state))
        runtime = app._MeterRuntime(cfg, state, bank)
        try:
            for payloads, expected in zip(case["steps"], case["registers"], strict=True):
                for client, payload in zip(clients, payloads, strict=True):
                    await client.aclose()
                    client._async_http = httpx.AsyncClient(
                        transport=httpx.MockTransport(
                            lambda request, payload=payload: httpx.Response(200, json=payload)
                        )
                    )
                    reading = await client.fetch_async(now=1.0, commit=False)
                    await runtime.accept(client, reading)
                assert bank.read(40000, len(expected)) == expected
        finally:
            await asyncio.gather(*(client.aclose() for client in clients))

    asyncio.run(scenario())
