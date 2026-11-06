"""Publication defaults remain inert; explicit private settings retain validation."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from switchboard.alert_config import heartbeat_url
from switchboard.config import Config, validate_probes, validate_remote_hosts
from switchboard.nws import validate_settings

from .net_helpers import ip


def test_unconfigured_defaults(config):
    loaded = Config.load()
    assert loaded.weather == validate_settings({})
    assert loaded.weather["enabled"] is False
    assert all(loaded.weather[key] is None for key in ("name", "zip", "lat", "lon"))
    for name, fields in (
        ("media_compress", ("python", "script", "state_dir")),
        ("maintenance", ("path",)),
    ):
        assert loaded.backends[name]["enabled"] is False
        assert all(loaded.backends[name][field] is None for field in fields)


@pytest.mark.parametrize("backend", ["media_compress", "maintenance"])
def test_enabling_path_backend_requires_explicit_paths(config, backend):
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"backends": {backend: {"enabled": True}}})
    )
    with pytest.raises(ValueError, match="explicit"):
        Config.load()


@pytest.mark.parametrize("missing", ["name", "zip", "lat", "lon"])
def test_enabling_weather_requires_every_location_field(config, missing):
    weather = {
        "enabled": True,
        "name": "Test location",
        "zip": "00000",
        "lat": 40.0,
        "lon": -100.0,
    }
    del weather[missing]
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"weather": weather}))
    with pytest.raises(ValueError, match=missing):
        Config.load()


@pytest.mark.parametrize(
    ("octets", "lan", "remote"),
    [
        ((9, 255, 255, 255), False, False),
        ((10, 0, 0, 0), True, True),
        ((10, 255, 255, 255), True, True),
        ((11, 0, 0, 0), False, False),
        ((172, 15, 255, 255), False, False),
        ((172, 16, 0, 0), True, True),
        ((172, 31, 255, 255), True, True),
        ((172, 32, 0, 0), False, False),
        ((192, 167, 255, 255), False, False),
        ((192, 168, 0, 0), True, True),
        ((192, 168, 255, 255), True, True),
        ((192, 169, 0, 0), False, False),
        ((100, 63, 255, 255), False, False),
        ((100, 64, 0, 0), False, True),
        ((100, 127, 255, 255), False, True),
        ((100, 128, 0, 0), False, False),
        ((127, 0, 0, 1), False, False),
        ((169, 254, 1, 1), False, False),
    ],
)
def test_exact_network_boundaries(octets, lan, remote):
    address = ip(*octets)
    url = f"http://{address}/ping"
    if lan:
        assert heartbeat_url(url) == url
    else:
        with pytest.raises(ValueError):
            heartbeat_url(url)
    if lan or octets[0] == 127:
        assert validate_probes([{"name": "test", "url": url}])[0]["url"] == url
    else:
        with pytest.raises(ValueError):
            validate_probes([{"name": "test", "url": url}])
    hosts = {
        "remotehost": {
            "address": address,
            "user": "serviceuser",
            "key_file": "/private/remote.key",
            "known_hosts_file": "/private/known_hosts",
        }
    }
    if remote:
        assert validate_remote_hosts(hosts) == hosts
    else:
        with pytest.raises(ValueError):
            validate_remote_hosts(hosts)


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o400, 0o700])
def test_private_deploy_env_modes_and_loading(tmp_path, mode):
    settings = tmp_path / "deploy.env"
    settings.write_text(
        "SWITCHBOARD_LABEL=com.example.custom\nSWITCHBOARD_BUNDLE_ID=com.example.helper\n"
    )
    settings.chmod(mode)
    script = tmp_path / "deploy-env.sh"
    # Exercise the production loader against an isolated file, never the user's config.
    script.write_text(
        Path("scripts/deploy-env.sh")
        .read_text()
        .replace('"$HOME/.config/switchboard/deploy.env"', shlex.quote(str(settings)))
    )
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; printf "%s %s" "$SWITCHBOARD_LABEL" "$SWITCHBOARD_BUNDLE_ID"',
            "test",
            str(script),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (mode == 0o600)
    if mode == 0o600:
        assert result.stdout == "com.example.custom com.example.helper"
    else:
        assert "Refusing deploy.env" in result.stderr


@pytest.mark.parametrize("kind", ["absent", "symlink", "directory", "foreign_owner"])
def test_private_deploy_env_file_types_and_owner(tmp_path, kind):
    settings = tmp_path / "deploy.env"
    if kind == "directory":
        settings.mkdir(mode=0o600)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("exit 99\n")
        target.chmod(0o600)
        settings.symlink_to(target)
    elif kind == "foreign_owner":
        settings.write_text("exit 99\n")
        settings.chmod(0o600)
    text = Path("scripts/deploy-env.sh").read_text()
    text = text.replace('"$HOME/.config/switchboard/deploy.env"', shlex.quote(str(settings)))
    if kind == "foreign_owner":
        # Fake stat ownership only; no chown or access to another user's files.
        text = text.replace("$(/usr/bin/stat -f '%u' \"$DEPLOY_ENV\")", str(os.getuid() + 1))
    script = tmp_path / "deploy-env.sh"
    script.write_text(text)
    result = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == (0 if kind == "absent" else 1)
