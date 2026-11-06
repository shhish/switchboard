"""Fixed media-ctrl admission/status commands; managed lanes own all media work."""

import asyncio
import math
import re
from pathlib import Path

from .errors import CapabilityError

MAX_OUTPUT = 64 * 1024
MINIMAL_PATH = "/usr/bin:/bin"
REQUEST_ID = re.compile(r"compress-[0-9a-f]{24}")
STATUSES = [
    "requested",
    "queued",
    "encoding",
    "encoded_pending_install",
    "installed",
    "skipped",
    "attention",
    "failed",
]
NOTE = "Managed lanes decide eligibility; skipped is common."


def validate_settings(settings):
    for key in ("python", "script", "state_dir"):
        value = settings.get(key)
        if value is None and not settings.get("enabled", False):
            continue
        if not isinstance(value, str) or "\x00" in value or not Path(value).is_absolute():
            raise ValueError(f"Compression requires an explicit absolute {key} path when enabled")
    timeout = settings.get("timeout_s")
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise ValueError("Compression timeout must be greater than zero and at most 60 seconds")
    if settings.get("enabled", False):
        if not all(Path(settings[key]).is_file() for key in ("python", "script")):
            raise ValueError("Compression python/script must be existing regular files")


def valid_path(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 1024
        and "\x00" not in value
        and Path(value).is_absolute()
    )


def invalid():
    return CapabilityError("compress_invalid", "Compression command returned an invalid response")


def validate_receipt(data):
    if data == {"status": "not_found"}:
        return data
    if (
        set(data) != {"request_id", "path", "status", "lane"}
        or not isinstance(data.get("request_id"), str)
        or not REQUEST_ID.fullmatch(data["request_id"])
        or data.get("status") != "requested"
        or data.get("lane") != "compression"
        or not valid_path(data.get("path"))
    ):
        raise invalid()
    return data


def validate_status(data, request_id):
    updated = data.get("updated_at")
    if (
        data.get("request_id") != request_id
        or data.get("status") not in STATUSES
        or not valid_path(data.get("path"))
        or not (
            (isinstance(updated, str) and 1 <= len(updated) <= 100 and "\x00" not in updated)
            or (type(updated) in (int, float) and math.isfinite(updated))
        )
        or (
            "reason" in data
            and (
                not isinstance(data["reason"], str)
                or not 1 <= len(data["reason"]) <= 200
                or "\x00" in data["reason"]
            )
        )
    ):
        raise invalid()
    return {
        key: data[key]
        for key in ("request_id", "path", "status", "reason", "updated_at")
        if key in data
    }


class MediaCompress:
    def __init__(self, config):
        self.config = config

    def settings(self):
        settings = self.config.backends["media_compress"]
        if not settings.get("enabled", False):
            raise CapabilityError(
                "compress_unavailable", "Per-title compression is not enabled yet"
            )
        try:
            validate_settings(settings)
        except (ValueError, OSError):
            raise CapabilityError(
                "compress_down", "Compression command files unavailable"
            ) from None
        return settings

    def probe(self):
        if not self.config.backends["media_compress"].get("enabled", False):
            return {"status": "disabled", "reason": "Per-title compression is not enabled yet"}
        try:
            self.settings()
            return {"status": "ok", "reason": "command files present; command not executed"}
        except CapabilityError:
            return {"status": "down", "reason": "command configuration or files invalid"}

    async def run(self, command, option, value):
        # These arguments are internal, never a generic subprocess capability.
        if (command, option) not in {
            ("admit", "--tmdb-id"),
            ("admit", "--path"),
            ("status", "--request-id"),
        }:
            raise ValueError("Unknown compression command")
        settings = self.settings()
        process = None
        tasks = []

        async def read(stream):
            body = bytearray()
            while chunk := await stream.read(8192):
                body.extend(chunk)
                if len(body) > MAX_OUTPUT:
                    raise CapabilityError(
                        "compress_output_limit", "Compression command output too large"
                    )
            return bytes(body)

        try:
            async with asyncio.timeout(settings["timeout_s"]):
                process = await asyncio.create_subprocess_exec(
                    settings["python"],
                    settings["script"],
                    command,
                    option,
                    str(value),
                    env={"PATH": MINIMAL_PATH, "MEDIA_CTRL_STATE_DIR": settings["state_dir"]},
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                tasks = [
                    asyncio.create_task(read(process.stdout)),
                    asyncio.create_task(read(process.stderr)),
                    asyncio.create_task(process.wait()),
                ]
                stdout, _, code = await asyncio.gather(*tasks)
            if code != 0:
                raise CapabilityError(
                    "compress_rejected" if code == 2 else "compress_failed",
                    "Compression command rejected the request"
                    if code == 2
                    else "Compression command failed",
                )
            from .server import load_json

            data = load_json(stdout)
            if not isinstance(data, dict):
                raise invalid()
            return data
        except TimeoutError:
            raise CapabilityError("compress_timeout", "Compression command timed out") from None
        except (ValueError, UnicodeError, RecursionError):
            raise invalid() from None
        except OSError:
            raise CapabilityError("compress_down", "Compression command unavailable") from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if process:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
