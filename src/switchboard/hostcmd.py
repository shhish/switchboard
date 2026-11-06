"""Internal fixed host commands: no shell, inherited environment or unbounded pipes."""

import asyncio
import os
import re

LAUNCHCTL = "/bin/launchctl"
DOCKER = "/usr/local/bin/docker"
PS = "/bin/ps"
SYSCTL = "/usr/sbin/sysctl"
VM_STAT = "/usr/bin/vm_stat"
STATE_FORMAT = "{{json .State}} {{.RestartCount}}"
SSH = "/usr/bin/ssh"
OUTPUT_LIMIT = 256 * 1024


def runtime_id(value):
    # Leading '-' would be interpreted as an option by Docker.
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,119}", value)


def switchboard_label(value):
    """Refuse self-restarts, including a deployment's custom launchd label."""
    return bool(re.search(r"(^|\.)switchboard(\.|$)", value)) or value == os.environ.get(
        "SWITCHBOARD_LABEL"
    )


def remote_token(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]{1,40}", value) is not None


def valid_request(request):
    if not isinstance(request, str):
        return False
    tokens = request.split(" ")
    if not 1 <= len(tokens) <= 3 or not all(remote_token(t) for t in tokens):
        return False
    command, *args = tokens
    return (
        command in {"version", "snapshot", "checks", "refresh-updates"}
        and not args
        or command == "status"
        and len(args) <= 1
        or command == "restart"
        and len(args) == 1
        or command == "logs"
        and len(args) == 2
        and args[1].isascii()
        and args[1].isdigit()
        and 1 <= int(args[1]) <= 400
    )


def ssh_argv(host, request):
    return [
        SSH,
        "-F",
        "/dev/null",
        "-i",
        host["key_file"],
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + host["known_hosts_file"],
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "LogLevel=ERROR",
        host["user"] + "@" + host["address"],
        request,
    ]


def valid_argv(argv, remote_hosts=None):
    if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
        return False
    if argv[:1] == [SSH]:
        # Reconstruct the entire vector from validated configuration. No extra options,
        # alternative ordering, identities, destinations or request whitespace escape.
        from .config import validate_remote_hosts

        try:
            hosts = validate_remote_hosts(remote_hosts or {})
        except ValueError:
            return False
        return bool(
            valid_request(argv[-1])
            and any(argv == ssh_argv(host, argv[-1]) for host in hosts.values())
        )
    if argv in (
        [LAUNCHCTL, "list"],
        [PS, "-Axo", "pid,pcpu,rss,comm"],
        [VM_STAT],
    ):
        return True
    if len(argv) == 3 and argv[:2] == [SYSCTL, "-n"]:
        return argv[2] in {"kern.boottime", "hw.memsize", "vm.swapusage"}
    if argv[0:1] == [LAUNCHCTL] and len(argv) in (3, 4):
        prefix = [LAUNCHCTL, "print"] if len(argv) == 3 else [LAUNCHCTL, "kickstart", "-k"]
        target = argv[-1].split("/")
        return (
            argv[:-1] == prefix
            and len(target) == 3
            and target[:2] == ["gui", str(os.getuid())]
            and bool(runtime_id(target[2]))
            and not (len(argv) == 4 and switchboard_label(target[2]))
        )
    if argv[:1] == [DOCKER] and runtime_id(argv[-1]):
        return (
            len(argv) == 3
            and argv[1] == "restart"
            or len(argv) == 5
            and argv[1:4] == ["inspect", "--format", STATE_FORMAT]
            or len(argv) == 5
            and argv[1:3] == ["logs", "--tail"]
            and argv[3].isdigit()
            and 1 <= int(argv[3]) <= 800
        )
    return False


async def run(argv, timeout_s=10, *, remote_hosts=None):
    """Return (code, stdout, stderr); -1 means timeout/output limit/unavailable.

    Cancellation kills and reaps the child. Both pipes share a 256 KiB budget;
    reaching it stops the child rather than buffering its remaining output.
    """
    if not valid_argv(argv, remote_hosts):
        raise ValueError("Unsupported host command")
    process = None
    chunks = [bytearray(), bytearray()]
    size = 0

    async def read(stream, index):
        nonlocal size
        while data := await stream.read(8192):
            remaining = max(0, OUTPUT_LIMIT - size)
            chunks[index].extend(data[:remaining])
            size += len(data)
            if size > OUTPUT_LIMIT:
                raise OverflowError

    try:
        async with asyncio.timeout(timeout_s):
            process = await asyncio.create_subprocess_exec(
                *argv,
                env={"PATH": "/usr/bin:/bin"},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=(
                    asyncio.subprocess.STDOUT
                    if argv[:2] == [DOCKER, "logs"]
                    else asyncio.subprocess.PIPE
                ),
            )
            async with asyncio.TaskGroup() as group:
                group.create_task(read(process.stdout, 0))
                if process.stderr is not None:
                    group.create_task(read(process.stderr, 1))
                group.create_task(process.wait())
            return process.returncode, *(part.decode("utf-8", "replace") for part in chunks)
    except (OSError, TimeoutError, ExceptionGroup):
        return -1, "", "Host command unavailable or exceeded limit"
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

            # Drain bounded pipe buffers after killing; Process.wait can otherwise
            # deadlock when a StreamReader paused its transport at the high watermark.
            async def discard(stream):
                if stream is not None:
                    while await stream.read(8192):
                        pass

            await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())
