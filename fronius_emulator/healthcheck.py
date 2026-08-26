from __future__ import annotations

import os
import socket
import struct
import sys

_MBAP = struct.Struct(">HHHB")


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("connection closed before complete response")
        chunks.extend(chunk)
    return bytes(chunks)


def check(host: str, port: int, unit_id: int, timeout: float = 2.0) -> None:
    request_pdu = struct.pack(">BHH", 3, 40000, 2)
    request = _MBAP.pack(1, 0, len(request_pdu) + 1, unit_id) + request_pdu
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(request)
        header = _receive_exact(connection, _MBAP.size)
        transaction_id, protocol_id, length, response_unit_id = _MBAP.unpack(header)
        pdu = _receive_exact(connection, length - 1)

    if (transaction_id, protocol_id, response_unit_id) != (1, 0, unit_id):
        raise RuntimeError("unexpected Modbus response header")
    if pdu != bytes((3, 4, 0x53, 0x75, 0x6E, 0x53)):
        raise RuntimeError(f"unexpected SunSpec signature response: {pdu.hex()}")


def main() -> None:
    try:
        check(
            os.environ.get("HEALTHCHECK_HOST", "127.0.0.1"),
            int(os.environ.get("MODBUS_PORT", "1502")),
            int(os.environ.get("MODBUS_UNIT_ID", "2")),
        )
    except Exception as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
