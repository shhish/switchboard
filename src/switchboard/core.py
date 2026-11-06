"""All adapters enter here: authorization, validation, grants, timeout and audit."""

import asyncio
import hashlib
import json
import time

from .alerts import AlertDelivery
from .approvals import Approvals
from .audit import Audit
from .backends import Backends, OrbApprovalNotifier
from .disk_health import DiskHealth
from .errors import CapabilityError
from .execution import Execution
from .guidance import SkillCatalog
from .jobs import Jobs
from .monitor import Monitor
from .names import canonical_name
from .owner_context import OwnerContext
from .policy import Policy
from .probes import HealthProbes
from .schema import validate
from .services import Services
from .storage import Storage
from .upstreams.orb import EVENT_METHODS, EVENT_NAMES, READ_TOOLS, TOOLS, WRITE_TOOLS, OrbUpstream
from .watches import Watches


class Core:
    def __init__(self, config, registry=None, notifier=None):
        from .domains import build_registry

        self.config = config
        self.registry = registry or build_registry()
        self.skills = SkillCatalog()
        # Changes whenever a deploy changes any tool's name, schema or description, so
        # the stdio bridge can tell clients to re-fetch (notifications/tools/list_changed).
        self.catalog_version = hashlib.sha256(
            json.dumps(self.registry.manifest(), sort_keys=True).encode()
        ).hexdigest()[:16]
        self.policy = Policy(config.config_dir / "policy.yaml")
        self.storage = Storage(config.state_dir)
        self.audit = Audit(self.storage, config.log_dir, self.catalog_version)
        self.approvals = Approvals(self.storage, config.approval_ttl_s)
        self.jobs = Jobs(self.storage)
        self.backends = Backends(config)
        self.probes = HealthProbes(config)
        self.alerts = AlertDelivery(config, self.storage)
        self.watches = Watches(self)
        self.monitor = Monitor(config, self.storage, self.probes, self.alerts, self.watches)
        self.services = Services(self)
        self.disk_health = DiskHealth(self)
        self.monitor.disk_health = self.disk_health
        self.context = OwnerContext(self)
        self.orb = OrbUpstream(config, self.policy)
        self.media_request_lock = asyncio.Lock()
        self.watch_state_lock = asyncio.Lock()
        self.notifier = notifier
        from .routines.engine import RoutineEngine

        self.routines = RoutineEngine(self)
        if notifier is None and config.backends["orb"].get("enabled", False):
            self.notifier = OrbApprovalNotifier(self.backends)

    async def start(self):
        await self.backends.start()
        await self.probes.start()
        await self.orb.start()
        await self.alerts.start()
        await self.monitor.start()
        try:
            await self.routines.start()
        except Exception:
            pass  # Routines wait for HomeRelay; doctor and routines_list report it.
        self.maintenance = asyncio.create_task(self.maintain())

    async def maintain(self):
        """Hourly housekeeping that must run even when monitoring is disabled."""
        while True:
            try:
                self.audit.maintain()
            except Exception:
                pass  # Doctor reports audit chain and retention state; serving continues.
            try:
                await self.disk_health.maintain()
            except Exception:
                pass  # Storage discovery must not stop audit housekeeping.
            await asyncio.sleep(600)

    async def close(self):
        if task := getattr(self, "maintenance", None):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.routines.close()
        await self.monitor.close()
        await self.alerts.close()
        await self.jobs.close()
        await self.probes.close()
        await self.backends.close()
        await self.orb.close()
        self.storage.close()

    def tools(self, client):
        self.policy.client(client)
        return [
            self.registry.capabilities[name].tool()
            for name in sorted(self.registry.capabilities)
            if self.policy.allowed(client, self.registry.capabilities[name])
        ]

    async def mcp_tools(self, client):
        tools = self.tools(client)
        try:
            tools.extend(await self.orb.tools(client))
        except CapabilityError:
            pass  # Doctor reports unavailable mappings; local tools remain usable.
        return sorted(tools, key=lambda tool: tool["name"])

    def events(self, client):
        """Local event declarations, merged with the upstream catalog by the adapter."""
        self.policy.client(client)
        return []

    async def mcp_capabilities(self, client):
        self.policy.client(client)
        capabilities = {"tools": {"listChanged": True}, "prompts": {}, "resources": {}}
        events = await self.orb.events_capability(client)
        if self.events(client) or events is not None:
            capabilities["events"] = events or {}
        return capabilities

    def guidance(self, client, method, params, ctx=None):
        """Public release guidance needs a known client, but no tool scope or grant."""
        decision, outcome = "denied", "error"
        error_code = None
        try:
            self.policy.client(client)
            result = self.skills.request(method, params)
            # An unknown prompt or resource is a denied request, as for unknown tools.
            decision, outcome = "allowed", "ok"
            return result
        except CapabilityError as exc:
            error_code = exc.code
            raise
        finally:
            # Do not persist prompt tasks or arbitrary resource names as plain text.
            self.audit.append(
                client=client,
                capability=method,
                args=params,
                decision=decision,
                outcome=outcome,
                error_code=error_code,
                ctx=ctx,
                hash_fields=frozenset({"task", "name", "uri"}),
            )

    async def federate(self, client, method, params, *, version, meta, ctx=None):
        """Fixed Orb routes share core policy and audit, preserving MCP envelopes."""
        started = time.monotonic()
        error_code = None
        name = params.get("name")
        is_tool = method == "tools/call"
        capability = name if is_tool and isinstance(name, str) and name in TOOLS else method
        tier = (
            "read"
            if (is_tool and isinstance(name, str) and name in READ_TOOLS) or method == "events/list"
            else "write"
        )
        decision, outcome = "denied", "cancelled"
        try:
            self.policy.require_scope(client, "orb:use")
            if is_tool:
                if not isinstance(name, str) or name not in TOOLS:
                    raise CapabilityError("unknown_capability", "Unknown Orb tool")
                if name in WRITE_TOOLS:
                    self.policy.require_allowed("orb", "tools", name)
            elif method not in EVENT_METHODS:
                raise CapabilityError("unknown_capability", "Unknown Orb method")
            elif method != "events/list":
                if not isinstance(name, str) or name not in EVENT_NAMES:
                    raise CapabilityError("unknown_capability", "Unknown Orb event")
                self.policy.require_allowed("orb", "events", name)
            # credentials() enforces mapping before I/O and rechecks file revocation.
            self.orb.credentials(client)
            decision = "allowed"
            envelope = await self.orb.request(client, method, params, version=version, meta=meta)
            outcome = "error" if "error" in envelope or envelope["result"].get("isError") else "ok"
            return envelope
        except CapabilityError as exc:
            outcome = "error"
            error_code = exc.code
            return {"error": {"code": -32000, "message": str(exc), "data": exc.result()}}
        finally:
            # All forwarded payloads are private, including text, URLs and signing secrets.
            self.audit.append(
                client=client,
                capability=capability,
                args="[REDACTED]",
                tier=tier,
                decision=decision,
                outcome=outcome,
                duration=time.monotonic() - started,
                ctx=ctx,
                error_code=error_code,
            )

    async def call(self, client, name, arguments, ctx=None):
        from .domains.common import result_envelope

        ctx = ctx or Execution.new("owner" if client == "owner" else "agent")
        started = time.monotonic()
        name = canonical_name(name)
        cap = self.registry.capabilities.get(name) if isinstance(name, str) else None
        decision = "denied"
        approval_id = None
        result = None
        try:
            self.policy.client(client)
            if cap is None:
                raise CapabilityError("unknown_capability", "Unknown capability")
            self.policy.require(client, cap)
            validate(arguments, cap.wire_schema)
            args = dict(arguments)
            if cap.preflight:
                # Availability and resource checks precede approval notifications and execution.
                cap.preflight(self, client, args)
            if cap.name == "approvals_confirm":
                approval_id = args["approval_id"]
            if cap.tier == "approval" and cap.name != "approvals_confirm":
                approval_id = args.pop("approval_id", None)
                if approval_id is None:
                    result = self.approvals.request(client, name, args)
                    approval_id = result["approval_id"]
                    decision = "approval_required"
                    if self.notifier:
                        try:
                            async with asyncio.timeout(min(5, cap.timeout_s)):
                                await self.notifier.notify(result)
                        except Exception:
                            result["notification_status"] = "failed; use sb approve or sb deny"
                    result = result_envelope(result)
                    return result
                self.approvals.consume(approval_id, client, name, args)
            decision = "allowed"
            async with asyncio.timeout(cap.timeout_s):
                result = await cap.handler(self, client, args)
            if not isinstance(result, dict):
                raise CapabilityError("handler_invalid", "Handler did not return a JSON object")
            try:
                json.dumps(result, allow_nan=False)
            except (TypeError, ValueError):
                raise CapabilityError("handler_invalid", "Handler returned invalid JSON") from None
            result = result_envelope(result, source=self.result_source(cap, result), arguments=args)
            # Validate the declared output, including future handlers, without leaking values.
            try:
                validate(result, cap.output_schema, "result")
            except CapabilityError:
                raise CapabilityError(
                    "handler_invalid", "Handler output violates its schema"
                ) from None
            return result
        except CapabilityError as exc:
            result = exc.result()
            return result
        except TimeoutError:
            result = CapabilityError("timeout", "Capability timed out; run sb doctor").result()
            return result
        except Exception:
            result = CapabilityError("internal_error", "Capability failed; run sb doctor").result()
            return result
        finally:
            error = (result or {}).get("error")
            self.audit.append(
                client=client,
                capability=name if isinstance(name, str) else None,
                args=arguments,
                tier=cap.tier if cap else None,
                decision=decision,
                approval_id=approval_id,
                job_id=result.get("job_id") if result else None,
                outcome=(result or {}).get("status", "cancelled"),
                duration=time.monotonic() - started,
                ctx=ctx,
                error_code=error.get("code") if isinstance(error, dict) else None,
                hash_fields=cap.audit_hash_fields if cap else frozenset(),
                result_bytes=len(json.dumps(result, allow_nan=False, default=str))
                if result
                else None,
            )

    @staticmethod
    def result_source(cap, result):
        if cap.service == "eventkit+frame":
            native = result.get("event_ids_available", result.get("due_dates_available"))
            return "eventkit" if native else "frameserver"
        return {
            "home": "homerelay",
            "overseerr": "seerr",
            "NWS": "nws",
            "media": "media-api",
            "media+overseerr": "media-api+seerr",
            "maintenance": "media-doctor",
            "media_compress": "media-doctor",
            "plex": "plex",
            "tautulli": "tautulli",
            "lazylibrarian": "lazylibrarian",
            "things": "things",
            "recipes": "recipes",
            "eventkit": "eventkit",
            "console": "ps5",
            "gitea": "gitea",
            "local_git": "local_git",
        }.get(cap.service, "switchboard")

    def resolve(self, client, approval_id, approve, ctx=None):
        started = time.monotonic()
        result = None
        try:
            self.policy.client(client)
            if client != "owner":
                raise CapabilityError("owner_required", "Owner identity required")
            result = self.approvals.resolve(approval_id, actor=client, approve=approve)
            return result
        except CapabilityError as exc:
            result = exc.result()
            return result
        finally:
            self.audit.append(
                client=client,
                capability="approvals_approve" if approve else "approvals_deny",
                args={"approval_id": approval_id},
                tier="approval",
                decision="allowed" if result and "error" not in result else "denied",
                approval_id=approval_id,
                outcome=(result or {}).get("status", "error"),
                duration=time.monotonic() - started,
                ctx=ctx or Execution.new("owner"),
                error_code=(result or {}).get("error", {}).get("code"),
            )
