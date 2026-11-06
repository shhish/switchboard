"""Local file configuration; secrets are paths, never configuration values."""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .alert_config import alert_settings, monitor_settings
from .auth import CLIENTS, local_url, loopback_host
from .hostcmd import remote_token, runtime_id
from .netrules import REMOTE_NETWORKS, RFC1918_NETWORKS
from .nws import DEFAULT_WEATHER
from .nws import validate_settings as validate_weather

DEFAULT_BACKENDS = {
    "health": {"url": "http://127.0.0.1:8050"},
    "media": {"url": "http://127.0.0.1:8010"},
    "plex": {
        "url": "http://127.0.0.1:32400",
        "token_file": "~/.config/switchboard/secrets/plex.token",
        "enabled": True,
    },
    "tautulli": {
        "url": "http://127.0.0.1:8181",
        "api_key_file": "~/.config/switchboard/secrets/tautulli.key",
        "enabled": True,
    },
    "maintenance": {
        "path": None,
        "enabled": False,
    },
    "overseerr": {"url": "http://127.0.0.1:5055", "enabled": False},
    # Book requests are optional and require an owner-provisioned private API key.
    "lazylibrarian": {"url": "http://127.0.0.1:5299", "enabled": False},
    "media_compress": {
        "enabled": False,
        # Deployment-specific paths must be set explicitly before enabling.
        "python": None,
        "script": None,
        "state_dir": None,
        "timeout_s": 60,
    },
    "home": {
        "url": "http://127.0.0.1:18796/api/v1",
        "control_token_file": str(
            Path.home() / ".config/switchboard/secrets/homerelay-control.token"
        ),
    },
    "frame": {"url": "http://127.0.0.1:8095"},
    "things": {"enabled": False},
    "eventkit": {
        "socket": "~/Library/Application Support/switchboard/eventkit.sock",
        "enabled": False,
    },
    "orb": {"url": "http://127.0.0.1:8445/mcp", "enabled": False},
}


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError("Backend URL must be a string")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or not loopback_host(parsed.netloc)
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Backend URLs must use HTTP and a literal loopback IP")
    return url.rstrip("/")


def validate_probes(probes):
    if not isinstance(probes, list) or len(probes) > 60:
        raise ValueError("Probes must be a list with at most 60 entries")
    networks = RFC1918_NETWORKS
    names = set()
    result = []
    for probe in probes:
        if (
            not isinstance(probe, dict)
            or set(probe)
            - {
                "name",
                "url",
                "tls_verify",
                "group",
                "alert",
                "label",
                "timeout_s",
                "failures_before_alert",
            }
            or not {"name", "url"} <= probe.keys()
        ):
            raise ValueError("Invalid probe fields")
        name = probe["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", name):
            raise ValueError("Invalid probe name")
        if name in names:
            raise ValueError("Probe names must be unique")
        names.add(name)
        tls_verify = probe.get("tls_verify", True)
        group = probe.get("group", "")
        if (
            type(tls_verify) is not bool
            or type(probe.get("alert", True)) is not bool
            or not isinstance(probe.get("label", ""), str)
            or len(probe.get("label", "")) > 60
            or not isinstance(group, str)
            or len(group) > 80
            or any(ord(c) < 32 or ord(c) > 126 for c in group)
            or "://" in group
        ):
            raise ValueError("Invalid probe TLS verification or group")
        # Slow disk-backed apps get a longer timeout and more failures before a page.
        timeout_s = probe.get("timeout_s")
        failures = probe.get("failures_before_alert")
        if timeout_s is not None and (
            type(timeout_s) not in (int, float) or not 1 <= timeout_s <= 30
        ):
            raise ValueError("Probe timeout_s must be 1-30 seconds")
        if failures is not None and (type(failures) is not int or not 1 <= failures <= 10):
            raise ValueError("Probe failures_before_alert must be 1-10")
        url = probe["url"]
        try:
            if (
                not isinstance(url, str)
                or any(c.isspace() or ord(c) < 32 for c in url)
                or any(c in url for c in ("?", "#", "\\"))
            ):
                raise ValueError
            parsed = urlsplit(url)
            address = ipaddress.ip_address(parsed.hostname)
            if (
                parsed.scheme not in ("http", "https")
                or "%" in parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or len(parsed.path) > 200
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or not (address.is_loopback or any(address in network for network in networks))
                or (not tls_verify and not address.is_loopback)
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(
                "Probe URLs require a literal loopback or RFC1918 IP; TLS on LAN"
            ) from None
        if timeout_s is not None and not address.is_loopback:
            raise ValueError("Probe timeout_s applies only to loopback probes")
        result.append(
            {
                "name": name,
                "url": url,
                "tls_verify": tls_verify,
                "group": group,
                "alert": probe.get("alert", True),
                **({"label": probe["label"]} if probe.get("label") else {}),
                **({"timeout_s": timeout_s} if timeout_s is not None else {}),
                **({"failures_before_alert": failures} if failures is not None else {}),
            }
        )
    return result


LOCAL_RUNTIMES = {"launchd", "docker", "app", "process"}


def validate_remote_hosts(hosts):
    if not isinstance(hosts, dict) or len(hosts) > 8:
        raise ValueError("Remote hosts must be a mapping with at most 8 entries")
    result = {}
    for name, settings in hosts.items():
        if not remote_token(name) or name in LOCAL_RUNTIMES | {"mac"}:
            raise ValueError("Invalid or reserved remote host name")
        if not isinstance(settings, dict) or set(settings) != {
            "address",
            "user",
            "key_file",
            "known_hosts_file",
        }:
            raise ValueError("Invalid remote host fields")
        try:
            address = ipaddress.IPv4Address(settings["address"])
            if not isinstance(settings["address"], str) or not any(
                address in network for network in REMOTE_NETWORKS
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Remote address must be a literal private IPv4 address") from None
        user = settings["user"]
        if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("Invalid remote user")
        for key in ("key_file", "known_hosts_file"):
            path = settings[key]
            if (
                not isinstance(path, str)
                or not Path(path).is_absolute()
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
            ):
                raise ValueError("Remote credential paths must be absolute")
        result[name] = dict(settings)
    return result


def log_roots():
    return (Path.home(), Path("/tmp"), Path("/Applications"), Path("/Library/Logs"))


def allowed_log_path(path, *, resolve=False):
    if not path.is_absolute() or ".." in str(path):
        return False
    if resolve:
        path = path.resolve()
    return any(path.is_relative_to(root.resolve() if resolve else root) for root in log_roots())


def validate_services(services, probes, remote_hosts=None):
    remote_hosts = validate_remote_hosts(remote_hosts or {})
    if not isinstance(services, dict) or len(services) > 60:
        raise ValueError("Services must be a mapping with at most 60 entries")
    result = {}
    for name, settings in services.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", name):
            raise ValueError("Invalid service name")
        if (
            not isinstance(settings, dict)
            or set(settings) - {"probe", "runtime", "logs", "restart"}
            or "runtime" not in settings
        ):
            raise ValueError("Invalid service fields")
        runtime = settings["runtime"]
        if (
            not isinstance(runtime, dict)
            or len(runtime) != 1
            or not set(runtime) <= LOCAL_RUNTIMES | remote_hosts.keys()
        ):
            raise ValueError("Invalid service runtime")
        kind, target = next(iter(runtime.items()))
        if kind in remote_hosts:
            if not remote_token(target):
                raise ValueError("Invalid remote service name")
        elif kind == "process":
            # An executable path prefix, for apps launchd doesn't list (e.g. NZBGet).
            if (
                not isinstance(target, str)
                or not target.startswith(("/Applications/", str(Path.home()) + "/"))
                or ".." in target
                or len(target) > 200
                or any(ord(c) < 32 for c in target)
            ):
                raise ValueError("Invalid service runtime")
        elif not runtime_id(target):
            raise ValueError("Invalid service runtime")
        if "probe" in settings and (
            not isinstance(settings["probe"], str)
            or settings["probe"] not in {p["name"] for p in probes}
        ):
            raise ValueError("Service probe must exist")
        restart = settings.get("restart", False)
        if type(restart) is not bool or (restart and kind in ("app", "process")):
            raise ValueError("Invalid service restart setting")
        logs = settings.get("logs", "remote" if kind in remote_hosts else [])
        if kind in remote_hosts:
            if logs != "remote":
                raise ValueError("Remote logs must be the literal remote")
            result[name] = {**settings, "runtime": dict(runtime), "restart": restart, "logs": logs}
            continue
        if not isinstance(logs, list) or len(logs) > 4:
            raise ValueError("Service logs must be a list with at most 4 entries")
        paths = []
        for value in logs:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ValueError("Invalid service log path")
            if value == "docker":
                if "docker" not in runtime:
                    raise ValueError("Docker logs require a docker runtime")
                paths.append(value)
                continue
            path = Path(value).expanduser()
            if not allowed_log_path(path):
                raise ValueError("Service log path must be under an allowed root without '..'")
            paths.append(str(path))
        result[name] = {**settings, "runtime": dict(runtime), "restart": restart, "logs": paths}
    return result


@dataclass
class Config:
    config_dir: Path
    state_dir: Path
    log_dir: Path
    port: int = 18800
    approval_ttl_s: int = 1800
    weather: dict = field(default_factory=lambda: validate_weather(DEFAULT_WEATHER))
    backends: dict = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_BACKENDS.items()}
    )
    upstreams: dict = field(
        default_factory=lambda: {"orb": {"url": "http://127.0.0.1:8445/mcp", "client_tokens": {}}}
    )
    probes: list = field(default_factory=list)
    services: dict = field(default_factory=dict)
    remote_hosts: dict = field(default_factory=dict)

    monitor: dict = field(default_factory=dict)
    alerts: dict = field(default_factory=dict)

    def __post_init__(self):
        self.monitor = monitor_settings(self.monitor)
        self.alerts = alert_settings(self.alerts)
        self.probes = validate_probes(self.probes)
        self.remote_hosts = validate_remote_hosts(self.remote_hosts)
        self.services = validate_services(self.services, self.probes, self.remote_hosts)

    @property
    def token_dir(self):
        return self.config_dir / "tokens"

    @classmethod
    def load(cls):
        home = Path.home()
        directory = Path(os.environ.get("SWITCHBOARD_CONFIG_DIR", home / ".config/switchboard"))
        path = directory / "config.yaml"
        try:
            data = yaml.safe_load(path.read_text()) if path.exists() else {}
        except yaml.YAMLError:
            raise ValueError("Invalid config YAML") from None
        data = {} if data is None else data
        if not isinstance(data, dict) or set(data) - {
            "port",
            "approval_ttl_s",
            "backends",
            "upstreams",
            "probes",
            "services",
            "remote_hosts",
            "weather",
            "monitor",
            "alerts",
        }:
            raise ValueError("Invalid config fields")
        result = cls(
            directory,
            Path(
                os.environ.get(
                    "SWITCHBOARD_STATE_DIR", home / "Library/Application Support/switchboard"
                )
            ),
            Path(os.environ.get("SWITCHBOARD_LOG_DIR", home / "Library/Logs/switchboard")),
        )
        for key in ("port", "approval_ttl_s"):
            if key in data:
                setattr(result, key, data[key])
        result.monitor = monitor_settings(data.get("monitor", {}))
        result.alerts = alert_settings(data.get("alerts", {}))
        result.weather = validate_weather(data.get("weather", {}))
        if type(result.port) is not int or not 1 <= result.port <= 65535:
            raise ValueError("Invalid port")
        if type(result.approval_ttl_s) is not int or not 1 <= result.approval_ttl_s <= 86400:
            raise ValueError("Invalid approval TTL")
        result.probes = validate_probes(data.get("probes", []))
        result.remote_hosts = validate_remote_hosts(data.get("remote_hosts", {}))
        result.services = validate_services(
            data.get("services", {}), result.probes, result.remote_hosts
        )
        backends = data.get("backends", {})
        if not isinstance(backends, dict) or set(backends) - DEFAULT_BACKENDS.keys():
            raise ValueError("Invalid backend names")
        for name, settings in backends.items():
            fields = (
                {"enabled", "python", "script", "state_dir", "timeout_s"}
                if name == "media_compress"
                else {"enabled"}
                if name == "things"
                else {"socket" if name == "eventkit" else "path", "enabled"}
                if name in ("eventkit", "maintenance")
                else {
                    "url",
                    "enabled",
                    "token_file",
                    "control_token_file",
                    "api_key_file",
                    "api_key_json_path",
                }
            )
            if not isinstance(settings, dict) or set(settings) - fields:
                raise ValueError("Invalid backend fields; use secret FILE paths")
            if "enabled" in settings and type(settings["enabled"]) is not bool:
                raise ValueError("enabled must be boolean")
            if "control_token_file" in settings and name != "home":
                raise ValueError("control_token_file is only valid for home")
            for key in ("token_file", "control_token_file", "api_key_file", "api_key_json_path"):
                if key in settings and (
                    not isinstance(settings[key], str) or not settings[key].strip()
                ):
                    raise ValueError("Secret references must be nonempty strings")
            result.backends[name].update(settings)
        for name, settings in result.backends.items():
            if name == "media_compress":
                from .media_compress import validate_settings

                validate_settings(settings)
                continue
            if name == "maintenance":
                path = settings["path"]
                if path is None and not settings.get("enabled", True):
                    continue
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("Maintenance requires an explicit status path when enabled")
                path = Path(path).expanduser()
                if not path.is_absolute():
                    raise ValueError("Maintenance status path must be absolute")
                settings["path"] = str(path)
                continue
            if name == "things":
                continue
            if name == "eventkit":
                path = settings["socket"]
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("Invalid EventKit socket path")
                path = Path(path).expanduser()
                if not path.is_absolute() or len(os.fsencode(path)) >= 104:
                    raise ValueError("EventKit socket must be an absolute Unix socket path")
                settings["socket"] = str(path)
                continue
            settings["url"] = validate_url(settings["url"])
            for key in ("token_file", "control_token_file", "api_key_file"):
                if settings.get(key):
                    settings[key] = str(Path(settings[key]).expanduser())
        upstreams = data.get("upstreams", {})
        if not isinstance(upstreams, dict) or set(upstreams) - {"orb"}:
            raise ValueError("Invalid upstream names")
        orb = upstreams.get("orb", {})
        if not isinstance(orb, dict) or set(orb) - {"url", "client_tokens"}:
            raise ValueError("Invalid Orb upstream fields; use client token FILE paths")
        result.upstreams["orb"].update(orb)
        settings = result.upstreams["orb"]
        if not isinstance(settings["url"], str):
            raise ValueError("Orb URL must be a string")
        settings["url"] = local_url(settings["url"])
        tokens = settings["client_tokens"]
        if (
            not isinstance(tokens, dict)
            or set(tokens) - set(CLIENTS)
            or any(not isinstance(v, str) or not v.strip() for v in tokens.values())
        ):
            raise ValueError("Invalid Orb client token FILE paths")
        settings["client_tokens"] = {k: str(Path(v).expanduser()) for k, v in tokens.items()}
        return result
