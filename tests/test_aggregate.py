from __future__ import annotations

import pytest

from fronius_emulator.aggregate import aggregate_readings
from fronius_emulator.shelly import ShellyReading


def _reading(
    name: str,
    phase: str,
    *,
    power: float,
    voltage: float,
    current: float,
    frequency: float | None,
) -> ShellyReading:
    return ShellyReading(
        name=name,
        phase=phase,
        power_w=power,
        voltage_v=voltage,
        current_a=current,
        frequency_hz=frequency,
        apparent_power_va=voltage * current,
        power_factor=None,
        raw_energy_wh=0.0,
        energy_field="aenergy",
        timestamp=1.0,
    )


def test_combines_sources_on_their_real_phases() -> None:
    snapshot = aggregate_readings(
        [
            _reading("shelly_1", "L1", power=500.0, voltage=230.0, current=2.2, frequency=None),
            _reading("shelly_2", "L2", power=300.0, voltage=232.0, current=1.4, frequency=49.98),
        ],
        {"shelly_1": 1000.0, "shelly_2": 2000.0},
        {"shelly_1": "L1", "shelly_2": "L2"},
        fallback_voltage_v=230.0,
        grid_frequency_hz=50.0,
    )

    assert snapshot.phase_a.power_w == 500.0
    assert snapshot.phase_b.power_w == 300.0
    assert snapshot.phase_c.power_w == 0.0
    assert snapshot.phase_a.exported_energy_wh == 1000.0
    assert snapshot.phase_b.exported_energy_wh == 2000.0
    assert snapshot.phase_c.exported_energy_wh == 0.0
    assert snapshot.phase_c.voltage_v == pytest.approx(231.0)
    assert snapshot.frequency_hz == 49.98
    assert snapshot.exported_energy_wh == 3000.0


def test_same_phase_values_are_added_and_voltage_is_averaged() -> None:
    snapshot = aggregate_readings(
        [
            _reading("shelly_1", "L1", power=100.0, voltage=229.0, current=0.5, frequency=50.0),
            _reading("shelly_2", "L1", power=200.0, voltage=231.0, current=1.0, frequency=50.1),
        ],
        {},
        {"shelly_1": "L1", "shelly_2": "L1"},
        fallback_voltage_v=230.0,
        grid_frequency_hz=50.0,
    )

    assert snapshot.phase_a.power_w == 300.0
    assert snapshot.phase_a.current_a == 1.5
    assert snapshot.phase_a.voltage_v == 230.0
    assert snapshot.frequency_hz == pytest.approx(50.05)


def test_empty_snapshot_uses_grid_fallbacks_and_persisted_energy() -> None:
    snapshot = aggregate_readings(
        [],
        {"shelly_1": 42.0},
        {"shelly_1": "L3"},
        fallback_voltage_v=230.0,
        grid_frequency_hz=50.0,
    )

    assert [phase.voltage_v for phase in snapshot.phases] == [230.0, 230.0, 230.0]
    assert [phase.power_w for phase in snapshot.phases] == [0.0, 0.0, 0.0]
    assert snapshot.phase_c.exported_energy_wh == 42.0
    assert snapshot.frequency_hz == 50.0
