"""Declarative, bounded entity extraction for shared-memory notes."""

import re
from dataclasses import asdict, dataclass

from .schema import obj, string

DOMAIN_SCOPES = {
    "ops": "ops:read",
    "media": "media:read",
    "home": "home:read",
    "tasks": "tasks:read",
    "calendar": "calendar:read",
    "books": "media:read",
    "console": "console:control",
    "dns": "ops:read",
    "storage": "ops:read",
    "git": "git:read",
    "routines": "routines:read",
    "general": "memory:read",
}
ENTITY_KEYS = frozenset(
    {
        "service",
        "host",
        "probe",
        "accessory_id",
        "scene_id",
        "state_id",
        "state",
        "name",
        "title",
        "tmdb_id",
        "rating_key",
        "repo",
        "routine",
        "recipe_id",
        "path",
        "label",
        "disk",
        "book_id",
        "event_id",
        "reminder_id",
        "todo_id",
        "title_id",
    }
)
UNDECLARED = object()
NOTE_SCHEMA = {
    "type": "array",
    "maxItems": 3,
    "items": obj(
        {"id": string(40), "title": string(120), "body": string(300), "stale": {"type": "boolean"}},
        ("id", "title", "body", "stale"),
    ),
}


@dataclass(frozen=True)
class MemorySubject:
    domain: str
    arg_keys: tuple[str, ...] = ()
    result_paths: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    fallback_subjects: tuple[str, ...] = ()

    def __post_init__(self):
        if self.domain not in DOMAIN_SCOPES:
            raise ValueError("Unknown memory subject domain")
        if any(
            type(v) is not tuple or len(v) > 16
            for v in (self.arg_keys, self.result_paths, self.subjects, self.fallback_subjects)
        ):
            raise ValueError("Memory subject selectors must be bounded tuples")
        if any(
            not re.fullmatch(
                r"[a-zA-Z_][a-zA-Z0-9_]*(?:\[\])?(?:\.[a-zA-Z_][a-zA-Z0-9_]*(?:\[\])?){0,5}", p
            )
            for p in (*self.arg_keys, *self.result_paths)
        ):
            raise ValueError("Invalid memory subject path")
        if any(
            not isinstance(v, str) or not 1 <= len(v) <= 200
            for v in (*self.subjects, *self.fallback_subjects)
        ):
            raise ValueError("Invalid static memory subject")

    def manifest(self):
        return asdict(self)

    def extract(self, args, result):
        """Walk only declared paths, at most 256 nodes per path and 300 subjects."""
        subjects = list(self.subjects)
        for root, paths in ((args, self.arg_keys), (result, self.result_paths)):
            before = len(subjects)
            for path in paths:
                nodes = [root]
                for segment in path.split("."):
                    many = segment.endswith("[]")
                    key = segment[:-2] if many else segment
                    following = []
                    for node in nodes[:256]:
                        value = node.get(key) if isinstance(node, dict) else None
                        if many and isinstance(value, list):
                            following.extend(value[: 256 - len(following)])
                        elif not many and value is not None:
                            following.append(value)
                        if len(following) >= 256:
                            break
                    nodes = following[:256]
                subjects.extend(str(v)[:200] for v in nodes if type(v) in (str, int))
            if root is args and len(subjects) == before:
                subjects.extend(self.fallback_subjects)
        values = list(dict.fromkeys(s for s in subjects if s.strip()))[:300]
        return values or list(self.fallback_subjects)


def validate_declaration(cap):
    if cap.memory is UNDECLARED:
        raise ValueError("Every capability must explicitly declare memory or memory=None")
    if cap.memory is not None and not isinstance(cap.memory, MemorySubject):
        raise ValueError("Invalid memory subject declaration")
    if cap.memory is None and ENTITY_KEYS & cap.input_schema.get("properties", {}).keys():
        if not isinstance(cap.memory_optout, str) or not cap.memory_optout.strip():
            raise ValueError("Entity tools need a memory subject or explicit opt-out reason")
