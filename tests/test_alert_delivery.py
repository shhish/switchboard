"""HTTPS ntfy and STARTTLS SMTP are exercised against ephemeral local servers."""

import asyncio
import contextlib
import json
import shutil
import ssl
import subprocess
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from switchboard.alert_config import alert_settings
from switchboard.alerts import AlertDelivery
from switchboard.storage import Storage

from .conftest import change_policy


@pytest.fixture
def tls(tmp_path):
    cert, key = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    openssl = shutil.which("openssl")
    assert openssl is not None
    conf = tmp_path / "openssl.cnf"
    conf.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
        "[dn]\nCN=127.0.0.1\n[ext]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=CA:TRUE\n"
    )
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-config",
            str(conf),
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=cert)
    return server, client


def private_file(config, name, value):
    path = config.config_dir / name
    path.write_text(value)
    path.chmod(0o600)
    return str(path)


@asynccontextmanager
async def https_app(app, tls):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls[0])
    await site.start()
    try:
        yield f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


async def test_ntfy_https_plaintext_headers_and_no_redirects(config, tls):
    received = []

    async def handle(request):
        assert request.method == "POST"
        assert request.content_type == "text/plain"
        assert not request.query
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        received.append((dict(request.headers), await request.text()))
        if len(received) == 2:
            return web.Response(status=302, headers={"Location": "/never"})
        return web.Response(text="PRIVATE response body")

    async def never(_):
        pytest.fail("ntfy followed a redirect")

    app = web.Application()
    app.router.add_post("/topic-secret", handle)
    app.router.add_route("*", "/never", never)
    async with https_app(app, tls) as url:
        config.alerts = alert_settings(
            {
                "allowed_hosts": [
                    "127.0.0.1"
                ],  # Test-only loopback override, HTTPS still required.
                "ntfy": {"url_file": private_file(config, "ntfy.url", url + "/topic-secret")},
            }
        )
        storage = Storage(config.state_dir)
        delivery = AlertDelivery(config, storage)
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=tls[1]),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=20),
        ) as session:
            delivery.session = session
            try:
                payload = {
                    "title": "[Switchboard] DOWN: homebridge (pi)",
                    "body": "homebridge (pi)\nStatus: down\nSince: test\nDuration: 2 min",
                    "kind": "down",
                }
                with storage.db:
                    delivery.queue("incident:down", payload, 100)
                await delivery.deliver_due(100)
                assert delivery.status()["outbox_backlog"] == 0
                assert received[0][0]["Title"] == payload["title"]
                assert received[0][0]["Priority"] == "high"
                assert received[0][0]["Tags"] == "warning"
                assert received[0][1] == payload["body"]
                recovery = {
                    **payload,
                    "kind": "recovery",
                    "title": "[Switchboard] RECOVERED: homebridge after 2 min",
                }
                with storage.db:
                    delivery.queue("incident:recovery", recovery, 200)
                await delivery.deliver_due(200)
                assert received[1][0]["Priority"] == "default"
                assert received[1][0]["Tags"] == "white_check_mark"
                assert delivery.status()["outbox_backlog"] == 1
                assert len(received) == 2
                await delivery.deliver_due(260)
                assert delivery.status()["outbox_backlog"] == 0
                assert delivery.status()["channels"]["ntfy"]["last_failure_at"] == 200
                assert delivery.status()["channels"]["ntfy"]["last_success_at"] == 260
                assert "topic-secret" not in json.dumps(delivery.status())
            finally:
                storage.close()


@asynccontextmanager
async def smtp_server(tls, *, starttls=True):
    commands, messages, tasks = [], [], set()

    async def connection(reader, writer):
        tasks.add(asyncio.current_task())
        secure = False
        try:
            writer.write(b"220 fake SMTP\r\n")
            await writer.drain()
            while line := await reader.readline():
                command = line.decode().strip()
                verb = command.split()[0].upper()
                commands.append((verb, secure))
                if verb == "EHLO":
                    if not secure and starttls:
                        writer.write(b"250-fake\r\n250 STARTTLS\r\n")
                    else:
                        writer.write(b"250-fake\r\n250 AUTH PLAIN\r\n")
                elif verb == "STARTTLS":
                    writer.write(b"220 Start TLS\r\n")
                    await writer.drain()
                    await writer.start_tls(tls[0])
                    secure = True
                    continue
                elif verb in ("AUTH", "MAIL", "RCPT"):
                    assert secure, "Credentials or messages transmitted before STARTTLS"
                    writer.write(b"235 Authenticated\r\n" if verb == "AUTH" else b"250 OK\r\n")
                elif verb == "DATA":
                    assert secure
                    writer.write(b"354 Send message\r\n")
                    await writer.drain()
                    body = []
                    while (part := await reader.readline()) != b".\r\n":
                        assert part
                        body.append(part)
                    messages.append(b"".join(body).decode())
                    writer.write(b"250 Queued\r\n")
                elif verb == "QUIT":
                    writer.write(b"221 Bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"500 Unsupported\r\n")
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(connection, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], commands, messages
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)


def email_config(config, port):
    config.alerts = alert_settings(
        {
            "allowed_smtp_hosts": ["127.0.0.1"],
            "email": {
                "host": "127.0.0.1",
                "port": port,
                "starttls": True,
                **{
                    key + "_file": private_file(config, key, value)
                    for key, value in {
                        "username": "fake-user",
                        "password": "fake-password",
                        "from": "from@example.test",
                        "to": "to@example.test",
                    }.items()
                },
            },
        }
    )


@pytest.mark.parametrize("starttls", [True, False])
async def test_smtp_starttls_required_and_real_wire_shape(config, tls, monkeypatch, starttls):
    async with smtp_server(tls, starttls=starttls) as (port, commands, messages):
        email_config(config, port)
        monkeypatch.setattr("switchboard.alerts.ssl.create_default_context", lambda: tls[1])
        storage = Storage(config.state_dir)
        delivery = AlertDelivery(config, storage)
        try:
            with storage.db:
                delivery.queue(
                    "test",
                    {
                        "title": "[Switchboard] DOWN: homebridge (pi)",
                        "body": "Status: down\nDuration: 2 min",
                        "kind": "down",
                    },
                    100,
                )
            await asyncio.wait_for(delivery.deliver_due(100), 5)
            if starttls:
                assert len(messages) == 1
                assert "Subject: [Switchboard] DOWN: homebridge (pi)" in messages[0]
                assert "From: from@example.test" in messages[0]
                assert "To: to@example.test" in messages[0]
                assert "Duration: 2 min" in messages[0]
                assert commands[:4] == [
                    ("EHLO", False),
                    ("STARTTLS", False),
                    ("EHLO", True),
                    ("AUTH", True),
                ]
                assert delivery.status()["outbox_backlog"] == 0
            else:
                assert not messages
                assert not any(verb == "AUTH" for verb, _ in commands)
                assert delivery.status()["outbox_backlog"] == 1
            assert "fake-password" not in json.dumps(delivery.status())
        finally:
            storage.close()


async def test_doctor_channel_modes_allowlist_and_monitor_age(core, monkeypatch):
    config = core.config
    path = private_file(config, "ntfy.url", "https://ntfy.sh/topic-secret")
    config.alerts = alert_settings({"ntfy": {"url_file": path}})
    config.monitor["enabled"] = True
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["checks"]["monitor"]["status"] == "degraded"
    assert result["checks"]["alerts_ntfy"]["status"] == "ok"
    core.monitor.process([], 100)
    monkeypatch.setattr("switchboard.doctor.time.time", lambda: 100 + 180)
    assert (await core.call("dot", "ops_switchboard_status", {}))["checks"]["monitor"][
        "status"
    ] == "ok"
    monkeypatch.setattr("switchboard.doctor.time.time", lambda: 281)
    assert (await core.call("dot", "ops_switchboard_status", {}))["checks"]["monitor"][
        "status"
    ] == "degraded"
    secret = config.config_dir / "ntfy.url"
    secret.chmod(0o644)
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["checks"]["alerts_ntfy"]["status"] == "degraded"
    secret.chmod(0o600)
    secret.write_text("https://evil.test/topic-secret")
    assert core.alerts.config_status()["ntfy"]["status"] == "degraded"
    secret.unlink()
    assert core.alerts.config_status()["ntfy"]["status"] == "degraded"
    target = config.config_dir / "target"
    target.write_text("https://ntfy.sh/topic-secret")
    target.chmod(0o600)
    secret.symlink_to(target)
    assert core.alerts.config_status()["ntfy"]["status"] == "degraded"
    assert "topic-secret" not in json.dumps(result)
    # This diagnostic never queues or sends anything.
    assert core.alerts.status()["outbox_backlog"] == 0


async def test_ops_alerts_test_queues_every_channel(core):
    config = core.config
    email_config(config, 12345)
    config.alerts["ntfy"] = {"url_file": private_file(config, "ntfy.url", "https://ntfy.sh/topic")}
    change_policy(config, lambda p: p["clients"]["owner"]["scopes"].append("ops:admin"))
    # Keep the worker stopped: this test verifies queuing, not transport.
    await core.alerts.close()
    result = await core.call("owner", "ops_alerts_test", {})
    assert len(result["outbox_ids"]) == 2
    assert {r[0] for r in core.storage.db.execute("SELECT channel FROM alert_outbox")} == {
        "ntfy",
        "email",
    }
    assert (await core.call("dot", "ops_alerts_status", {}))["outbox_backlog"] == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_smtp_total_deadline_cancels_dripping_server_and_other_channel_progresses(
    config, monkeypatch, cancel
):
    connected, closed = asyncio.Event(), asyncio.Event()
    tasks = set()

    async def connection(reader, writer):
        tasks.add(asyncio.current_task())
        connected.set()

        async def drip():
            while True:
                writer.write(b"220-still talking\r\n")
                await writer.drain()
                await asyncio.sleep(0.02)

        task = asyncio.create_task(drip())
        try:
            with contextlib.suppress(ConnectionError):
                await reader.read()
        finally:
            closed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            writer.close()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(connection, "127.0.0.1", 0)
    email_config(config, server.sockets[0].getsockname()[1])
    monkeypatch.setattr("switchboard.alerts.SMTP_DEADLINE_S", 0.12)
    storage = Storage(config.state_dir)
    delivery = AlertDelivery(config, storage)
    payload = {"kind": "test", "title": "test", "body": ".dot\nbody"}
    sent = []
    original = delivery.send

    async def send(channel, payload):
        if channel == "ntfy":
            sent.append(channel)
        else:
            await original(channel, payload)

    monkeypatch.setattr(delivery, "send", send)
    monkeypatch.setattr(delivery, "channels", lambda: ["email", "ntfy"])
    try:
        with storage.db:
            delivery.queue("deadline", payload, 100)
        if cancel:
            task = asyncio.create_task(delivery.send("email", payload))
            await connected.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(delivery.deliver_due(100), 1)
            assert sent == ["ntfy"]
            assert delivery.status()["channels"]["email"]["last_failure_code"] == "transport_failed"
        await asyncio.wait_for(closed.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
        storage.close()


async def test_smtp_rejects_untrusted_tls_without_authentication(config, tls):
    async with smtp_server(tls) as (port, commands, messages):
        email_config(config, port)
        storage = Storage(config.state_dir)
        delivery = AlertDelivery(config, storage)
        try:
            with pytest.raises(ssl.SSLCertVerificationError):
                await delivery.send("email", {"kind": "test", "title": "test", "body": "test"})
            assert not messages
            assert not any(verb == "AUTH" for verb, _ in commands)
        finally:
            storage.close()
