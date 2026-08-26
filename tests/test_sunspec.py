import math
import struct

import pytest

from fronius_emulator.sunspec import MeterSnapshot, PhaseValues, build_registers


def _phase(
    *,
    current: float = 0.0,
    voltage: float = 230.0,
    power: float = 0.0,
    apparent: float = 0.0,
    power_factor: float | None = None,
    exported_energy: float | None = None,
    imported_energy: float | None = None,
) -> PhaseValues:
    return PhaseValues(
        current_a=current,
        voltage_v=voltage,
        power_w=power,
        apparent_power_va=apparent,
        power_factor=power_factor,
        exported_energy_wh=exported_energy,
        imported_energy_wh=imported_energy,
    )


def _snapshot() -> MeterSnapshot:
    return MeterSnapshot(
        phase_a=_phase(
            current=1.5,
            voltage=229.5,
            power=325.7,
            apparent=344.0,
            power_factor=0.9468,
            exported_energy=12345.5,
            imported_energy=1.25,
        ),
        phase_b=_phase(
            current=0.75,
            voltage=231.0,
            power=160.25,
            apparent=172.0,
            power_factor=0.9317,
            exported_energy=6789.25,
            imported_energy=2.5,
        ),
        phase_c=_phase(voltage=230.0),
        frequency_hz=49.98,
        exported_energy_wh=19134.75,
        imported_energy_wh=3.75,
    )


def _float_at(registers: dict[int, int], documented_register: int) -> float:
    address = documented_register - 1
    return struct.unpack(">f", struct.pack(">HH", registers[address], registers[address + 1]))[0]


def _string_at(registers: dict[int, int], documented_register: int, register_count: int) -> str:
    address = documented_register - 1
    raw = b"".join(
        registers[address + offset].to_bytes(2, "big") for offset in range(register_count)
    )
    return raw.split(b"\0", 1)[0].decode("utf-8")


def _signed_word_at(registers: dict[int, int], documented_register: int) -> int:
    word = registers[documented_register - 1]
    return word - 0x10000 if word & 0x8000 else word


def _scaled_at(registers: dict[int, int], documented_register: int, scale_register: int) -> float:
    return _signed_word_at(registers, documented_register) * (
        10 ** _signed_word_at(registers, scale_register)
    )


def _acc32_at(registers: dict[int, int], documented_register: int, scale_register: int) -> float:
    address = documented_register - 1
    raw = (registers[address] << 16) | registers[address + 1]
    return raw * (10 ** _signed_word_at(registers, scale_register))


def test_map_is_dense_and_uses_zero_based_pdu_addresses() -> None:
    registers = build_registers(_snapshot(), unit_id=240, serial="FSM00001")

    assert set(registers) == set(range(40000, 40197))
    assert 39999 not in registers
    assert (registers[40000], registers[40001]) == (0x5375, 0x6E53)


def test_common_identity_and_model_headers() -> None:
    registers = build_registers(_snapshot(), unit_id=17, serial="FSM-123", model="Smart Meter 63A")

    assert registers[40002] == 1  # documented 40003
    assert registers[40003] == 65
    assert _string_at(registers, 40005, 16) == "Fronius"
    assert registers[40004] == 0x4672  # dense big-endian "Fr"
    assert registers[40005] == 0x6F6E  # dense big-endian "on"
    assert _string_at(registers, 40021, 16) == "Smart Meter 63A"
    assert _string_at(registers, 40053, 16) == "FSM-123"
    assert registers[40068] == 17
    assert registers[40069] == 213
    assert registers[40070] == 124


def test_real_power_keeps_both_float_words() -> None:
    registers = build_registers(_snapshot(), unit_id=1, serial="1")
    total_power = 325.7 + 160.25
    expected_total = struct.unpack(">HH", struct.pack(">f", total_power))
    expected_phase_a = struct.unpack(">HH", struct.pack(">f", 325.7))

    assert (registers[40097], registers[40098]) == expected_total
    assert (registers[40099], registers[40100]) == expected_phase_a
    assert registers[40098] != 0
    assert registers[40100] != 0
    assert _float_at(registers, 40098) == pytest.approx(total_power)
    assert _float_at(registers, 40100) == pytest.approx(325.7)


def test_pf_block_and_energy_positions_do_not_shift() -> None:
    registers = build_registers(_snapshot(), unit_id=1, serial="1")

    assert _float_at(registers, 40122) == pytest.approx((325.7 + 160.25) / (344.0 + 172.0))
    assert _float_at(registers, 40124) == pytest.approx(0.9468)
    assert _float_at(registers, 40126) == pytest.approx(0.9317)
    assert math.isnan(_float_at(registers, 40128))

    assert _float_at(registers, 40130) == pytest.approx(19134.75)
    assert _float_at(registers, 40132) == pytest.approx(12345.5)
    assert _float_at(registers, 40134) == pytest.approx(6789.25)
    assert math.isnan(_float_at(registers, 40136))
    assert _float_at(registers, 40138) == pytest.approx(3.75)
    assert _float_at(registers, 40140) == pytest.approx(1.25)
    assert _float_at(registers, 40142) == pytest.approx(2.5)


def test_optional_float_fields_are_sunspec_nan_and_event_is_zero() -> None:
    registers = build_registers(_snapshot(), unit_id=1, serial="1")

    for documented_register in (40114, 40116, 40146, 40162):
        address = documented_register - 1
        assert (registers[address], registers[address + 1]) == (0x7FC0, 0x0000)
        assert math.isnan(_float_at(registers, documented_register))

    assert (registers[40193], registers[40194]) == (0, 0)


def test_line_voltage_is_derived_from_available_phase_voltages() -> None:
    registers = build_registers(_snapshot(), unit_id=1, serial="1")
    expected_ab = math.sqrt(229.5**2 + 231.0**2 + 229.5 * 231.0)
    expected_bc = math.sqrt(231.0**2 + 230.0**2 + 231.0 * 230.0)
    expected_ca = math.sqrt(230.0**2 + 229.5**2 + 230.0 * 229.5)

    assert _float_at(registers, 40088) == pytest.approx(
        (expected_ab + expected_bc + expected_ca) / 3
    )
    assert _float_at(registers, 40090) == pytest.approx(expected_ab)
    assert _float_at(registers, 40092) == pytest.approx(expected_bc)
    assert _float_at(registers, 40094) == pytest.approx(expected_ca)


def test_end_marker_is_at_documented_registers_40196_and_40197() -> None:
    registers = build_registers(_snapshot(), unit_id=1, serial="1")

    assert registers[40195] == 0xFFFF
    assert registers[40196] == 0


def test_model_203_is_dense_and_has_its_own_end_marker() -> None:
    registers = build_registers(_snapshot(), unit_id=2, serial="203-TEST", meter_model=203)

    assert set(registers) == set(range(40000, 40178))
    assert (registers[40069], registers[40070]) == (203, 105)
    assert (registers[40176], registers[40177]) == (0xFFFF, 0)


def test_model_203_round_trips_measurements_and_energy() -> None:
    registers = build_registers(_snapshot(), unit_id=2, serial="203", meter_model=203)

    assert _scaled_at(registers, 40072, 40076) == pytest.approx(2.25)
    assert _scaled_at(registers, 40077, 40085) == pytest.approx(230.2, abs=0.1)
    assert _scaled_at(registers, 40086, 40087) == pytest.approx(49.98)
    assert _scaled_at(registers, 40088, 40092) == pytest.approx(486.0, abs=0.1)
    assert _scaled_at(registers, 40093, 40097) == pytest.approx(516.0)
    assert _scaled_at(registers, 40103, 40107) == pytest.approx(94.2, abs=0.1)
    assert _scaled_at(registers, 40104, 40107) == pytest.approx(94.7, abs=0.1)
    assert _acc32_at(registers, 40108, 40124) == 19135
    assert _acc32_at(registers, 40110, 40124) == 12346
    assert _acc32_at(registers, 40112, 40124) == 6789
    assert (registers[40107], registers[40108]) == (0, 19135)


def test_model_203_uses_static_scale_factors_and_typed_sentinels() -> None:
    registers = build_registers(_snapshot(), unit_id=2, serial="203", meter_model=203)

    assert tuple(
        _signed_word_at(registers, documented_register)
        for documented_register in (40076, 40085, 40087, 40092, 40097, 40107, 40124)
    ) == (-3, -1, -2, -1, -1, -1, 0)
    assert all(
        registers[documented_register - 1] == 0x8000 for documented_register in range(40098, 40103)
    )
    assert registers[40140] == 0x8000  # documented TotVAh_SF 40141
    assert registers[40173] == 0x8000  # documented TotVArh_SF 40174
    assert (registers[40174], registers[40175]) == (0, 0)


def test_model_203_zero_va_marks_power_factor_unavailable() -> None:
    snapshot = MeterSnapshot(
        phase_a=_phase(),
        phase_b=_phase(),
        phase_c=_phase(),
        frequency_hz=50.0,
        exported_energy_wh=0.0,
    )
    registers = build_registers(snapshot, unit_id=2, serial="203", meter_model=203)

    assert all(
        registers[documented_register - 1] == 0x8000 for documented_register in range(40103, 40107)
    )
    assert _signed_word_at(registers, 40107) == -1


def test_model_203_rejects_int16_and_accumulator_overflow() -> None:
    snapshot = _snapshot()
    excessive_power = MeterSnapshot(
        phase_a=_phase(current=1, power=3276.8, apparent=1),
        phase_b=snapshot.phase_b,
        phase_c=snapshot.phase_c,
        frequency_hz=snapshot.frequency_hz,
        exported_energy_wh=snapshot.exported_energy_wh,
    )
    excessive_energy = MeterSnapshot(
        phase_a=snapshot.phase_a,
        phase_b=snapshot.phase_b,
        phase_c=snapshot.phase_c,
        frequency_hz=snapshot.frequency_hz,
        exported_energy_wh=0xFFFFFFFF + 1.0,
    )

    with pytest.raises(ValueError, match="total power"):
        build_registers(excessive_power, unit_id=2, serial="203", meter_model=203)
    with pytest.raises(ValueError, match="exported energy"):
        build_registers(excessive_energy, unit_id=2, serial="203", meter_model=203)


def test_rejects_unknown_meter_model() -> None:
    with pytest.raises(ValueError, match="meter_model"):
        build_registers(_snapshot(), unit_id=2, serial="bad", meter_model=211)


@pytest.mark.parametrize(
    ("documented_register", "expected"),
    [
        (40072, 2.25),
        (40074, 1.5),
        (40080, (229.5 + 231.0 + 230.0) / 3),
        (40082, 229.5),
        (40096, 49.98),
        (40106, 516.0),
        (40108, 344.0),
        (40110, 172.0),
    ],
)
def test_mandatory_snapshot_values_round_trip(documented_register: int, expected: float) -> None:
    registers = build_registers(_snapshot(), unit_id=1, serial="1")
    assert _float_at(registers, documented_register) == pytest.approx(expected)


@pytest.mark.parametrize("unit_id", [0, 248])
def test_rejects_invalid_unit_id(unit_id: int) -> None:
    with pytest.raises(ValueError, match="unit_id"):
        build_registers(_snapshot(), unit_id=unit_id, serial="1")


def test_rejects_missing_mandatory_phase_voltage() -> None:
    snapshot = _snapshot()
    invalid = MeterSnapshot(
        phase_a=snapshot.phase_a,
        phase_b=snapshot.phase_b,
        phase_c=_phase(voltage=0.0),
        frequency_hz=snapshot.frequency_hz,
        exported_energy_wh=snapshot.exported_energy_wh,
    )

    with pytest.raises(ValueError, match="phase voltage"):
        build_registers(invalid, unit_id=1, serial="1")


@pytest.mark.parametrize("power_factor", [math.nan, math.inf, -1.01, 1.01])
def test_rejects_invalid_optional_phase_power_factor(power_factor: float) -> None:
    snapshot = _snapshot()
    invalid = MeterSnapshot(
        phase_a=_phase(voltage=230.0, power_factor=power_factor),
        phase_b=snapshot.phase_b,
        phase_c=snapshot.phase_c,
        frequency_hz=snapshot.frequency_hz,
        exported_energy_wh=snapshot.exported_energy_wh,
    )

    with pytest.raises(ValueError, match="phase power factor"):
        build_registers(invalid, unit_id=1, serial="1")


@pytest.mark.parametrize("energy", [-0.01, math.nan, math.inf])
def test_rejects_invalid_optional_phase_energy(energy: float) -> None:
    snapshot = _snapshot()
    invalid = MeterSnapshot(
        phase_a=_phase(voltage=230.0, exported_energy=energy),
        phase_b=snapshot.phase_b,
        phase_c=snapshot.phase_c,
        frequency_hz=snapshot.frequency_hz,
        exported_energy_wh=snapshot.exported_energy_wh,
    )

    with pytest.raises(ValueError, match="phase energy"):
        build_registers(invalid, unit_id=1, serial="1")
