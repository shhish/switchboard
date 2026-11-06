"""Configured service diagnostics and bounded, automatically silenced restarts."""

import asyncio
import glob
import json
import math
import os
import re
import stat
import time
from pathlib import Path

from . import hostcmd, remote
from .audit import redact
from .config import allowed_log_path, validate_services
from .errors import CapabilityError

TAIL_LIMIT = 2 * 1024 * 1024
SECRET_KEY = r"(?:authorization|x-api-key|x-plex-token|api_?key|token|password|secret)"
KEY_VALUE = re.compile(
    rf"""(?i)(["']?{SECRET_KEY}["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\r\n,;&}}\]]+)"""
)
AUTH = re.compile(r"(?i)\b(?:authorization\s*[:=]\s*|bearer\s+)[^\r\n,;]+")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
# Long key-like runs need a digit and a letter and no path separators, so file paths,
# titles and slugs in logs survive; 32+ hex (API keys, hashes) is always removed.
OPAQUE = re.compile(
    r"(?<![\w/.-])(?=[A-Za-z0-9_+=-]*\d)(?=[A-Za-z0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{32,}(?![\w/.-])"
    r"|\b[0-9a-fA-F]{32,}\b"
    # Base64 with '/' gives itself away by a '+' or '=' padding, which paths rarely have.
    r"|(?<![\w/.-])(?=[A-Za-z0-9_+/=-]*[+=])[A-Za-z0-9_+/=-]{32,}"
)


def redact_line(line):
    # Recursive key redaction also handles nested JSON log payloads.
    try:
        parsed = json.loads(line)
        if isinstance(parsed, (dict, list)):
            line = json.dumps(redact(parsed), ensure_ascii=False).replace(
                "[REDACTED]", "<REDACTED>"
            )
    except (ValueError, RecursionError):
        pass
    line = AUTH.sub("<REDACTED>", line)
    line = KEY_VALUE.sub(lambda m: m[1] + "<REDACTED>", line)
    line = JWT.sub("<REDACTED>", line)
    line = OPAQUE.sub("<REDACTED>", line)
    # Terminal controls must not change how log data is displayed; drop whole ANSI
    # sequences (container logs are colored) before stray control characters.
    line = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", line)
    line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", line)
    return line[:499] + "…" if len(line) > 500 else line


def number(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError
    return value


def processes(text):
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 3)
        try:
            pid, cpu, rss, comm = fields
            if not pid.isdigit() or int(pid) <= 0:
                continue
            rows.append(
                {
                    "pid": int(pid),
                    "cpu_percent": number(cpu),
                    "rss_mb": round(number(rss) / 1024, 2),
                    "name": Path(comm).name[:300],
                }
            )
        except ValueError:
            continue
    return rows


def tail_file(pattern):
    """Resolve globs, check roots, then open every resolved component without symlinks."""
    paths = []
    for match in glob.iglob(pattern):
        candidate = Path(match)
        resolved = candidate.resolve()
        if not allowed_log_path(resolved, resolve=True):
            raise CapabilityError("resource_denied", "Log symlink leaves allowed roots")
        try:
            info = resolved.stat()
            if stat.S_ISREG(info.st_mode):
                paths.append((info.st_mtime_ns, resolved))
        except OSError:
            continue
    if not paths:
        raise CapabilityError(
            "not_found",
            "Log file not found",
            hint="Check the configured log path and whether the service has written logs",
        )
    path = max(paths, key=lambda item: item[0])[1]
    # O_NOFOLLOW on the entire walk closes the resolve/open symlink race.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CapabilityError("resource_denied", "Log source must be a regular file")
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - TAIL_LIMIT))
            data = stream.read(TAIL_LIMIT)
            partial = size > TAIL_LIMIT
            if partial:
                # Drop the potentially incomplete first line.
                data = data.partition(b"\n")[2]
            return str(path), data.decode("utf-8", "replace").splitlines(), partial
    finally:
        os.close(fd)


def disks():
    paths = [Path("/")]
    try:
        paths.extend(
            p
            for p in sorted(Path("/Volumes").iterdir())
            # Hidden and system mounts (Time Machine snapshots, Recovery) repeat other disks.
            if p.is_dir() and not p.name.startswith((".", "com.apple.")) and p.name != "Recovery"
        )
    except OSError:
        pass
    rows, seen = [], set()
    for path in paths:
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue  # The same filesystem under another name (e.g. Macintosh HD).
            seen.add(device)
            info = os.statvfs(path)
            total, free = info.f_blocks * info.f_frsize, info.f_bavail * info.f_frsize
            rows.append(
                {
                    "name": str(path),
                    "total_gb": round(total / 1024**3, 2),
                    "free_gb": round(free / 1024**3, 2),
                    "used_percent": round((total - free) / total * 100, 2) if total else 0,
                }
            )
        except OSError:
            continue
    return rows


class Services:
    def __init__(
        self, core, runner=None, *, poll_interval=5, poll_timeout=120, sleep=asyncio.sleep
    ):
        self.core = core
        self.runner = runner or hostcmd.run
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.sleep = sleep

    def configured(self, name=None):
        # Revalidate at the boundary as well as config load (including in-process configs).
        services = validate_services(
            self.core.config.services, self.core.config.probes, self.core.config.remote_hosts
        )
        if name is not None:
            if name not in services:
                raise CapabilityError("not_found", "Unknown configured service")
            return services[name]
        return services

    async def command(self, argv):
        try:
            return await self.runner(argv)
        except (OSError, TimeoutError):
            return -1, "", ""

    def remote_row(self, name, host, target, data):
        from .domains.services import SERVICE

        row = {
            "name": name,
            "runtime": "remote",
            "host": host,
            "status": "unknown",
            "running": False,
            "pid": None,
        }
        try:
            services = data.get("services")
            if not isinstance(services, list):
                raise ValueError
            matches = [r for r in services if isinstance(r, dict) and r.get("name") == target]
            if len(matches) != 1 or type(matches[0].get("running")) is not bool:
                raise ValueError
            source = matches[0]
            for key in (
                "running",
                "pid",
                "state",
                "restart_count",
                "started_at",
                "rss_mb",
                "docker_health",
            ):
                if key in source:
                    row[key] = source[key]
            if not isinstance(source.get("state"), str):
                raise ValueError
            if source["state"].split("/", 1)[0] != "unknown":
                row["status"] = "running" if row["running"] else "stopped"
            else:
                row["reason"] = "Remote runtime state unavailable"
            # Silence is added by status(), but validate all backend fields here.
            remote.project({**row, "silenced": False}, SERVICE)
        except (ValueError, CapabilityError):
            row = {
                "name": name,
                "runtime": "remote",
                "host": host,
                "status": "unknown",
                "running": False,
                "pid": None,
                "reason": "Invalid remote runtime response",
            }
        return row

    async def remote_status(self, host, target=None):
        try:
            return await remote.call(self.core, host, "status", *((target,) if target else ()))
        except CapabilityError as exc:
            return {
                "reason": f"{host} unreachable"
                if exc.code == "backend_down"
                else "Remote status unavailable"
            }

    async def runtime_status(self, name, settings):
        kind, target = next(iter(settings["runtime"].items()))
        if kind in self.core.config.remote_hosts:
            data = await self.remote_status(kind, target)
            row = self.remote_row(name, kind, target, data)
            if "reason" in data:
                row["reason"] = data["reason"]
            return row
        row = {"name": name, "runtime": kind, "status": "unknown", "running": False, "pid": None}
        if kind == "launchd":
            argv = [hostcmd.LAUNCHCTL, "print", f"gui/{os.getuid()}/{target}"]
        elif kind == "docker":
            argv = [hostcmd.DOCKER, "inspect", "--format", hostcmd.STATE_FORMAT, target]
        elif kind == "process":
            argv = [hostcmd.PS, "-Axo", "pid,pcpu,rss,comm"]
        else:
            argv = [hostcmd.LAUNCHCTL, "list"]
        code, text, _ = await self.command(argv)
        if code:
            row["reason"] = "Runtime command failed or unavailable"
            return row
        try:
            if kind == "launchd":
                state = re.search(r"(?m)^\s*state = ([\w -]+)\s*$", text)
                if not state:
                    raise ValueError
                row["running"] = state[1].strip() == "running"
                for field, pattern in (
                    ("pid", r"(?m)^\s*pid = (\d+)\s*$"),
                    ("last_exit", r"(?m)^\s*last exit code = (-?\d+)\s*$"),
                ):
                    if match := re.search(pattern, text):
                        row[field] = int(match[1])
                if row["running"] and not row["pid"]:
                    raise ValueError
            elif kind == "docker":
                state_text, count = text.strip().rsplit(" ", 1)
                state = json.loads(state_text)
                if type(state.get("Running")) is not bool:
                    raise ValueError
                row["running"] = state["Running"]
                pid = state.get("Pid")
                if type(pid) is int and pid > 0:
                    row["pid"] = pid
                row["restart_count"] = int(count)
                if row["restart_count"] < 0:
                    raise ValueError
                if isinstance(state.get("StartedAt"), str) and state["StartedAt"]:
                    row["started_at"] = state["StartedAt"][:100]
                health = state.get("Health", {}).get("Status")
                if health in {"starting", "healthy", "unhealthy"}:
                    row["docker_health"] = health
            elif kind == "process":
                for line in text.splitlines():
                    fields = line.split(None, 3)
                    if len(fields) == 4 and fields[0].isdigit() and fields[3].startswith(target):
                        row.update(running=True, pid=int(fields[0]))
                        break
            else:
                # launchctl list uses PID, last exit status, label. Only exact bundle prefixes.
                prefix = f"application.{target}."
                for line in text.splitlines():
                    fields = line.split(None, 2)
                    if len(fields) == 3 and fields[2].startswith(prefix) and fields[0].isdigit():
                        if int(fields[0]) > 0:
                            row.update(running=True, pid=int(fields[0]))
                            break
            row["status"] = "running" if row["running"] else "stopped"
        except (ValueError, TypeError, AttributeError):
            row = {
                "name": name,
                "runtime": kind,
                "status": "unknown",
                "running": False,
                "pid": None,
                "reason": "Invalid runtime response",
            }
        return row

    async def status(self, name=None):
        settings = {name: self.configured(name)} if name is not None else self.configured()
        if not settings:
            return {"status": "ok", "services": []}
        hosts = sorted(
            {next(iter(s["runtime"])) for s in settings.values()}
            & self.core.config.remote_hosts.keys()
        )
        local = {n: s for n, s in settings.items() if next(iter(s["runtime"])) not in hosts}
        host_replies, local_rows, process_result = await asyncio.gather(
            asyncio.gather(*(self.remote_status(h) for h in hosts)),
            asyncio.gather(*(self.runtime_status(n, s) for n, s in local.items())),
            self.command([hostcmd.PS, "-Axo", "pid,pcpu,rss,comm"]),
        )
        replies = dict(zip(hosts, host_replies, strict=True))
        by_name = {r["name"]: r for r in local_rows}
        rows = []
        for n, setting in settings.items():
            kind, target = next(iter(setting["runtime"].items()))
            if kind in replies:
                row = self.remote_row(n, kind, target, replies[kind])
                if "reason" in replies[kind]:
                    row["reason"] = replies[kind]["reason"]
            else:
                row = by_name[n]
            rows.append(row)
        code, text, _ = process_result
        metrics = {p["pid"]: p for p in processes(text)} if not code else {}
        for row in rows:
            # Docker Desktop PIDs live in its VM; matching them to host processes is wrong.
            if row["runtime"] not in ("docker", "remote") and (metric := metrics.get(row["pid"])):
                row.update(cpu_percent=metric["cpu_percent"], rss_mb=metric["rss_mb"])
            probe = settings[row["name"]].get("probe")
            row["silenced"] = bool(probe and self.core.monitor.silenced(probe, time.time()))
            if probe:
                state = self.core.storage.db.execute(
                    "SELECT status FROM monitor_state WHERE name=?", (probe,)
                ).fetchone()
                cached = (self.core.probes.cached or {}).get("services", [])
                row["probe_status"] = next(
                    (p["status"] for p in cached if p["name"] == probe),
                    state["status"] if state else "unknown",
                )
        return {"status": "ok", "services": rows}

    async def logs(self, args):
        name = args["service"]
        settings = self.configured(name)
        index = args.get("source", 0)
        sources = ["remote"] if settings["logs"] == "remote" else settings["logs"]
        if index >= len(sources):
            raise CapabilityError("not_found", "No configured log source at this index")
        source = sources[index]
        limit = args.get("lines", 50)
        if source == "remote":
            kind, target = next(iter(settings["runtime"].items()))
            count = min(limit * 4, 400)
            data = await remote.call(self.core, kind, "logs", target, str(count))
            lines = data.get("lines")
            if (
                not isinstance(lines, list)
                or len(lines) > count
                or not all(isinstance(line, str) for line in lines)
            ):
                raise CapabilityError("backend_invalid", "Invalid remote log response")
            truncated = len(lines) >= count
        elif source == "docker":
            code, stdout, stderr = await self.command(
                [
                    hostcmd.DOCKER,
                    "logs",
                    "--tail",
                    str(min(limit * 4, 800)),
                    settings["runtime"]["docker"],
                ]
            )
            if code:
                raise CapabilityError("backend_down", "Container logs unavailable")
            # The real runner merges streams in order. Fakes may return them separately.
            text = stdout
            if stderr:
                text += ("\n" if text and not text.endswith("\n") else "") + stderr
            lines = text.splitlines()
            truncated = len(lines) >= min(limit * 4, 800)
        else:
            try:
                result = await asyncio.to_thread(tail_file, source)
            except OSError:
                raise CapabilityError("resource_denied", "Log file cannot be read safely") from None
            source, lines, truncated = result
        contains = args.get("contains", "").casefold()
        matching = [line for line in lines if contains in line.casefold()]
        selected = matching[-limit:]
        return {
            "status": "ok",
            "service": name,
            "source": source,
            "lines": [redact_line(line) for line in selected],
            "truncated": truncated or len(matching) > limit or any(len(s) > 500 for s in selected),
            "matched": len(matching),
        }

    async def snapshot(self, host="mac"):
        if host != "mac":
            from .domains.services import SNAPSHOT

            data = await remote.call(self.core, host, "snapshot")
            if isinstance(data.get("memory"), dict) and "available_mb" in data["memory"]:
                data["memory"]["free_mb"] = data["memory"]["available_mb"]
            result = remote.project(
                {**data, "status": "ok"},
                {
                    **SNAPSHOT,
                    "required": [
                        "status",
                        "load_averages",
                        "cpu_count",
                        "uptime_s",
                        "memory",
                        "swap",
                        "disks",
                        "top_cpu",
                        "top_rss",
                    ],
                },
            )
            for key in ("top_cpu", "top_rss"):
                for row in result.get(key, []):
                    row["name"] = row["name"].rsplit("/", 1)[-1]
            return result
        commands = [
            [hostcmd.SYSCTL, "-n", "kern.boottime"],
            [hostcmd.SYSCTL, "-n", "hw.memsize"],
            [hostcmd.SYSCTL, "-n", "vm.swapusage"],
            [hostcmd.VM_STAT],
            [hostcmd.PS, "-Axo", "pid,pcpu,rss,comm"],
        ]
        results = await asyncio.gather(*(self.command(argv) for argv in commands))
        values = [text if not code else "" for code, text, _ in results]
        boot, total, swap, vm, ps = values
        result = {"status": "ok", "cpu_count": os.cpu_count(), "memory": {}, "swap": {}}
        try:
            result["load_averages"] = list(os.getloadavg())
        except OSError:
            result["load_averages"] = []
        if match := re.search(r"sec\s*=\s*(\d+)", boot):
            result["uptime_s"] = max(0, time.time() - int(match[1]))
        if total.strip().isdigit():
            result["memory"]["total_mb"] = int(total) / 1024**2
        if match := re.search(r"page size of (\d+) bytes", vm):
            page = int(match[1]) / 1024**2
            pages = {
                key: int(value) for key, value in re.findall(r"^([^:\n]+):\s*(\d+)\.", vm, re.M)
            }
            for field, key in (
                ("free_mb", "Pages free"),
                ("compressed_mb", "Pages occupied by compressor"),
                ("wired_mb", "Pages wired down"),
                ("active_mb", "Pages active"),
                ("cached_mb", "Pages inactive"),
            ):
                if key in pages:
                    result["memory"][field] = round(pages[key] * page, 2)
            # Like Activity Monitor: apps + wired + compressed; inactive file cache is free.
            used = ("active_mb", "wired_mb", "compressed_mb")
            if all(k in result["memory"] for k in used):
                result["memory"]["used_mb"] = round(sum(result["memory"][k] for k in used), 2)
        for field, value, unit in re.findall(r"(total|used|free)\s*=\s*([\d.]+)([KMG])", swap):
            try:
                result["swap"][field + "_mb"] = (
                    number(value) * {"K": 1 / 1024, "M": 1, "G": 1024}[unit]
                )
            except ValueError:
                pass
        rows = processes(ps)
        result["top_cpu"] = sorted(rows, key=lambda p: p["cpu_percent"], reverse=True)[:8]
        result["top_rss"] = sorted(rows, key=lambda p: p["rss_mb"], reverse=True)[:8]
        result["disks"] = await asyncio.to_thread(disks)
        result["unavailable"] = [
            argv[0].rsplit("/", 1)[-1] + ":" + argv[-1]
            for argv, r in zip(commands, results, strict=True)
            if r[0]
        ]
        return result

    async def restart(self, client, args):
        name = args["service"]
        settings = self.configured(name)
        kind, target = next(iter(settings["runtime"].items()))
        if (
            not settings["restart"]
            or kind in ("app", "process")
            or hostcmd.switchboard_label(target)
        ):
            raise CapabilityError(
                "resource_denied",
                "Service restart is not allowed",
                hint=(
                    "Enable restart for a configured launchd/docker/remote service; "
                    "exclude Switchboard"
                ),
            )
        now = time.time()
        db = self.core.storage.db
        # Reserve before awaiting: concurrent calls and failures cannot bypass cooldown.
        with db:
            row = db.execute(
                "SELECT restarted_at FROM service_restarts WHERE name=?", (name,)
            ).fetchone()
            if row and now - row["restarted_at"] < 600:
                raise CapabilityError(
                    "resource_denied",
                    "Service restart cooldown is active",
                    hint=f"Retry in {math.ceil(600 - (now - row['restarted_at']))} seconds",
                )
            db.execute("INSERT OR REPLACE INTO service_restarts VALUES (?, ?)", (name, now))
        if probe := settings.get("probe"):
            self.core.monitor.restart_silence(probe, args["reason"], client)
        if kind in self.core.config.remote_hosts:
            data = await remote.call(self.core, kind, "restart", target)
            if data.get("restarted") is not True:
                raise CapabilityError(
                    "backend_error", "Remote restart failed; cooldown and silence remain active"
                )
        else:
            argv = (
                [hostcmd.LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/{target}"]
                if kind == "launchd"
                else [hostcmd.DOCKER, "restart", target]
            )
            code, _, _ = await self.command(argv)
            if code:
                raise CapabilityError(
                    "backend_down", "Restart command failed; cooldown and silence remain active"
                )

        async def recover(job_id):
            deadline = asyncio.get_running_loop().time() + self.poll_timeout
            while True:
                try:
                    async with asyncio.timeout(
                        max(0, deadline - asyncio.get_running_loop().time())
                    ):
                        if probe:
                            config = next(p for p in self.core.config.probes if p["name"] == probe)
                            observation = await self.core.probes.probe(config)
                            recovered = observation["status"] == "up"
                        else:
                            observation = await self.runtime_status(name, settings)
                            recovered = observation["status"] == "running"
                except TimeoutError:
                    return {"service": name, "recovered": False}
                if recovered:
                    if probe:
                        # Covers disabled monitoring and outages shorter than a monitor cycle.
                        with db:
                            db.execute(
                                "DELETE FROM monitor_silence WHERE name=? AND system=1", (probe,)
                            )
                    return {"service": name, "recovered": True}
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {"service": name, "recovered": False}
                await self.sleep(min(self.poll_interval, remaining))

        handle = self.core.jobs.submit("ops_service_restart", client, recover)
        return {**handle, "service": name}
