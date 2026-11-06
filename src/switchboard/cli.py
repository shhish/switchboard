"""JSON stdout CLI. Token commands print metadata only; never credential values."""

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import quote

import aiohttp

from .auth import CLIENTS, Tokens, create_token, read_token
from .config import Config
from .domains import build_registry
from .errors import CapabilityError
from .policy import Policy
from .server import load_json


def parser():
    root = argparse.ArgumentParser(prog="sb")
    root.add_argument("--client", choices=CLIENTS, default="owner", help="CLI token identity")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("list")
    call = sub.add_parser("call")
    call.add_argument("capability")
    call.add_argument("--json", default="{}", dest="arguments")
    manifest = sub.add_parser("manifest")
    manifest.add_argument("--markdown", action="store_true")
    sub.add_parser("doctor")
    for name in ("eventkit", "things"):
        native = sub.add_parser(name).add_subparsers(dest="action", required=True)
        native.add_parser("status")
        native.add_parser("request-access")
    sub.add_parser("net").add_subparsers(dest="action", required=True).add_parser("request-access")
    token = sub.add_parser("token").add_subparsers(dest="action", required=True)
    token.add_parser("list")
    for action in ("create", "revoke"):
        token.add_parser(action).add_argument("name", choices=CLIENTS)
    for action in ("approve", "deny"):
        sub.add_parser(action).add_argument("approval_id")
    sub.add_parser("approvals")
    audit = sub.add_parser("audit").add_subparsers(dest="action", required=True).add_parser("tail")
    audit.add_argument("--limit", type=int, default=20)
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--limit", type=int, default=20)
    issues = sub.add_parser("issues").add_subparsers(dest="action", required=True)
    issues_list = issues.add_parser("list")
    issues_list.add_argument("--status", choices=("open", "closed", "all"), default="open")
    issues_add = issues.add_parser("add")
    issues_add.add_argument("--kind", choices=("bug", "improvement", "missing_capability", "ops"))
    issues_add.add_argument("--capability")
    issues_add.add_argument("title")
    issues_add.add_argument("details")
    issues_close = issues.add_parser("close")
    issues_close.add_argument("issue_id")
    issues_close.add_argument("resolution")
    stdio = sub.add_parser("mcp-stdio")
    stdio.add_argument("--client", dest="stdio_client", choices=CLIENTS, required=True)
    return root


async def request(config, client, method, path, args=None):
    token = read_token(config.token_dir / f"{client}.token")
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=60)
    ) as session:
        async with session.request(
            method,
            f"http://127.0.0.1:{config.port}" + path,
            json=args,
            headers={"Authorization": "Bearer " + token},
            allow_redirects=False,
        ) as response:
            if response.status >= 300:
                raise CapabilityError("server_error", f"Server returned HTTP {response.status}")
            return await response.json()


async def execute(args, config):
    client = args.client
    if args.command in ("eventkit", "things", "net"):
        from .eventkit import EventKit
        from .things import Things

        # Authenticate the local owner credential too; --client is not identity proof.
        policy = Policy(config.config_dir / "policy.yaml")
        policy.client(client)
        token = read_token(config.token_dir / f"{client}.token")
        if client != "owner" or Tokens(config.token_dir).authenticate("Bearer " + token) != "owner":
            raise CapabilityError("owner_required", "Local owner credential required")
        helper = Things(config) if args.command == "things" else EventKit(config)
        if args.command == "net":
            result = await helper.net_request_access()
            return {**result, "status": "ok" if "http_status" in result else "degraded"}
        if args.action == "status":
            return await helper.probe()
        result = await helper.request_access()
        if args.command == "things":
            return {
                **result,
                "status": "ok" if result.get("automation") == "authorized" else "degraded",
            }
        status = (
            "ok"
            if all(result.get(key) == "full_access" for key in ("events", "reminders"))
            else "degraded"
        )
        return {**result, "status": status}
    if args.command == "doctor":
        from .backends import Backends
        from .doctor import diagnose

        backend = Backends(config)
        await backend.start()
        try:
            return await diagnose(config, backend, Policy(config.config_dir / "policy.yaml"))
        finally:
            await backend.close()
    if args.command == "list":
        return await request(config, client, "GET", "/v1/tools")
    if args.command == "call":
        return await request(
            config,
            client,
            "POST",
            "/v1/call/" + quote(args.capability, safe=""),
            load_json(args.arguments),
        )
    if args.command in ("approve", "deny"):
        # Owner identity is derived by server from this token, never a JSON client field.
        return await request(
            config,
            client,
            "POST",
            "/v1/approvals/" + quote(args.approval_id, safe="") + "/" + args.command,
            {},
        )
    if args.command == "approvals":
        return await request(config, client, "GET", "/v1/approvals")
    if args.command == "audit":
        return await request(config, client, "GET", f"/v1/audit?limit={args.limit}")
    if args.command == "issues":
        name, body = {
            "list": ("switchboard_list_issues", lambda: {"status": args.status}),
            "add": (
                "switchboard_report_issue",
                lambda: {
                    "kind": args.kind or "improvement",
                    "title": args.title,
                    "details": args.details,
                    **({"capability": args.capability} if args.capability else {}),
                },
            ),
            "close": (
                "switchboard_close_issue",
                lambda: {"issue_id": args.issue_id, "resolution": args.resolution},
            ),
        }[args.action]
        return await request(config, client, "POST", "/v1/call/" + name, body())
    if args.command == "jobs":
        return await request(config, client, "POST", "/v1/call/jobs_list", {"limit": args.limit})
    raise ValueError("Unknown command")


def main():
    args = parser().parse_args()
    try:
        if args.command == "manifest":
            registry = build_registry()
            print(
                registry.markdown() if args.markdown else json.dumps(registry.manifest(), indent=2),
                end="" if args.markdown else "\n",
            )
            return 0
        config = Config.load()
        if args.command == "serve":
            from .core import Core
            from .server import serve

            # Restrict SQLite sidecars and all newly created runtime files.
            os.umask(0o077)
            serve(Core(config))
            return 0
        if args.command == "mcp-stdio":
            from .stdio import run_stdio

            return asyncio.run(
                run_stdio(
                    f"http://127.0.0.1:{config.port}/mcp",
                    config.token_dir / f"{args.stdio_client}.token",
                )
            )
        if args.command == "token":
            if args.action == "create":
                path = create_token(config.token_dir, args.name)
                result = {"client": args.name, "token_file": str(path), "status": "created"}
            elif args.action == "revoke":
                (config.token_dir / f"{args.name}.token").unlink(missing_ok=True)
                result = {"client": args.name, "status": "revoked"}
            else:
                tokens = Tokens(config.token_dir)
                tokens.refresh()
                result = {"clients": sorted(tokens.hashes)}
        else:
            result = asyncio.run(execute(args, config))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result.get("status") in ("error", "degraded") else 0
    except (OSError, ValueError, RecursionError, aiohttp.ClientError, TimeoutError) as exc:
        # No exception text: filesystem/backend errors can include credentials.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": "configuration_or_connection",
                        "message": "Configuration, token or connection invalid; run sb doctor",
                        # The class name alone carries no credential text but saves a debug loop.
                        "exception": type(exc).__name__,
                    },
                }
            )
        )
        return 1
    except CapabilityError as exc:
        print(json.dumps(exc.result()))
        return 1


if __name__ == "__main__":
    sys.exit(main())
