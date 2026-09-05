"""Read power-production measurements from Shelly Gen2+ smart plugs."""

from __future__ import annotations

import asyncio
import math
import os
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx
import requests
from requests.auth import HTTPDigestAuth

_POWER_DIRECTIONS = frozenset({"auto", "positive", "negative", "absolute"})
_ENERGY_FIELDS = frozenset({"auto", "aenergy", "ret_aenergy"})
_PHASES = frozenset({"L1", "L2", "L3"})


class ShellyError(Exception):
    """Base class for errors raised while reading a Shelly source."""


class ShellyConfigurationError(ShellyError, ValueError):
    """The Shelly source configuration is invalid."""


class ShellyConnectionError(ShellyError):
    """The Shelly could not be reached or returned an HTTP error."""


class ShellyPayloadError(ShellyError, ValueError):
    """The Shelly response is not a usable Switch.GetStatus payload."""


def _configuration_number(name: str, value: Any, *, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ShellyConfigurationError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ShellyConfigurationError(f"{name} must be a finite number")
    if number < 0 or (not allow_zero and number == 0):
        comparison = "non-negative" if allow_zero else "greater than zero"
        raise ShellyConfigurationError(f"{name} must be {comparison}")
    return number


def _normalise_base_url(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise ShellyConfigurationError("host must not be empty")

    candidate = host.strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    try:
        parts: SplitResult = urlsplit(candidate)
        # Accessing .port also validates malformed/non-numeric port values.
        _ = parts.port
    except ValueError as exc:
        raise ShellyConfigurationError(f"invalid Shelly host {host!r}: {exc}") from exc

    if parts.scheme not in {"http", "https"}:
        raise ShellyConfigurationError("Shelly host URL must use http or https")
    if not parts.hostname:
        raise ShellyConfigurationError(f"invalid Shelly host {host!r}")
    if parts.username is not None or parts.password is not None:
        raise ShellyConfigurationError(
            "credentials must be configured separately, not embedded in host"
        )
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ShellyConfigurationError(
            "Shelly host must be a bare host or base URL without path, query, or fragment"
        )

    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


@dataclass(frozen=True, slots=True)
class ShellySourceConfig:
    """Connection and interpretation settings for one Shelly smart plug."""

    name: str
    host: str
    phase: str
    username: str | None = None
    password: str | None = None
    power_direction: str = "auto"
    energy_field: str = "auto"
    min_power_w: float = 3.0
    connect_timeout: float = 1.0
    read_timeout: float = 2.0
    total_timeout: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ShellyConfigurationError("name must not be empty")
        if self.phase not in _PHASES:
            raise ShellyConfigurationError("phase must be one of L1, L2, or L3")
        if self.power_direction not in _POWER_DIRECTIONS:
            choices = ", ".join(sorted(_POWER_DIRECTIONS))
            raise ShellyConfigurationError(f"power_direction must be one of {choices}")
        if self.energy_field not in _ENERGY_FIELDS:
            choices = ", ".join(sorted(_ENERGY_FIELDS))
            raise ShellyConfigurationError(f"energy_field must be one of {choices}")
        if (self.username is None) != (self.password is None):
            raise ShellyConfigurationError(
                "username and password must either both be set or both be omitted"
            )
        if self.username is not None and not self.username:
            raise ShellyConfigurationError("username must not be empty")
        if self.password is not None and not self.password:
            raise ShellyConfigurationError("password must not be empty")

        _normalise_base_url(self.host)
        _configuration_number("min_power_w", self.min_power_w, allow_zero=True)
        _configuration_number("connect_timeout", self.connect_timeout, allow_zero=False)
        _configuration_number("read_timeout", self.read_timeout, allow_zero=False)
        if self.total_timeout is not None:
            _configuration_number("total_timeout", self.total_timeout, allow_zero=False)


@dataclass(frozen=True, slots=True)
class ShellyReading:
    """Normalised production reading from one Shelly smart plug."""

    name: str
    phase: str
    power_w: float
    voltage_v: float
    current_a: float
    frequency_hz: float | None
    apparent_power_va: float
    power_factor: float | None
    raw_energy_wh: float
    energy_field: str
    timestamp: float
    monotonic_timestamp: float | None = field(default=None, compare=False)
    auto_uses_negative: bool | None = field(default=None, compare=False, repr=False)


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class _HttpTransport(Protocol):
    def get(self, url: str, **kwargs: Any) -> _Response: ...


class ShellyClient:
    """HTTP client for the Gen2+ ``Switch.GetStatus`` RPC endpoint."""

    def __init__(
        self,
        config: ShellySourceConfig,
        session: _HttpTransport | None = None,
    ) -> None:
        if not isinstance(config, ShellySourceConfig):
            raise ShellyConfigurationError("config must be a ShellySourceConfig")

        self.config = config
        base_url = _normalise_base_url(config.host)
        self.url = f"{base_url}/rpc/Switch.GetStatus?id=0"
        self._http: _HttpTransport = session if session is not None else requests
        self._auth = (
            HTTPDigestAuth(config.username, config.password)
            if config.username is not None and config.password is not None
            else None
        )
        self._auto_uses_negative: bool | None = None
        self._async_http: httpx.AsyncClient | None = None

    def fetch(self, now: float | None = None) -> ShellyReading:
        """Fetch and validate one instantaneous and cumulative reading."""

        timestamp = None if now is None else self._timestamp(now)

        try:
            response = self._http.get(
                self.url,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                auth=self._auth,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ShellyConnectionError(
                f"{self.config.name}: failed to fetch {self.url}: {exc}"
            ) from exc

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise ShellyPayloadError(f"{self.config.name}: Shelly returned invalid JSON") from exc

        reading = self._parse_payload(payload, timestamp)
        self.accept(reading)
        return reading

    async def fetch_async(self, now: float | None = None, *, commit: bool = True) -> ShellyReading:
        """Read with a cancellable total deadline; the poller commits after validation."""
        timestamp = None if now is None else self._timestamp(now)
        if self._async_http is None:
            # Requests honors these CA variables ahead of its bundled certificate store.
            ca_bundle = (
                os.environ.get("REQUESTS_CA_BUNDLE")
                or os.environ.get("CURL_CA_BUNDLE")
                or requests.certs.where()
            )
            try:
                verify = ssl.create_default_context(
                    capath=ca_bundle if os.path.isdir(ca_bundle) else None,
                    cafile=ca_bundle if not os.path.isdir(ca_bundle) else None,
                )
            except OSError as exc:
                raise ShellyConnectionError(
                    f"{self.config.name}: cannot load HTTP certificate authorities: {exc}"
                ) from exc
            auth = (
                httpx.DigestAuth(self.config.username, self.config.password)
                if self.config.username is not None
                else None
            )
            if auth is None and (credentials := requests.utils.get_netrc_auth(self.url)):
                auth = httpx.BasicAuth(*credentials)
            self._async_http = httpx.AsyncClient(
                auth=auth,
                verify=verify,
                follow_redirects=True,
                max_redirects=30,
                timeout=httpx.Timeout(
                    connect=self.config.connect_timeout,
                    read=self.config.read_timeout,
                    write=self.config.connect_timeout,
                    pool=self.config.connect_timeout,
                ),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            )
        total_timeout = self.config.total_timeout
        if total_timeout is None:
            total_timeout = 2 * (self.config.connect_timeout + self.config.read_timeout)
        try:
            async with asyncio.timeout(total_timeout):
                # requests.get uses a new Session for each poll: don't carry cookies over.
                self._async_http.cookies.clear()
                response = await self._async_http.get(self.url)
                if response.status_code >= 400:
                    response.raise_for_status()
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ShellyConnectionError(
                f"{self.config.name}: failed to fetch {self.url}: {exc}"
            ) from exc
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise ShellyPayloadError(f"{self.config.name}: Shelly returned invalid JSON") from exc
        reading = self._parse_payload(payload, timestamp)
        if commit:
            self.accept(reading)
        return reading

    async def aclose(self) -> None:
        if self._async_http is not None:
            await self._async_http.aclose()
            self._async_http = None

    def accept(self, reading: ShellyReading) -> None:
        """Accept capability detection only once the complete sample is usable."""
        if self.config.power_direction == "auto" and self._auto_uses_negative is None:
            self._auto_uses_negative = reading.auto_uses_negative

    def _parse_payload(self, payload: Any, timestamp: float | None) -> ShellyReading:
        if not isinstance(payload, Mapping):
            raise ShellyPayloadError(
                f"{self.config.name}: Switch.GetStatus response must be an object"
            )

        raw_power = self._number(payload, "apower")
        voltage = self._number(payload, "voltage", non_negative=True)
        measured_current = self._number(payload, "current", non_negative=True)
        frequency = self._optional_number(payload, "freq", non_negative=True)
        if frequency is not None and frequency == 0:
            raise ShellyPayloadError(f"{self.config.name}: freq must be greater than zero")
        payload_pf = self._optional_number(payload, "pf")

        aenergy = self._energy(payload, "aenergy", required=False)
        returned_energy = self._energy(payload, "ret_aenergy", required=False)

        # The presence of ret_aenergy is the stable capability signal.  Do not
        # switch direction/energy field at night merely because raw power turns
        # positive while a bidirectional plug consumes standby energy.
        auto_uses_negative = self._auto_uses_negative
        if self.config.power_direction == "auto" and auto_uses_negative is None:
            auto_uses_negative = returned_energy is not None
        use_negative_auto = self.config.power_direction == "auto" and bool(auto_uses_negative)
        power = self._normalise_power(raw_power, use_negative_auto)
        energy, selected_energy_field = self._select_energy(
            aenergy=aenergy,
            returned_energy=returned_energy,
            use_negative_auto=use_negative_auto,
        )

        if power <= 0 or power < self.config.min_power_w:
            power = 0.0
            current = 0.0
            apparent_power = 0.0
            power_factor = None
        else:
            current = measured_current
            apparent_power = voltage * current
            if not math.isfinite(apparent_power):
                raise ShellyPayloadError(
                    f"{self.config.name}: calculated apparent power is not finite"
                )
            if payload_pf is not None:
                power_factor = self._clip_power_factor(payload_pf)
            elif apparent_power > 0:
                power_factor = self._clip_power_factor(power / apparent_power)
            else:
                power_factor = None

        return ShellyReading(
            name=self.config.name,
            phase=self.config.phase,
            power_w=power,
            voltage_v=voltage,
            current_a=current,
            frequency_hz=frequency,
            apparent_power_va=apparent_power,
            power_factor=power_factor,
            raw_energy_wh=energy,
            energy_field=selected_energy_field,
            timestamp=time.time() if timestamp is None else timestamp,
            monotonic_timestamp=time.monotonic(),
            auto_uses_negative=auto_uses_negative,
        )

    def _normalise_power(self, raw_power: float, use_negative_auto: bool) -> float:
        direction = self.config.power_direction
        if direction == "auto":
            direction = "negative" if use_negative_auto else "positive"

        if direction == "positive":
            return max(raw_power, 0.0)
        if direction == "negative":
            return max(-raw_power, 0.0)
        return abs(raw_power)

    def _select_energy(
        self,
        *,
        aenergy: float | None,
        returned_energy: float | None,
        use_negative_auto: bool,
    ) -> tuple[float, str]:
        field = self.config.energy_field
        if field == "auto":
            use_returned = use_negative_auto or (
                self.config.power_direction == "negative" and returned_energy is not None
            )
            field = "ret_aenergy" if use_returned else "aenergy"

        if field == "ret_aenergy":
            if returned_energy is None:
                raise ShellyPayloadError(
                    f"{self.config.name}: ret_aenergy.total is required but missing"
                )
            return returned_energy, field

        if aenergy is None:
            raise ShellyPayloadError(f"{self.config.name}: aenergy.total is required but missing")
        return aenergy, field

    def _number(
        self,
        payload: Mapping[str, Any],
        field: str,
        *,
        non_negative: bool = False,
    ) -> float:
        if field not in payload:
            raise ShellyPayloadError(f"{self.config.name}: missing field {field}")
        return self._validated_number(payload[field], field, non_negative=non_negative)

    def _optional_number(
        self,
        payload: Mapping[str, Any],
        field: str,
        *,
        non_negative: bool = False,
    ) -> float | None:
        value = payload.get(field)
        if value is None:
            return None
        return self._validated_number(value, field, non_negative=non_negative)

    def _energy(
        self,
        payload: Mapping[str, Any],
        field: str,
        *,
        required: bool,
    ) -> float | None:
        value = payload.get(field)
        if value is None:
            if required:
                raise ShellyPayloadError(f"{self.config.name}: missing field {field}")
            return None
        if not isinstance(value, Mapping):
            raise ShellyPayloadError(f"{self.config.name}: {field} must be an object")
        if "total" not in value:
            raise ShellyPayloadError(f"{self.config.name}: missing field {field}.total")
        return self._validated_number(value["total"], f"{field}.total", non_negative=True)

    def _validated_number(
        self,
        value: Any,
        field: str,
        *,
        non_negative: bool,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ShellyPayloadError(f"{self.config.name}: {field} must be a finite number")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ShellyPayloadError(f"{self.config.name}: {field} must be finite") from exc
        if not math.isfinite(number):
            raise ShellyPayloadError(f"{self.config.name}: {field} must be a finite number")
        if non_negative and number < 0:
            raise ShellyPayloadError(f"{self.config.name}: {field} must be non-negative")
        return number

    def _timestamp(self, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ShellyPayloadError("timestamp must be a finite number")
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ShellyPayloadError("timestamp must be a finite number")
        return timestamp

    @staticmethod
    def _clip_power_factor(value: float) -> float:
        return min(1.0, max(-1.0, value))


__all__ = [
    "ShellyClient",
    "ShellyConfigurationError",
    "ShellyConnectionError",
    "ShellyError",
    "ShellyPayloadError",
    "ShellyReading",
    "ShellySourceConfig",
]
