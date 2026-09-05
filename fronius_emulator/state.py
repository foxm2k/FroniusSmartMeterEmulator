from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Mapping
from contextlib import suppress
from copy import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from .sunspec import FLOAT32_MAX


class StateError(RuntimeError):
    """Raised when persistent energy state cannot be read or written."""


@dataclass(frozen=True, slots=True)
class EnergyCounter:
    last_raw_wh: float
    offset_wh: float
    value_wh: float
    energy_field: str


class EnergyStateStore:
    """Keep virtual lifetime energy monotonic across Shelly counter resets."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sources: dict[str, EnergyCounter] = {}
        self._source_phases: dict[str, str] = {}
        self._dirty = False
        self._urgent = False
        self.recovered_from_backup = False

    @property
    def values(self) -> dict[str, float]:
        return {name: counter.value_wh for name, counter in self._sources.items()}

    @property
    def source_phases(self) -> dict[str, str]:
        return self._source_phases.copy()

    def copy(self) -> EnergyStateStore:
        """An isolated candidate; counters are immutable and mappings are copied."""
        candidate = copy(self)
        candidate._sources = self._sources.copy()
        candidate._source_phases = self._source_phases.copy()
        return candidate

    def adopt(self, candidate: EnergyStateStore) -> None:
        if candidate.path != self.path:
            raise ValueError("candidate belongs to a different state file")
        self._sources = candidate._sources.copy()
        self._source_phases = candidate._source_phases.copy()
        self._dirty = candidate._dirty
        self._urgent = candidate._urgent
        self.recovered_from_backup = candidate.recovered_from_backup

    def set_phases(self, active: Mapping[str, str], legacy: Mapping[str, str]) -> None:
        phases = self._source_phases.copy()
        for name in self._sources.keys() | active.keys():
            phase = active.get(name, phases.get(name, legacy.get(name)))
            if phase not in {"L1", "L2", "L3"}:
                if name not in active and name not in phases and name in legacy:
                    raise StateError(
                        f"No saved phase for historical energy of {name}; "
                        f"{name.upper()}_PHASE={legacy[name]!r} is invalid; "
                        "expected L1, L2, or L3"
                    )
                raise StateError(f"Cannot assign historical energy of {name} to a phase")
            phases[name] = phase
        if phases != self._source_phases:
            self._source_phases = phases
            self._dirty = self._urgent = True

    @property
    def needs_save(self) -> bool:
        return self._dirty

    @property
    def urgent_save(self) -> bool:
        return self._urgent

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.bak")

    def load(self) -> None:
        candidates = tuple(path for path in (self.path, self.backup_path) if path.exists())
        if not candidates:
            return
        errors: list[str] = []
        for candidate in candidates:
            try:
                self._sources, self._source_phases = self._load_file(candidate)
                self.recovered_from_backup = candidate == self.backup_path
                self._dirty = self.recovered_from_backup
                self._urgent = self.recovered_from_backup
                return
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                errors.append(f"{candidate}: {exc}")
        raise StateError("Cannot read persistent energy state: " + "; ".join(errors))

    def _load_file(self, path: Path) -> tuple[dict[str, EnergyCounter], dict[str, str]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("state document must be an object")
        if payload.get("version") != 1 or not isinstance(payload.get("sources"), dict):
            raise ValueError("unsupported state format")
        loaded: dict[str, EnergyCounter] = {}
        for name, item in payload["sources"].items():
            counter = EnergyCounter(
                last_raw_wh=float(item["last_raw_wh"]),
                offset_wh=float(item["offset_wh"]),
                value_wh=float(item["value_wh"]),
                energy_field=str(item["energy_field"]),
            )
            if not all(
                math.isfinite(value)
                for value in (counter.last_raw_wh, counter.offset_wh, counter.value_wh)
            ) or not all(value >= 0 for value in (counter.last_raw_wh, counter.value_wh)):
                raise ValueError(f"invalid counter for {name}")
            if counter.energy_field not in {"aenergy", "ret_aenergy"}:
                raise ValueError(f"invalid energy field for {name}")
            if max(counter.last_raw_wh, counter.value_wh) > FLOAT32_MAX:
                raise ValueError(f"counter for {name} exceeds float32 energy range")
            loaded[str(name)] = counter
        if sum(counter.value_wh for counter in loaded.values()) > FLOAT32_MAX:
            raise ValueError("total energy exceeds float32 range")
        phases = payload.get("source_phases", {})
        if not isinstance(phases, dict) or any(
            phase not in ("L1", "L2", "L3") for phase in phases.values()
        ):
            raise ValueError("invalid source_phases")
        return loaded, phases.copy()

    def update(self, name: str, raw_wh: float, energy_field: str) -> float:
        if not math.isfinite(raw_wh) or not 0 <= raw_wh <= FLOAT32_MAX:
            raise ValueError("raw energy must fit non-negative finite float32 energy")
        if energy_field not in {"aenergy", "ret_aenergy"}:
            raise ValueError("energy_field must be aenergy or ret_aenergy")

        counter = self._sources.get(name)
        previous = counter
        urgent = False
        if counter is None:
            counter = EnergyCounter(raw_wh, 0.0, raw_wh, energy_field)
            urgent = True
        elif energy_field != counter.energy_field:
            # Treat the first value from a newly selected field as its baseline.
            # Adding that complete raw counter would double-count its prior history.
            counter = EnergyCounter(
                last_raw_wh=raw_wh,
                offset_wh=counter.value_wh - raw_wh,
                value_wh=counter.value_wh,
                energy_field=energy_field,
            )
            urgent = True
        elif raw_wh + max(1.0, counter.last_raw_wh * 0.000001) < counter.last_raw_wh:
            # A reset of the same counter starts again at zero.  Its first observed raw
            # value is energy produced since the reset and must therefore be retained.
            counter = EnergyCounter(
                last_raw_wh=raw_wh,
                offset_wh=counter.value_wh,
                value_wh=counter.value_wh + raw_wh,
                energy_field=energy_field,
            )
            urgent = True
        else:
            value = max(counter.value_wh, counter.offset_wh + raw_wh)
            counter = EnergyCounter(raw_wh, counter.offset_wh, value, energy_field)

        if not math.isfinite(counter.value_wh) or counter.value_wh > FLOAT32_MAX:
            raise ValueError("virtual energy exceeds float32 range")
        self._sources[name] = counter
        if counter != previous:
            self._dirty = True
            self._urgent = self._urgent or urgent
        return counter.value_wh

    def save(self) -> None:
        payload = {
            "version": 1,
            "sources": {name: asdict(counter) for name, counter in self._sources.items()},
            "source_phases": self._source_phases.copy(),
        }
        temporary_path: Path | None = None
        backup_temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())

            if self.path.exists() and not self.recovered_from_backup:
                with (
                    self.path.open("rb") as source,
                    NamedTemporaryFile(
                        "wb",
                        dir=self.path.parent,
                        prefix=f".{self.backup_path.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as backup_handle,
                ):
                    backup_temporary_path = Path(backup_handle.name)
                    shutil.copyfileobj(source, backup_handle)
                    backup_handle.flush()
                    os.fsync(backup_handle.fileno())
                os.replace(backup_temporary_path, self.backup_path)
                backup_temporary_path = None

            os.replace(temporary_path, self.path)
            temporary_path = None
            if os.name != "nt":
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._dirty = False
            self._urgent = False
            self.recovered_from_backup = False
        except OSError as exc:
            raise StateError(f"Cannot write state file {self.path}: {exc}") from exc
        finally:
            for leftover in (temporary_path, backup_temporary_path):
                if leftover is not None:
                    with suppress(OSError):
                        leftover.unlink(missing_ok=True)
