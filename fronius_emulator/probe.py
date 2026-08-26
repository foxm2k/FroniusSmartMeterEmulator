from __future__ import annotations

import argparse
import json
import os
import socket
import struct
from typing import Any

_MBAP = struct.Struct(">HHHB")


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Modbus connection closed before response was complete")
        data.extend(chunk)
    return bytes(data)


def read_holding_registers(
    host: str,
    port: int,
    unit_id: int,
    start: int,
    count: int,
    *,
    timeout: float = 3.0,
    transaction_id: int = 1,
) -> list[int]:
    if not 1 <= count <= 125:
        raise ValueError("Modbus FC03 supports 1 to 125 registers per request")
    request_pdu = struct.pack(">BHH", 3, start, count)
    request = _MBAP.pack(transaction_id, 0, len(request_pdu) + 1, unit_id) + request_pdu
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(request)
        header = _receive_exact(connection, _MBAP.size)
        response_transaction, protocol_id, length, response_unit = _MBAP.unpack(header)
        pdu = _receive_exact(connection, length - 1)

    if (response_transaction, protocol_id, response_unit) != (
        transaction_id,
        0,
        unit_id,
    ):
        raise RuntimeError("unexpected Modbus response header")
    if pdu and pdu[0] & 0x80:
        exception_code = pdu[1] if len(pdu) > 1 else None
        raise RuntimeError(f"Modbus exception {exception_code}")
    if len(pdu) != 2 + count * 2 or pdu[:2] != bytes((3, count * 2)):
        raise RuntimeError(f"unexpected FC03 response: {pdu.hex()}")
    return list(struct.unpack(f">{count}H", pdu[2:]))


def _float_at(registers: dict[int, int], documented_register: int) -> float:
    address = documented_register - 1
    return struct.unpack(">f", struct.pack(">HH", registers[address], registers[address + 1]))[0]


def _scaled_at(
    registers: dict[int, int], documented_register: int, scale_register: int
) -> float | None:
    raw_word = registers[documented_register - 1]
    scale_word = registers[scale_register - 1]
    if raw_word == 0x8000 or scale_word == 0x8000:
        return None
    raw = raw_word - 0x10000 if raw_word & 0x8000 else raw_word
    scale = scale_word - 0x10000 if scale_word & 0x8000 else scale_word
    return raw * (10**scale)


def _acc32_at(
    registers: dict[int, int], documented_register: int, scale_register: int
) -> float | None:
    scale_word = registers[scale_register - 1]
    if scale_word == 0x8000:
        return None
    scale = scale_word - 0x10000 if scale_word & 0x8000 else scale_word
    address = documented_register - 1
    raw = (registers[address] << 16) | registers[address + 1]
    return raw * (10**scale)


def _string_at(registers: dict[int, int], documented_register: int, register_count: int) -> str:
    address = documented_register - 1
    raw = b"".join(
        registers[address + offset].to_bytes(2, "big") for offset in range(register_count)
    )
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def probe(
    host: str, port: int, unit_id: int, *, expected_model: int | None = None
) -> dict[str, Any]:
    registers: dict[int, int] = {}
    initial = read_holding_registers(host, port, unit_id, 40000, 71, transaction_id=1)
    registers.update({40000 + offset: value for offset, value in enumerate(initial)})

    meter_model = registers[40069]
    model_length = registers[40070]
    expected_lengths = {203: 105, 213: 124}
    if meter_model not in expected_lengths:
        raise RuntimeError(f"unsupported meter model {meter_model}")
    if model_length != expected_lengths[meter_model]:
        raise RuntimeError(
            f"meter_model={meter_model} length={model_length}, "
            f"expected {expected_lengths[meter_model]}"
        )
    remaining = model_length + 2  # model body followed by the two-word end model
    start = 40071
    transaction_id = 2
    while remaining:
        count = min(125, remaining)
        values = read_holding_registers(
            host,
            port,
            unit_id,
            start,
            count,
            transaction_id=transaction_id,
        )
        registers.update({start + offset: value for offset, value in enumerate(values)})
        start += count
        remaining -= count
        transaction_id += 1

    if meter_model == 203:
        layout = {
            "length": 105,
            "current_a": _scaled_at(registers, 40072, 40076),
            "voltage_v": _scaled_at(registers, 40077, 40085),
            "frequency_hz": _scaled_at(registers, 40086, 40087),
            "power_w": _scaled_at(registers, 40088, 40092),
            "apparent_power_va": _scaled_at(registers, 40093, 40097),
            "power_factor": (
                None
                if (pf_percent := _scaled_at(registers, 40103, 40107)) is None
                else pf_percent / 100
            ),
            "exported_energy_wh": _acc32_at(registers, 40108, 40124),
            "event_register": 40175,
            "end_register": 40177,
        }
    elif meter_model == 213:
        layout = {
            "length": 124,
            "current_a": _float_at(registers, 40072),
            "voltage_v": _float_at(registers, 40080),
            "frequency_hz": _float_at(registers, 40096),
            "power_w": _float_at(registers, 40098),
            "apparent_power_va": _float_at(registers, 40106),
            "power_factor": _float_at(registers, 40122),
            "exported_energy_wh": _float_at(registers, 40130),
            "event_register": 40194,
            "end_register": 40196,
        }
    else:
        raise RuntimeError(f"unsupported meter model {meter_model}")
    event_address = layout["event_register"] - 1
    end_address = layout["end_register"] - 1

    signature = struct.pack(">HH", registers[40000], registers[40001]).decode("ascii")
    result = {
        "endpoint": f"{host}:{port}",
        "unit_id": unit_id,
        "signature": signature,
        "common_model": {"id": registers[40002], "length": registers[40003]},
        "manufacturer": _string_at(registers, 40005, 16),
        "model": _string_at(registers, 40021, 16),
        "serial": _string_at(registers, 40053, 16),
        "device_address": registers[40068],
        "meter_model": {"id": meter_model, "length": model_length},
        "current_a": layout["current_a"],
        "voltage_v": layout["voltage_v"],
        "frequency_hz": layout["frequency_hz"],
        "power_w": layout["power_w"],
        "apparent_power_va": layout["apparent_power_va"],
        "power_factor": layout["power_factor"],
        "exported_energy_wh": layout["exported_energy_wh"],
        "event": (registers[event_address] << 16) | registers[event_address + 1],
        "end_model": {"id": registers[end_address], "length": registers[end_address + 1]},
    }
    expected = {
        "signature": "SunS",
        "common_model": {"id": 1, "length": 65},
        "device_address": unit_id,
        "meter_model": {"id": meter_model, "length": layout["length"]},
        "end_model": {"id": 0xFFFF, "length": 0},
    }
    mismatches = [
        f"{name}={result[name]!r}, expected {value!r}"
        for name, value in expected.items()
        if result[name] != value
    ]
    if mismatches:
        raise RuntimeError("invalid emulator SunSpec chain: " + "; ".join(mismatches))
    if expected_model is not None and meter_model != expected_model:
        raise RuntimeError(f"meter_model={meter_model}, expected {expected_model}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and display the SunSpec meter emulator")
    parser.add_argument("--host", default=os.environ.get("PROBE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MODBUS_PORT", "1502")))
    parser.add_argument("--unit", type=int, default=int(os.environ.get("MODBUS_UNIT_ID", "2")))
    parser.add_argument(
        "--model",
        type=int,
        choices=(203, 213),
        default=int(os.environ.get("SUNSPEC_METER_MODEL", "213")),
    )
    arguments = parser.parse_args()
    print(
        json.dumps(
            probe(
                arguments.host,
                arguments.port,
                arguments.unit,
                expected_model=arguments.model,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
