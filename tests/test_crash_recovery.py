"""Abrupt process exit bypasses asyncio cancellation and normal shutdown saves."""

import os
import subprocess
import sys

import pytest

from fronius_emulator.state import EnergyStateStore

CHILD = r"""
import asyncio, os, sys
from pathlib import Path
from fronius_emulator.app import _MeterRuntime, _registers
from fronius_emulator.config import load_config
from fronius_emulator.modbus import RegisterBank
from fronius_emulator.shelly import ShellyClient, ShellyReading
from fronius_emulator.state import EnergyStateStore

path, stage = sys.argv[1:]
cfg = load_config({"SHELLY_2_HOST":"", "STATE_FILE":path})
store = EnergyStateStore(Path(path))
store.load()
bank = RegisterBank(_registers(cfg, [], store))
runtime = _MeterRuntime(cfg, store, bank)
original_save = EnergyStateStore.save
original_replace = os.replace

def save(self):
    if stage == "before_save":
        os._exit(77)
    original_save(self)
    if stage == "after_save":
        os._exit(77)

def replace(source, target):
    if Path(target) == Path(path) and stage == "before_replace":
        os._exit(77)
    original_replace(source, target)
    if Path(target) == Path(path) and stage == "after_replace":
        os._exit(77)

EnergyStateStore.save = save
os.replace = replace
async def scenario():
    client = ShellyClient(cfg.sources[0])
    reading = ShellyReading("shelly_1", "L1", 500, 230, 2.2, 50, 506, None,
                            1003, "aenergy", 1)
    await runtime.accept(client, reading)
    os._exit(77)
asyncio.run(scenario())
"""


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("before_save", 1000),
        ("before_replace", 1000),
        ("after_replace", 1003),
        ("after_save", 1003),
        ("after_publish", 1003),
    ],
)
def test_abrupt_exit_at_commit_boundaries(tmp_path, stage, expected):
    path = tmp_path / "state.json"
    store = EnergyStateStore(path)
    store.update("shelly_1", 1000, "aenergy")
    store.set_phases({"shelly_1": "L1"}, {})
    store.save()
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(path), stage],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 77, result.stderr
    restored = EnergyStateStore(path)
    restored.load()
    assert restored.values == {"shelly_1": expected}
    assert not restored.recovered_from_backup
    assert restored.update("shelly_1", 1005, "aenergy") == 1005
