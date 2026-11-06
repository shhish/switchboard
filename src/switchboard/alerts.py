"""Durable alert delivery, bounded transports and redacted error codes."""

import asyncio
import base64
import json
import re
import ssl
import time
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from uuid import uuid4

import aiohttp

from .alert_config import ntfy_url
from .auth import read_private

BACKOFF_MINUTES = (1, 2, 5, 15, 30, 60)
MAX_AGE_S = 86400
RETENTION_S = 14 * 86400
SMTP_DEADLINE_S = 30


class AlertDelivery:
    def __init__(self, config, storage):
        self.config = config
        self.db = storage.db if storage else None
        self.session = None
        self.task = None
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()

    def channels(self):
        return [name for name in ("ntfy", "email") if name in self.config.alerts]

    def secrets(self, channel):
        settings = self.config.alerts[channel]
        values = {
            key[:-5]: read_private(Path(value))
            for key, value in settings.items()
            if key.endswith("_file")
        }
        if any(not value or "\r" in value or "\n" in value for value in values.values()):
            raise ValueError("Invalid alert credential file")
        if channel == "ntfy":
            ntfy_url(values["url"], self.config.alerts)
        for key in ("from", "to") if channel == "email" else ():
            # One mailbox per file; prevent header/envelope injection and ambiguous lists.
            if not values[key].isascii() or not re.fullmatch(
                r"[^\s<>@,;]+@[^\s<>@,;]+", values[key]
            ):
                raise ValueError("Invalid alert mailbox")
        return values

    def config_status(self):
        result = {}
        for channel in self.channels():
            try:
                self.secrets(channel)
                result[channel] = {"status": "ok", "reason": "valid private files and host"}
            except (OSError, ValueError, UnicodeError):
                result[channel] = {
                    "status": "degraded",
                    "reason": "invalid channel config or private files",
                }
        return result

    def queue(self, dedupe_key, payload, now=None):
        """Caller commits with its incident transition: no await before durable write."""
        now = time.time() if now is None else now
        if payload["kind"] == "recovery":
            self.supersede(dedupe_key.split(":", 1)[0], now)
        ids = []
        for channel in self.channels():
            ident = "alert_" + uuid4().hex
            self.db.execute(
                "INSERT OR IGNORE INTO alert_outbox "
                "(id, channel, payload, dedupe_key, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ident, channel, json.dumps(payload), dedupe_key, now, now),
            )
            row = self.db.execute(
                "SELECT id FROM alert_outbox WHERE channel=? AND dedupe_key=?",
                (channel, dedupe_key),
            ).fetchone()
            ids.append(row["id"])
        self.wake.set()
        return ids

    def supersede(self, incident, now):
        # Exact incident prefix, not LIKE: names/IDs can contain wildcard characters.
        self.db.execute(
            "UPDATE alert_outbox SET superseded_at=? WHERE delivered_at IS NULL "
            "AND failed_at IS NULL AND superseded_at IS NULL "
            "AND substr(dedupe_key, 1, instr(dedupe_key, ':')-1)=? "
            "AND json_extract(payload, '$.kind') IN ('down', 'reminder')",
            (now, incident),
        )

    def prune(self, now):
        row = self.db.execute(
            "SELECT value FROM monitor_meta WHERE key='last_retention_at'"
        ).fetchone()
        if row and now - row[0] < 3600:
            return
        with self.db:
            self.db.execute(
                "DELETE FROM alert_outbox WHERE "
                "coalesce(delivered_at, failed_at, superseded_at) IS NOT NULL AND "
                "coalesce(delivered_at, failed_at, superseded_at)<?",
                (now - RETENTION_S,),
            )
            self.db.execute("DELETE FROM monitor_silence WHERE until<=?", (now,))
            self.db.execute(
                "INSERT OR REPLACE INTO monitor_meta VALUES ('last_retention_at', ?)", (now,)
            )
        # Monitor state is one row per configured probe, not append-only history.

    def test(self):
        with self.db:
            return self.queue(
                "test:" + uuid4().hex,
                {
                    "title": "[Test] [✅ Up]",
                    "body": "Test alert: home server alerts are working.",
                    "kind": "test",
                },
            )

    async def start(self):
        self.session = aiohttp.ClientSession(
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=20),
        )
        # Delivery retries continue even if monitoring has since been disabled.
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.session:
            await self.session.close()

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.deliver_due()
            except Exception:
                # Keep the worker alive; no exception strings, URLs or credentials in logs.
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except TimeoutError:
                pass

    async def deliver_due(self, now=None):
        async with self.lock:
            await self._deliver_due(now)

    async def _deliver_due(self, now=None):
        real_clock = now is None
        now = time.time() if real_clock else now
        self.prune(now)
        rows = self.db.execute(
            "SELECT * FROM alert_outbox WHERE delivered_at IS NULL AND failed_at IS NULL "
            "AND superseded_at IS NULL "
            "AND (next_attempt_at<=? OR created_at<=?) ORDER BY created_at LIMIT 100",
            (now, now - MAX_AGE_S),
        ).fetchall()
        for row in rows:
            # Queueing recovery/silence can invalidate rows while an earlier send awaits I/O.
            current = self.db.execute(
                "SELECT superseded_at FROM alert_outbox WHERE id=?", (row["id"],)
            ).fetchone()
            if not current or current[0] is not None:
                continue
            if real_clock:
                now = time.time()
            if now - row["created_at"] >= MAX_AGE_S:
                with self.db:
                    self.db.execute(
                        "UPDATE alert_outbox SET failed_at=?, last_error_code='expired' WHERE id=?",
                        (now, row["id"]),
                    )
                    self.record_channel(row["channel"], now, "expired")
                continue
            try:
                await self.send(row["channel"], json.loads(row["payload"]))
                code = None
            except (aiohttp.ClientError, TimeoutError):
                code = "transport_failed"
            except (OSError, ValueError, UnicodeError):
                code = "delivery_failed"
            except Exception:
                code = "delivery_failed"
            attempts = row["attempts"] + 1
            completed = time.time() if real_clock else now
            delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)] * 60
            with self.db:
                if code is None and json.loads(row["payload"])["kind"] in ("down", "reminder"):
                    self.db.execute(
                        "UPDATE monitor_state SET notified=1, alerted=1 WHERE incident_id=?",
                        (row["dedupe_key"].split(":", 1)[0],),
                    )
                self.record_channel(row["channel"], completed, code)
                self.db.execute(
                    "UPDATE alert_outbox SET attempts=?, next_attempt_at=?, delivered_at=?, "
                    "last_error_code=? WHERE id=?",
                    (
                        attempts,
                        completed + delay,
                        completed if code is None else None,
                        code,
                        row["id"],
                    ),
                )

    def record_channel(self, channel, now, code):
        self.db.execute("INSERT OR IGNORE INTO alert_channel_state(channel) VALUES (?)", (channel,))
        if code is None:
            self.db.execute(
                "UPDATE alert_channel_state SET last_success_at=? WHERE channel=?", (now, channel)
            )
        else:
            self.db.execute(
                "UPDATE alert_channel_state SET last_failure_at=?, last_error_code=? "
                "WHERE channel=?",
                (now, code, channel),
            )

    async def send(self, channel, payload):
        values = await asyncio.to_thread(self.secrets, channel)
        if channel == "ntfy":
            async with self.session.post(
                values["url"],
                data=payload["body"].encode(),
                allow_redirects=False,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Title": payload["title"],
                    "Priority": "high" if payload["kind"] in ("down", "reminder") else "default",
                    "Tags": "white_check_mark" if payload["kind"] == "recovery" else "warning",
                },
            ) as response:
                if not 200 <= response.status < 300:
                    raise ValueError("ntfy delivery rejected")
        elif channel == "email":
            await self.smtp(values, payload)
        else:
            raise ValueError("Channel no longer configured")

    async def smtp(self, values, payload):
        """Verified STARTTLS only; the total deadline cancels I/O and aborts the socket."""
        settings = self.config.alerts["email"]
        message = EmailMessage(policy=SMTP)
        message["From"], message["To"] = values["from"], values["to"]
        message["Subject"] = payload["title"]
        message.set_content(payload["body"])
        writer = None
        try:
            async with asyncio.timeout(SMTP_DEADLINE_S):
                reader, writer = await asyncio.open_connection(settings["host"], settings["port"])

                async def reply(expected):
                    lines = []
                    for _ in range(100):
                        line = await reader.readline()
                        if len(line) < 5 or not line.endswith(b"\r\n") or not line[:3].isdigit():
                            raise ValueError("Invalid SMTP response")
                        if int(line[:3]) != expected or line[3:4] not in (b"-", b" "):
                            raise ValueError("SMTP command rejected")
                        lines.append(line[4:-2].upper())
                        if line[3:4] == b" ":
                            return lines
                    raise ValueError("SMTP response too large")

                async def command(value, expected):
                    writer.write(value + b"\r\n")
                    await writer.drain()
                    return await reply(expected)

                await reply(220)
                features = await command(b"EHLO localhost", 250)
                if b"STARTTLS" not in features:
                    raise ValueError("SMTP requires STARTTLS")
                await command(b"STARTTLS", 220)
                await writer.start_tls(
                    ssl.create_default_context(), server_hostname=settings["host"]
                )
                features = await command(b"EHLO localhost", 250)
                auth = {
                    item
                    for line in features
                    if line.startswith(b"AUTH ")
                    for item in line.split()[1:]
                }
                if b"PLAIN" in auth:
                    credential = base64.b64encode(
                        ("\0" + values["username"] + "\0" + values["password"]).encode()
                    )
                    await command(b"AUTH PLAIN " + credential, 235)
                elif b"LOGIN" in auth:
                    await command(b"AUTH LOGIN", 334)
                    await command(base64.b64encode(values["username"].encode()), 334)
                    await command(base64.b64encode(values["password"].encode()), 235)
                else:
                    raise ValueError("SMTP authentication unavailable")
                await command(f"MAIL FROM:<{values['from']}>".encode("ascii"), 250)
                await command(f"RCPT TO:<{values['to']}>".encode("ascii"), 250)
                await command(b"DATA", 354)
                body = message.as_bytes()
                body = re.sub(rb"(?m)^\.", b"..", body)
                writer.write(body + b".\r\n")
                await writer.drain()
                await reply(250)
                # Remote acceptance is success. QUIT isn't needed to commit DATA.
        finally:
            if writer is not None:
                writer.close()
                writer.transport.abort()

    def status(self):
        channels = {}
        for channel in self.channels():
            state = self.db.execute(
                "SELECT * FROM alert_channel_state WHERE channel=?", (channel,)
            ).fetchone()
            channels[channel] = {
                "last_success_at": state["last_success_at"] if state else None,
                "last_failure_at": state["last_failure_at"] if state else None,
                "last_failure_code": state["last_error_code"] if state else None,
            }
        backlog = self.db.execute(
            "SELECT COUNT(*) FROM alert_outbox WHERE delivered_at IS NULL AND failed_at IS NULL "
            "AND superseded_at IS NULL"
        ).fetchone()[0]
        failed_count = self.db.execute(
            "SELECT COUNT(*) FROM alert_outbox WHERE failed_at IS NOT NULL"
        ).fetchone()[0]
        return {"channels": channels, "outbox_backlog": backlog, "outbox_failed": failed_count}
