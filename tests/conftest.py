"""Tests never connect to real services or use the owner's configuration."""

import copy
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
import yaml
from aiohttp import web

from switchboard.auth import CLIENTS, create_token, read_token
from switchboard.config import Config
from switchboard.core import Core
from switchboard.server import create_app

SCOPES = [
    "feedback:write",
    "feedback:read",
    "feedback:manage",
    "ops:read",
    "media:read",
    "home:read",
    "calendar:read",
    "calendar:write",
    "reminders:read",
    "reminders:write",
    "tasks:read",
    "tasks:write",
    "jobs:read",
    "jobs:cancel",
    "approvals:confirm",
    "test:approval",
]


@pytest.fixture(autouse=True)
def block_live_services(monkeypatch):
    """Only ephemeral loopback test ports are allowed, including child test configuration."""
    original = socket.socket.connect
    forbidden = {18800, 8050, 8010, 5055, 18796, 8095, 8445, 32400, 8181}

    def guarded(sock, address):
        if isinstance(address, tuple):
            assert address[0] in ("127.0.0.1", "::1"), "Tests cannot use external networks"
            assert address[1] not in forbidden, "Tests cannot use a live service port"
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def block_real_host_commands(monkeypatch):
    import asyncio

    from switchboard import hostcmd

    original = asyncio.create_subprocess_exec

    async def guarded(program, *args, **kwargs):
        assert program not in {
            hostcmd.LAUNCHCTL,
            hostcmd.DOCKER,
            hostcmd.PS,
            hostcmd.SYSCTL,
            hostcmd.VM_STAT,
            hostcmd.SSH,
        }, "Tests must fake host commands"
        return await original(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded)


@pytest.fixture
def config(tmp_path, monkeypatch):
    cfg = Config(tmp_path / "config", tmp_path / "state", tmp_path / "logs")
    for name in ("plex", "tautulli", "maintenance"):
        cfg.backends[name]["enabled"] = False
    cfg.backends["maintenance"]["path"] = str(tmp_path / "maintenance.json")
    cfg.config_dir.mkdir(mode=0o700)
    policy = {
        "clients": {
            name: {"scopes": list(SCOPES), "trust_client_confirmation": name in ("dot", "orb")}
            for name in CLIENTS
        },
        "allowlists": {"home": {"scenes": ["safe"]}},
    }
    policy["clients"]["agy"]["scopes"] = ["jobs:read"]
    policy["clients"]["cortex"]["scopes"] = ["ops:read"]
    (cfg.config_dir / "policy.yaml").write_text(yaml.safe_dump(policy))
    (cfg.config_dir / "policy.yaml").chmod(0o600)
    (cfg.config_dir / "config.yaml").write_text("{}\n")
    (cfg.config_dir / "config.yaml").chmod(0o600)
    for name in CLIENTS:
        create_token(cfg.token_dir, name)
    monkeypatch.setenv("SWITCHBOARD_CONFIG_DIR", str(cfg.config_dir))
    monkeypatch.setenv("SWITCHBOARD_STATE_DIR", str(cfg.state_dir))
    monkeypatch.setenv("SWITCHBOARD_LOG_DIR", str(cfg.log_dir))
    return cfg


@asynccontextmanager
async def running_app(app):
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def bearer(cfg, client="dot"):
    return {"Authorization": "Bearer " + read_token(cfg.token_dir / f"{client}.token")}


@pytest.fixture
async def fake_backends(config):
    now = datetime.now(UTC)
    state = {
        "/library/summary": {
            "movies": {"total": 12, "downloaded": 10},
            "shows": {"total": 4},
            "books": {"total_books": 7},
            "downloads": {"active": 1, "downloads": [{"private": "RAW"}]},
            "plex_sessions": 2,
            "total_size_gb": 300,
            "errors": {"readarr": "PRIVATE with auth=secret"},
        },
        "/downloads/active": {
            "active": 1,
            "paused": 1,
            "total_speed": 300,
            "downloads": [
                {
                    "id": "dl1",
                    "title": "Dune",
                    "status": "downloading",
                    "progress": 12,
                    "private": "RAW",
                }
            ],
        },
        "/downloads/history": {"history": [{"id": "dl2", "title": "Arrival", "status": "SUCCESS"}]},
        "/movies/list": {
            "movies": [
                {
                    "id": i,
                    "title": f"Dune {i}",
                    "year": 2021,
                    "tmdb_id": i + 100,
                    "has_file": True,
                    "path": "PRIVATE",
                }
                for i in range(15)
            ]
            + [{"id": 50, "title": "Arrival"}]
        },
        "/shows/list": {
            "shows": [
                {
                    "id": 70,
                    "title": "Dune Prophecy",
                    "year": 2024,
                    "tvdb_id": 1000,
                    "episode_file_count": 1,
                }
            ]
        },
        "/api/v1/search": {
            "results": [
                {
                    "id": 900,
                    "mediaType": "movie",
                    "title": "Dune 0",
                    "releaseDate": "2021-01-01",
                    "mediaInfo": {"status": 5},
                },
                {
                    "id": 901,
                    "mediaType": "tv",
                    "name": "Dune Prophecy",
                    "firstAirDate": "2024-01-01",
                    "mediaInfo": {"status": 4},
                },
                {"id": 902, "mediaType": "person", "name": "Dune Person"},
            ]
        },
        "/api/v1/auth/me": {"id": 1},
        "/api/v1/health": {"status": "ok", "homekit": "ready"},
        "/api/v1/homes": [{"id": "h1", "name": "Home", "rooms": 1, "accessories": 1, "scenes": 1}],
        "/api/v1/snapshot": [
            {
                "id": "h1",
                "name": "Home",
                "rooms": [{"id": "r1", "name": "Office"}],
                "accessories": [
                    {
                        "id": "a1",
                        "name": "Lamp",
                        "room": "Office",
                        "category": "lightbulb",
                        "reachable": True,
                    }
                ],
                "scenes": [{"id": "s1", "name": "Evening", "kind": "user"}],
            }
        ],
        "/api/v1/accessories/a1": {
            "id": "a1",
            "name": "Lamp",
            "home": "Home",
            "room": "Office",
            "category": "lightbulb",
            "state": {"on": True, "brightness": 50, "custom.secret": "RAW"},
            "services": [{"private": "RAW"}],
        },
        "/api/state": {
            "generatedAt": now.isoformat(),
            "events": [
                {
                    "title": "Meeting",
                    "start": (now + timedelta(hours=1)).isoformat(),
                    "end": (now + timedelta(hours=2)).isoformat(),
                    "allDay": False,
                },
                {
                    "title": "Far away",
                    "start": (now + timedelta(days=10)).isoformat(),
                    "end": (now + timedelta(days=11)).isoformat(),
                    "allDay": True,
                },
            ],
            "todos": [
                {"id": "rem1", "title": "Milk", "source": "reminders", "evening": False},
                {"id": "thing1", "title": "Private Things", "source": "things"},
            ],
            "markets": [{"symbol": "NEVER", "price": 123}],
            "weather": {"private": "RAW"},
        },
        "/health": {"status": "ok"},
    }
    calls = []
    home_token = config.config_dir / "home-read.secret"
    home_token.write_text("fake-home-read")
    home_token.chmod(0o600)
    overseerr_key = config.config_dir / "overseerr.secret"
    overseerr_key.write_text("fake-overseerr-key")
    overseerr_key.chmod(0o600)

    async def handle(request):
        assert request.method == "GET", "Read capabilities must never mutate a backend"
        calls.append(
            {
                "path": request.path,
                "query": dict(request.query),
                "auth": request.headers.get("Authorization"),
                "key": request.headers.get("X-Api-Key"),
            }
        )
        if request.path in ("/api/v1/snapshot", "/api/v1/accessories/a1"):
            assert request.headers.get("Authorization") == "Bearer fake-home-read"
        if request.path == "/api/v1/search":
            assert request.headers.get("X-Api-Key") == "fake-overseerr-key"
            # Like real Overseerr: a "+" for a space is a 400.
            if "+" in request.rel_url.raw_query_string:
                return web.json_response({"message": "bad query"}, status=400)
        if request.path == "/api/v1/accessories/Office/Lamp":
            return web.json_response(state["/api/v1/accessories/a1"])
        if request.path == "/api/v1/accessories/ambiguous":
            return web.json_response({"error": "PRIVATE"}, status=409)
        value = state.get(request.path)
        if isinstance(value, int):
            return web.json_response({"error": "PRIVATE"}, status=value)
        if value is None:
            return web.Response(status=404)
        return web.json_response(value)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with running_app(app) as url:
        for name in ("media", "health", "frame"):
            config.backends[name]["url"] = url
        config.backends["home"].update(url=url + "/api/v1", token_file=str(home_token))
        config.backends["overseerr"].update(url=url, enabled=True, api_key_file=str(overseerr_key))
        yield state, calls


@pytest.fixture
async def core(config, fake_backends):
    result = Core(config)
    await result.start()
    try:
        yield result
    finally:
        await result.close()


@pytest.fixture
async def server(config, fake_backends):
    core = Core(config)
    async with running_app(create_app(core)) as url:
        config.port = int(url.rsplit(":", 1)[1])
        async with aiohttp.ClientSession(trust_env=False) as session:
            yield core, url, session


@pytest.fixture
def fake_approval():
    from switchboard.registry import Capability
    from switchboard.schema import obj, string

    async def handler(core, client, args):
        return {"status": "executed", "value": args["value"]}

    return Capability(
        "test.perform",
        "Test action",
        "Test approval only",
        obj({"value": string()}, ("value",)),
        "test:approval",
        handler,
        tier="approval",
        output_schema=obj({"status": string(), "value": string()}),
        idempotent=False,
    )


def change_policy(config, edit):
    path = config.config_dir / "policy.yaml"
    data = yaml.safe_load(path.read_text())
    edit(data)
    path.write_text(yaml.safe_dump(copy.deepcopy(data)))


@pytest.fixture(autouse=True)
def outputs_match_advertised_schema(monkeypatch):
    """Every successful tool result in every test must validate against the
    outputSchema clients receive (Claude Code enforces it client-side)."""
    import jsonschema

    from switchboard.core import Core
    from switchboard.names import canonical_name

    original = Core.call

    async def checked(self, client, name, arguments):
        result = await original(self, client, name, arguments)
        cap = (
            self.registry.capabilities.get(canonical_name(name)) if isinstance(name, str) else None
        )
        if cap is not None and isinstance(result, dict) and "error" not in result:
            schema = cap.tool().get("outputSchema")
            if schema:
                jsonschema.Draft202012Validator(schema).validate(result)
        return result

    monkeypatch.setattr(Core, "call", checked)


def domain_payload(result):
    """Compare existing domain contracts independently of observation metadata."""
    return {
        key: value for key, value in result.items() if key not in {"outcome", "source", "as_of"}
    }
