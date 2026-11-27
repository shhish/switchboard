"""Narrow owner-approved Slate changes; never a general config or policy editor."""

import os
import re
import stat
import tempfile
from pathlib import Path

import yaml

from .config import validate_board
from .errors import CapabilityError
from .policy import Policy


def invalid():
    raise CapabilityError("invalid_arguments", "Invalid or unavailable ask action")


def media_root(core, value):
    """Resolve before approval and again before use; exclude private trees and ancestors."""
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        invalid()
    try:
        if not Path(value).is_absolute():
            invalid()
        root = Path(validate_board({"ask_media_roots": [value]})["ask_media_roots"][0])
        home = Path.home().resolve()
        protected = [
            (home / ".config").resolve(),
            (home / ".ssh").resolve(),
            core.config.config_dir.resolve(),
            core.config.state_dir.resolve(),
            core.config.token_dir.resolve(),
            core.policy.path.resolve().parent,
        ]
        if (
            not root.is_dir()
            or root in {Path("/"), home}
            or any(root.is_relative_to(p) or p.is_relative_to(root) for p in protected)
        ):
            invalid()
        return str(root)
    except (OSError, ValueError, RuntimeError):
        invalid()


def grant_summary(grants):
    """Short enough for the owner's screen: clients with the same scopes share one clause."""
    if len(grants) == 1:
        return f"Gives {grants[0]['client']} the {grants[0]['scope']} permission"
    by_client = {}
    for grant in grants:
        by_client.setdefault(grant["client"], []).append(grant["scope"])
    groups = {}
    for client, scopes in by_client.items():
        groups.setdefault(tuple(scopes), []).append(client)

    def names(items):
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

    clauses = [f"{names(clients)} {names(list(scopes))}" for scopes, clients in groups.items()]
    return "Gives " + "; ".join(clauses)


def validate_action(core, action, *, applying=False, helper_status=None):
    """Return canonical, server-authored approval text, never agent-authored text."""
    kind = action["type"]
    if kind == "run_as_root":
        from .elevate import validate_root

        return validate_root(action, helper_status)
    if kind in {"grant_scope", "grant_scopes"}:
        if kind == "grant_scope":
            if set(action) != {"type", "client", "scope"}:
                invalid()
            grants = [{"client": action["client"], "scope": action["scope"]}]
        else:
            if set(action) != {"type", "grants"} or not isinstance(action["grants"], list):
                invalid()
            grants = action["grants"]
            if not 2 <= len(grants) <= 8 or any(
                not isinstance(g, dict) or set(g) != {"client", "scope"} for g in grants
            ):
                invalid()
            if len({(g["client"], g["scope"]) for g in grants}) != len(grants):
                invalid()
        clients = core.policy.load().get("clients", {})
        scopes = {cap.scope for cap in core.registry.capabilities.values()}
        for grant in grants:
            client, scope = grant["client"], grant["scope"]
            if (
                client not in clients
                or scope not in scopes
                or scope.startswith("approvals:")
                or scope == "board:answer"
                or (
                    scope == "elevate:pair"
                    and "board:answer" not in clients[client].get("scopes", [])
                )
                or (not applying and scope in clients[client].get("scopes", []))
            ):
                invalid()
        summary = grant_summary(grants)
        canonical = dict(action)
    elif kind == "add_ask_media_root":
        if set(action) != {"type", "path"}:
            invalid()
        path = media_root(core, action["path"])
        # A stored canonical path may not be redirected after the owner saw it.
        if applying and path != action["path"]:
            invalid()
        file = core.config.config_dir / "config.yaml"
        data = yaml.safe_load(file.read_text()) or {}
        roots = validate_board(data.get("board", {}))["ask_media_roots"]
        validate_board({"ask_media_roots": roots + ([] if path in roots else [path])})
        canonical = {"type": kind, "path": path}
        summary = f"Lets questions use pictures from {path}"
    else:
        invalid()
    return {**canonical, "summary": summary}


def insert_scope(text, client, scope):
    """Add one list line under the client's scopes, keeping every other byte (comments too)."""
    lines = text.splitlines(keepends=True)
    client_re = re.compile(rf"^(\s+){re.escape(client)}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        match = client_re.match(line.rstrip("\n"))
        if not match:
            continue
        indent = len(match.group(1))
        for j in range(i + 1, len(lines)):
            current = lines[j].rstrip("\n")
            if current.strip() and len(current) - len(current.lstrip()) <= indent:
                break
            empty = re.match(r"^(\s+)scopes:\s*\[\s*\]\s*(#.*)?$", current)
            if empty:
                lines[j] = f"{empty.group(1)}scopes:\n{empty.group(1)}- {scope}\n"
                return "".join(lines)
            scopes = re.match(r"^(\s+)scopes:\s*(#.*)?$", current)
            if scopes:
                item = next(
                    (
                        lines[k]
                        for k in range(j + 1, len(lines))
                        if lines[k].lstrip().startswith("- ")
                    ),
                    None,
                )
                prefix = item[: len(item) - len(item.lstrip())] if item else scopes.group(1)
                lines.insert(j + 1, f"{prefix}- {scope}\n")
                return "".join(lines)
    raise ValueError("Client scopes not found")


def insert_root(text, root):
    """Add one root under board.ask_media_roots, or append the section, keeping comments."""
    if text.strip() in {"", "{}"}:
        return f"board:\n  ask_media_roots:\n  - {root}\n"
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r"^\s+ask_media_roots:\s*(\[\s*\])?\s*(#.*)?$", line.rstrip("\n")):
            indent = line[: len(line) - len(line.lstrip())]
            if "[" in line:
                lines[i] = f"{indent}ask_media_roots:\n"
            lines.insert(i + 1, f"{indent}- {root}\n")
            return "".join(lines)
    if any(re.match(r"^board:", line) for line in lines):
        raise ValueError("Board section without roots")
    tail = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{tail}board:\n  ask_media_roots:\n  - {root}\n"


def replace_yaml(path, ask_id, original, data, validate):
    """Validate a private staged file, preserve exact old bytes, then atomically replace.
    `data` is the new file text; it must parse to the expected data (checked by callers)."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Invalid target")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.ask-", dir=path.parent)
    stage = Path(name)
    backup = path.with_name(f"{path.name}.before-ask-{ask_id}")
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        validate(stage)
        # Fail rather than overwrite an unrelated edit made while preparing this write.
        if path.lstat() != info or path.read_bytes() != original:
            raise ValueError("Target changed")
        backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(backup_fd, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        stage.unlink(missing_ok=True)


def apply_action(core, ask_id, action):
    """Synchronous under Board's writer lock; all failures produce a safe saved receipt."""
    try:
        raw = {k: v for k, v in action.items() if k not in {"summary", "result", "message"}}
        validated = validate_action(core, raw, applying=True)
        if validated != {k: v for k, v in action.items() if k not in {"result", "message"}}:
            invalid()
        if action["type"] in {"grant_scope", "grant_scopes"}:
            grants = action.get("grants") or [
                {"client": action["client"], "scope": action["scope"]}
            ]
            path = core.policy.path
            original = path.read_bytes()
            data = yaml.safe_load(original)
            text = original.decode()
            added = 0
            for grant in grants:
                scopes = data["clients"][grant["client"]].setdefault("scopes", [])
                if grant["scope"] in scopes:
                    continue
                scopes.insert(0, grant["scope"])  # the text edit adds it first
                text = insert_scope(text, grant["client"], grant["scope"])
                added += 1
            already = added == 0
            if not already:
                # The text edit must mean exactly the intended change, nothing else.
                if yaml.safe_load(text) != data:
                    raise ValueError("Edit does not match")
                replace_yaml(path, ask_id, original, text, lambda p: Policy(p).load())
        else:
            path = core.config.config_dir / "config.yaml"
            original = path.read_bytes()
            data = yaml.safe_load(original) or {}
            board = validate_board(data.get("board", {}))
            roots = board["ask_media_roots"]
            already = action["path"] in roots
            if not already:
                roots.insert(0, action["path"])  # the text edit adds it first
                data["board"] = board
                text = insert_root(original.decode(), action["path"])
                parsed = yaml.safe_load(text) or {}
                if {k: v for k, v in parsed.items() if k != "board"} != {
                    k: v for k, v in data.items() if k != "board"
                } or validate_board(parsed.get("board", {})) != board:
                    raise ValueError("Edit does not match")
                replace_yaml(
                    path,
                    ask_id,
                    original,
                    text,
                    lambda p: validate_board(yaml.safe_load(p.read_text())["board"]),
                )
            core.config.board = validate_board(board)
        return {
            "result": "already" if already else "applied",
            "message": "Already applied" if already else "Applied",
        }
    except Exception:
        # Exception text may contain private file contents, paths or backend details.
        return {"result": "failed", "message": "Could not apply the approved change"}
