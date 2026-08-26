from __future__ import annotations

import json
from pathlib import Path

import pytest

from fronius_emulator.state import EnergyStateStore, StateError


def test_counter_stays_monotonic_across_raw_reset(tmp_path: Path) -> None:
    store = EnergyStateStore(tmp_path / "state.json")

    assert store.update("shelly_1", 1000.0, "aenergy") == 1000.0
    assert store.update("shelly_1", 1005.0, "aenergy") == 1005.0
    assert store.update("shelly_1", 2.0, "aenergy") == 1007.0
    assert store.update("shelly_1", 3.5, "aenergy") == 1008.5


def test_energy_field_change_continues_virtual_counter(tmp_path: Path) -> None:
    store = EnergyStateStore(tmp_path / "state.json")
    store.update("shelly_1", 500.0, "aenergy")

    assert store.update("shelly_1", 20.0, "ret_aenergy") == 500.0
    assert store.update("shelly_1", 21.5, "ret_aenergy") == 501.5


def test_switching_energy_fields_repeatedly_never_adds_prior_raw_total(tmp_path: Path) -> None:
    store = EnergyStateStore(tmp_path / "state.json")

    assert store.update("shelly_1", 4000.0, "ret_aenergy") == 4000.0
    assert store.update("shelly_1", 5100.0, "aenergy") == 4000.0
    assert store.update("shelly_1", 4001.0, "ret_aenergy") == 4000.0
    assert store.update("shelly_1", 4002.0, "ret_aenergy") == 4001.0


def test_tiny_counter_rounding_drop_does_not_create_reset_offset(tmp_path: Path) -> None:
    store = EnergyStateStore(tmp_path / "state.json")
    store.update("shelly_1", 1_000_000.0, "aenergy")

    assert store.update("shelly_1", 999_999.5, "aenergy") == 1_000_000.0
    assert store.update("shelly_1", 1_000_001.0, "aenergy") == 1_000_001.0


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = EnergyStateStore(path)
    store.update("shelly_1", 123.5, "aenergy")
    store.save()

    restored = EnergyStateStore(path)
    restored.load()
    assert restored.values == {"shelly_1": 123.5}
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert restored.needs_save is False


def test_dirty_state_is_only_marked_for_real_changes_and_reset_is_urgent(
    tmp_path: Path,
) -> None:
    store = EnergyStateStore(tmp_path / "state.json")

    store.update("shelly_1", 100.0, "aenergy")
    assert store.needs_save is True
    assert store.urgent_save is True
    store.save()
    assert store.needs_save is False
    assert store.urgent_save is False

    store.update("shelly_1", 100.0, "aenergy")
    assert store.needs_save is False
    store.update("shelly_1", 101.0, "aenergy")
    assert store.needs_save is True
    assert store.urgent_save is False
    store.save()

    store.update("shelly_1", 0.5, "aenergy")
    assert store.urgent_save is True


def test_previous_generation_recovers_corrupt_primary_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = EnergyStateStore(path)
    store.update("shelly_1", 100.0, "aenergy")
    store.save()
    store.update("shelly_1", 101.0, "aenergy")
    store.save()
    assert store.backup_path.exists()
    path.write_text("not json", encoding="utf-8")

    restored = EnergyStateStore(path)
    restored.load()

    assert restored.values == {"shelly_1": 100.0}
    assert restored.recovered_from_backup is True
    assert restored.needs_save is True
    assert restored.urgent_save is True
    restored.save()
    assert restored.recovered_from_backup is False


def test_restart_recovers_unsaved_delta_from_preserved_shelly_counter(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = EnergyStateStore(path)
    store.update("shelly_1", 1000.0, "aenergy")
    store.save()
    assert store.update("shelly_1", 1020.0, "aenergy") == 1020.0

    restarted = EnergyStateStore(path)
    restarted.load()
    assert restarted.update("shelly_1", 1025.0, "aenergy") == 1025.0


def test_negative_baseline_offset_survives_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = EnergyStateStore(path)
    store.update("shelly_1", 500.0, "aenergy")
    assert store.update("shelly_1", 1000.0, "ret_aenergy") == 500.0
    store.save()

    restored = EnergyStateStore(path)
    restored.load()
    assert restored.update("shelly_1", 1001.5, "ret_aenergy") == 501.5


def test_invalid_state_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version":99,"sources":{}}', encoding="utf-8")

    with pytest.raises(StateError):
        EnergyStateStore(path).load()


@pytest.mark.parametrize("raw", [-1.0, float("nan"), float("inf")])
def test_invalid_energy_is_rejected(tmp_path: Path, raw: float) -> None:
    with pytest.raises(ValueError):
        EnergyStateStore(tmp_path / "state.json").update("shelly_1", raw, "aenergy")
