"""Read-only inventory of configured checkouts and their worktrees."""

import asyncio
import itertools
import re
import time
from pathlib import Path

from . import hostcmd
from .config import validate_git
from .domains.common import sanitize_text
from .errors import CapabilityError

MAX_CHECKOUTS = 100


class LocalGit:
    def __init__(self, config):
        self.config = config
        self.roots = [Path(p) for p in validate_git(config.git)["roots"]]

    def safe(self, path):
        try:
            path = Path(path).resolve(strict=True)
            if any(path.is_relative_to(root) and path != root for root in self.roots):
                return path
        except (OSError, ValueError, RuntimeError):
            pass
        raise CapabilityError("resource_denied", "Git checkout or metadata leaves configured roots")

    def metadata(self, path):
        """Validate the gitfile before git reads configuration outside an admitted root."""
        dot = self.safe(path / ".git")
        if dot.is_file():
            if dot.stat().st_size > 4096:
                raise CapabilityError("resource_denied", "Invalid git metadata file")
            value = dot.read_text().strip()
            if not value.startswith("gitdir: "):
                raise CapabilityError("resource_denied", "Invalid git metadata file")
            dot = self.safe(path / value[8:])
        if not dot.is_dir():
            raise CapabilityError("resource_denied", "Invalid git metadata directory")
        common = dot / "commondir"
        if common.exists():
            common = self.safe(common)
            if common.stat().st_size > 4096:
                raise CapabilityError("resource_denied", "Invalid common git directory")
            self.safe(dot / common.read_text().strip())
        return dot

    async def command(self, path, tokens, *, unborn=False):
        self.safe(path)
        self.metadata(path)
        # core.worktree can redirect an otherwise admitted .git directory. Ask
        # Git where it would operate before any inventory command reads that tree.
        code, top, _ = await hostcmd.run(hostcmd.git_argv(path, ["rev-parse", "--show-toplevel"]))
        if code:
            raise CapabilityError("backend_down", "Local git checkout could not be validated")
        if self.safe(top.rstrip("\n")) != path:
            raise CapabilityError(
                "resource_denied", "Git working tree differs from its admitted path"
            )
        code, stdout, _ = await hostcmd.run(hostcmd.git_argv(path, tokens))
        if code and not (unborn and code == 128):
            raise CapabilityError("backend_down", "Local git command failed or exceeded its limit")
        return stdout if not code else ""

    async def status_one(self, path, name):
        dot = self.metadata(path)
        text = await self.command(path, ["status", "--porcelain=v2", "--branch"])
        branch = "detached"
        upstream = ""
        ahead = behind = 0
        counts = dict.fromkeys(("staged", "unstaged", "untracked", "conflicted"), 0)
        for line in text.splitlines():
            if line.startswith("# branch.head "):
                branch = line[14:]
                if branch == "(detached)":
                    branch = "detached"
            elif line.startswith("# branch.upstream "):
                upstream = line[18:]
            elif line.startswith("# branch.ab "):
                match = re.fullmatch(r"# branch.ab \+(\d+) -(\d+)", line)
                if match:
                    ahead, behind = map(int, match.groups())
            elif line.startswith("? "):
                counts["untracked"] += 1
            elif line.startswith("u "):
                counts["conflicted"] += 1
            elif line.startswith(("1 ", "2 ")):
                xy = line.split(" ", 2)[1]
                counts["staged"] += xy[0] != "."
                counts["unstaged"] += xy[1] != "."
        stash = await self.command(path, ["stash", "list", "--format=%gd"])
        log = await self.command(
            path, ["log", "-1", "--format=" + hostcmd.GIT_LOG_FORMAT], unborn=True
        )
        last = None
        if log:
            try:
                sha, epoch, subject = log.rstrip("\n").split("\x00", 2)
                last = {
                    "sha": sha,
                    "age_s": max(0, int(time.time()) - int(epoch)),
                    "subject": sanitize_text(subject, 300),
                }
            except ValueError:
                raise CapabilityError("backend_invalid", "Invalid local git log response") from None
        root = next(root for root in self.roots if path.is_relative_to(root))
        return {
            "repo": name,
            "root": self.roots.index(root),
            "path": str(path.relative_to(root)),
            "branch": sanitize_text(branch, 256),
            "upstream": sanitize_text(upstream, 256),
            "ahead": ahead,
            "behind": behind,
            **counts,
            "stash_count": len(stash.splitlines()),
            "last_commit": last,
            "merge_in_progress": (dot / "MERGE_HEAD").exists(),
            "rebase_in_progress": (dot / "rebase-merge").exists()
            or (dot / "rebase-apply").exists(),
        }

    async def status(self, repo=None):
        checkouts = {}
        for root in self.roots:
            if not root.is_dir():
                raise CapabilityError("backend_down", "Configured git root is unavailable")
            entries = list(itertools.islice(root.iterdir(), 2001))
            if len(entries) > 2000:
                raise CapabilityError("backend_invalid", "Git root exceeds discovery limit")
            for entry in sorted(entries):
                if repo and entry.name != repo:
                    continue
                if not (entry / ".git").exists():
                    continue
                path = self.safe(entry)
                self.metadata(path)
                text = await self.command(path, ["worktree", "list", "--porcelain"])
                # Porcelain quotes unusual paths using Git's C escaping. Refuse those
                # rather than accidentally interpreting escapes as a different path.
                for line in text.splitlines():
                    if not line.startswith("worktree "):
                        continue
                    value = line[9:]
                    if value.startswith('"'):
                        raise CapabilityError(
                            "resource_denied", "Quoted worktree paths are unsupported"
                        )
                    worktree = self.safe(value)
                    self.metadata(worktree)
                    checkouts[worktree] = entry.name
                    if len(checkouts) > MAX_CHECKOUTS:
                        raise CapabilityError("backend_invalid", "More than 100 git checkouts")
        # A single checkout may have several worktrees; six commands at a time.
        semaphore = asyncio.Semaphore(6)

        async def one(path, name):
            async with semaphore:
                return await self.status_one(path, name)

        rows = await asyncio.gather(*(one(path, name) for path, name in sorted(checkouts.items())))
        return {"status": "ok", "checkouts": rows, "truncated": False}
