from __future__ import annotations

import math
from typing import Any

import pytest
import requests
from requests.auth import HTTPDigestAuth

from fronius_emulator.shelly import (
    ShellyClient,
    ShellyConfigurationError,
    ShellyConnectionError,
    ShellyPayloadError,
    ShellySourceConfig,
)


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        http_error: requests.RequestException | None = None,
        json_error: ValueError | None = None,
    ) -> None:
        self.payload = payload
        self.http_error = http_error
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.http_error is not None:
            raise self.http_error

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.response


class SequenceSession:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.responses = [FakeResponse(payload) for payload in payloads]

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.responses.pop(0)


def config(**changes: Any) -> ShellySourceConfig:
    values = {
        "name": "roof-west",
        "host": "192.168.123.100",
        "phase": "L1",
    }
    values.update(changes)
    return ShellySourceConfig(**values)


def fetch(payload: Any, **config_changes: Any):
    session = FakeSession(FakeResponse(payload))
    client = ShellyClient(config(**config_changes), session=session)
    return client.fetch(now=1234.5), client, session


def test_gen2_positive_live_payload_without_optional_fields() -> None:
    reading, client, session = fetch(
        {
            "id": 0,
            "output": True,
            "apower": 410.2,
            "voltage": 230.0,
            "current": 1.8,
            "aenergy": {"total": 1234.5},
        }
    )

    assert client.url == "http://192.168.123.100/rpc/Switch.GetStatus?id=0"
    assert session.calls == [
        (
            client.url,
            {"timeout": (1.0, 2.0), "auth": None},
        )
    ]
    assert reading.name == "roof-west"
    assert reading.phase == "L1"
    assert reading.power_w == 410.2
    assert reading.voltage_v == 230.0
    assert reading.current_a == 1.8
    assert reading.frequency_hz is None
    assert reading.apparent_power_va == 414.0
    assert reading.power_factor == pytest.approx(410.2 / 414.0)
    assert reading.raw_energy_wh == 1234.5
    assert reading.energy_field == "aenergy"
    assert reading.timestamp == 1234.5


def test_gen3_negative_generation_uses_returned_energy() -> None:
    reading, _, _ = fetch(
        {
            "id": 0,
            "apower": -800.0,
            "voltage": 230.0,
            "current": 3.6,
            "freq": 50.02,
            "pf": -1.4,
            "aenergy": {"total": 5100.0},
            "ret_aenergy": {"total": 4000.0},
        },
        phase="L2",
    )

    assert reading.phase == "L2"
    assert reading.power_w == 800.0
    assert reading.current_a == 3.6
    assert reading.frequency_hz == 50.02
    assert reading.apparent_power_va == 828.0
    assert reading.power_factor == -1.0
    assert reading.raw_energy_wh == 4000.0
    assert reading.energy_field == "ret_aenergy"


def test_auto_does_not_take_absolute_value_for_positive_fallback() -> None:
    reading, _, _ = fetch(
        {
            "apower": -50.0,
            "voltage": 230.0,
            "current": 0.22,
            "aenergy": {"total": 100.0},
        }
    )

    assert reading.power_w == 0.0
    assert reading.current_a == 0.0
    assert reading.apparent_power_va == 0.0
    assert reading.power_factor is None
    assert reading.energy_field == "aenergy"


def test_explicit_positive_direction_does_not_auto_select_returned_energy() -> None:
    reading, _, _ = fetch(
        {
            "apower": -50.0,
            "voltage": 230.0,
            "current": 0.22,
            "aenergy": {"total": 100.0},
            "ret_aenergy": {"total": 80.0},
        },
        power_direction="positive",
    )

    assert reading.power_w == 0.0
    assert reading.raw_energy_wh == 100.0
    assert reading.energy_field == "aenergy"


def test_night_value_below_threshold_zeroes_instantaneous_flow() -> None:
    reading, _, _ = fetch(
        {
            "apower": -2.9,
            "voltage": 231.0,
            "current": 0.02,
            "freq": 49.98,
            "pf": -0.63,
            "aenergy": {"total": 500.0},
            "ret_aenergy": {"total": 450.0},
        }
    )

    assert reading.power_w == 0.0
    assert reading.voltage_v == 231.0
    assert reading.current_a == 0.0
    assert reading.apparent_power_va == 0.0
    assert reading.power_factor is None
    assert reading.frequency_hz == 49.98
    assert reading.raw_energy_wh == 450.0


def test_auto_keeps_returned_counter_and_negative_direction_at_night() -> None:
    reading, _, _ = fetch(
        {
            "apower": 1.5,
            "voltage": 231.0,
            "current": 0.01,
            "aenergy": {"total": 500.0},
            "ret_aenergy": {"total": 450.0},
        }
    )

    assert reading.power_w == 0.0
    assert reading.raw_energy_wh == 450.0
    assert reading.energy_field == "ret_aenergy"


def test_auto_direction_is_pinned_if_returned_field_temporarily_disappears() -> None:
    common = {
        "apower": -100.0,
        "voltage": 230.0,
        "current": 0.5,
        "aenergy": {"total": 500.0},
    }
    client = ShellyClient(
        config(),
        session=SequenceSession(
            [
                {**common, "ret_aenergy": {"total": 450.0}},
                common,
            ]
        ),
    )

    assert client.fetch().energy_field == "ret_aenergy"
    with pytest.raises(ShellyPayloadError, match=r"ret_aenergy\.total.*missing"):
        client.fetch()


@pytest.mark.parametrize(
    ("direction", "raw_power", "expected"),
    [
        ("positive", 120.0, 120.0),
        ("positive", -120.0, 0.0),
        ("negative", -120.0, 120.0),
        ("negative", 120.0, 0.0),
        ("absolute", -120.0, 120.0),
    ],
)
def test_explicit_power_directions(direction: str, raw_power: float, expected: float) -> None:
    reading, _, _ = fetch(
        {
            "apower": raw_power,
            "voltage": 230.0,
            "current": 0.6,
            "aenergy": {"total": 10.0},
        },
        power_direction=direction,
    )

    assert reading.power_w == expected


def test_explicit_returned_energy_field_requires_counter() -> None:
    with pytest.raises(ShellyPayloadError, match=r"ret_aenergy\.total.*missing"):
        fetch(
            {
                "apower": 100.0,
                "voltage": 230.0,
                "current": 0.5,
                "aenergy": {"total": 10.0},
            },
            energy_field="ret_aenergy",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("voltage", -1.0),
        ("voltage", math.nan),
        ("current", -0.1),
        ("current", math.inf),
    ],
)
def test_rejects_invalid_voltage_and_current(field: str, value: float) -> None:
    payload = {
        "apower": 100.0,
        "voltage": 230.0,
        "current": 0.5,
        "aenergy": {"total": 10.0},
    }
    payload[field] = value

    with pytest.raises(ShellyPayloadError, match=field):
        fetch(payload)


@pytest.mark.parametrize("value", [-1.0, math.nan, math.inf, "12"])
def test_rejects_invalid_energy(value: Any) -> None:
    with pytest.raises(ShellyPayloadError, match="aenergy.total"):
        fetch(
            {
                "apower": 100.0,
                "voltage": 230.0,
                "current": 0.5,
                "aenergy": {"total": value},
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"apower": True},
        {
            "apower": 100.0,
            "voltage": 230.0,
            "current": 0.5,
            "aenergy": 10.0,
        },
    ],
)
def test_rejects_malformed_payloads(payload: Any) -> None:
    with pytest.raises(ShellyPayloadError):
        fetch(payload)


def test_invalid_json_and_http_error_have_distinct_exceptions() -> None:
    bad_json = FakeSession(FakeResponse(json_error=ValueError("broken")))
    with pytest.raises(ShellyPayloadError, match="invalid JSON"):
        ShellyClient(config(), session=bad_json).fetch()

    http_error = FakeSession(FakeResponse(http_error=requests.HTTPError("503 Service Unavailable")))
    with pytest.raises(ShellyConnectionError, match="failed to fetch"):
        ShellyClient(config(), session=http_error).fetch()


def test_zero_frequency_is_rejected_instead_of_crashing_register_build_later() -> None:
    with pytest.raises(ShellyPayloadError, match="freq must be greater than zero"):
        fetch(
            {
                "apower": 100.0,
                "voltage": 230.0,
                "current": 0.5,
                "freq": 0.0,
                "aenergy": {"total": 10.0},
            }
        )


def test_https_base_url_and_digest_auth() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "apower": 100.0,
                "voltage": 230.0,
                "current": 0.5,
                "aenergy": {"total": 10.0},
            }
        )
    )
    client = ShellyClient(
        config(
            host="https://shelly.example:8443/",
            username="admin",
            password="secret",
        ),
        session=session,
    )

    client.fetch()

    url, kwargs = session.calls[0]
    assert url == "https://shelly.example:8443/rpc/Switch.GetStatus?id=0"
    assert isinstance(kwargs["auth"], HTTPDigestAuth)
    assert kwargs["auth"].username == "admin"
    assert kwargs["auth"].password == "secret"


@pytest.mark.parametrize(
    "host",
    [
        "ftp://192.168.1.2",
        "http://192.168.1.2/custom/path",
        "http://192.168.1.2?x=1",
        "http://admin:secret@192.168.1.2",
        "",
    ],
)
def test_rejects_non_base_urls(host: str) -> None:
    with pytest.raises(ShellyConfigurationError):
        config(host=host)


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "A"},
        {"power_direction": "guess"},
        {"energy_field": "total"},
        {"min_power_w": -1.0},
        {"connect_timeout": 0.0},
        {"read_timeout": math.inf},
        {"username": "admin"},
    ],
)
def test_rejects_invalid_configuration(changes: dict[str, Any]) -> None:
    with pytest.raises(ShellyConfigurationError):
        config(**changes)
