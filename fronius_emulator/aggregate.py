from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from .shelly import ShellyReading
from .sunspec import MeterSnapshot, PhaseValues

_PHASES = ("L1", "L2", "L3")


def aggregate_readings(
    readings: Iterable[ShellyReading],
    energy_by_source: Mapping[str, float],
    source_phases: Mapping[str, str],
    *,
    fallback_voltage_v: float,
    grid_frequency_hz: float,
) -> MeterSnapshot:
    """Combine fresh single-phase readings into one three-phase meter snapshot."""

    per_phase = {
        phase: {"current": 0.0, "power": 0.0, "apparent": 0.0, "voltages": []} for phase in _PHASES
    }
    frequencies: list[float] = []

    for reading in readings:
        if reading.phase not in per_phase:
            raise ValueError(f"invalid phase in reading {reading.name}: {reading.phase}")
        values = per_phase[reading.phase]
        values["current"] += reading.current_a
        values["power"] += reading.power_w
        values["apparent"] += reading.apparent_power_va
        if reading.voltage_v > 0:
            values["voltages"].append(reading.voltage_v)
        if reading.frequency_hz is not None and math.isfinite(reading.frequency_hz):
            frequencies.append(reading.frequency_hz)

    observed_voltages = [voltage for values in per_phase.values() for voltage in values["voltages"]]
    voltage_fallback = (
        sum(observed_voltages) / len(observed_voltages) if observed_voltages else fallback_voltage_v
    )

    phase_energy = dict.fromkeys(_PHASES, 0.0)
    for name, energy_wh in energy_by_source.items():
        phase = source_phases.get(name)
        if phase in phase_energy:
            phase_energy[phase] += energy_wh

    phase_values: list[PhaseValues] = []
    for phase in _PHASES:
        values = per_phase[phase]
        voltage = (
            sum(values["voltages"]) / len(values["voltages"])
            if values["voltages"]
            else voltage_fallback
        )
        phase_values.append(
            PhaseValues(
                current_a=values["current"],
                voltage_v=voltage,
                power_w=values["power"],
                apparent_power_va=values["apparent"],
                power_factor=None,
                exported_energy_wh=phase_energy[phase],
                imported_energy_wh=0.0,
            )
        )

    frequency = sum(frequencies) / len(frequencies) if frequencies else grid_frequency_hz
    total_energy = sum(phase_energy.values())
    return MeterSnapshot(
        phase_a=phase_values[0],
        phase_b=phase_values[1],
        phase_c=phase_values[2],
        frequency_hz=frequency,
        exported_energy_wh=total_energy,
        imported_energy_wh=0.0,
    )
