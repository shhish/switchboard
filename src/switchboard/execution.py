"""Per-call execution context: who asked, in which chain, and why.

Adapters build one from MCP `_meta` (or CLI/HTTP headers); internal callers
(routines, event rules, the monitor) build one with an exact origin and cause.
Client-supplied values are hints for correlation only: they are length-capped,
pattern-checked and never used for authorization.
"""

import contextvars
import os
import re
import time
from dataclasses import dataclass, replace

# Set while a routine step calls a tool, so HomeRelay writes carry the run as
# their cause and the engine can tell its own echoes from real changes.
ROUTINE_CAUSE = contextvars.ContextVar("routine_cause", default=None)

ORIGINS = ("agent", "owner", "routine", "event_rule", "schedule", "system")
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
HINT = re.compile(r"[A-Za-z0-9._:@/+=-]{1,128}")
SESSION_KEY = "switchboard/session"
TURN_KEY = "switchboard/turn"
CAUSE_KEY = "switchboard/causation"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now=None):
    """Time-sortable 26-character ID (ULID layout) without a dependency."""
    value = (int((time.time() if now is None else now) * 1000) << 80) | int.from_bytes(
        os.urandom(10)
    )
    out = []
    for _ in range(26):
        value, digit = divmod(value, 32)
        out.append(_ALPHABET[digit])
    return "".join(reversed(out))


def new_trace_id():
    return os.urandom(16).hex()


def new_span_id():
    return os.urandom(8).hex()


def hint(value, limit=128):
    """A correlation hint, or None. Never raises on hostile input."""
    if isinstance(value, str) and len(value) <= limit and HINT.fullmatch(value):
        return value
    return None


@dataclass(frozen=True)
class Execution:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    origin: str = "agent"
    causation_id: str | None = None
    session_key: str | None = None
    turn_key: str | None = None
    client_name: str | None = None
    client_version: str | None = None
    # Bit flags for links the client asserted rather than Switchboard observed:
    # 1 = trace_id came from the caller's traceparent, 2 = causation_id came from a hint.
    # Trace views never join other clients' rows through asserted links.
    asserted: int = 0

    @classmethod
    def new(cls, origin="agent", causation_id=None, trace_id=None):
        if origin not in ORIGINS:
            raise ValueError("Invalid execution origin")
        return cls(
            trace_id=trace_id or new_trace_id(),
            span_id=new_span_id(),
            origin=origin,
            causation_id=causation_id,
        )

    @classmethod
    def from_meta(cls, meta):
        """Build from MCP request `_meta`. Unknown or malformed values are ignored."""
        meta = meta if isinstance(meta, dict) else {}
        trace_id, parent = None, None
        traceparent = meta.get("traceparent")
        if isinstance(traceparent, str) and (match := TRACEPARENT.fullmatch(traceparent)):
            if match.group(1) != "0" * 32 and match.group(2) != "0" * 16:
                trace_id, parent = match.group(1), match.group(2)
        info = meta.get(CLIENT_INFO_KEY)
        info = info if isinstance(info, dict) else {}
        cause = hint(meta.get(CAUSE_KEY))
        return cls(
            trace_id=trace_id or new_trace_id(),
            span_id=new_span_id(),
            parent_span_id=parent,
            origin="agent",
            causation_id=cause,
            session_key=hint(meta.get(SESSION_KEY)),
            turn_key=hint(meta.get(TURN_KEY)),
            client_name=hint(info.get("name"), 64),
            client_version=hint(info.get("version"), 64),
            asserted=(1 if trace_id else 0) | (2 if cause else 0),
        )

    @classmethod
    def from_headers(cls, headers):
        """HTTP/CLI adapters: the same hints carried as X-Switchboard-* headers."""
        meta = {
            "traceparent": headers.get("traceparent"),
            SESSION_KEY: headers.get("X-Switchboard-Session"),
            TURN_KEY: headers.get("X-Switchboard-Turn"),
            CAUSE_KEY: headers.get("X-Switchboard-Causation"),
        }
        return cls.from_meta(meta)

    def child(self, origin=None, causation_id=None):
        """A new span in the same trace, e.g. a routine step under its run."""
        return replace(
            self,
            span_id=new_span_id(),
            parent_span_id=self.span_id,
            origin=origin or self.origin,
            causation_id=causation_id or self.causation_id,
        )
