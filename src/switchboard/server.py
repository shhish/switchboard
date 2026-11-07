"""Loopback-only authenticated HTTP adapters; all capability calls enter Core."""

import ipaddress
import json
import math
from collections import defaultdict, deque
from dataclasses import replace
from time import monotonic

from aiohttp import web

from .auth import Tokens, loopback_host
from .errors import CapabilityError
from .execution import Execution
from .protocol import Protocol, error_response, http_metadata_error

MAX_REQUEST = 64 * 1024
CLIENT_KEY = web.RequestKey("client", str)


def load_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def invalid(_):
        raise ValueError("Non-finite JSON number")

    def finite_float(number):
        result = float(number)
        if not math.isfinite(result):
            raise ValueError("Non-finite JSON number")
        return result

    return json.loads(
        value, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite_float
    )


CATALOG_HEADER = "X-Switchboard-Catalog"


def create_app(core):
    tokens = Tokens(core.config.token_dir)
    protocol = Protocol(core)
    recent = defaultdict(deque)
    inflight = defaultdict(int)

    def rejected(reason, status):
        core.audit.append(client="anonymous", capability="transport.request", outcome=reason)
        return web.json_response({"status": "error", "error": {"code": reason}}, status=status)

    @web.middleware
    async def security(request, handler):
        guarded = ("Host", "Authorization", "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name")
        if (
            "Origin" in request.headers
            or any(len(request.headers.getall(k, [])) > 1 for k in guarded)
            or not loopback_host(request.headers.get("Host", ""))
        ):
            return rejected("forbidden", 403)
        try:
            if request.remote is None or not ipaddress.ip_address(request.remote).is_loopback:
                return rejected("forbidden", 403)
        except ValueError:
            return rejected("forbidden", 403)
        if request.path == "/health" and request.method == "GET":
            return await handler(request)
        client = tokens.authenticate(request.headers.get("Authorization", ""))
        if client is None:
            return rejected("unauthorized", 401)
        request[CLIENT_KEY] = client
        now = monotonic()
        history = recent[client]
        while history and history[0] < now - 60:
            history.popleft()
        if len(history) >= 600 or inflight[client] >= 32:
            core.audit.append(client=client, capability="transport.request", outcome="rate_limited")
            return web.json_response({"error": "Request limit exceeded"}, status=429)
        history.append(now)
        inflight[client] += 1
        try:
            return await handler(request)
        finally:
            inflight[client] -= 1

    app = web.Application(middlewares=[security], client_max_size=MAX_REQUEST)

    async def parse(request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected application/json")
        try:
            return load_json(await request.read())
        except (ValueError, UnicodeError, RecursionError):
            raise web.HTTPBadRequest(text="Invalid JSON") from None

    async def mcp(request):
        accept = {v.strip().split(";")[0] for v in request.headers.get("Accept", "").split(",")}
        if not {"application/json", "text/event-stream"} <= accept:
            return web.json_response({"error": "Accept JSON and text/event-stream"}, status=406)
        try:
            message = await parse(request)
        except web.HTTPBadRequest:
            return web.json_response(
                error_response(
                    -32700,
                    "Parse error",
                    protocol_version=request.headers.get("MCP-Protocol-Version", "2025-03-26"),
                ),
                status=400,
            )
        metadata_error = http_metadata_error(message, request.headers)
        if metadata_error:
            return web.json_response(metadata_error, status=400)
        result = await protocol.handle(
            message, request[CLIENT_KEY], request.headers.get("MCP-Protocol-Version")
        )
        if result is None:
            return web.Response(status=202)
        code = result.get("error", {}).get("code")
        status = 404 if code == -32601 else 400 if code in (-32022, -32020, -32600) else 200
        response = web.json_response(result, status=status)
        response.headers[CATALOG_HEADER] = core.catalog_version
        return response

    async def call(request):
        args = await parse(request)
        ctx = Execution.from_headers(request.headers)
        if request[CLIENT_KEY] == "owner":
            ctx = replace(ctx, origin="owner")
        return web.json_response(
            await core.call(request[CLIENT_KEY], request.match_info["name"], args, ctx)
        )

    async def list_tools(request):
        try:
            return web.json_response({"tools": core.tools(request[CLIENT_KEY])})
        except CapabilityError as exc:
            return web.json_response(exc.result(), status=403)

    def owner(request):
        try:
            core.policy.client(request[CLIENT_KEY])
        except CapabilityError:
            raise web.HTTPForbidden(text="Policy denied request") from None
        if request[CLIENT_KEY] != "owner":
            core.audit.append(
                client=request[CLIENT_KEY], capability="owner.operation", outcome="denied"
            )
            raise web.HTTPForbidden(text="Owner required")

    async def resolve(request):
        args = await parse(request)
        if args != {}:
            raise web.HTTPBadRequest(text="Expected empty object")
        return web.json_response(
            core.resolve(
                request[CLIENT_KEY],
                request.match_info["id"],
                request.match_info["decision"] == "approve",
            )
        )

    async def approvals(request):
        owner(request)
        return web.json_response({"approvals": core.approvals.list()})

    async def audit(request):
        owner(request)
        try:
            limit = int(request.query.get("limit", 20))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid limit") from None
        return web.json_response({"audit": core.audit.tail(limit)})

    async def audit_verify(request):
        owner(request)
        result = core.audit.verify()
        return web.json_response({"status": "ok" if result["ok"] else "error", **result})

    async def health(request):
        return web.json_response({"status": "ok", "service": "switchboard"})

    app.router.add_get("/health", health)
    app.router.add_post("/mcp", mcp)
    app.router.add_post("/v1/call/{name}", call)
    app.router.add_get("/v1/tools", list_tools)
    app.router.add_post("/v1/approvals/{id}/{decision:approve|deny}", resolve)
    app.router.add_get("/v1/approvals", approvals)
    app.router.add_get("/v1/audit", audit)
    app.router.add_get("/v1/audit/verify", audit_verify)

    async def lifecycle(_):
        await core.start()
        try:
            yield
        finally:
            await core.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def serve(core):
    web.run_app(
        create_app(core),
        host="127.0.0.1",
        port=core.config.port,
        access_log=None,
        print=None,
        handler_cancellation=True,
        # TCP keepalive is pointless on loopback, and setting it on a socket the client
        # already closed raised EINVAL tracebacks into the error log on every such accept.
        tcp_keepalive=False,
    )
