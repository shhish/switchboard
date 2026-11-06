"""Bounded public guidance bundled with this release, never deployment config."""

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import CapabilityError


def release_skills_root():
    """The skills folder of the running release.

    A source checkout runs from src/ (repo/skills sits two levels up). A deployed
    release installs the package into its own .venv, so skills sit beside that venv.
    """
    source = Path(__file__).resolve().parents[2] / "skills"
    if source.is_dir():
        return source
    return Path(sys.prefix).resolve().parent / "skills"


SKILLS_ROOT = release_skills_root()
MAX_SKILL_BYTES = 64 * 1024
MAX_SKILLS = 64
MAX_TOTAL_BYTES = 512 * 1024
MAX_TASK_CHARS = 4096
URI_PREFIX = "switchboard://skills/"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    markdown: str

    @property
    def uri(self):
        return URI_PREFIX + self.name


class SkillCatalog:
    def __init__(self, root=None):
        self.root = SKILLS_ROOT if root is None else root
        self._cache = None

    def load(self):
        """The parsed catalog, re-read only when the skills folder changes.

        Requests run on the event loop, so parsing every skill per request would block it.
        """
        try:
            key = os.stat(self.root, follow_symlinks=False).st_mtime_ns
        except OSError:
            key = None
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, self._read())
        return self._cache[1]

    def _read(self):
        """Inspect only regular SKILL.md files in direct, non-symlink children.

        A release is immutable. Never accept a client path, follow a skill symlink,
        or fall back to the user's installed skills or private configuration.
        """
        skills = {}
        total = 0
        try:
            if self.root.is_symlink():
                return skills
            with os.scandir(self.root) as entries:
                directories = []
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False) and re.fullmatch(
                        r"[a-z0-9][a-z0-9_-]{0,63}", entry.name
                    ):
                        directories.append(Path(entry.path))
                        if len(directories) > MAX_SKILLS:
                            return {}  # Fail closed rather than advertise an arbitrary subset.
            for directory in sorted(directories):
                try:
                    fd = os.open(
                        directory / "SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    )
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SKILL_BYTES:
                            continue
                        data = stream.read(MAX_SKILL_BYTES + 1)
                    if len(data) > MAX_SKILL_BYTES:
                        continue
                    total += len(data)
                    if total > MAX_TOTAL_BYTES:
                        return {}
                    markdown = data.decode("utf-8")
                    lines = markdown.splitlines(keepends=True)
                    if not lines or lines[0].strip() != "---":
                        continue
                    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
                    front = yaml.safe_load("".join(lines[1:end]))
                    description = front.get("description") if isinstance(front, dict) else None
                    body = "".join(lines[end + 1 :]).strip()
                    if (
                        not isinstance(description, str)
                        or not 1 <= len(description) <= 1024
                        or not body
                    ):
                        continue
                    skills[directory.name] = Skill(directory.name, description, body, markdown)
                except Exception:  # noqa: BLE001 - a bad bundled skill is skipped, never a 500
                    continue
        except OSError:
            return {}
        return skills

    def request(self, method, params):
        listing = method.endswith("/list")
        allowed = (
            {"cursor"} if listing else {"name", "arguments"} if method == "prompts/get" else {"uri"}
        )
        if set(params) - allowed or (listing and params.get("cursor") is not None):
            raise CapabilityError("invalid_arguments", "Unexpected guidance parameter or cursor")
        skills = self.load()
        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "arguments": [
                            {
                                "name": "task",
                                "description": "Task to apply this guidance to",
                                "required": False,
                            }
                        ],
                    }
                    for skill in skills.values()
                ]
            }
        if method == "resources/list":
            return {
                "resources": [
                    {
                        "uri": skill.uri,
                        "name": skill.name,
                        "description": skill.description,
                        "mimeType": "text/markdown",
                    }
                    for skill in skills.values()
                ]
            }
        if method == "prompts/get":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if (
                not isinstance(name, str)
                or not isinstance(arguments, dict)
                or set(arguments) - {"task"}
                or (
                    "task" in arguments
                    and (
                        not isinstance(arguments["task"], str)
                        or len(arguments["task"]) > MAX_TASK_CHARS
                    )
                )
            ):
                raise CapabilityError("invalid_arguments", "Invalid skill name or task argument")
            skill = skills.get(name)
            if skill is None:
                raise CapabilityError("not_found", "Unknown public skill")
            text = skill.body
            if "task" in arguments:
                text += "\n\nTask: " + arguments["task"]
            return {
                "description": skill.description,
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise CapabilityError("invalid_arguments", "Skill URI must be a string")
        skill = next((skill for skill in skills.values() if skill.uri == uri), None)
        if skill is None:
            raise CapabilityError("not_found", "Unknown public skill URI")
        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": skill.markdown}]}
