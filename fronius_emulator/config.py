from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .shelly import ShellyConfigurationError, ShellySourceConfig


class ConfigError(ValueError):
    """Raised when an environment setting is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    sources: tuple[ShellySourceConfig, ...]
    poll_interval_seconds: float
    stale_after_seconds: float
    state_save_interval_seconds: float
    modbus_host: str
    modbus_port: int
    modbus_unit_id: int
    modbus_serial: str
    sunspec_meter_model: int
    grid_frequency_hz: float
    fallback_voltage_v: float
    state_file: Path
    log_level: str
    legacy_source_phases: Mapping[str, str] = field(default_factory=dict)


def _float_setting(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or not value > 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def _nonnegative_float_setting(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ConfigError(f"{name} must be zero or greater")
    return value


def _int_setting(
    env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _source_config(
    env: Mapping[str, str],
    number: int,
    *,
    default_host: str,
    default_phase: str,
    default_power_direction: str,
    default_energy_field: str,
    connect_timeout: float,
    read_timeout: float,
    total_timeout: float,
) -> ShellySourceConfig | None:
    prefix = f"SHELLY_{number}_"
    host = env.get(f"{prefix}HOST", default_host).strip()
    if not host:
        return None

    phase = env.get(f"{prefix}PHASE", default_phase).strip().upper()
    if phase not in {"L1", "L2", "L3"}:
        raise ConfigError(f"{prefix}PHASE must be L1, L2, or L3")

    power_direction = env.get(f"{prefix}POWER_DIRECTION", default_power_direction).strip().lower()
    if power_direction not in {"auto", "positive", "negative", "absolute"}:
        raise ConfigError(f"{prefix}POWER_DIRECTION must be auto, positive, negative, or absolute")

    energy_field = env.get(f"{prefix}ENERGY_FIELD", default_energy_field).strip().lower()
    if energy_field not in {"auto", "aenergy", "ret_aenergy"}:
        raise ConfigError(f"{prefix}ENERGY_FIELD must be auto, aenergy, or ret_aenergy")

    username = env.get(f"{prefix}USERNAME", "").strip() or None
    password = env.get(f"{prefix}PASSWORD", "") or None
    if password and not username:
        username = "admin"

    try:
        return ShellySourceConfig(
            name=f"shelly_{number}",
            host=host,
            phase=phase,
            username=username,
            password=password,
            power_direction=power_direction,
            energy_field=energy_field,
            min_power_w=_nonnegative_float_setting(env, f"{prefix}MIN_POWER_W", 3.0),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_timeout=total_timeout,
        )
    except ShellyConfigurationError as exc:
        raise ConfigError(f"Invalid {prefix.rstrip('_')} configuration: {exc}") from exc


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    values = os.environ if env is None else env
    connect_timeout = _float_setting(values, "HTTP_CONNECT_TIMEOUT_SECONDS", 3.0)
    read_timeout = _float_setting(values, "HTTP_READ_TIMEOUT_SECONDS", 2.0)
    total_timeout = _float_setting(
        {
            "HTTP_TOTAL_TIMEOUT_SECONDS": values.get("HTTP_TOTAL_TIMEOUT_SECONDS", "").strip()
            or str(2 * (connect_timeout + read_timeout))
        },
        "HTTP_TOTAL_TIMEOUT_SECONDS",
        10.0,
    )
    poll_interval = _float_setting(values, "POLL_INTERVAL_SECONDS", 2.0)
    stale_after = _float_setting(values, "STALE_AFTER_SECONDS", 10.0)
    if stale_after < poll_interval:
        raise ConfigError("STALE_AFTER_SECONDS must be at least POLL_INTERVAL_SECONDS")

    sources = tuple(
        source
        for source in (
            _source_config(
                values,
                1,
                default_host="192.168.123.100",
                default_phase="L1",
                default_power_direction="positive",
                default_energy_field="aenergy",
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                total_timeout=total_timeout,
            ),
            _source_config(
                values,
                2,
                default_host="192.168.123.102",
                default_phase="L1",
                default_power_direction="negative",
                default_energy_field="ret_aenergy",
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                total_timeout=total_timeout,
            ),
        )
        if source is not None
    )
    if not sources:
        raise ConfigError("At least one SHELLY_n_HOST must be configured")

    serial = values.get("MODBUS_SERIAL", "FSMEMU0000000001").strip()
    if not serial or "\x00" in serial or len(serial.encode("utf-8")) > 32:
        raise ConfigError("MODBUS_SERIAL must contain 1 to 32 UTF-8 bytes and no NUL")

    log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("LOG_LEVEL is invalid")

    state_file_raw = values.get("STATE_FILE", "data/state.json").strip()
    if not state_file_raw:
        raise ConfigError("STATE_FILE must not be empty")

    sunspec_meter_model = _int_setting(values, "SUNSPEC_METER_MODEL", 213, 203, 213)
    if sunspec_meter_model not in {203, 213}:
        raise ConfigError("SUNSPEC_METER_MODEL must be 203 or 213")

    return AppConfig(
        sources=sources,
        poll_interval_seconds=poll_interval,
        stale_after_seconds=stale_after,
        state_save_interval_seconds=_float_setting(values, "STATE_SAVE_INTERVAL_SECONDS", 10.0),
        modbus_host=values.get("MODBUS_HOST", "0.0.0.0").strip() or "0.0.0.0",
        modbus_port=_int_setting(values, "MODBUS_PORT", 1502, 1, 65535),
        modbus_unit_id=_int_setting(values, "MODBUS_UNIT_ID", 2, 1, 247),
        modbus_serial=serial,
        sunspec_meter_model=sunspec_meter_model,
        grid_frequency_hz=_float_setting(values, "GRID_FREQUENCY_HZ", 50.0),
        fallback_voltage_v=_float_setting(values, "FALLBACK_VOLTAGE_V", 230.0),
        state_file=Path(state_file_raw),
        log_level=log_level,
        # Keep invalid inactive settings for diagnostics if legacy history needs them.
        legacy_source_phases={
            f"shelly_{number}": values.get(f"SHELLY_{number}_PHASE", "L1").strip().upper()
            for number in (1, 2)
        },
    )
