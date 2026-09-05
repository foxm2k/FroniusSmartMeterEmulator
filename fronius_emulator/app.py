from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Sequence
from contextlib import suppress

from .aggregate import aggregate_readings
from .config import AppConfig, ConfigError, load_config
from .modbus import ModbusTcpServer, RegisterBank
from .shelly import ShellyClient, ShellyError, ShellyReading
from .state import EnergyStateStore
from .sunspec import build_registers

LOGGER = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _register_stop_signals(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


def _registers(
    config: AppConfig, readings: Sequence[ShellyReading], state: EnergyStateStore
) -> dict[int, int]:
    snapshot = aggregate_readings(
        readings,
        state.values,
        state.source_phases,
        fallback_voltage_v=config.fallback_voltage_v,
        grid_frequency_hz=config.grid_frequency_hz,
    )
    return build_registers(
        snapshot,
        unit_id=config.modbus_unit_id,
        serial=config.modbus_serial,
        meter_model=config.sunspec_meter_model,
    )


class _MeterRuntime:
    """One state owner and writer; expiry never waits for HTTP or disk I/O."""

    def __init__(self, config: AppConfig, state: EnergyStateStore, bank: RegisterBank) -> None:
        self.config = config
        self.state = state
        self.bank = bank
        self.latest: dict[str, tuple[ShellyReading, float]] = {}
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()

    def fresh(self, now: float | None = None) -> list[ShellyReading]:
        now = time.monotonic() if now is None else now
        return [
            reading
            for reading, received in self.latest.values()
            if now < received + self.config.stale_after_seconds
        ]

    def publish(self, now: float | None = None) -> None:
        self.bank.replace(_registers(self.config, self.fresh(now), self.state))

    async def persist(self, candidate: EnergyStateStore) -> None:
        # Only isolated candidates enter the writer thread. Join an in-flight
        # write before releasing the lock, including during cancellation.
        write = asyncio.create_task(asyncio.to_thread(candidate.save), name="state-write")
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            self.state.adopt(candidate)
            raise
        self.state.adopt(candidate)

    async def accept(self, client: ShellyClient, reading: ShellyReading) -> float:
        received = reading.monotonic_timestamp
        if received is None:
            received = time.monotonic()
        async with self.lock:
            candidate = self.state.copy()
            value = candidate.update(reading.name, reading.raw_energy_wh, reading.energy_field)
            # Expiry of another source must not uncover an out-of-range
            # voltage/frequency that averaging previously hid.
            # load_config supports at most two sources: singleton and combined
            # checks cover their non-empty fresh subsets; startup checks the empty set.
            # Revisit subset validation and its tests before adding more sources.
            _registers(self.config, [reading], candidate)
            fresh = [item for item in self.fresh() if item.name != reading.name]
            _registers(self.config, [*fresh, reading], candidate)
            client.accept(reading)
            self.latest[reading.name] = (reading, received)
            self.changed.set()
            self.publish()  # Fresh W with durably committed Wh.
            if candidate.urgent_save or candidate.values != self.state.values:
                await self.persist(candidate)
            else:
                self.state.adopt(candidate)  # Only raw-counter metadata can remain dirty.
            self.publish()  # Re-evaluate expiry after a potentially slow fsync.
            return value

    async def expire(self) -> None:
        while True:
            self.changed.clear()
            now = time.monotonic()
            self.publish(now)
            deadlines = [
                received + self.config.stale_after_seconds
                for _, received in self.latest.values()
                if received + self.config.stale_after_seconds > now
            ]
            delay = max(0.0, min(deadlines) - now) if deadlines else None
            with suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=delay)

    async def flush_metadata(self) -> None:
        while True:
            await asyncio.sleep(self.config.state_save_interval_seconds)
            async with self.lock:
                if self.state.needs_save:
                    await self.persist(self.state.copy())


async def _poll_sources(
    config: AppConfig,
    clients: Sequence[ShellyClient],
    state: EnergyStateStore,
    bank: RegisterBank,
    stop_event: asyncio.Event,
) -> None:
    state.set_phases(
        {source.name: source.phase for source in config.sources}, config.legacy_source_phases
    )
    runtime = _MeterRuntime(config, state, bank)

    async def poll(client: ShellyClient) -> None:
        name = client.config.name
        failing = False
        while True:
            started = time.monotonic()
            LOGGER.info("Shelly poll request sources=%s", name)
            try:
                reading = await client.fetch_async(commit=False)
                value = await runtime.accept(client, reading)
            except (ShellyError, ValueError, OverflowError) as exc:
                if not failing:
                    LOGGER.warning("Shelly %s unavailable: %s", name, exc)
                    failing = True
                LOGGER.info(
                    "Shelly poll result elapsed_ms=%.0f ok=0/1 - source=%s",
                    (time.monotonic() - started) * 1000,
                    name,
                )
            else:
                if failing:
                    LOGGER.info("Shelly %s recovered", name)
                    failing = False
                LOGGER.info(
                    "Shelly poll result elapsed_ms=%.0f ok=1/1 %s=%.1fW/%.1fWh[%s]",
                    (time.monotonic() - started) * 1000,
                    name,
                    reading.power_w,
                    value,
                    reading.energy_field,
                )
                LOGGER.debug(
                    "%s: %.2f W, %.2f V, %.3f A, %.2f VA, %.3f Wh virtual",
                    name,
                    reading.power_w,
                    reading.voltage_v,
                    reading.current_a,
                    reading.apparent_power_va,
                    value,
                )
            await asyncio.sleep(config.poll_interval_seconds)

    workers = [
        *(
            asyncio.create_task(poll(client), name=f"poll-{client.config.name}")
            for client in clients
        ),
        asyncio.create_task(runtime.expire(), name="meter-expiry"),
        asyncio.create_task(runtime.flush_metadata(), name="state-metadata"),
    ]
    stop = asyncio.create_task(stop_event.wait(), name="poll-stop")
    graceful = False
    try:
        done, _ = await asyncio.wait([*workers, stop], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stop:
                task.result()
        graceful = stop_event.is_set()
    finally:
        for task in [*workers, stop]:
            task.cancel()
        results = await asyncio.gather(*workers, stop, return_exceptions=True)
        # Don't hide a write failure raised while joining the writer at shutdown.
        for result in results:
            if isinstance(result, Exception):
                raise result
        if graceful and state.needs_save:
            await runtime.persist(state.copy())


async def run(config: AppConfig) -> None:
    state = EnergyStateStore(config.state_file)
    state.load()
    if state.recovered_from_backup:
        LOGGER.warning("Recovered persistent energy state from %s", state.backup_path)
    candidate = state.copy()
    candidate.set_phases(
        {source.name: source.phase for source in config.sources}, config.legacy_source_phases
    )
    missing_phases = state.values.keys() - state.source_phases.keys()
    migrated_phases = {
        name: phase for name, phase in candidate.source_phases.items() if name in missing_phases
    }
    # Model/config incompatibility must not roll back to an older backup.
    initial = _registers(config, [], candidate)
    if candidate.needs_save:
        await asyncio.to_thread(candidate.save)
    state.adopt(candidate)
    if migrated_phases:
        LOGGER.info(
            "Migrated historical source phases: %s",
            ", ".join(f"{name}={phase}" for name, phase in sorted(migrated_phases.items())),
        )
    bank = RegisterBank(initial)
    server = ModbusTcpServer(bank, config.modbus_host, config.modbus_port, config.modbus_unit_id)
    clients = tuple(ShellyClient(source) for source in config.sources)
    stop_event = asyncio.Event()
    _register_stop_signals(stop_event)
    LOGGER.info(
        "Configured Shelly sources: %s; SunSpec meter model: %d",
        ", ".join(f"{source.name}={source.host}/{source.phase}" for source in config.sources),
        config.sunspec_meter_model,
    )
    await server.start()
    server_task = asyncio.create_task(server.serve_forever(), name="modbus-server")
    poll_task = asyncio.create_task(
        _poll_sources(config, clients, state, bank, stop_event), name="shelly-poller"
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")
    try:
        done, _ = await asyncio.wait(
            [server_task, poll_task, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is not stop_task:
                task.result()
    finally:
        stop_event.set()
        await server.close()
        server_task.cancel()
        stop_task.cancel()
        await asyncio.gather(server_task, stop_task, return_exceptions=True)
        try:
            await poll_task
        finally:
            await asyncio.gather(*(client.aclose() for client in clients))


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("Configuration error: %s", exc)
        raise SystemExit(2) from exc
    _configure_logging(config.log_level)
    with suppress(KeyboardInterrupt):
        asyncio.run(run(config))


if __name__ == "__main__":
    main()
