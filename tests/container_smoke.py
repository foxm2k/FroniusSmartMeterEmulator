"""Stdlib-only acceptance test run inside the built Linux production image."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The script is mounted at /tests; the production package is installed at /app.
sys.path.insert(0, "/app")

from fronius_emulator.healthcheck import check  # noqa: E402
from fronius_emulator.probe import probe  # noqa: E402


def shelly(payload, slow, started, shutdown):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps(payload).encode()
            padding = 10000 if slow.is_set() else 0
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + padding))
            self.end_headers()
            try:
                if padding:
                    started.set()
                for _ in range(padding):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    if shutdown.wait(0.005):
                        return
                self.wfile.write(body)
            except ConnectionError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def wait_reading(port, model, predicate):
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        try:
            last = probe("127.0.0.1", port, 2, expected_model=model)
            if predicate(last):
                return last
        except (OSError, ConnectionError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"Expected meter state not reached: {last}")


def scenario(model):
    stop, slow, started = threading.Event(), threading.Event(), threading.Event()
    payload1 = {"apower": 500, "voltage": 230, "current": 2.2, "aenergy": {"total": 1000}}
    payload2 = {"apower": -300, "voltage": 230, "current": 1.4, "ret_aenergy": {"total": 2000}}
    one, thread1 = shelly(payload1, slow, started, stop)
    two, thread2 = shelly(payload2, threading.Event(), threading.Event(), stop)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    processes = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            env = {
                **os.environ,
                "SHELLY_1_HOST": f"127.0.0.1:{one.server_port}",
                "SHELLY_2_HOST": f"127.0.0.1:{two.server_port}",
                "MODBUS_HOST": "127.0.0.1",
                "MODBUS_PORT": str(port),
                "SUNSPEC_METER_MODEL": str(model),
                "STATE_FILE": str(Path(directory) / "state.json"),
                "POLL_INTERVAL_SECONDS": "0.02",
                "STALE_AFTER_SECONDS": "0.2",
                "HTTP_TOTAL_TIMEOUT_SECONDS": "10",
                "HTTP_READ_TIMEOUT_SECONDS": "1",
            }

            def launch():
                process = subprocess.Popen(
                    [sys.executable, "-m", "fronius_emulator"],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                processes.append(process)
                return process

            process = launch()
            wait_reading(
                port, model, lambda r: r["power_w"] == -800 and r["exported_energy_wh"] == 3000
            )
            slow.set()
            payload2["apower"] = 0
            assert started.wait(3)
            expired = wait_reading(port, model, lambda r: r["power_w"] == 0)
            assert expired["exported_energy_wh"] == 3000
            check("127.0.0.1", port, 2)
            before = time.monotonic()
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=5)
            assert process.returncode == 0, output
            assert time.monotonic() - before < 5
            slow.clear()
            payload1["aenergy"]["total"] = 1001
            restarted = launch()
            result = wait_reading(port, model, lambda r: r["exported_energy_wh"] >= 3001)
            assert result["exported_energy_wh"] == 3001
            restarted.send_signal(signal.SIGTERM)
            output, _ = restarted.communicate(timeout=5)
            assert restarted.returncode == 0, output
            print(f"Model {model}: polling, expiry, healthcheck, SIGTERM and restart passed")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        stop.set()
        for server, thread in ((one, thread1), (two, thread2)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    for selected_model in (213, 203):
        scenario(selected_model)
