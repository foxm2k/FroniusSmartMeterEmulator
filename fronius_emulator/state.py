from __future__ import annotations

import json
import math
import os
import shutil
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


class StateError(RuntimeError):
    """Raised when persistent energy state cannot be read or written."""


@dataclass(slots=True)
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
        self._dirty = False
        self._urgent = False
        self.recovered_from_backup = False

    @property
    def values(self) -> dict[str, float]:
        return {name: counter.value_wh for name, counter in self._sources.items()}

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
                self._sources = self._load_file(candidate)
                self.recovered_from_backup = candidate == self.backup_path
                self._dirty = self.recovered_from_backup
                self._urgent = self.recovered_from_backup
                return
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                errors.append(f"{candidate}: {exc}")
        raise StateError("Cannot read persistent energy state: " + "; ".join(errors))

    def _load_file(self, path: Path) -> dict[str, EnergyCounter]:
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
            loaded[str(name)] = counter
        return loaded

    def update(self, name: str, raw_wh: float, energy_field: str) -> float:
        if not math.isfinite(raw_wh) or raw_wh < 0:
            raise ValueError("raw energy must be finite and non-negative")
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

        self._sources[name] = counter
        if counter != previous:
            self._dirty = True
            self._urgent = self._urgent or urgent
        return counter.value_wh

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sources": {name: asdict(counter) for name, counter in self._sources.items()},
        }
        temporary_path: Path | None = None
        backup_temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)

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
                    shutil.copyfileobj(source, backup_handle)
                    backup_handle.flush()
                    os.fsync(backup_handle.fileno())
                    backup_temporary_path = Path(backup_handle.name)
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
