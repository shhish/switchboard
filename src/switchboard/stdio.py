"""NDJSON adapter: one process per client, concurrent calls, no stdout logging."""

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import aiohttp

from .auth import local_url, read_token
from .protocol import error_response, request_headers
from .server import CATALOG_HEADER, MAX_REQUEST, load_json

MAX_RESPONSE = 2 * 1024 * 1024


async def run_stdio(url: str, token_file: Path) -> int:
    local_url(url)
    read_token(token_file)  # Validate credentials before attaching stdin.
    reader = asyncio.StreamReader(limit=MAX_REQUEST)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: protocol, sys.stdin.buffer
    )
    version = "2025-03-26"
    catalog = None  # Last catalog version seen; a change means a deploy changed tools.
    pending: dict[object, asyncio.Task] = {}
    slots = asyncio.Semaphore(32)

    def emit(message):
        if message is not None:
            sys.stdout.write(
                json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                + "\n"
            )
            sys.stdout.flush()

    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=60)
    ) as session:

        async def forward(message, request_version):
            nonlocal version, catalog
            has_id = isinstance(message, dict) and "id" in message
            request_id = message.get("id") if isinstance(message, dict) else None
            try:
                headers = {
                    **request_headers(message, request_version),
                    "Authorization": "Bearer " + read_token(token_file),
                    "Accept": "application/json, text/event-stream",
                }
                async with slots:
                    async with session.post(
                        url, json=message, headers=headers, allow_redirects=False
                    ) as response:
                        if response.status == 202:
                            return
                        if response.content_type != "application/json":
                            raise ValueError("Unexpected hub response")
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE:
                                raise ValueError("Hub response too large")
                        result = load_json(body)
                        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0":
                            raise ValueError("Invalid hub response")
                        if isinstance(message, dict) and message.get("method") == "initialize":
                            version = result.get("result", {}).get("protocolVersion", version)
                        emit(result)
                        seen = response.headers.get(CATALOG_HEADER)
                        if seen and catalog and seen != catalog:
                            emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                        if seen:
                            catalog = seen
            except (OSError, ValueError, aiohttp.ClientError, TimeoutError):
                if has_id or not isinstance(message, dict):
                    emit(
                        error_response(
                            -32000,
                            "Switchboard unavailable or response invalid",
                            request_id,
                            protocol_version=request_version,
                        )
                    )

        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    emit(
                        error_response(-32700, "JSON-RPC line too large", protocol_version=version)
                    )
                    return 1
                if not line:
                    break
                try:
                    message = load_json(line)
                except (ValueError, UnicodeError, RecursionError):
                    emit(error_response(-32700, "Parse error", protocol_version=version))
                    continue
                version = request_headers(message, version)["MCP-Protocol-Version"]
                if (
                    isinstance(message, dict)
                    and message.get("method") == "notifications/cancelled"
                    and "id" not in message
                ):
                    params = message.get("params", {})
                    request_id = params.get("requestId") if isinstance(params, dict) else None
                    if type(request_id) in (str, int) and request_id in pending:
                        pending[request_id].cancel()
                    continue
                request_id = message.get("id") if isinstance(message, dict) else None
                if type(request_id) not in (str, int):
                    request_id = object()
                if request_id in pending:
                    emit(
                        error_response(
                            -32600,
                            "Request ID already in flight",
                            request_id,
                            protocol_version=version,
                        )
                    )
                    continue
                # Bound memory, including the queued task count.
                if len(pending) >= 32:
                    if not isinstance(message, dict) or "id" in message:
                        emit(
                            error_response(
                                -32000,
                                "Too many requests in flight",
                                request_id,
                                protocol_version=version,
                            )
                        )
                    continue
                task = asyncio.create_task(forward(message, version))
                pending[request_id] = task
                task.add_done_callback(lambda _, key=request_id: pending.pop(key, None))
                # Handshake establishes version before the next legacy request.
                if isinstance(message, dict) and message.get("method") == "initialize":
                    await task
            if pending:
                await asyncio.gather(*list(pending.values()), return_exceptions=True)
        finally:
            transport.close()
            for task in list(pending.values()):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*list(pending.values()), return_exceptions=True)
    return 0
