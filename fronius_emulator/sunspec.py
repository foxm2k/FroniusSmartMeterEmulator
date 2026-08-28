"""SunSpec register image for a three-phase Fronius-compatible meter.

The public register numbers in the Fronius register map are one-based.  The
dictionary returned by :func:`build_registers` deliberately uses zero-based
Modbus PDU addresses: documented register 40001 is therefore key 40000.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

SUNSPEC_BASE_REGISTER = 40001
SUNSPEC_BASE_ADDRESS = SUNSPEC_BASE_REGISTER - 1
SUNSPEC_NAN_WORDS = (0x7FC0, 0x0000)
SUNSPEC_INT16_NA = 0x8000
SUNSPEC_SF_NA = 0x8000


@dataclass(frozen=True, slots=True)
class PhaseValues:
    """Instantaneous values and optional counters for one AC phase."""

    current_a: float
    voltage_v: float
    power_w: float
    apparent_power_va: float
    power_factor: float | None = None
    exported_energy_wh: float | None = None
    imported_energy_wh: float | None = None


@dataclass(frozen=True, slots=True)
class MeterSnapshot:
    """A coherent three-phase meter sample used to build one register image."""

    phase_a: PhaseValues
    phase_b: PhaseValues
    phase_c: PhaseValues
    frequency_hz: float
    exported_energy_wh: float
    imported_energy_wh: float = 0.0

    @property
    def phases(self) -> tuple[PhaseValues, PhaseValues, PhaseValues]:
        return (self.phase_a, self.phase_b, self.phase_c)


def _pdu_address(documented_register: int) -> int:
    return documented_register - 1


def _float_words(value: float | None) -> tuple[int, int]:
    """Encode one SunSpec float32 as high word followed by low word."""

    if value is None:
        return SUNSPEC_NAN_WORDS
    return struct.unpack(">HH", struct.pack(">f", float(value)))


def _write_float(registers: dict[int, int], documented_register: int, value: float | None) -> None:
    high, low = _float_words(value)
    address = _pdu_address(documented_register)
    registers[address] = high
    registers[address + 1] = low


def _signed_word(value: int) -> int:
    return value & 0xFFFF


def _scaled_int16(value: float | None, scale_factor: int, *, field: str) -> int:
    """Encode an int16 measurement without using SunSpec's reserved -32768."""

    if value is None:
        return SUNSPEC_INT16_NA
    raw = round(value / (10**scale_factor))
    if not -32767 <= raw <= 32767:
        raise ValueError(f"{field} does not fit int16 with scale factor {scale_factor}")
    return _signed_word(raw)


def _write_acc32(
    registers: dict[int, int],
    documented_register: int,
    value: float | None,
    scale_factor: int,
    *,
    field: str,
) -> None:
    """Encode one SunSpec unsigned accumulator, high word first."""

    raw = 0 if value is None else round(value / (10**scale_factor))
    if not 0 <= raw <= 0xFFFFFFFF:
        raise ValueError(f"{field} does not fit acc32 with scale factor {scale_factor}")
    address = _pdu_address(documented_register)
    registers[address] = raw >> 16
    registers[address + 1] = raw & 0xFFFF


def _string_words(value: str, register_count: int) -> list[int]:
    """Encode a fixed-width SunSpec UTF-8 string, two bytes per register."""

    encoded = value.encode("utf-8")
    byte_count = register_count * 2
    if len(encoded) > byte_count:
        raise ValueError(f"SunSpec string needs {len(encoded)} bytes, maximum is {byte_count}")
    encoded = encoded.ljust(byte_count, b"\0")
    return [
        int.from_bytes(encoded[offset : offset + 2], byteorder="big")
        for offset in range(0, byte_count, 2)
    ]


def _write_string(
    registers: dict[int, int],
    documented_register: int,
    register_count: int,
    value: str,
) -> None:
    address = _pdu_address(documented_register)
    for offset, word in enumerate(_string_words(value, register_count)):
        registers[address + offset] = word


def _average_phase_voltage(phases: tuple[PhaseValues, ...]) -> float:
    active_voltages = [phase.voltage_v for phase in phases if phase.voltage_v > 0]
    if not active_voltages:
        return 0.0
    return sum(active_voltages) / len(active_voltages)


def _power_factor(power_w: float, apparent_power_va: float) -> float | None:
    if apparent_power_va == 0:
        return None
    return max(-1.0, min(1.0, power_w / abs(apparent_power_va)))


def _line_voltage(first: float, second: float) -> float | None:
    """Approximate line-to-line voltage for two 120-degree-separated phases."""

    if first <= 0 or second <= 0:
        return None
    return math.sqrt(first * first + second * second + first * second)


def _phase_power_factor(phase: PhaseValues) -> float | None:
    if phase.power_factor is not None:
        return max(-1.0, min(1.0, phase.power_factor))
    return _power_factor(phase.power_w, phase.apparent_power_va)


def _external_generator_meter_power(power_w: float) -> float:
    """Convert positive generation magnitude to the meter's raw flow direction.

    The application keeps Shelly generation positive.  A physical meter between
    an external generator and the installation sees that flow in the reverse
    direction, though, so the Verto expects negative raw W/Wph values and then
    exposes them as positive production for meter location 3.
    """

    return 0.0 if power_w == 0 else -power_w


def build_registers(
    snapshot: MeterSnapshot,
    *,
    unit_id: int,
    serial: str,
    model: str = "Smart Meter 63A",
    meter_model: int = 213,
) -> dict[int, int]:
    """Build a complete Fronius/SunSpec three-phase meter register map.

    Model 213 is the default float representation.  Model 203 is an explicit
    integer-plus-static-scale-factor compatibility fallback.
    """

    if not 1 <= unit_id <= 247:
        raise ValueError("unit_id must be between 1 and 247")
    if meter_model not in {203, 213}:
        raise ValueError("meter_model must be 203 or 213")

    phases = snapshot.phases
    for phase in phases:
        for value in (
            phase.current_a,
            phase.voltage_v,
            phase.power_w,
            phase.apparent_power_va,
        ):
            if not math.isfinite(value):
                raise ValueError("mandatory phase values must be finite")
        if phase.current_a < 0 or phase.apparent_power_va < 0:
            raise ValueError("current and apparent power must be non-negative")
        if phase.power_w < 0:
            raise ValueError("phase power must be a non-negative generation magnitude")
        if phase.voltage_v <= 0:
            raise ValueError("every phase voltage must be greater than zero")
        if phase.power_factor is not None and (
            not math.isfinite(phase.power_factor) or not -1 <= phase.power_factor <= 1
        ):
            raise ValueError("phase power factor must be finite and between -1 and 1")
        for energy in (phase.exported_energy_wh, phase.imported_energy_wh):
            if energy is not None and (not math.isfinite(energy) or energy < 0):
                raise ValueError("phase energy must be finite and non-negative")
    for value in (
        snapshot.frequency_hz,
        snapshot.exported_energy_wh,
        snapshot.imported_energy_wh,
    ):
        if not math.isfinite(value):
            raise ValueError("mandatory meter values must be finite")
    if snapshot.frequency_hz <= 0:
        raise ValueError("frequency must be greater than zero")
    if snapshot.exported_energy_wh < 0 or snapshot.imported_energy_wh < 0:
        raise ValueError("energy values must be non-negative")

    # A dense image prevents accidental holes in model discovery or bulk reads.
    end_register = 40197 if meter_model == 213 else 40178
    registers = {
        address: 0 for address in range(SUNSPEC_BASE_ADDRESS, _pdu_address(end_register) + 1)
    }

    # SunSpec identifier and Common Model 1.
    registers[_pdu_address(40001)] = 0x5375
    registers[_pdu_address(40002)] = 0x6E53
    registers[_pdu_address(40003)] = 1
    registers[_pdu_address(40004)] = 65
    _write_string(registers, 40005, 16, "Fronius")
    _write_string(registers, 40021, 16, model)
    _write_string(registers, 40037, 8, "")
    _write_string(registers, 40045, 8, "")
    _write_string(registers, 40053, 16, serial)
    registers[_pdu_address(40069)] = unit_id

    if meter_model == 203:
        _populate_model_203(registers, snapshot)
        return registers

    _populate_model_213(registers, snapshot)
    return registers


def _populate_model_213(registers: dict[int, int], snapshot: MeterSnapshot) -> None:
    """Populate AC Meter Model 213 (WYE-connected, three phase)."""

    phases = snapshot.phases
    registers[_pdu_address(40070)] = 213
    registers[_pdu_address(40071)] = 124

    # Initialize every float point in the model body to the SunSpec NaN sentinel.
    # The final two body registers are the non-float Evt bitfield.
    for documented_register in range(40072, 40194, 2):
        _write_float(registers, documented_register, None)

    total_current = sum(phase.current_a for phase in phases)
    total_power = sum(phase.power_w for phase in phases)
    total_apparent_power = sum(phase.apparent_power_va for phase in phases)

    _write_float(registers, 40072, total_current)
    for documented_register, phase in zip((40074, 40076, 40078), phases, strict=False):
        _write_float(registers, documented_register, phase.current_a)

    _write_float(registers, 40080, _average_phase_voltage(phases))
    for documented_register, phase in zip((40082, 40084, 40086), phases, strict=False):
        _write_float(registers, documented_register, phase.voltage_v)

    line_voltages = (
        _line_voltage(phases[0].voltage_v, phases[1].voltage_v),
        _line_voltage(phases[1].voltage_v, phases[2].voltage_v),
        _line_voltage(phases[2].voltage_v, phases[0].voltage_v),
    )
    available_line_voltages = [value for value in line_voltages if value is not None]
    _write_float(
        registers,
        40088,
        sum(available_line_voltages) / len(available_line_voltages)
        if available_line_voltages
        else None,
    )
    for documented_register, value in zip((40090, 40092, 40094), line_voltages, strict=False):
        _write_float(registers, documented_register, value)
    _write_float(registers, 40096, snapshot.frequency_hz)

    _write_float(registers, 40098, _external_generator_meter_power(total_power))
    for documented_register, phase in zip((40100, 40102, 40104), phases, strict=False):
        _write_float(
            registers,
            documented_register,
            _external_generator_meter_power(phase.power_w),
        )

    _write_float(registers, 40106, total_apparent_power)
    for documented_register, phase in zip((40108, 40110, 40112), phases, strict=False):
        _write_float(registers, documented_register, phase.apparent_power_va)

    # Reactive power is not supplied by the source devices.  Preserve the
    # complete block at 40114..40121 as not implemented.
    _write_float(registers, 40122, _power_factor(total_power, total_apparent_power))
    for documented_register, phase in zip((40124, 40126, 40128), phases, strict=False):
        _write_float(registers, documented_register, _phase_power_factor(phase))

    _write_float(registers, 40130, snapshot.exported_energy_wh)
    for documented_register, phase in zip((40132, 40134, 40136), phases, strict=False):
        _write_float(registers, documented_register, phase.exported_energy_wh)

    _write_float(registers, 40138, snapshot.imported_energy_wh)
    for documented_register, phase in zip((40140, 40142, 40144), phases, strict=False):
        _write_float(registers, documented_register, phase.imported_energy_wh)

    # Apparent/reactive energy is unavailable and retains the NaN sentinel.
    registers[_pdu_address(40194)] = 0
    registers[_pdu_address(40195)] = 0

    registers[_pdu_address(40196)] = 0xFFFF
    registers[_pdu_address(40197)] = 0


def _populate_model_203(registers: dict[int, int], snapshot: MeterSnapshot) -> None:
    """Populate AC Meter Model 203 using fixed, never-changing scale factors."""

    current_sf = -3
    voltage_sf = -1
    frequency_sf = -2
    power_sf = -1
    apparent_power_sf = -1
    power_factor_sf = -1
    energy_sf = 0
    phases = snapshot.phases

    registers[_pdu_address(40070)] = 203
    registers[_pdu_address(40071)] = 105

    total_current = sum(phase.current_a for phase in phases)
    registers[_pdu_address(40072)] = _scaled_int16(total_current, current_sf, field="total current")
    for documented_register, phase in zip((40073, 40074, 40075), phases, strict=False):
        registers[_pdu_address(documented_register)] = _scaled_int16(
            phase.current_a, current_sf, field="phase current"
        )
    registers[_pdu_address(40076)] = _signed_word(current_sf)

    line_voltages = (
        _line_voltage(phases[0].voltage_v, phases[1].voltage_v),
        _line_voltage(phases[1].voltage_v, phases[2].voltage_v),
        _line_voltage(phases[2].voltage_v, phases[0].voltage_v),
    )
    registers[_pdu_address(40077)] = _scaled_int16(
        _average_phase_voltage(phases), voltage_sf, field="average phase voltage"
    )
    for documented_register, phase in zip((40078, 40079, 40080), phases, strict=False):
        registers[_pdu_address(documented_register)] = _scaled_int16(
            phase.voltage_v, voltage_sf, field="phase voltage"
        )
    registers[_pdu_address(40081)] = _scaled_int16(
        sum(line_voltages) / len(line_voltages),
        voltage_sf,
        field="average line voltage",
    )
    for documented_register, value in zip((40082, 40083, 40084), line_voltages, strict=False):
        registers[_pdu_address(documented_register)] = _scaled_int16(
            value, voltage_sf, field="line voltage"
        )
    registers[_pdu_address(40085)] = _signed_word(voltage_sf)

    registers[_pdu_address(40086)] = _scaled_int16(
        snapshot.frequency_hz, frequency_sf, field="frequency"
    )
    registers[_pdu_address(40087)] = _signed_word(frequency_sf)

    total_power = sum(phase.power_w for phase in phases)
    registers[_pdu_address(40088)] = _scaled_int16(
        _external_generator_meter_power(total_power),
        power_sf,
        field="total power",
    )
    for documented_register, phase in zip((40089, 40090, 40091), phases, strict=False):
        registers[_pdu_address(documented_register)] = _scaled_int16(
            _external_generator_meter_power(phase.power_w),
            power_sf,
            field="phase power",
        )
    registers[_pdu_address(40092)] = _signed_word(power_sf)

    total_apparent_power = sum(phase.apparent_power_va for phase in phases)
    registers[_pdu_address(40093)] = _scaled_int16(
        total_apparent_power, apparent_power_sf, field="total apparent power"
    )
    for documented_register, phase in zip((40094, 40095, 40096), phases, strict=False):
        registers[_pdu_address(documented_register)] = _scaled_int16(
            phase.apparent_power_va, apparent_power_sf, field="phase apparent power"
        )
    registers[_pdu_address(40097)] = _signed_word(apparent_power_sf)

    for documented_register in range(40098, 40102):
        registers[_pdu_address(documented_register)] = SUNSPEC_INT16_NA
    registers[_pdu_address(40102)] = SUNSPEC_SF_NA

    total_pf = _power_factor(total_power, total_apparent_power)
    registers[_pdu_address(40103)] = _scaled_int16(
        None if total_pf is None else total_pf * 100,
        power_factor_sf,
        field="total power factor",
    )
    for documented_register, phase in zip((40104, 40105, 40106), phases, strict=False):
        phase_pf = _phase_power_factor(phase)
        registers[_pdu_address(documented_register)] = _scaled_int16(
            None if phase_pf is None else phase_pf * 100,
            power_factor_sf,
            field="phase power factor",
        )
    registers[_pdu_address(40107)] = _signed_word(power_factor_sf)

    energy_points = (
        (40108, snapshot.exported_energy_wh, "exported energy"),
        (40110, phases[0].exported_energy_wh, "phase A exported energy"),
        (40112, phases[1].exported_energy_wh, "phase B exported energy"),
        (40114, phases[2].exported_energy_wh, "phase C exported energy"),
        (40116, snapshot.imported_energy_wh, "imported energy"),
        (40118, phases[0].imported_energy_wh, "phase A imported energy"),
        (40120, phases[1].imported_energy_wh, "phase B imported energy"),
        (40122, phases[2].imported_energy_wh, "phase C imported energy"),
    )
    for documented_register, value, field in energy_points:
        _write_acc32(registers, documented_register, value, energy_sf, field=field)
    registers[_pdu_address(40124)] = _signed_word(energy_sf)

    # VAh and VArh are unavailable: zero means "not accumulated" and their
    # group scale factor carries SunSpec's not-implemented sentinel.
    for documented_register in range(40125, 40141):
        registers[_pdu_address(documented_register)] = 0
    registers[_pdu_address(40141)] = SUNSPEC_SF_NA
    for documented_register in range(40142, 40174):
        registers[_pdu_address(documented_register)] = 0
    registers[_pdu_address(40174)] = SUNSPEC_SF_NA

    registers[_pdu_address(40175)] = 0
    registers[_pdu_address(40176)] = 0
    registers[_pdu_address(40177)] = 0xFFFF
    registers[_pdu_address(40178)] = 0


__all__ = ["MeterSnapshot", "PhaseValues", "build_registers"]
