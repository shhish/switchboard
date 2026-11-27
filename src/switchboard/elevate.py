"""Relay phone-signed bytes to the root helper; never execute commands here."""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import tempfile
import time
from pathlib import Path

from .auth import private_dir
from .errors import CapabilityError
from .execution import Execution

DEFAULT_SOCKET = "/var/run/com.switchboard.elevate.sock"
MAX_MESSAGE = 512 * 1024
TOKEN = re.compile(r"\{([a-z][a-z0-9_]{0,30})\}")


class ElevateError(CapabilityError):
    def __init__(self, message="Root helper unavailable"):
        super().__init__("elevate_unavailable", message)


class NotInstalled(ElevateError):
    def __init__(self):
        super().__init__("Root helper not installed")


class Refused(ElevateError):
    def __init__(self, message):
        super().__init__(message)


def settings(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"socket"}
        or not isinstance(value.get("socket", DEFAULT_SOCKET), str)
        or not value.get("socket", DEFAULT_SOCKET).startswith("/")
        or "\0" in value.get("socket", DEFAULT_SOCKET)
    ):
        raise ValueError("Invalid elevate socket")
    return {"socket": value.get("socket", DEFAULT_SOCKET)}


class Elevate:
    def __init__(self, socket=DEFAULT_SOCKET):
        self.socket = socket

    async def call(self, request):
        wire = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(wire) > MAX_MESSAGE:
            raise ElevateError("Root helper request too large")
        writer = None
        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_unix_connection(self.socket, limit=MAX_MESSAGE)
                writer.write(wire)
                await writer.drain()
                line = await reader.readline()
                if not line.endswith(b"\n") or len(line) > MAX_MESSAGE:
                    raise ElevateError("Invalid root helper response")
                reply = json.loads(line)
                if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                    raise ElevateError("Invalid root helper response")
                if not reply["ok"]:
                    error = reply.get("error")
                    if not isinstance(error, str) or len(error) > 2000:
                        raise ElevateError("Invalid root helper response")
                    raise Refused(error)
                return reply
        except FileNotFoundError:
            raise NotInstalled() from None
        except (OSError, TimeoutError):
            raise ElevateError() from None
        except (ValueError, UnicodeError):
            raise ElevateError("Invalid root helper response") from None
        finally:
            if writer is not None:
                writer.close()
                # Closing the transport must not extend the ten-second request budget.

    async def status(self):
        reply = await self.call({"op": "status"})
        if (
            not all(isinstance(reply.get(k), str) for k in ("version", "machine", "home", "owner"))
            or not all(isinstance(reply.get(k), list) for k in ("keys", "recipes"))
            or not all(isinstance(r, dict) for r in reply["keys"] + reply["recipes"])
        ):
            raise ElevateError("Invalid root helper status")
        return reply

    async def run(self, payload_b64, signature):
        reply = await self.call({"op": "run", "payload": payload_b64, "signature": signature})
        if reply.get("state") not in {"running", "done"} or not isinstance(reply.get("job"), str):
            raise ElevateError("Invalid root helper response")
        return reply

    async def result(self, job):
        reply = await self.call({"op": "result", "job": job})
        if (
            reply.get("state") not in {"running", "done", "failed", "unknown", "lost"}
            or ("exit" in reply and type(reply["exit"]) is not int)
            or ("output" in reply and not isinstance(reply["output"], str))
            or (
                reply.get("state") in {"done", "failed"}
                and (type(reply.get("exit")) is not int or not isinstance(reply.get("output"), str))
            )
        ):
            raise ElevateError("Invalid root helper response")
        return reply


def invalid(message="Invalid root action"):
    raise CapabilityError("invalid_arguments", message)


def steps(value):
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 5
        or any(
            not isinstance(argv, list)
            or not 1 <= len(argv) <= 64
            or any(not isinstance(s, str) or "\0" in s or len(s.encode()) > 4096 for s in argv)
            or not argv[0].startswith("/")
            for argv in value
        )
    ):
        invalid()
    return value


def timeout(value, default):
    if value is None:
        return default
    if type(value) is not int:
        invalid()
    return min(max(value, 5), 7200)


def render(recipe, args, status):
    declared = recipe.get("args", [])
    if any(a.get("name") in {"home", "owner"} for a in declared):
        invalid("Recipe argument not allowed")
    if not isinstance(args, dict) or set(args) != {a["name"] for a in declared}:
        invalid("Recipe requires exactly its declared arguments")
    for arg in declared:
        value = args[arg["name"]]
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 1024
            or "\0" in value
            or "/../" in value
            or value.endswith("/..")
        ):
            invalid("Recipe argument not allowed")
        if "choices" in arg:
            accepted = value in arg["choices"]
        else:
            try:
                accepted = (
                    re.fullmatch(arg["pattern"].replace("{home}", re.escape(status["home"])), value)
                    is not None
                )
            except (KeyError, re.error):
                accepted = False
        if not accepted:
            invalid("Recipe argument not allowed")
    # The helper's own values always win, exactly as the helper renders them.
    fills = {**args, "home": status["home"], "owner": status["owner"]}

    def fill(match):
        if match[1] not in fills:
            invalid("Recipe names an unknown token")
        return fills[match[1]]

    return steps([[TOKEN.sub(fill, s) for s in argv] for argv in recipe["steps"]])


def key(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "x963"}
        or not isinstance(value["name"], str)
        or re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", value["name"]) is None
        or not isinstance(value["x963"], str)
    ):
        invalid("Invalid pairing key")
    try:
        raw = base64.b64decode(value["x963"], validate=True)
    except ValueError:
        invalid("Invalid pairing key")
    if len(raw) != 65 or raw[0] != 4:
        invalid("Invalid pairing key")
    return dict(value)


def fingerprint(value):
    raw = base64.b64decode(value["x963"], validate=True)
    code = hashlib.sha256(raw).hexdigest()[:16]
    return " ".join(code[n : n + 4] for n in range(0, 16, 4))


def read_update(path, limit):
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        invalid("Update requires an absolute file path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                invalid("Update file missing or too large")
            data = source.read(limit + 1)
        if len(data) > limit:
            invalid("Update file missing or too large")
        return data
    except OSError:
        invalid("Update file missing or too large")


def validate_root(action, status):
    """Canonicalize using the helper's current catalog; summary is always server-authored."""
    if status is None:
        invalid("Root helper not installed")
    canonical = {"type": "run_as_root"}
    if "recipe" in action:
        if set(action) - {"type", "recipe", "args"}:
            invalid()
        recipe = next((r for r in status["recipes"] if r["id"] == action["recipe"]), None)
        if recipe is None:
            invalid("Unknown root recipe")
        values = action.get("args", {})
        rendered = render(recipe, values, status)
        canonical.update(
            kind="recipe",
            recipe=recipe["id"],
            args=values,
            steps=rendered,
            timeout=timeout(recipe.get("timeout", 300), 300),
        )
        summary = f"Runs as root on this Mac: {recipe['title']}: "
    elif "steps" in action:
        if set(action) - {"type", "steps", "timeout"}:
            invalid()
        canonical.update(
            kind="custom",
            steps=steps(action["steps"]),
            timeout=timeout(action.get("timeout", 600), 600),
        )
        summary = "Runs a custom command as root on this Mac: "
    elif action.get("manage") == "add-key":
        if set(action) != {"type", "manage", "key"}:
            invalid()
        public = key(action["key"])
        canonical.update(kind="add-key", key=public, steps=[], timeout=10)
        summary = (
            f"Pairs an iPhone for root commands on this Mac: {public['name']} "
            f"({fingerprint(public)})"
        )
    elif action.get("manage") in {"update-recipes", "update-helper"}:
        if set(action) != {"type", "manage", "path"}:
            invalid()
        kind = action["manage"]
        data = read_update(action["path"], 1_000_000 if kind == "update-recipes" else 30_000_000)
        canonical.update(
            kind=kind,
            steps=[],
            timeout=10,
            file={"path": action["path"], "sha256": hashlib.sha256(data).hexdigest()},
        )
        summary = (
            f"Updates root {kind.removeprefix('update-')} on this Mac: {action['path']} "
            f"(sha256 {canonical['file']['sha256']})"
        )
        if kind == "update-recipes":
            try:
                document = json.loads(data)
                recipes = document["recipes"]
                if not isinstance(recipes, list) or any(
                    not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in recipes
                ):
                    invalid("Invalid recipes file")
                new = {r["id"]: r for r in recipes}
                if len(new) != len(recipes):
                    invalid("Duplicate recipe IDs")
                old = {r["id"]: r for r in status["recipes"]}
            except (ValueError, TypeError, KeyError):
                invalid("Invalid recipes file")
            changes = {
                "added": sorted(new.keys() - old.keys()),
                "removed": sorted(old.keys() - new.keys()),
                "changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
            }
            canonical["changes"] = changes
            summary += "; " + "; ".join(
                f"{k}: {', '.join(v) or 'none'}" for k, v in changes.items()
            )
    else:
        invalid()
    if canonical["steps"]:
        summary += "; ".join(shlex.join(argv) for argv in canonical["steps"])
    canonical["summary"] = summary
    canonical["machine"] = status["machine"]
    return canonical


def sign_action(action, ask_id, client, reason):
    payload = {
        "v": 1,
        "machine": action["machine"],
        "nonce": secrets.token_hex(16),
        "expires": int(time.time()) + 7 * 86400,
        "kind": action["kind"],
        "steps": action["steps"],
        "ask": ask_id,
        "asker": client,
        "reason": reason,
    }
    for name in ("recipe", "args", "key", "file"):
        if name in action:
            payload[name] = action[name]
    if action["kind"] == "custom":
        payload["timeout"] = action["timeout"]
    wire = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    action["sign_payload"] = base64.b64encode(wire).decode()
    # Leave space for the enclosing run request and its signature.
    if len(action["sign_payload"]) > MAX_MESSAGE - 1000:
        invalid("Root request too large")
    action["nonce"] = payload["nonce"]
    if len(json.dumps(action)) > 400000:
        invalid("Root request too large")


class RootActions:
    """Durable running receipts; recovery queries a nonce, never resubmits a command."""

    def __init__(self, core):
        self.core = core
        self.tasks = {}
        self.lock = asyncio.Lock()

    def schedule(self, ask_id):
        if ask_id not in self.tasks or self.tasks[ask_id].done():
            task = asyncio.create_task(self.poll(ask_id))
            self.tasks[ask_id] = task
            task.add_done_callback(
                lambda done: (
                    self.tasks.pop(ask_id, None) if self.tasks.get(ask_id) is done else None
                )
            )

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()

    def save(self, ask_id, spec):
        with self.core.storage.db as db:
            db.execute("UPDATE board_asks SET spec=? WHERE ask_id=?", (json.dumps(spec), ask_id))

    def audit(self, row, action):
        self.core.audit.append(
            client=row["answered_by"],
            capability="board_answer",
            tier="write",
            decision="allowed",
            outcome=action["result"],
            args={
                "ask_id": row["ask_id"],
                "kind": action["kind"],
                "recipe": action.get("recipe"),
                "result": action["result"],
                "exit": action.get("exit"),
            },
            ctx=Execution.new("system"),
        )

    async def submit(self, row, spec, signature):
        action = spec["action"]
        if not signature:
            action.update(result="failed", message="Approve it in the Server app with Face ID")
            return
        action.update(
            result="running",
            job=action["nonce"],
            deadline=time.time() + action["timeout"] + 120,
            message="Running",
        )
        # Save before crossing the socket: cancellation or restart cannot lose the nonce.
        self.save(row["ask_id"], spec)
        try:
            # A busy helper refuses before spending the approval, so waiting is safe.
            for attempt in range(30):
                try:
                    reply = await self.core.elevate.run(action["sign_payload"], signature)
                    break
                except Refused as exc:
                    if str(exc) != "busy, try again" or attempt == 29:
                        raise
                    await asyncio.sleep(2)
            if reply["job"] != action["job"]:
                raise ElevateError("Invalid root helper job")
        except Refused as exc:
            action.update(result="failed", message=str(exc))
        except ElevateError:
            # Delivery may have succeeded. Recover through result; never replay run.
            action["message"] = "Checking root helper result"
        self.save(row["ask_id"], spec)

    async def refresh(self, ask_id):
        async with self.lock:
            board = self.core.board
            row = board.ask_row(ask_id)
            spec = json.loads(row["spec"])
            action = spec.get("action", {})
            if action.get("type") != "run_as_root":
                return
            if action.get("result") == "running":
                try:
                    reply = await self.core.elevate.result(action["job"])
                except ElevateError:
                    reply = {"state": "running"}
                state = reply["state"]
                if state == "running" and time.time() < action["deadline"]:
                    self.schedule(ask_id)
                    return
                action["result"] = (
                    "applied" if state == "done" and reply.get("exit") == 0 else "failed"
                )
                action["message"] = {
                    "done": "Completed",
                    "failed": "Command failed",
                    "lost": "Root helper lost the job",
                    "unknown": "Root helper does not know this job",
                }.get(state, "Timed out waiting for root helper")
                if "exit" in reply:
                    action["exit"] = reply["exit"]
                action["output"] = reply.get("output", "")[-4000:]
                self.save(ask_id, spec)
                self.audit(row, action)
            if (
                "job" in action
                and action.get("result") != "running"
                and not action.get("commented")
            ):
                await self.comment(row, spec)

    async def comment(self, row, spec):
        action = spec["action"]
        board = self.core.board
        repo, number = board.reference(row["card"])
        path = f"{board.git.path(repo)}/issues/{number}/comments"
        marker = f"_Root result {row['ask_id']}._"
        # Recover a post whose reply was lost, without duplicating its card comment.
        comments, _ = await board.api.pages(path)
        if any(marker in c.get("body", "") for c in comments):
            action["commented"] = True
            self.save(row["ask_id"], spec)
            return
        # A long card hides older comments; posting again risks only a rare duplicate,
        # while waiting for a receipt that can't be seen would poll forever.
        tail = action.get("output", "")[-1500:]
        fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", tail)), default=3))
        body = (
            f"Root command {action['result']}: {action['message']}. "
            f"Exit code: {action.get('exit', 'unavailable')}."
        )
        if tail:
            body += f"\n\n{fence}\n{tail}\n{fence}"
        body += "\n\n" + marker
        await board.api.request(path, method="POST", payload={"body": body})
        action["commented"] = True
        self.save(row["ask_id"], spec)

    async def recover(self):
        for row in list(
            self.core.storage.db.execute("SELECT * FROM board_asks WHERE state='answered'")
        ):
            action = json.loads(row["spec"]).get("action", {})
            if action.get("type") == "run_as_root" and (
                action.get("result") == "running"
                or ("job" in action and not action.get("commented"))
            ):
                try:
                    await self.refresh(row["ask_id"])
                except CapabilityError:
                    pass  # The saved receipt remains available; a later read retries.
                current = json.loads(self.core.board.ask_row(row["ask_id"])["spec"])["action"]
                if current.get("result") == "running" or not current.get("commented"):
                    self.schedule(row["ask_id"])

    async def poll(self, ask_id):
        while True:
            await asyncio.sleep(2)
            try:
                await self.refresh(ask_id)
            except CapabilityError:
                pass
            action = json.loads(self.core.board.ask_row(ask_id)["spec"])["action"]
            if action["result"] != "running" and action.get("commented"):
                return


def pending_path():
    return Path.home() / ".local/state/switchboard-elevate/pending-key.json"


def write_pending(public):
    path = pending_path()
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump({**public, "created": int(time.time())}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


SETUP_SCRIPT = Path.home() / ".local/share/switchboard/current/scripts/elevate-setup.sh"


def open_setup(code, *, now=None, launch=None):
    """Show macOS's password box for the one-time install, at most every 30 minutes per key.

    Runs a fixed, deployed script as the owner; the password goes to macOS, never here.
    Returns True when a box was opened."""
    now = time.time() if now is None else now
    marker = pending_path().with_name("setup-opened.json")
    try:
        last = json.loads(marker.read_text())
        if last.get("code") == code and now - float(last.get("at", 0)) < 1800:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    if not SETUP_SCRIPT.is_file():
        return False
    (launch or _launch)([str(SETUP_SCRIPT)])
    marker.write_text(json.dumps({"code": code, "at": now}))
    marker.chmod(0o600)
    return True


def _launch(argv):
    import subprocess

    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
