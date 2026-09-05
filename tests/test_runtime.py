from __future__ import annotations

import asyncio
import errno
import json
import struct
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fronius_emulator import app
from fronius_emulator.config import load_config
from fronius_emulator.modbus import RegisterBank
from fronius_emulator.shelly import ShellyClient, ShellyConnectionError, ShellyReading
from fronius_emulator.state import EnergyStateStore, StateError


def config(tmp_path, **env):
    return load_config(
        {
            "SHELLY_1_HOST": "example.test",
            "SHELLY_2_HOST": "",
            "STATE_FILE": str(tmp_path / "state.json"),
            "POLL_INTERVAL_SECONDS": "0.01",
            "STALE_AFTER_SECONDS": "0.05",
            "STATE_SAVE_INTERVAL_SECONDS": "0.05",
            **env,
        }
    )


def reading(source, *, energy=1000.0, power=500.0, received=None):
    return ShellyReading(
        name=source.name,
        phase=source.phase,
        power_w=power,
        voltage_v=230.0,
        current_a=power / 230.0,
        frequency_hz=50.0,
        apparent_power_va=power,
        power_factor=1.0,
        raw_energy_wh=energy,
        energy_field=source.energy_field,
        timestamp=time.time(),
        monotonic_timestamp=time.monotonic() if received is None else received,
    )


def runtime(cfg):
    state = EnergyStateStore(cfg.state_file)
    state.set_phases({s.name: s.phase for s in cfg.sources}, cfg.legacy_source_phases)
    state.save()
    bank = RegisterBank(app._registers(cfg, [], state))
    return app._MeterRuntime(cfg, state, bank)


def power(bank):
    return struct.unpack(">f", struct.pack(">HH", *bank.read(40097, 2)))[0]


def energy(bank, model=213):
    words = bank.read(40129 if model == 213 else 40107, 2)
    return (
        struct.unpack(">f", struct.pack(">HH", *words))[0]
        if model == 213
        else (words[0] << 16 | words[1])
    )


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


@pytest.mark.parametrize("clock_shift", [-3600, 3600])
def test_expiry_uses_monotonic_time(tmp_path, monkeypatch, clock_shift):
    cfg = config(tmp_path)
    rt = runtime(cfg)
    clock = SimpleNamespace(monotonic=lambda: clock.now, time=lambda: 1000 + clock_shift, now=1.0)
    monkeypatch.setattr(app, "time", clock)

    async def scenario():
        await rt.accept(ShellyClient(cfg.sources[0]), reading(cfg.sources[0], received=1.0))
        assert power(rt.bank) == -500
        clock.now = 1 + cfg.stale_after_seconds
        rt.publish()
        assert power(rt.bank) == 0
        assert energy(rt.bank) == 1000

    asyncio.run(scenario())


def test_hung_source_does_not_block_fast_source_or_expiry(tmp_path):
    cfg = config(tmp_path, SHELLY_2_HOST="example.test")
    rt = runtime(cfg)
    second_started = asyncio.Event()

    class Slow(ShellyClient):
        calls = 0

        async def fetch_async(self, **kwargs):
            self.calls += 1
            if self.calls > 1:
                second_started.set()
                await asyncio.Future()
            return reading(self.config)

    class Fast(ShellyClient):
        calls = 0

        async def fetch_async(self, **kwargs):
            self.calls += 1
            return reading(self.config, power=300 if self.calls == 1 else 0)

    async def scenario():
        slow, fast = Slow(cfg.sources[0]), Fast(cfg.sources[1])
        stop = asyncio.Event()
        task = asyncio.create_task(app._poll_sources(cfg, [slow, fast], rt.state, rt.bank, stop))
        try:
            await asyncio.wait_for(second_started.wait(), 3)
            await until(lambda: fast.calls >= 4 and power(rt.bank) == 0)
            assert energy(rt.bank) == 2000
        finally:
            stop.set()
            await asyncio.wait_for(task, 3)
        assert slow.calls == 2

    asyncio.run(scenario())


def test_disk_delay_never_publishes_unsaved_energy_and_expiry_keeps_running(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    rt = runtime(cfg)
    entered, release = threading.Event(), threading.Event()
    original_save = EnergyStateStore.save

    def delayed_save(self):
        entered.set()
        assert release.wait(3)
        original_save(self)

    async def scenario():
        client = ShellyClient(cfg.sources[0])
        await rt.accept(client, reading(client.config))
        monkeypatch.setattr(EnergyStateStore, "save", delayed_save)
        expiry = asyncio.create_task(rt.expire())
        update = asyncio.create_task(rt.accept(client, reading(client.config, energy=1003)))
        try:
            await until(entered.is_set)
            assert energy(rt.bank) == 1000
            assert power(rt.bank) == -500
            await until(lambda: power(rt.bank) == 0)
            assert energy(rt.bank) == 1000
            release.set()
            await update
            assert energy(rt.bank) == 1003
            assert power(rt.bank) == 0  # Finishing fsync must not resurrect old W.
            restored = EnergyStateStore(cfg.state_file)
            restored.load()
            assert restored.values == {"shelly_1": 1003}
        finally:
            release.set()
            await update
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)

    asyncio.run(scenario())


def test_cancellation_joins_write_without_overwriting_new_state(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    rt = runtime(cfg)
    entered, release = threading.Event(), threading.Event()
    original_save = EnergyStateStore.save

    def delayed_save(self):
        entered.set()
        assert release.wait(3)
        original_save(self)

    async def scenario():
        client = ShellyClient(cfg.sources[0])
        await rt.accept(client, reading(client.config))
        monkeypatch.setattr(EnergyStateStore, "save", delayed_save)
        task = asyncio.create_task(rt.accept(client, reading(client.config, energy=1003)))
        try:
            await until(entered.is_set)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert rt.state.values == {"shelly_1": 1003}
        assert energy(rt.bank) == 1000
        restored = EnergyStateStore(cfg.state_file)
        restored.load()
        assert restored.values == {"shelly_1": 1003}

    asyncio.run(scenario())


@pytest.mark.parametrize("model,bad_energy", [(213, 1e40), (203, 2**32)])
def test_bad_reading_is_transactional_and_next_valid_reading_recovers(tmp_path, model, bad_energy):
    cfg = config(tmp_path, SUNSPEC_METER_MODEL=str(model), SHELLY_1_POWER_DIRECTION="auto")
    rt = runtime(cfg)
    client = ShellyClient(cfg.sources[0])

    async def scenario():
        before = cfg.state_file.read_bytes()
        invalid = replace(reading(client.config, energy=bad_energy), auto_uses_negative=True)
        with pytest.raises(ValueError):
            await rt.accept(client, invalid)
        assert cfg.state_file.read_bytes() == before
        assert rt.latest == {}
        assert rt.state.values == {}
        assert client._auto_uses_negative is None
        await rt.accept(client, replace(reading(client.config), auto_uses_negative=False))
        assert client._auto_uses_negative is False
        assert energy(rt.bank, model) == 1000

    asyncio.run(scenario())


def test_sum_overflow_does_not_modify_second_source(tmp_path):
    cfg = config(
        tmp_path, SHELLY_2_HOST="example.test", SUNSPEC_METER_MODEL="203", STALE_AFTER_SECONDS="60"
    )
    rt = runtime(cfg)

    async def scenario():
        first, second = [ShellyClient(source) for source in cfg.sources]
        await rt.accept(first, reading(first.config, power=2000))
        before = cfg.state_file.read_bytes()
        with pytest.raises(ValueError, match="total power"):
            await rt.accept(second, reading(second.config, power=2000))
        assert cfg.state_file.read_bytes() == before
        assert set(rt.latest) == {"shelly_1"}
        assert rt.state.values == {"shelly_1": 1000}
        await rt.accept(second, reading(second.config, power=1000))
        assert rt.state.values == {"shelly_1": 1000, "shelly_2": 1000}

    asyncio.run(scenario())


def test_deactivation_and_reactivation_preserve_total_and_phase_energy(tmp_path):
    cfg = config(tmp_path, SHELLY_2_HOST="example.test", SHELLY_2_PHASE="L2")
    rt = runtime(cfg)

    async def scenario():
        for source in cfg.sources:
            await rt.accept(ShellyClient(source), reading(source))
        disabled = replace(cfg, sources=(cfg.sources[0],))
        state = EnergyStateStore(cfg.state_file)
        state.load()
        state.set_phases({"shelly_1": "L1"}, {})
        bank = RegisterBank(app._registers(disabled, [], state))
        assert energy(bank) == 2000
        assert bank.read(40133, 2) == rt.bank.read(40133, 2)
        restored = app._MeterRuntime(cfg, state, bank)
        await restored.accept(ShellyClient(cfg.sources[1]), reading(cfg.sources[1], energy=1005))
        assert energy(bank) == 2005

    asyncio.run(scenario())


def test_unchanged_readings_do_not_write(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    rt = runtime(cfg)

    async def scenario():
        client = ShellyClient(cfg.sources[0])
        await rt.accept(client, reading(client.config, power=0))

        def unexpected_write(self):
            pytest.fail("unchanged night counter caused a write")

        monkeypatch.setattr(EnergyStateStore, "save", unexpected_write)
        for _ in range(5):
            await rt.accept(client, reading(client.config, power=0))
        assert not rt.state.needs_save

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EACCES])
def test_write_failure_never_publishes_new_counter(tmp_path, monkeypatch, error):
    cfg = config(tmp_path)
    rt = runtime(cfg)

    async def scenario():
        client = ShellyClient(cfg.sources[0])
        await rt.accept(client, reading(client.config))
        before = cfg.state_file.read_bytes()

        def fail_replace(*args):
            raise OSError(error, "simulated storage failure")

        monkeypatch.setattr("fronius_emulator.state.os.replace", fail_replace)
        with pytest.raises(StateError):
            await rt.accept(client, reading(client.config, energy=1003))
        assert energy(rt.bank) == 1000
        assert cfg.state_file.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp"))

    asyncio.run(scenario())


def test_model_change_does_not_load_older_backup(tmp_path):
    cfg = config(tmp_path, SUNSPEC_METER_MODEL="203")
    store = EnergyStateStore(cfg.state_file)
    store.update("shelly_1", 1000, "aenergy")
    store.save()
    store.update("shelly_1", 2**32, "aenergy")
    store.save()
    before = cfg.state_file.read_bytes()
    with pytest.raises(ValueError, match="acc32"):
        asyncio.run(app.run(cfg))
    assert cfg.state_file.read_bytes() == before
    assert json.loads(store.backup_path.read_text())["sources"]["shelly_1"]["value_wh"] == 1000


def test_restart_serves_last_published_energy_while_shelly_offline(tmp_path, monkeypatch):
    cfg = replace(config(tmp_path), modbus_host="127.0.0.1", modbus_port=0)
    rt = runtime(cfg)
    servers, stops = [], []
    original_server = app.ModbusTcpServer

    class Server(original_server):
        def __init__(self, *args):
            super().__init__(*args)
            servers.append(self)

    class Offline(ShellyClient):
        async def fetch_async(self, **kwargs):
            raise ShellyConnectionError("offline after restart")

    monkeypatch.setattr(app, "ModbusTcpServer", Server)
    monkeypatch.setattr(app, "ShellyClient", Offline)
    monkeypatch.setattr(app, "_register_stop_signals", stops.append)

    async def scenario():
        client = ShellyClient(cfg.sources[0])
        await rt.accept(client, reading(client.config))
        await rt.accept(client, reading(client.config, energy=1003))
        published = energy(rt.bank)
        task = asyncio.create_task(app.run(cfg))
        try:
            await until(lambda: servers and servers[0]._server is not None)
            assert energy(servers[0].bank) >= published
        finally:
            stops[0].set()
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("raw_phase,normalised_phase", [(" l9 ", "L9"), ("", "")])
def test_legacy_migration_identifies_invalid_phase_without_changing_state(
    tmp_path, caplog, raw_phase, normalised_phase
):
    cfg = config(tmp_path, SHELLY_1_HOST="", SHELLY_1_PHASE=raw_phase, SHELLY_2_HOST="example.test")
    store = EnergyStateStore(cfg.state_file)
    store.update("shelly_1", 1000, "aenergy")
    store.save()
    payload = json.loads(cfg.state_file.read_text())
    del payload["source_phases"]
    cfg.state_file.write_text(json.dumps(payload))
    before = cfg.state_file.read_bytes()
    caplog.set_level("INFO", logger="fronius_emulator.app")

    with pytest.raises(StateError) as error:
        asyncio.run(app.run(cfg))

    message = str(error.value)
    assert "No saved phase for historical energy of shelly_1" in message
    assert f"SHELLY_1_PHASE={normalised_phase!r} is invalid" in message
    assert "expected L1, L2, or L3" in message
    assert cfg.state_file.read_bytes() == before
    assert not any("Migrated historical source phases" in message for message in caplog.messages)


def test_failed_save_does_not_report_successful_phase_migration(tmp_path, monkeypatch, caplog):
    cfg = config(tmp_path)
    store = EnergyStateStore(cfg.state_file)
    store.update("shelly_1", 1000, "aenergy")
    store.save()
    before = cfg.state_file.read_bytes()
    caplog.set_level("INFO", logger="fronius_emulator.app")

    def fail_save(self):
        raise StateError("simulated migration write failure")

    monkeypatch.setattr(EnergyStateStore, "save", fail_save)
    with pytest.raises(StateError, match="migration write failure"):
        asyncio.run(app.run(cfg))
    assert cfg.state_file.read_bytes() == before
    assert not any("Migrated historical source phases" in message for message in caplog.messages)


def test_startup_logs_saved_phase_migration_once_and_keeps_it_when_source_disabled(
    tmp_path, monkeypatch, caplog
):
    cfg = replace(
        config(tmp_path, SHELLY_1_HOST="", SHELLY_1_PHASE="L3", SHELLY_2_HOST="example.test"),
        modbus_host="127.0.0.1",
        modbus_port=0,
    )
    store = EnergyStateStore(cfg.state_file)
    store.update("shelly_1", 500, "aenergy")
    store.update("shelly_1", 1000, "ret_aenergy")
    store.save()
    payload = json.loads(cfg.state_file.read_text())
    del payload["source_phases"]
    cfg.state_file.write_text(json.dumps(payload))
    caplog.set_level("INFO", logger="fronius_emulator.app")

    class Offline(ShellyClient):
        async def fetch_async(self, **kwargs):
            raise ShellyConnectionError("simulated offline source")

    def stop_after_startup(event):
        # Signals are registered after migration has been saved and logged.
        saved = json.loads(cfg.state_file.read_text())
        assert saved["source_phases"]["shelly_1"] == "L3"
        assert saved["sources"] == payload["sources"]
        assert caplog.messages.count("Migrated historical source phases: shelly_1=L3") == 1
        event.set()

    monkeypatch.setattr(app, "ShellyClient", Offline)
    monkeypatch.setattr(app, "_register_stop_signals", stop_after_startup)
    asyncio.run(app.run(cfg))
    saved = cfg.state_file.read_bytes()
    cfg = replace(
        config(tmp_path, SHELLY_1_HOST="", SHELLY_1_PHASE="L9", SHELLY_2_HOST="example.test"),
        modbus_host="127.0.0.1",
        modbus_port=0,
    )
    asyncio.run(app.run(cfg))
    assert cfg.state_file.read_bytes() == saved
    assert caplog.messages.count("Migrated historical source phases: shelly_1=L3") == 1


async def read_words(reader, writer, address, count):
    writer.write(struct.pack(">HHHBBHH", 1, 0, 6, 2, 3, address, count))
    await writer.drain()
    header = await reader.readexactly(7)
    _, _, length, _ = struct.unpack(">HHHB", header)
    body = await reader.readexactly(length - 1)
    assert body[:2] == bytes((3, count * 2))
    return struct.unpack(f">{count}H", body[2:])


def test_fatal_write_failure_closes_live_modbus_connections(tmp_path, monkeypatch):
    cfg = replace(config(tmp_path), modbus_host="127.0.0.1", modbus_port=0)
    servers, stops = [], []
    fail_next = asyncio.Event()
    original_server = app.ModbusTcpServer

    class Server(original_server):
        def __init__(self, *args):
            super().__init__(*args)
            servers.append(self)

    class Source(ShellyClient):
        calls = 0

        async def fetch_async(self, **kwargs):
            self.calls += 1
            if self.calls > 1:
                await fail_next.wait()
            return reading(self.config, energy=1000 if self.calls == 1 else 1003)

    monkeypatch.setattr(app, "ModbusTcpServer", Server)
    monkeypatch.setattr(app, "ShellyClient", Source)
    monkeypatch.setattr(app, "_register_stop_signals", stops.append)

    async def scenario():
        task = asyncio.create_task(app.run(cfg))
        writer = None
        try:
            await until(lambda: servers and energy(servers[0].bank) == 1000)
            port = servers[0]._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            saved_words = await read_words(reader, writer, 40129, 2)
            before = cfg.state_file.read_bytes()

            def fail_replace(*args):
                raise OSError(errno.ENOSPC, "simulated full disk")

            monkeypatch.setattr("fronius_emulator.state.os.replace", fail_replace)
            fail_next.set()
            with pytest.raises(StateError):
                await asyncio.wait_for(task, 3)
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert tuple(servers[0].bank.read(40129, 2)) == saved_words
            assert cfg.state_file.read_bytes() == before
        finally:
            if stops:
                stops[0].set()
            await asyncio.gather(task, return_exceptions=True)
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("model,count", [(213, 124), (203, 105)])
def test_concurrent_modbus_reads_only_see_complete_published_snapshots(tmp_path, model, count):
    cfg = config(tmp_path, SUNSPEC_METER_MODEL=str(model), STALE_AFTER_SECONDS="60")
    rt = runtime(cfg)
    published = set()

    class ObservedBank(RegisterBank):
        def replace(self, registers):
            published.add(tuple(registers[address] for address in range(40071, 40071 + count)))
            super().replace(registers)

    rt.bank = ObservedBank(app._registers(cfg, [], rt.state))

    async def scenario():
        client = ShellyClient(cfg.sources[0])
        await rt.accept(client, reading(client.config))
        server = app.ModbusTcpServer(rt.bank, "127.0.0.1", 0, 2)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        done = asyncio.Event()
        observed = set()

        async def update():
            sequence = 1
            while not done.is_set():
                await rt.accept(client, reading(client.config, power=100 + sequence % 500))
                sequence += 1
                await asyncio.sleep(0.001)

        async def read():
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                for _ in range(100):
                    words = await read_words(reader, writer, 40071, count)
                    assert words in published
                    observed.add(words)
            finally:
                writer.close()
                await writer.wait_closed()

        updating = asyncio.create_task(update())
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(read(), read(), read())
            assert len(observed) > 1
        finally:
            done.set()
            await updating
            await server.close()

    asyncio.run(scenario())
