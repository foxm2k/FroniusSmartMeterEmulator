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
from .shelly import ShellyClient, ShellyReading
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
        # Windows' default event loop does not implement add_signal_handler.
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


async def _poll_sources(
    config: AppConfig,
    clients: Sequence[ShellyClient],
    state: EnergyStateStore,
    bank: RegisterBank,
    stop_event: asyncio.Event,
) -> None:
    latest: dict[str, ShellyReading] = {}
    failing: set[str] = set()
    source_phases = {source.name: source.phase for source in config.sources}
    enabled_names = set(source_phases)
    last_state_save = 0.0
    source_names = ",".join(client.config.name for client in clients)

    while not stop_event.is_set():
        poll_started = time.monotonic()
        LOGGER.info("Shelly poll request sources=%s", source_names)
        results = await asyncio.gather(
            *(asyncio.to_thread(client.fetch) for client in clients),
            return_exceptions=True,
        )
        successful: list[str] = []
        for client, result in zip(clients, results, strict=False):
            name = client.config.name
            if isinstance(result, BaseException):
                if name not in failing:
                    LOGGER.warning("Shelly %s unavailable: %s", name, result)
                    failing.add(name)
                continue

            if name in failing:
                LOGGER.info("Shelly %s recovered", name)
                failing.remove(name)
            latest[name] = result
            virtual_energy = state.update(name, result.raw_energy_wh, result.energy_field)
            successful.append(
                f"{name}={result.power_w:.1f}W/{virtual_energy:.1f}Wh[{result.energy_field}]"
            )
            LOGGER.debug(
                "%s: %.2f W, %.2f V, %.3f A, %.2f VA, %.3f Wh virtual",
                name,
                result.power_w,
                result.voltage_v,
                result.current_a,
                result.apparent_power_va,
                virtual_energy,
            )

        LOGGER.info(
            "Shelly poll result elapsed_ms=%.0f ok=%d/%d %s",
            (time.monotonic() - poll_started) * 1000,
            len(successful),
            len(clients),
            "; ".join(successful) if successful else "-",
        )

        monotonic_now = time.monotonic()
        if state.needs_save and (
            state.urgent_save
            or monotonic_now - last_state_save >= config.state_save_interval_seconds
        ):
            state.save()
            last_state_save = monotonic_now

        now = time.time()
        fresh = [
            reading
            for reading in latest.values()
            if now - reading.timestamp <= config.stale_after_seconds
        ]
        energies = {name: value for name, value in state.values.items() if name in enabled_names}
        snapshot = aggregate_readings(
            fresh,
            energies,
            source_phases,
            fallback_voltage_v=config.fallback_voltage_v,
            grid_frequency_hz=config.grid_frequency_hz,
        )
        bank.replace(
            build_registers(
                snapshot,
                unit_id=config.modbus_unit_id,
                serial=config.modbus_serial,
                meter_model=config.sunspec_meter_model,
            )
        )

        with suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=config.poll_interval_seconds)


async def run(config: AppConfig) -> None:
    state = EnergyStateStore(config.state_file)
    state.load()
    if state.recovered_from_backup:
        LOGGER.warning("Recovered persistent energy state from %s", state.backup_path)
    source_phases = {source.name: source.phase for source in config.sources}
    enabled_names = set(source_phases)
    initial_snapshot = aggregate_readings(
        [],
        {name: value for name, value in state.values.items() if name in enabled_names},
        source_phases,
        fallback_voltage_v=config.fallback_voltage_v,
        grid_frequency_hz=config.grid_frequency_hz,
    )
    bank = RegisterBank(
        build_registers(
            initial_snapshot,
            unit_id=config.modbus_unit_id,
            serial=config.modbus_serial,
            meter_model=config.sunspec_meter_model,
        )
    )
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
    tasks = {server_task, poll_task, stop_task}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stop_task:
                task.result()
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.close()
        if state.needs_save:
            state.save()


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
