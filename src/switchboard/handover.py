"""Write-only secret hand-over: an owner's answer goes straight to an allowlisted store.

The owner allowlists destinations in `<config>/handover.yaml` (0600). A destination is a
login Keychain generic password under `homelab.` or a 0600 file under
`<config>/secrets/handover/`. Values are never returned, logged, stored elsewhere or
put on a command line; every failure is a fixed message. See docs/SECRET_HANDOVER.md.
"""

import hmac
import os
import pwd
import re
import stat
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import yaml

from .errors import CapabilityError

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SERVICE = re.compile(r"homelab\.[a-z0-9][a-z0-9._-]{0,62}")
CHARSETS = {
    "hex": re.compile(r"[0-9A-Fa-f]+"),
    "alnum": re.compile(r"[0-9A-Za-z]+"),
    "base64url": re.compile(r"[0-9A-Za-z_-]+=*"),
    "printable": None,
}
STORES = ("keychain", "file")
FIELDS = {"label", "store", "service", "charset", "min_length", "max_length"}
MAX_DESTINATIONS = 32
MAX_VALUE = 4096
# `security add-generic-password -w` reads at most 128 characters from stdin and silently
# truncates the rest, so Keychain values are capped here before anything is written.
KEYCHAIN_MAX = 128
SECURITY = "/usr/bin/security"
TIMEOUT_S = 15


def fail(message="Secret hand-over unavailable", code="invalid_arguments"):
    raise CapabilityError(code, message)


def allowlist_path(config_dir):
    return Path(config_dir) / "handover.yaml"


def secret_dir(config_dir):
    return Path(config_dir) / "secrets" / "handover"


def destination(name, entry):
    """Canonical destination or ValueError; agent proposals and the owner's file share this."""
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise ValueError("name")
    if not isinstance(entry, dict) or set(entry) - FIELDS or "label" not in entry:
        raise ValueError("fields")
    label = entry["label"]
    if (
        not isinstance(label, str)
        or not 1 <= len(label) <= 80
        or label != " ".join(label.split())
        or any(unicodedata.category(c).startswith("C") for c in label)
    ):
        raise ValueError("label")
    store = entry.get("store", "file")
    if store not in STORES:
        raise ValueError("store")
    charset = entry.get("charset", "printable")
    if charset not in CHARSETS:
        raise ValueError("charset")
    ceiling = KEYCHAIN_MAX if store == "keychain" else MAX_VALUE
    low, high = entry.get("min_length", 1), entry.get("max_length", ceiling)
    if any(type(v) is not int for v in (low, high)) or not 1 <= low <= high <= ceiling:
        raise ValueError("length")
    result = {
        "name": name,
        "label": label,
        "store": store,
        "charset": charset,
        "min_length": low,
        "max_length": high,
    }
    if store == "keychain":
        service = entry.get("service", f"homelab.{name}")
        if not isinstance(service, str) or SERVICE.fullmatch(service) is None:
            raise ValueError("service")
        result["service"] = service
    elif "service" in entry:
        raise ValueError("service")
    return result


def open_config(config_dir):
    """The config directory as a descriptor: owned by us and private."""
    fd = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise ValueError("config")
    return fd


def open_private_dir(parent_fd, name, *, create=True):
    """One path component below `parent_fd`, never a symlink, owned and 0700."""
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        os.close(fd)
        raise ValueError("directory")
    return fd


def allowlist_bytes(config_dir):
    """The owner's allowlist bytes, read through one checked descriptor; None if absent."""
    dir_fd = open_config(config_dir)
    try:
        try:
            # O_NONBLOCK: a FIFO in its place must not block the event loop on open.
            fd = os.open(
                "handover.yaml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd
            )
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError("allowlist")
            return handle.read(256 * 1024 + 1)
    finally:
        os.close(dir_fd)


def parse_allowlist(raw):
    if raw is None:
        return {}
    if len(raw) > 256 * 1024:
        raise ValueError("allowlist")
    data = yaml.safe_load(raw.decode()) or {}
    if not isinstance(data, dict) or set(data) - {"destinations"}:
        raise ValueError("allowlist")
    rows = data.get("destinations") or {}
    if not isinstance(rows, dict) or len(rows) > MAX_DESTINATIONS:
        raise ValueError("allowlist")
    destinations = {name: destination(name, entry) for name, entry in rows.items()}
    services = [d["service"] for d in destinations.values() if "service" in d]
    if len(services) != len(set(services)):
        raise ValueError("allowlist")
    return destinations


def read_allowlist(config_dir):
    """Owner's destinations, re-read on every use so a removed entry fails closed."""
    return parse_allowlist(allowlist_bytes(config_dir))


def lookup(config_dir, name):
    try:
        found = read_allowlist(config_dir).get(name)
    except (OSError, ValueError, UnicodeError, yaml.YAMLError):
        raise CapabilityError("invalid_arguments", "Secret hand-over unavailable") from None
    if found is None:
        fail("Secret destination is not allowlisted")
    return found


def where(core, dest):
    if dest["store"] == "keychain":
        return f"Keychain item {dest['service']}"
    path = secret_dir(core.config.config_dir) / dest["name"]
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home + "/") else text


def account():
    return pwd.getpwuid(os.getuid()).pw_name


def security(args, data=None):
    """Run /usr/bin/security; the value only ever travels on stdin and output pipes."""
    try:
        return subprocess.run(
            [SECURITY, *args],
            input=data,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        )
    except (OSError, subprocess.SubprocessError):
        return None


def login_keychain():
    """The login Keychain's path, which must also be the default one `add` writes to.

    `find` answers 44 both for a missing item and a missing keychain, so the keychain is
    checked first; every read and delete then names it explicitly.
    """
    path = str(Path.home() / "Library" / "Keychains" / "login.keychain-db")
    info = security(["show-keychain-info", path])
    default = security(["default-keychain"])
    if (
        info is None
        or info.returncode != 0
        or default is None
        or default.returncode != 0
        or default.stdout.strip().strip('"') != path
    ):
        raise ValueError("keychain")
    return path


def keychain_get(service, keychain):
    """Exact stored bytes, None when absent; ValueError when the answer is unknown.

    `-w` prints hex for some values, which is indistinguishable from a hex key, so the
    `-g` line is parsed instead: `password: "text"`, `password: 0x<hex>  "..."` or empty.
    """
    result = security(["find-generic-password", "-a", account(), "-s", service, "-g", keychain])
    if result is None:
        raise ValueError("keychain")
    if result.returncode == 44:
        return None
    if result.returncode != 0:
        raise ValueError("keychain")
    for line in result.stderr.splitlines():
        if line.rstrip() == "password:":
            return b""
        if line.startswith("password: 0x"):
            return bytes.fromhex(line[len("password: 0x") :].split(None, 1)[0])
        if line.startswith('password: "') and line.endswith('"') and len(line) > 11:
            return line[len('password: "') : -1].encode()
    raise ValueError("keychain")


def keychain_home(service, keychain):
    """Which keychain `add -U` would update: it searches the whole list, not just login.

    Returns True when no keychain on the search list holds the item, or the first one
    that does is the login keychain; False when another keychain has it.
    """
    result = security(["find-generic-password", "-a", account(), "-s", service])
    if result is None:
        raise ValueError("keychain")
    if result.returncode == 44:
        return True
    if result.returncode != 0:
        raise ValueError("keychain")
    for line in result.stdout.splitlines():
        if line.startswith("keychain: "):
            return line[len("keychain: ") :].strip().strip('"') == keychain
    raise ValueError("keychain")


def keychain_write(service, value):
    # `-w` with no value prompts twice and reads at most 128 bytes per line; a mismatch
    # can store an empty item while still exiting 0, so callers always read the item back.
    result = security(
        ["add-generic-password", "-U", "-a", account(), "-s", service, "-w"],
        f"{value}\n{value}\n",
    )
    return result is not None and result.returncode == 0


def keychain_has(service, keychain, expected):
    try:
        stored = keychain_get(service, keychain)
    except ValueError:
        return False
    if expected is None:
        return stored is None
    return stored is not None and hmac.compare_digest(stored, expected)


def keychain_safe(raw):
    """Values the stdin prompt can write back byte for byte."""
    return len(raw) <= KEYCHAIN_MAX and all(0x20 <= b <= 0x7E for b in raw)


def exists(core, dest):
    """Whether a value is already saved, without reading it into the result."""
    if dest["store"] == "keychain":
        try:
            return keychain_get(dest["service"], login_keychain()) is not None
        except ValueError:
            return False
    try:
        return stat.S_ISREG((secret_dir(core.config.config_dir) / dest["name"]).lstat().st_mode)
    except OSError:
        return False


def check_value(dest, value):
    """The trimmed value, or a fixed error that never echoes it."""
    if not isinstance(value, str):
        fail("Invalid secret")
    value = value.strip()
    pattern = CHARSETS[dest["charset"]]
    if (
        not dest["min_length"] <= len(value) <= dest["max_length"]
        or any(unicodedata.category(c).startswith("C") for c in value)
        or (pattern is not None and pattern.fullmatch(value) is None)
        or (dest["store"] == "keychain" and not keychain_safe(value.encode()))
    ):
        fail("That doesn't look like the expected secret; nothing was saved")
    return value


def write_file(config_dir, name, value):
    """Atomic 0600 write; every directory is opened by descriptor without following links."""
    config_fd = open_config(config_dir)
    fds = [config_fd]
    temp = None
    try:
        fds.append(open_private_dir(fds[-1], "secrets"))
        fds.append(open_private_dir(fds[-1], "handover"))
        dir_fd = fds[-1]
        try:
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (
            not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
        ):
            raise ValueError("target")
        temp = f".{name}.{os.urandom(8).hex()}.tmp"
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp = None
        os.fsync(dir_fd)
    finally:
        if temp is not None:
            try:
                os.unlink(temp, dir_fd=fds[-1])
            except OSError:
                pass
        for fd in reversed(fds):
            os.close(fd)


def store(core, dest, value):
    """Save and verify. On failure, put back exactly what was there before, and say so."""
    if dest["store"] == "file":
        try:
            write_file(core.config.config_dir, dest["name"], value)
        except (OSError, ValueError):
            raise CapabilityError(
                "backend_error", "Could not save the secret; nothing was recorded"
            ) from None
        return
    service = dest["service"]
    try:
        keychain = login_keychain()
        previous = keychain_get(service, keychain)
        home = keychain_home(service, keychain)
    except ValueError:
        fail("The Keychain is unavailable; nothing was saved", "backend_error")
    if not home:
        # `add -U` would update that other keychain's item instead of the login one.
        fail("Another keychain already has this item; nothing was saved", "backend_error")
    if previous is not None and not keychain_safe(previous):
        # It could not be written back byte for byte if the new value failed to verify.
        fail(
            "The saved Keychain value can't be safely replaced; nothing was saved", "backend_error"
        )
    expected = value.encode()
    if keychain_write(service, value) and keychain_has(service, keychain, expected):
        return
    if previous is None:
        security(["delete-generic-password", "-a", account(), "-s", service, keychain])
    else:
        keychain_write(service, previous.decode("ascii"))
    if keychain_has(service, keychain, previous):
        fail("Could not save the secret in the Keychain; nothing was recorded", "backend_error")
    fail(
        "Could not save the secret, and the Keychain item could not be put back; "
        "check it before relying on it",
        "backend_error",
    )


def add_destination(core, ask_id, dest):
    """Add one canonical destination to the owner's allowlist; False if already there.

    The replacement is built from the same bytes that replace_yaml checks are still on
    disk, so an owner edit made meanwhile (such as a revocation) is never undone.
    """
    from .ask_actions import replace_yaml

    path = allowlist_path(core.config.config_dir)
    raw = allowlist_bytes(core.config.config_dir)
    existing = parse_allowlist(raw)
    entry = {k: v for k, v in dest.items() if k != "name"}
    if existing.get(dest["name"]) == dest:
        return False
    if dest["name"] in existing:
        raise ValueError("exists")
    if len(existing) >= MAX_DESTINATIONS:
        raise ValueError("full")
    if "service" in dest and any(d.get("service") == dest["service"] for d in existing.values()):
        raise ValueError("service")
    rows = {name: {k: v for k, v in d.items() if k != "name"} for name, d in existing.items()}
    rows[dest["name"]] = entry
    text = (
        "# Secrets agents may ask the owner for (docs/SECRET_HANDOVER.md).\n"
        "# Switchboard rewrites this file when you approve a new destination.\n"
        + yaml.safe_dump({"destinations": rows}, sort_keys=True)
    )

    def check(stage):
        if parse_allowlist(stage.read_bytes()) != {**existing, dest["name"]: dest}:
            raise ValueError("mismatch")

    if raw is not None:
        replace_yaml(path, ask_id, raw, text, check)
        return True
    fd, name = tempfile.mkstemp(prefix=".handover.yaml.ask-", dir=path.parent)
    stage = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        check(stage)
        # link() fails if the file appeared meanwhile, so an owner edit is never lost.
        os.link(stage, path)
    finally:
        stage.unlink(missing_ok=True)
    return True
