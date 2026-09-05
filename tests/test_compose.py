import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "host,expected",
    [
        (None, "192.168.123.102"),
        ("", ""),
        ("example.test", "example.test"),
    ],
)
def test_compose_preserves_unset_empty_and_explicit_hosts(tmp_path, host, expected):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose required; executed by Linux CI")
    env = os.environ.copy()
    env.pop("SHELLY_1_HOST", None)
    env.pop("SHELLY_2_HOST", None)
    if host is not None:
        env["SHELLY_2_HOST"] = host
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(empty_env),
            "-f",
            str(Path(__file__).resolve().parents[1] / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    settings = json.loads(result.stdout)["services"]["emulator"]["environment"]
    assert settings["SHELLY_2_HOST"] == expected
    assert settings["SHELLY_1_HOST"] == "192.168.123.100"
