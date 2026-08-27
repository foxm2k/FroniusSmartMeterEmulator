from __future__ import annotations

import pytest

from fronius_emulator.config import ConfigError, load_config


def test_defaults_match_initial_installation() -> None:
    config = load_config({})

    assert len(config.sources) == 2
    assert config.sources[0].host == "192.168.123.100"
    assert config.sources[0].phase == "L1"
    assert config.sources[0].power_direction == "positive"
    assert config.sources[0].energy_field == "aenergy"
    assert config.sources[0].connect_timeout == 3.0
    assert config.sources[1].host == "192.168.123.102"
    assert config.sources[1].phase == "L1"
    assert config.sources[1].power_direction == "negative"
    assert config.sources[1].energy_field == "ret_aenergy"
    assert config.sources[1].connect_timeout == 3.0
    assert config.modbus_port == 1502
    assert config.modbus_unit_id == 2
    assert config.sunspec_meter_model == 213
    assert config.state_save_interval_seconds == 10.0


def test_optional_second_shelly_and_digest_password_default_user() -> None:
    config = load_config(
        {
            "SHELLY_2_HOST": "192.168.123.101",
            "SHELLY_2_PHASE": "L3",
            "SHELLY_2_PASSWORD": "secret",
        }
    )

    assert len(config.sources) == 2
    assert config.sources[1].phase == "L3"
    assert config.sources[1].username == "admin"
    assert config.sources[1].password == "secret"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SHELLY_1_PHASE", "A"),
        ("SHELLY_1_POWER_DIRECTION", "guess"),
        ("SHELLY_1_ENERGY_FIELD", "total"),
        ("SHELLY_1_HOST", "ftp://example.test"),
        ("MODBUS_UNIT_ID", "0"),
        ("MODBUS_PORT", "70000"),
        ("SUNSPEC_METER_MODEL", "211"),
        ("POLL_INTERVAL_SECONDS", "0"),
        ("STATE_SAVE_INTERVAL_SECONDS", "0"),
        ("GRID_FREQUENCY_HZ", "inf"),
        ("SHELLY_1_MIN_POWER_W", "nan"),
        ("STATE_FILE", ""),
    ],
)
def test_invalid_environment_is_rejected(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_config({name: value})


def test_stale_timeout_must_cover_poll_interval() -> None:
    with pytest.raises(ConfigError, match="STALE_AFTER_SECONDS"):
        load_config({"POLL_INTERVAL_SECONDS": "5", "STALE_AFTER_SECONDS": "2"})


def test_all_sources_cannot_be_disabled() -> None:
    with pytest.raises(ConfigError, match="At least one"):
        load_config({"SHELLY_1_HOST": "", "SHELLY_2_HOST": ""})


def test_model_203_can_be_selected_explicitly() -> None:
    assert load_config({"SUNSPEC_METER_MODEL": "203"}).sunspec_meter_model == 203
