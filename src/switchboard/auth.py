"""Per-client files, hashed identities and loopback boundary helpers (from orb_mcp)."""

import hashlib
import hmac
import ipaddress
import os
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

CLIENTS = ("dot", "orb", "openclaw", "claude", "codex", "agy", "deepseek", "cortex", "owner")


SECRET_MAX_BYTES = 1024 * 1024


def read_private(path: Path, max_bytes: int = SECRET_MAX_BYTES) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError("Secret must be an owned regular file with mode 0600")
        if info.st_size > max_bytes:
            raise ValueError("Private file too large")
        chunks, total = [], 0
        while chunk := os.read(fd, min(1024 * 1024, max_bytes + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Private file too large")
        content = b"".join(chunks)
        return content.decode().strip()
    finally:
        os.close(fd)


def read_token(path):
    value = read_private(Path(path))
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Expected a 32-byte lowercase hex token")
    return value


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Expected an owned directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Directory must have mode 0700")


def create_token(directory, client):
    if client not in CLIENTS:
        raise ValueError("Unknown client")
    private_dir(directory)
    path = directory / f"{client}.token"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(secrets.token_hex(32) + "\n")
        output.flush()
        os.fsync(output.fileno())
    return path


class Tokens:
    """Only hashes are retained. Files are reloaded to make revocation immediate."""

    def __init__(self, directory):
        self.directory = directory
        self.hashes = {}

    def refresh(self):
        self.hashes = {}
        try:
            info = self.directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()
            ):
                return
        except OSError:
            return
        for client in CLIENTS:
            try:
                value = read_token(self.directory / f"{client}.token")
                self.hashes[client] = hashlib.sha256(value.encode()).digest()
            except (OSError, ValueError, UnicodeError):
                continue
        # Duplicate credentials must never grant an arbitrary identity.
        duplicates = {v for v in self.hashes.values() if list(self.hashes.values()).count(v) > 1}
        self.hashes = {k: v for k, v in self.hashes.items() if v not in duplicates}

    def authenticate(self, authorization):
        self.refresh()
        if not authorization.startswith("Bearer "):
            return None
        digest = hashlib.sha256(authorization[7:].encode()).digest()
        matched = None
        for client, expected in self.hashes.items():
            if hmac.compare_digest(expected, digest):
                matched = client
        return matched


def loopback_host(host):
    try:
        parsed = urlsplit("http://" + host)
        if (
            parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            return False
        return bool(parsed.hostname and ipaddress.ip_address(parsed.hostname).is_loopback)
    except ValueError:
        return False


def local_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or not loopback_host(parsed.netloc)
    ):
        raise ValueError("MCP URL must be http://<loopback-IP>:<port>/mcp")
    return url
