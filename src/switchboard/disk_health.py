"""Read-only physical disk discovery, SMART cache and bounded hourly fill history.

No serials or raw backend payloads are retained. APFS samples represent shared
container allocation, never the sum of the volumes' statvfs results.
"""

import asyncio
import hashlib
import json
import os
import plistlib
import re
import time
from pathlib import Path

from . import hostcmd, remote
from .domains.common import sanitize_text
from .errors import CapabilityError
from .mounts import important_free, mounted_volumes

HOUR = 3600
DAY = 24 * HOUR
MAX_POINTS = 35 * 24
MAX_ROWS = 200_000
SMART_INTERVAL = 6 * HOUR
COUNTERS = ("reallocated", "pending", "uncorrectable", "media_errors")


def text(value, limit=120):
    return sanitize_text(value, limit) if isinstance(value, str) else ""


def number(value):
    return value if type(value) in (int, float) and 0 <= value < 10**20 else None


def label_for(host, identity):
    # No mount names in probe identifiers: safe, short, unique across hosts, stable
    # over renumbering when diskutil supplies persistent partition/container UUIDs.
    return "disk-" + hashlib.sha256((host + ":" + identity).encode()).hexdigest()[:20]


def smart_reading(data, code=0):
    result = {
        "model": None,
        "enabled": None,
        "passed": None,
        "state": "unsupported",
        **{
            key: None
            for key in (
                "temperature_c",
                "power_on_hours",
                "reallocated",
                "pending",
                "uncorrectable",
                "crc_errors",
                "percentage_used",
                "media_errors",
                "available_spare",
            )
        },
        "warnings": [],
        "notes": [],
    }
    support = data.get("smart_support", {})
    if isinstance(support, dict) and type(support.get("enabled")) is bool:
        result["enabled"] = support["enabled"]
    metadata = data.get("smartctl", {})
    messages = metadata.get("messages", []) if isinstance(metadata, dict) else []
    messages = messages[:256] if isinstance(messages, list) else []
    message = " ".join(str(m.get("string", "")) for m in messages if isinstance(m, dict))
    mode = str(data.get("power_mode", "")) + " " + message
    if "standby" in mode.lower() or "sleep" in mode.lower():
        result["state"] = "standby"
        return result
    if result["enabled"] is False or "smart disabled" in message.lower():
        result.update(
            enabled=False, state="disabled", warnings=["SMART is disabled; owner action required"]
        )
        return result
    model = data.get("model_name")
    if isinstance(model, str) and model.strip():
        result["model"] = text(model.strip())
    status = data.get("smart_status", {})
    if isinstance(status, dict) and type(status.get("passed")) is bool:
        result.update(passed=status["passed"], enabled=True, state="ok")
    nvme = data.get("nvme_smart_health_information_log", {})
    if isinstance(nvme, dict) and nvme:
        result.update(enabled=True, state="ok")
        for key in ("percentage_used", "media_errors", "available_spare"):
            result[key] = number(nvme.get(key))
        result["temperature_c"] = number(nvme.get("temperature"))
        result["power_on_hours"] = number(nvme.get("power_on_hours"))
    temperature = data.get("temperature", {})
    power = data.get("power_on_time", {})
    if isinstance(temperature, dict):
        current = number(temperature.get("current"))
        if current is not None:
            result["temperature_c"] = current
    if isinstance(power, dict):
        result["power_on_hours"] = (
            number(power.get("hours")) if "hours" in power else result["power_on_hours"]
        )
    attributes = data.get("ata_smart_attributes", {})
    attributes = attributes.get("table", []) if isinstance(attributes, dict) else []
    attributes = attributes if isinstance(attributes, list) else []
    mapping = {5: "reallocated", 197: "pending", 198: "uncorrectable", 199: "crc_errors"}
    for item in attributes[:256]:
        if not isinstance(item, dict):
            continue
        raw = item.get("raw", {})
        raw = number(raw.get("value")) if isinstance(raw, dict) else None
        if type(item.get("id")) is int and item["id"] in mapping:
            result[mapping[item["id"]]] = raw
        elif item.get("id") == 9 and result["power_on_hours"] is None:
            result["power_on_hours"] = raw
    if code < 0 or code & 7:  # bits 0-2: bad arguments, device open, failed SMART command
        result["warnings"].append("SMART read unavailable or incomplete")
        result.update(state="unsupported", passed=None)
    elif code > 0:
        # smartctl's exit status is a bitmask; bits 3+ describe the drive itself.
        if code & 16:
            result["warnings"].append("a pre-failure attribute is at or below its threshold")
        if code & 64:
            result["warnings"].append("the drive's error log has entries")
        if code & 128:
            result["warnings"].append("the drive's self-test log reports errors")
        if code & 32:
            result["notes"].append("an attribute was at or below its threshold in the past")
    if result["state"] == "unsupported":
        result["warnings"].append("SMART not supported or unavailable through this bridge")
    return result


def smart_warnings(reading, baseline, settings):
    warnings = list(reading["warnings"])
    if reading["passed"] is False:
        warnings.append("SMART overall health failed")
    for key in COUNTERS:
        value, previous = reading[key], baseline.get(key)
        if value is not None and previous is not None and value > previous:
            warnings.append(key + " increased")
    if (value := reading["percentage_used"]) is not None and value >= settings[
        "nvme_percentage_used"
    ]:
        warnings.append("NVMe endurance threshold reached")
    if (
        (value := reading["temperature_c"]) is not None
        and value >= settings["temperature_c"]
        and reading["percentage_used"] is None
    ):
        warnings.append("Drive temperature threshold reached")
    return warnings


def trend(points, total, free, now):
    points = [p for p in points if now - 7 * DAY <= p[0] <= now]
    if len(points) < 2 or points[-1][0] - points[0][0] < HOUR:
        return {"bytes_per_day": None, "days_to_full": None, "samples": len(points)}
    # Center time and allocation to avoid cancellation at epoch/terabyte magnitudes.
    xs = [(p[0] - points[0][0]) / DAY for p in points]
    ys = [p[1] - points[0][1] for p in points]
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - xm) ** 2 for x in xs)
    rate = (
        sum((x - xm) * (y - ym) for x, y in zip(xs, ys, strict=True)) / denominator
        if denominator
        else 0
    )
    return {
        "bytes_per_day": round(rate, 3),
        "days_to_full": free / rate if rate > 0 else None,
        "samples": len(points),
    }


class DiskHealth:
    def __init__(self, core):
        self.core = core
        self.db = core.storage.db
        self.runner = hostcmd.run
        self.statvfs = os.statvfs
        self.important_free = important_free
        self.mounts = mounted_volumes
        self.lock = asyncio.Lock()

    def enabled(self):
        if not self.core.config.storage["enabled"]:
            raise CapabilityError("backend_disabled", "Storage monitoring is disabled")

    async def plist(self, argv, inventory=None):
        code, stdout, _ = await self.runner(argv, physical_disks=inventory)
        if code:
            raise CapabilityError("backend_down", "Disk discovery unavailable")
        try:
            data = plistlib.loads(stdout.encode() if isinstance(stdout, str) else stdout)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise CapabilityError("backend_invalid", "Invalid disk discovery response") from None

    async def read_smart(self, disk, identity, inventory, refresh, now):
        row = self.db.execute(
            "SELECT * FROM storage_smart WHERE identity=?", (identity,)
        ).fetchone()
        if row and not refresh and 0 <= now - row["sampled_at"] < SMART_INTERVAL:
            return {**json.loads(row["reading"]), "sampled_at": row["sampled_at"]}
        baseline = json.loads(row["baseline"]) if row else {}
        code, stdout, _ = await self.runner(
            [hostcmd.SMARTCTL, "-j", "-n", "standby", "-H", "-A", "-i", disk],
            physical_disks=inventory,
        )
        try:
            data = json.loads(stdout)
            if not isinstance(data, dict):
                raise ValueError
            reading = smart_reading(data, code)
        except (ValueError, TypeError, AttributeError, RecursionError):
            # With -n standby, a sleeping drive makes smartctl exit 2 without a report.
            reading = smart_reading(
                {"power_mode": "standby"} if code == 2 else {}, -1 if code != 2 else 0
            )
        reading["warnings"] = smart_warnings(reading, baseline, self.core.config.storage)
        if reading["state"] == "ok":
            baseline = {
                key: reading[key] if reading[key] is not None else baseline.get(key)
                for key in COUNTERS
            }
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO storage_smart VALUES (?, ?, ?, ?)",
                (identity, now, json.dumps(reading), json.dumps(baseline)),
            )
            self.db.execute("DELETE FROM storage_smart WHERE sampled_at<?", (now - 35 * DAY,))
            self.db.execute(
                "DELETE FROM storage_smart WHERE rowid IN "
                "(SELECT rowid FROM storage_smart ORDER BY sampled_at DESC LIMIT -1 OFFSET 128)"
            )
        return {**reading, "sampled_at": now}

    async def discover(self, refresh, now):
        physical = await self.plist([hostcmd.DISKUTIL, "list", "-plist", "physical"])
        try:
            whole, members = hostcmd.physical_inventory(physical)
        except (TypeError, ValueError, AttributeError):
            raise CapabilityError("backend_invalid", "Invalid physical disk inventory") from None
        inventory = {"whole": whole, "members": members}
        infos = {}
        for identifier in sorted(members):
            try:
                info = await self.plist([hostcmd.DISKUTIL, "info", "-plist", identifier], inventory)
            except CapabilityError:
                continue  # One slow or vanished disk must not hide the others.
            if info.get("DeviceIdentifier") != identifier:
                raise CapabilityError("backend_invalid", "Disk metadata does not match discovery")
            infos[identifier] = info
        disks = []
        for raw in physical.get("AllDisksAndPartitions", [])[:64]:
            identifier = raw.get("DeviceIdentifier")
            if identifier not in whole or identifier not in infos:
                continue
            info = infos[identifier]
            identity = label_for(
                "mac",
                ":".join(
                    str(p.get("DiskUUID", p.get("DeviceIdentifier", "")))
                    for p in raw.get("Partitions", [])
                )
                + str(info.get("MediaName", ""))
                + str(raw.get("Size", "")),
            )
            disks.append(
                {
                    "id": identifier,
                    "health_id": identity,
                    "model": text(info.get("MediaName") or info.get("IORegistryEntryName")),
                    "capacity_bytes": number(info.get("TotalSize") or raw.get("Size")) or 0,
                    "internal": info.get("Internal") is True,
                    "protocol": text(info.get("BusProtocol"), 40),
                    "smart": await self.read_smart(identifier, identity, inventory, refresh, now),
                }
            )
        for disk in disks:
            # USB enclosures report their bridge chip as the media name; SMART names the drive.
            disk["model"] = disk["smart"].get("model") or disk["model"]
        containers, volumes = await self.local_volumes(physical, infos)
        return disks, containers, volumes

    async def local_volumes(self, physical, infos):
        try:
            mounts = await asyncio.to_thread(self.mounts)
        except (OSError, AttributeError):
            raise CapabilityError("backend_down", "Mount discovery unavailable") from None
        stores = {}
        physical_ids = {}
        for disk in physical.get("AllDisksAndPartitions", [])[:64]:
            for partition in disk.get("Partitions", [])[:128]:
                identifier = partition.get("DeviceIdentifier")
                if identifier not in infos:
                    continue
                physical_ids[identifier] = disk["DeviceIdentifier"]
                info = infos[identifier]
                container = info.get("APFSContainerReference")
                if hostcmd.disk_id(container, whole=True) and info.get("Content") == "Apple_APFS":
                    stores[container] = (
                        disk["DeviceIdentifier"],
                        partition.get("DiskUUID") or identifier,
                    )
        containers, volumes = {}, []
        for mount in mounts[:4096]:
            path, device, fs = mount["mount"], mount["device"], mount["fs"]
            # Discard every image/network/snapshot mount unless it maps back to an
            # actual listed physical partition or that partition's APFS container.
            if not device.startswith("/dev/") or not hostcmd.disk_id(device[5:]):
                continue
            identifier = device[5:]
            if (
                mount["readonly"]
                or path == "/"
                or path == "/Volumes/Recovery"
                or path.startswith(
                    ("/Library/Developer/CoreSimulator/Volumes/", "/Volumes/.timemachine/")
                )
            ):
                continue
            if not path.startswith("/") or len(path) > 1024 or any(ord(c) < 32 for c in path):
                continue
            if path.startswith("/System/Volumes/") and path != "/System/Volumes/Data":
                continue
            container = re.sub(r"s[0-9]+.*$", "", identifier) if fs == "apfs" else None
            if container in stores:
                physical_disk, identity = stores[container]
            elif identifier in physical_ids and identifier in infos and fs != "apfs":
                info = infos[identifier]
                if not info.get("WritableVolume", True) or info.get("SystemImage") is True:
                    continue
                physical_disk = physical_ids[identifier]
                identity = info.get("DiskUUID") or identifier
            else:
                continue
            try:
                usage = await asyncio.to_thread(self.statvfs, path)
            except OSError:
                raise CapabilityError("backend_down", "Mounted disk usage unavailable") from None
            # f_bfree is shared container free on APFS; f_bavail can be reduced by
            # volume quotas. No summing of per-volume used blocks.
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bfree * usage.f_frsize
            if not 0 < total < 10**20 or not 0 <= free <= total or usage.f_flag & os.ST_RDONLY:
                continue
            # macOS frees purgeable space (Time Machine local snapshots, caches) on
            # demand, so judge fill on what it reports as available for important use.
            available = await asyncio.to_thread(self.important_free, path)
            purgeable = 0
            if isinstance(available, int) and free < available <= total:
                purgeable, free = available - free, available
            label = label_for("mac", str(identity))
            name = text(infos.get(identifier, {}).get("VolumeName") or Path(path).name)
            role = "data" if path == "/System/Volumes/Data" else None
            # Legacy Time Machine destinations have this marker. APFS destinations
            # without a discoverable role remain unknown rather than inferred by name.
            if await asyncio.to_thread((Path(path) / "Backups.backupdb").is_dir):
                role = "time_machine"
            row = {
                "id": identifier,
                "label": label,
                "name": name or identifier,
                "mount": path,
                "fs": text(fs, 40),
                "role": role,
                "physical_disk": physical_disk,
                "container_id": container,
                "total_bytes": total,
                "used_bytes": total - free,
                "free_bytes": free,
                "purgeable_bytes": purgeable,
                "used_percent": round(100 * (total - free) / total, 2),
                "shares_space_with": [],
            }
            volumes.append(row)
            if label not in containers or path == "/System/Volumes/Data":
                containers[label] = {**row, "id": container or identifier}
        for row in volumes:
            peers = [
                v["mount"] for v in volumes if v["label"] == row["label"] and v["id"] != row["id"]
            ]
            row["shares_space_with"] = peers
            # All shared members expose the same container-level allocation.
            for key in (
                "total_bytes",
                "used_bytes",
                "free_bytes",
                "purgeable_bytes",
                "used_percent",
            ):
                row[key] = containers[row["label"]][key]
        for row in containers.values():
            row["shares_space_with"] = [
                v["mount"]
                for v in volumes
                if v["label"] == row["label"] and v["mount"] != row["mount"]
            ]
        return list(containers.values())[:128], volumes[:128]

    def sample(self, containers, now):
        with self.db:
            for row in containers:
                self.db.execute(
                    "INSERT OR IGNORE INTO storage_samples VALUES (?, ?, ?, ?, ?)",
                    (row["label"], int(now // HOUR), now, row["used_bytes"], row["total_bytes"]),
                )
            self.db.execute("DELETE FROM storage_samples WHERE sampled_at<?", (now - 35 * DAY,))
            self.db.execute(
                "DELETE FROM storage_samples WHERE rowid IN "
                "(SELECT rowid FROM storage_samples ORDER BY sampled_at DESC LIMIT -1 OFFSET ?)",
                (MAX_ROWS,),
            )

    def history(self, label, days=7, now=None):
        self.enabled()
        now = time.time() if now is None else now
        points = self.db.execute(
            "SELECT sampled_at, used_bytes FROM storage_samples "
            "WHERE label=? AND sampled_at>=? AND sampled_at<=? "
            "ORDER BY sampled_at DESC LIMIT ?",
            (label, now - days * DAY, now, MAX_POINTS),
        ).fetchall()
        if not points:
            raise CapabilityError("not_found", "No storage samples for this label and period")
        return {
            "status": "ok",
            "label": label,
            "days": days,
            "points": [{"sampled_at": p[0], "used_bytes": p[1]} for p in reversed(points)],
        }

    def alerts(self, host=None):
        names = self.probe_names(host)
        return [
            {
                "name": row["name"],
                "status": row["status"],
                "down_since": row["down_since"],
                "alerted": bool(row["alerted"]),
            }
            for row in self.db.execute(
                "SELECT * FROM monitor_state WHERE down_since IS NOT NULL ORDER BY name"
            )
            if row["name"] in names
        ]

    def probe_names(self, host=None):
        if not self.core.config.storage["enabled"]:
            return set()
        names = set()
        for row in self.db.execute("SELECT host, payload FROM storage_inventory"):
            if row["host"] not in {"mac", *self.core.config.remote_hosts}:
                continue
            if host is not None and row["host"] != host:
                continue
            payload = json.loads(row["payload"])
            names.update("storage:" + c["label"] for c in payload["containers"])
            names.update("smart:" + d["health_id"] for d in payload["disks"])
        return names

    def evaluate(self, payload, now):
        monitor = self.core.monitor
        settings = self.core.config.storage
        with self.db:
            for row in payload["containers"]:
                name = "storage:" + row["label"]
                previous = self.db.execute(
                    "SELECT status FROM monitor_state WHERE name=?", (name,)
                ).fetchone()
                recovering = bool(previous and previous[0] in ("down", "degraded"))
                margin = settings["hysteresis_percent"] if recovering else 0
                threshold = settings["warn_percent"] - margin
                fill_failing = (
                    row["used_percent"] > threshold if margin else row["used_percent"] >= threshold
                ) and below_floor(row, settings["warn_free_gb"], recovering)
                days = row["trend"]["days_to_full"]
                failing = fill_failing or (
                    days is not None and days < settings["warn_days_to_full"]
                )
                critical = row["used_percent"] >= settings["critical_percent"] and below_floor(
                    row, settings["critical_free_gb"], recovering
                )
                status = "down" if critical else "degraded" if failing else "up"
                detail = f"{row['used_percent']:.1f}% used, {free_text(row['free_bytes'])} free"
                if days is not None:
                    detail += f"; estimated full in {days:.1f} days"
                monitor.transition(
                    {
                        "name": name,
                        "label": row["name"],
                        "storage_detail": detail,
                        "failures_before_alert": 1,
                    },
                    status,
                    now,
                )
            for disk in payload["disks"]:
                reading = disk["smart"]
                # Unreadable/sleeping devices never resolve an existing fault incident.
                status = "unknown"
                if reading["state"] == "ok":
                    status = (
                        "down"
                        if reading["passed"] is False
                        else "degraded"
                        if reading["warnings"]
                        else "up"
                    )
                monitor.transition(
                    {
                        # diskN renumbers across reboots; the identity hash does not.
                        "name": "smart:" + disk["health_id"],
                        "label": (disk["model"] or disk["id"]) + " drive health",
                        "storage_detail": "; ".join(reading["warnings"]),
                        "failures_before_alert": 1,
                    },
                    status,
                    now,
                )

    async def remote_volumes(self, host):
        from .domains.services import SNAPSHOT

        data = await remote.call(self.core, host, "snapshot")
        schema = SNAPSHOT["properties"]["disks"]
        rows = remote.project(
            {"disks": data.get("disks")},
            {
                "type": "object",
                "properties": {"disks": schema},
                "required": ["disks"],
                "additionalProperties": False,
            },
        )["disks"]
        containers = []
        for row in rows:
            total, free = int(row["total_gb"] * 1024**3), int(row["free_gb"] * 1024**3)
            if not total or free > total:
                continue
            containers.append(
                {
                    "label": label_for(host, row["name"]),
                    "id": text(row["name"], 1024),
                    "name": text(row["name"]),
                    "mount": text(row["name"], 1024),
                    "fs": "unknown",
                    "role": None,
                    "physical_disk": None,
                    "container_id": None,
                    "total_bytes": total,
                    "used_bytes": total - free,
                    "free_bytes": free,
                    "purgeable_bytes": 0,
                    "used_percent": round(100 * (total - free) / total, 2),
                    "shares_space_with": [],
                }
            )
        return [], containers, []

    async def status(self, host="mac", refresh=False, now=None):
        self.enabled()
        if host != "mac" and host not in self.core.config.remote_hosts:
            raise CapabilityError("invalid_arguments", "Unknown configured host")
        async with self.lock:
            now = time.time() if now is None else now
            disks, containers, volumes = await (
                self.discover(refresh, now) if host == "mac" else self.remote_volumes(host)
            )
            self.sample(containers, now)
            for row in containers:
                points = self.db.execute(
                    "SELECT sampled_at, used_bytes FROM storage_samples "
                    "WHERE label=? AND sampled_at>=? AND total_bytes=? ORDER BY sampled_at",
                    (row["label"], now - 7 * DAY, row["total_bytes"]),
                ).fetchall()
                row["trend"] = trend(points, row["total_bytes"], row["free_bytes"], now)
            payload = {"disks": disks, "containers": containers, "volumes": volumes}
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO storage_inventory VALUES (?, ?, ?)",
                    (host, now, json.dumps(payload)),
                )
                if self.core.config.monitor["enabled"]:
                    self.evaluate(payload, now)
            return {
                "status": "ok",
                "host": host,
                "sampled_at": now,
                **payload,
                "alerts": self.alerts(host),
            }

    async def maintain(self):
        if not self.core.config.storage["enabled"]:
            return
        now = time.time()
        for host in ["mac", *self.core.config.remote_hosts]:
            row = self.db.execute(
                "SELECT sampled_at FROM storage_inventory WHERE host=?", (host,)
            ).fetchone()
            if row and 0 <= now - row[0] < HOUR:
                continue
            try:
                async with asyncio.timeout(180):
                    await self.status(host, now=now)
            except Exception:  # noqa: BLE001 - one host's failure must not skip the rest
                continue  # A failed read is never a fabricated recovery or a zero sample.


def below_floor(row, floor_gb, recovering):
    """True when a free-space floor is off (0) or free space is under it. A disk
    that is already alerting must clear the floor by 10% before it recovers."""
    if not floor_gb:
        return True
    floor = floor_gb * 1e9 * (1.1 if recovering else 1)
    return row["free_bytes"] < floor


def free_text(free_bytes):
    if free_bytes >= 1e12:
        return f"{free_bytes / 1e12:.1f} TB"
    return f"{free_bytes / 1e9:.0f} GB"
