"""Declared capabilities are the single source for tools and documentation."""

import re
from dataclasses import dataclass, field
from typing import Any

from .memory_subjects import NOTE_SCHEMA, UNDECLARED, MemorySubject, validate_declaration
from .names import canonical_name
from .schema import check_schema, obj, string

# Domain results are compact JSON objects; domain-specific schemas refine this envelope.
OUTPUT = {"type": "object"}


@dataclass(frozen=True)
class Capability:
    name: str
    title: str
    description: str
    input_schema: dict
    scope: str
    handler: Any = field(repr=False, compare=False)
    output_schema: dict = field(default_factory=lambda: dict(OUTPUT))
    tier: str = "read"
    timeout_s: float = 15
    idempotent: bool = True
    service: str = "switchboard"
    preflight: Any = field(default=None, repr=False, compare=False)
    # Free-text fields the audit log stores only as {len, sha256}, whatever their length
    # (message bodies, notes, memory text). Short scalars elsewhere are kept verbatim.
    audit_hash_fields: frozenset = field(default=frozenset(), compare=False)
    always_load: bool = False
    max_result_chars: int | None = None
    memory: MemorySubject | None = field(default=UNDECLARED)
    memory_optout: str | None = None

    def __post_init__(self):
        from .domains.common import result_schema

        object.__setattr__(self, "name", canonical_name(self.name))
        validate_declaration(self)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.name):
            raise ValueError("Invalid capability name")
        output = result_schema(
            self.output_schema, takes_limit="limit" in self.input_schema.get("properties", {})
        )
        if self.memory is not None:
            output["properties"]["notes"] = NOTE_SCHEMA
            output["properties"].setdefault("truncated", {"type": "boolean"})
        object.__setattr__(self, "output_schema", output)
        if self.tier not in ("read", "write", "approval"):
            raise ValueError("Invalid tier")
        if self.input_schema.get("additionalProperties") is not False:
            raise ValueError("Inputs must reject unknown properties")
        if self.input_schema.get("type") != "object" or any(
            key in self.input_schema for key in ("oneOf", "anyOf", "allOf")
        ):
            raise ValueError(
                "Inputs must be a single flat object; validate alternatives in handlers"
            )
        if type(self.always_load) is not bool:
            raise ValueError("always_load must be a boolean")
        if self.max_result_chars is not None and (
            type(self.max_result_chars) is not int or not 1 <= self.max_result_chars <= 500_000
        ):
            raise ValueError("max_result_chars must be an integer from 1 to 500000")
        check_schema(self.input_schema)
        check_schema(self.output_schema)

    @property
    def annotations(self):
        return {
            "readOnlyHint": self.tier == "read",
            "destructiveHint": self.tier == "approval",
            "openWorldHint": False,
            "idempotentHint": self.idempotent,
        }

    @property
    def wire_schema(self):
        schema = {**self.input_schema, "properties": dict(self.input_schema["properties"])}
        if self.tier == "approval":
            schema["properties"]["approval_id"] = string(128)
        return schema

    def advertised_output_schema(self):
        """What clients validate against. Approval-tier tools first answer with
        `approval_required`, so that shape must be allowed too. MCP needs a plain
        object schema at the root; status branches enforce each required shape."""
        from .domains.common import ENVELOPE, ERROR

        schema = self.output_schema
        approval = self.tier == "approval" and self.name != "approvals_confirm"
        properties = dict(schema.get("properties", {}))
        status = dict(properties.get("status", {"type": "string"}))
        if "enum" in status:
            status["enum"] = sorted(
                set(status["enum"]) | {"error"} | ({"approval_required"} if approval else set())
            )
        properties["status"] = status
        properties["error"] = ERROR
        variants = [
            {
                "properties": {"status": {"not": {"enum": ["error", "approval_required"]}}},
                "required": schema["required"],
            },
            {
                "properties": {
                    "status": {"enum": ["error"]},
                    "outcome": {
                        "enum": ["error", "blocked", "unavailable", "needs_approval", "not_found"]
                    },
                },
                "required": ["error"],
            },
        ]
        if approval:
            for key in ("approval_id", "expires_at", "summary", "notification_status"):
                properties.setdefault(key, {"type": "string"})
            variants.append(
                {
                    "properties": {
                        "status": {"enum": ["approval_required"]},
                        "outcome": {"enum": ["needs_approval"]},
                    },
                    "required": ["approval_id", "expires_at", "summary"],
                }
            )
        return {
            **schema,
            "properties": properties,
            "required": ["status", *ENVELOPE],
            "oneOf": variants,
        }

    def tool(self):
        meta = {}
        if self.tier == "approval":
            meta["anthropic/requiresUserInteraction"] = True
        if self.always_load:
            meta["anthropic/alwaysLoad"] = True
        if self.max_result_chars is not None:
            meta["anthropic/maxResultSizeChars"] = self.max_result_chars
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.wire_schema,
            "outputSchema": self.advertised_output_schema(),
            "annotations": self.annotations,
            **({"_meta": meta} if meta else {}),
        }

    def manifest(self):
        return {
            **self.tool(),
            "scope": self.scope,
            "tier": self.tier,
            "timeout_s": self.timeout_s,
            "idempotent": self.idempotent,
            "service": self.service,
            "memory": self.memory.manifest() if self.memory else None,
            "memory_optout": self.memory_optout,
        }


class Registry:
    def __init__(self):
        self.capabilities = {}

    def add(self, capability):
        if capability.name in self.capabilities:
            raise ValueError("Duplicate capability")
        self.capabilities[capability.name] = capability
        return capability

    def declare(self, name, title, description, input_schema=None, **metadata):
        def decorate(handler):
            self.add(
                Capability(
                    name, title, description, input_schema or obj(), handler=handler, **metadata
                )
            )
            return handler

        return decorate

    def manifest(self):
        return [self.capabilities[name].manifest() for name in sorted(self.capabilities)]

    def markdown(self):
        from .upstreams.orb import EVENT_NAMES, TOOLS

        lines = [
            "# Capabilities",
            "",
            "Generated by `sb manifest --markdown`; do not edit.",
            "",
            "| Name | Scope | Tier | Backend | Use |",
            "| --- | --- | --- | --- | --- |",
        ]
        for entry in self.manifest():
            lines.append(
                f"| `{entry['name']}` | `{entry['scope']}` | {entry['tier']} | "
                f"{entry['service']} | {entry['description']} |"
            )
        lines.extend(
            [
                "",
                "## Legacy name map",
                "",
                "Dotted aliases canonicalize before policy, approval binding and audit. Scopes",
                "are unchanged. Federated `orb_*` names are unchanged.",
                "",
                "| Old alias | Canonical name |",
                "| --- | --- |",
            ]
        )
        for name in sorted(self.capabilities):
            lines.append(f"| `{name.replace('_', '.', 1)}` | `{name}` |")
        lines.extend(
            [
                "",
                "## Federated MCP contracts",
                "",
                "Orb tools are fetched from the hub per mapped client, retaining its schemas,",
                "annotations and results. MCP federation is outside the local capability manifest.",
                "Calls require `orb:use` plus an `upstreams.orb.client_tokens` mapping.",
                "Card writes also require `allowlists.orb.tools`; subscription changes require",
                "`allowlists.orb.events`. No other hub tools or methods are forwarded.",
                "",
                "Tools: " + ", ".join(f"`{name}`" for name in sorted(TOOLS)) + ".",
                "",
                "Events: " + ", ".join(f"`{name}`" for name in sorted(EVENT_NAMES)) + ".",
                "Use `events/list`, `events/subscribe`, `events/unsubscribe` with modern metadata.",
                "The hub owns subscriptions and delivers signed webhooks directly.",
            ]
        )
        return "\n".join(lines) + "\n"
