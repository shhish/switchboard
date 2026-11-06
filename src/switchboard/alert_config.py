"""Closed monitoring settings and private delivery references; no inline credentials."""

import math
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from .netrules import RFC1918_NETWORKS

DEFAULT_MONITOR = {
    "enabled": False,
    "interval_s": 60,
    "failures_before_alert": 2,
    "reminder_hours": 6,
    "recovery_alerts": True,
}


def monitor_settings(value):
    if not isinstance(value, dict) or set(value) - (
        DEFAULT_MONITOR.keys() | {"heartbeat_url_file"}
    ):
        raise ValueError("Invalid monitor fields")
    result = {**DEFAULT_MONITOR, **value}
    for key in ("enabled", "recovery_alerts"):
        if type(result[key]) is not bool:
            raise ValueError("Monitor flags must be boolean")
    for key, maximum in (("interval_s", 86400), ("failures_before_alert", 1000)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError("Invalid monitor interval or threshold")
    hours = result["reminder_hours"]
    if type(hours) not in (int, float) or not 0 < hours <= 8760 or not math.isfinite(hours):
        raise ValueError("Invalid reminder interval")
    if "heartbeat_url_file" in result:
        path = result["heartbeat_url_file"]
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
        ):
            raise ValueError("Heartbeat secret path must be absolute")
    return result


def heartbeat_url(value):
    # Apply the probe URL contract, then narrow it to native net.probe's RFC1918 IPv4.
    import ipaddress

    from .config import validate_probes

    try:
        validate_probes([{"name": "heartbeat", "url": value}])
        parsed = urlsplit(value)
        if len(quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")) > 200:
            raise ValueError
        address = ipaddress.IPv4Address(parsed.hostname)
        if not any(address in network for network in RFC1918_NETWORKS):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Invalid heartbeat target") from None
    return value


def alert_settings(value):
    if not isinstance(value, dict) or set(value) - {
        "ntfy",
        "email",
        "allowed_hosts",
        "allowed_smtp_hosts",
    }:
        raise ValueError("Invalid alert fields; use secret FILE paths")
    result = {"allowed_hosts": ["ntfy.sh"], "allowed_smtp_hosts": ["smtp.gmail.com"], **value}
    for key in ("allowed_hosts", "allowed_smtp_hosts"):
        hosts = result[key]
        if not isinstance(hosts, list) or any(
            not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]{1,253}", host)
            for host in hosts
        ):
            raise ValueError("Invalid alert host allowlist")
    for channel, fields in {
        "ntfy": {"url_file"},
        "email": {
            "host",
            "port",
            "starttls",
            "username_file",
            "password_file",
            "from_file",
            "to_file",
        },
    }.items():
        if channel not in result:
            continue
        settings = result[channel]
        if not isinstance(settings, dict) or set(settings) != fields:
            raise ValueError("Invalid alert channel fields; use secret FILE paths")
        settings = dict(settings)
        for key in fields:
            if key.endswith("_file"):
                path = settings[key]
                if (
                    not isinstance(path, str)
                    or not path.strip()
                    or not Path(path).expanduser().is_absolute()
                ):
                    raise ValueError("Alert secret paths must be absolute")
                settings[key] = str(Path(path).expanduser())
        if channel == "email" and (
            settings["host"] not in result["allowed_smtp_hosts"]
            or type(settings["port"]) is not int
            or not 1 <= settings["port"] <= 65535
            or settings["starttls"] is not True
        ):
            raise ValueError("SMTP requires an allowlisted host and STARTTLS")
        result[channel] = settings
    return result


def ntfy_url(value, settings):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in settings["allowed_hosts"]
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and "?" not in value
            and "#" not in value
            and "\\" not in value
            and not any(c.isspace() or ord(c) < 32 for c in value)
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and len(parsed.path) > 1
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("ntfy requires an allowlisted HTTPS topic URL")
    return value
