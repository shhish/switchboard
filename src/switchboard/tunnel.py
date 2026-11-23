"""Cloudflared ingress probes and approved, comment-preserving route edits."""

import asyncio
import ipaddress
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import yaml

from . import hostcmd
from .errors import CapabilityError

MAX_ROUTES = 100
CONFIG_LIMIT = 256 * 1024


def settings(value):
    if not isinstance(value, dict) or set(value) - {"config_path", "launchd_label", "zone"}:
        raise ValueError("Invalid tunnel settings")
    if not value:
        return {}
    if set(value) != {"config_path", "launchd_label", "zone"}:
        raise ValueError("Tunnel requires config_path, launchd_label and zone")
    path = (
        str(Path(value["config_path"]).expanduser())
        if isinstance(value["config_path"], str)
        else ""
    )
    if (
        not hostcmd.tunnel_config_path(path)
        or not hostcmd.runtime_id(value["launchd_label"])
        or hostcmd.switchboard_label(value["launchd_label"])
        or not hostcmd.tunnel_hostname(value["zone"])
    ):
        raise ValueError("Invalid tunnel path, launchd label or zone")
    return {**value, "config_path": path}


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(k, deep=deep) for k, _ in node.value]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate YAML key")
        return super().construct_mapping(node, deep=deep)


def parse(text):
    try:
        if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(text)):
            raise ValueError
        data = yaml.load(text, Loader=UniqueLoader)
        if not isinstance(data, dict) or not hostcmd.tunnel_id(data.get("tunnel")):
            raise ValueError
        rules = data["ingress"]
        if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_ROUTES + 1:
            raise ValueError
        if any(not isinstance(r, dict) or not isinstance(r.get("service"), str) for r in rules):
            raise ValueError
        if "hostname" in rules[-1] or any("hostname" not in r for r in rules[:-1]):
            raise ValueError
        if any(not isinstance(r["hostname"], str) or len(r["hostname"]) > 253 for r in rules[:-1]):
            raise ValueError
        credentials = data.get("credentials-file")
        if credentials is not None and (
            not isinstance(credentials, str) or len(credentials) > 1024
        ):
            raise ValueError
        return data
    except (yaml.YAMLError, ValueError, KeyError, TypeError, RecursionError):
        raise CapabilityError(
            "tunnel_config_invalid", "Unsupported or invalid tunnel YAML"
        ) from None


def edit(text, hostname, port=None):
    """Use YAML source marks to edit only one block rule, retaining all other text."""
    data = parse(text)
    tree = yaml.compose(text)
    ingress = next(value for key, value in tree.value if key.value == "ingress")
    lines = text.splitlines(keepends=True)
    if ingress.flow_style or any(node.flow_style for node in ingress.value):
        raise CapabilityError("tunnel_config_invalid", "Route editing requires block-style ingress")
    starts = [node.start_mark.line for node in ingress.value]
    if any(not re.fullmatch(r"\s*-\s+\S.*\r?\n?", lines[i]) for i in starts):
        raise CapabilityError(
            "tunnel_config_invalid", "Route editing requires one block rule per line"
        )
    if port is not None:
        start = starts[-1]
        indent = re.match(r"\s*", lines[start]).group().replace("\n", "")
        newline = "\r\n" if "\r\n" in text else "\n"
        lines[start:start] = [
            f"{indent}- hostname: {hostname}{newline}",
            f"{indent}  service: http://127.0.0.1:{port}{newline}",
        ]
        expected = [
            *data["ingress"][:-1],
            {"hostname": hostname, "service": f"http://127.0.0.1:{port}"},
            data["ingress"][-1],
        ]
    else:
        indices = [i for i, rule in enumerate(data["ingress"]) if rule.get("hostname") == hostname]
        if len(indices) != 1:
            raise CapabilityError("tunnel_route_missing", "Route must exist exactly once")
        index = indices[0]
        start, end = starts[index], starts[index + 1]
        while end > start and (
            not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
        ):
            end -= 1
        del lines[start:end]
        expected = [rule for i, rule in enumerate(data["ingress"]) if i != index]
    updated = "".join(lines)
    if parse(updated) != {**data, "ingress": expected}:
        raise CapabilityError("tunnel_config_invalid", "Route edit changed unrelated configuration")
    return updated


def probe_path(pattern):
    """The literal prefix of an ingress path regex (``^/api/`` -> ``/api/``), else ``/``."""
    if not isinstance(pattern, str) or len(pattern) > 200:
        return "/"
    prefix = re.match(r"\^?(/[A-Za-z0-9_~/-]*)", pattern)
    return prefix.group(1) if prefix else "/"


def origin_url(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            return None
        if url.port is not None and not 1 <= url.port <= 65535:
            return None
        if url.hostname != "localhost":
            address = ipaddress.ip_address(url.hostname)
            # Probe only this Mac or the home LAN (RFC 1918 / ULA), never a public host.
            if not (address.is_loopback or address.is_private) or address.is_link_local:
                return None
        return value
    except (ValueError, TypeError):
        return None


class Tunnel:
    def __init__(self, core):
        self.core = core
        self.runner = hostcmd.run
        self.lock = asyncio.Lock()
        self.probe_limit = asyncio.Semaphore(8)

    def configured(self):
        if not self.core.config.tunnel:
            raise CapabilityError("tunnel_disabled", "Tunnel settings are not configured")
        return self.core.config.tunnel

    def read(self):
        path = Path(self.configured()["config_path"])
        try:
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                raise OSError
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o022
                ):
                    raise OSError
                raw = stream.read(CONFIG_LIMIT + 1)
            if len(raw) > CONFIG_LIMIT:
                raise OSError
            text = raw.decode("utf8")
        except (OSError, UnicodeError):
            raise CapabilityError(
                "tunnel_config_invalid",
                "Tunnel config must be a bounded owned regular file, without "
                "writable group/other access or symlinks",
            ) from None
        return path, text, parse(text)

    def hostname(self, hostname):
        zone = self.configured()["zone"]
        if (
            not hostcmd.tunnel_hostname(hostname)
            or not hostname.endswith("." + zone)
            or "." in hostname[: -len(zone) - 1]
        ):
            raise CapabilityError(
                "invalid_arguments", "Hostname must be one label under the configured zone"
            )

    def preflight(self, hostname, port=None):
        self.hostname(hostname)
        _, _, data = self.read()
        count = sum(r.get("hostname") == hostname for r in data["ingress"])
        if port is not None:
            if type(port) is not int or not 1024 <= port <= 65535:
                raise CapabilityError("invalid_arguments", "Invalid origin port")
            if count:
                raise CapabilityError("tunnel_route_exists", "Hostname is already routed")
            if len(data["ingress"]) > MAX_ROUTES:
                raise CapabilityError("tunnel_route_limit", "Tunnel route limit reached")
        elif count != 1:
            raise CapabilityError("tunnel_route_missing", "Route must exist exactly once")

    async def probe(self, session, url, timeout_s):
        async with self.probe_limit:
            started = time.monotonic()
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=timeout_s), allow_redirects=False
                ) as response:
                    return {
                        "status": response.status,
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                        "error": None,
                    }
            except (aiohttp.ClientError, TimeoutError, ValueError):
                return {
                    "status": None,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "error": "unreachable",
                }

    async def route(self, session, rule):
        hostname = rule["hostname"]
        path = probe_path(rule.get("path"))
        origin = origin_url(rule["service"])
        empty = {"status": None, "latency_ms": None, "error": "unsupported"}
        local, public = await asyncio.gather(
            self.probe(session, origin.rstrip("/") + path, 5)
            if origin
            else asyncio.sleep(0, result=dict(empty)),
            self.probe(session, "https://" + hostname + path, 8)
            if hostcmd.tunnel_hostname(hostname)
            else asyncio.sleep(0, result=dict(empty)),
        )
        state = "unknown"
        # Any answer below 500 means the app is up: a 404 at the probe path is the
        # app's own reply (an image server has nothing at "/").
        local_up = local["status"] is not None and local["status"] < 500
        if origin and not local_up:
            state = "origin_down"
        elif local_up:
            if (
                public["error"] == "unreachable"
                or public["status"] is not None
                and public["status"] >= 500
            ):
                state = "edge_error"
            elif public["status"] is not None and (
                public["status"] == local["status"]
                or 200 <= public["status"] < 400
                or public["status"] in {401, 403}
            ):
                state = "ok"
        return {
            "hostname": hostname,
            "path": path,
            "state": state,
            "origin": local,
            "public": public,
        }

    async def status(self):
        async with self.lock:
            _, _, data = self.read()
        label = self.configured()["launchd_label"]
        code, text, _ = await self.runner(
            [hostcmd.LAUNCHCTL, "print", f"gui/{os.getuid()}/{label}"]
        )
        match = re.search(r"(?m)^\s*state = ([a-z ]+)\s*$", text) if not code else None
        connector = (
            "running"
            if match and match[1].strip() == "running"
            else "stopped"
            if match
            else "unknown"
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            routes = await asyncio.gather(*(self.route(session, r) for r in data["ingress"][:-1]))
        return {
            "status": "ok",
            "tunnel_id": data["tunnel"],
            "credentials_file": data.get("credentials-file"),
            "connector_state": connector,
            "routes": routes,
            "counts": {
                state: sum(r["state"] == state for r in routes)
                for state in ("ok", "origin_down", "edge_error", "unknown")
            },
        }

    async def command(self, argv):
        code, _, _ = await self.runner(argv, timeout_s=30)
        if code:
            raise CapabilityError(
                "tunnel_command_failed", "Tunnel command failed; no command output is exposed"
            )

    def backup(self, path, text):
        directory = path.parent / "backups"
        if directory.is_symlink():
            raise CapabilityError("tunnel_config_invalid", "Backup directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
            raise CapabilityError("tunnel_config_invalid", "Backup directory must be owned")
        directory.chmod(0o700)
        backup = directory / ("config-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".yml")
        with backup.open("x", encoding="utf8", newline="") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        return backup

    def write(self, path, text):
        # Atomic replacement also makes cancellation between awaits recoverable.
        import tempfile

        descriptor, name = tempfile.mkstemp(prefix=".switchboard-", suffix=".yml", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf8", newline="") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def silence(self, client):
        label = self.configured()["launchd_label"]
        for service in self.core.config.services.values():
            if service["runtime"] == {"launchd": label} and service.get("probe"):
                self.core.monitor.restart_silence(
                    service["probe"], "Tunnel route maintenance", client
                )

    async def change(self, client, hostname, port=None):
        async with self.lock:
            self.preflight(hostname, port)
            path, text, data = self.read()
            updated = edit(text, hostname, port)
            backup = self.backup(path, text)
            dns_started = False
            try:
                self.write(path, updated)
                await self.command(
                    [hostcmd.CLOUDFLARED, "tunnel", "ingress", "validate", "--config", str(path)]
                )
                if port is not None:
                    # The DNS command may have succeeded even if its response is lost.
                    dns_started = True
                    await self.command(
                        [hostcmd.CLOUDFLARED, "tunnel", "route", "dns", data["tunnel"], hostname]
                    )
                self.silence(client)
                await self.command(
                    [
                        hostcmd.LAUNCHCTL,
                        "kickstart",
                        "-k",
                        f"gui/{os.getuid()}/{self.configured()['launchd_label']}",
                    ]
                )
            except BaseException:
                if not dns_started:
                    self.write(path, text)
                raise
        result = {
            "status": "ok",
            "hostname": hostname,
            "backup_path": str(backup),
            "dns_left_in_place": port is None,
        }
        if port is None:
            result["note"] = (
                "DNS records were left in place; the owner may delete the CNAME in Cloudflare."
            )
        else:
            async with aiohttp.ClientSession(trust_env=False) as session:
                result["route"] = await self.route(
                    session, {"hostname": hostname, "service": f"http://127.0.0.1:{port}"}
                )
        return result
