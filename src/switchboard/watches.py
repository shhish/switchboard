"""One-off media watches, evaluated by the monitor and queued atomically."""

import asyncio
import json
import math
import time
from uuid import uuid4

from .domains.common import sanitize_text
from .domains.media import active_downloads, library_rows, show_watch_snapshot
from .errors import CapabilityError

CHECK_INTERVAL_S = 600
FIRED_RETENTION_S = 7 * 86400


class Watches:
    def __init__(self, core):
        self.core = core
        self.db = core.storage.db
        self.lock = asyncio.Lock()

    def purge(self, now):
        self.db.execute(
            "DELETE FROM alert_watches WHERE (fired_at IS NULL AND expires_at<=?) OR fired_at<=?",
            (now, now - FIRED_RETENTION_S),
        )

    async def add(self, client, args):
        kind = args["kind"]
        if kind == "download_stalled":
            valid = bool(args.get("title_contains", "").strip()) and not any(
                k in args for k in ("tmdb_id", "media", "season", "episode")
            )
        else:
            valid = "tmdb_id" in args and "media" in args and "title_contains" not in args
            valid &= ("season" in args) == ("episode" in args)
            valid &= args.get("media") == "show" or "season" not in args
        if not valid:
            raise CapabilityError("invalid_arguments", "Invalid fields for this watch kind")
        async with self.lock:
            now = time.time()
            with self.db:
                self.purge(now)
            count = self.db.execute(
                "SELECT COUNT(*) FROM alert_watches WHERE client=? AND fired_at IS NULL", (client,)
            ).fetchone()[0]
            if count >= 20:
                raise CapabilityError("watch_limit", "At most 20 active watches per client")
            state = {"missing_since": now} if kind == "download_stalled" else {}
            if kind == "media_available" and args["media"] == "show" and "season" not in args:
                _, baseline, _ = await show_watch_snapshot(self.core, args["tmdb_id"])
                state["baseline"] = baseline
            ident = "watch_" + uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO alert_watches "
                    "(id, client, kind, args, state, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        ident,
                        client,
                        kind,
                        json.dumps(args),
                        json.dumps(state),
                        now,
                        now + args.get("expires_days", 14) * 86400,
                    ),
                )
            return {"status": "ok", "watch_id": ident}

    async def list(self, client):
        async with self.lock:
            with self.db:
                self.purge(time.time())
            values = self.db.execute(
                "SELECT * FROM alert_watches WHERE client=? OR ?='owner' ORDER BY created_at DESC",
                (client, client),
            ).fetchall()
            return {
                "status": "ok",
                "watches": [
                    {
                        "watch_id": row["id"],
                        "client": row["client"],
                        **json.loads(row["args"]),
                        "created_at": row["created_at"],
                        "expires_at": row["expires_at"],
                        "last_checked_at": row["last_checked_at"],
                        "fired_at": row["fired_at"],
                    }
                    for row in values
                ],
            }

    async def remove(self, client, ident):
        async with self.lock:
            with self.db:
                cursor = self.db.execute(
                    "DELETE FROM alert_watches WHERE id=? AND (client=? OR ?='owner')",
                    (ident, client, client),
                )
            if not cursor.rowcount:
                raise CapabilityError("not_found", "Watch not found for this client")
            return {"status": "ok", "watch_id": ident}

    async def evaluate(self, now=None):
        now = time.time() if now is None else now
        async with self.lock:
            with self.db:
                self.purge(now)
            due = self.db.execute(
                "SELECT * FROM alert_watches WHERE fired_at IS NULL AND "
                "(last_checked_at IS NULL OR last_checked_at<=?) ORDER BY created_at",
                (now - CHECK_INTERVAL_S,),
            ).fetchall()
            for row in due:
                # Persist failed attempts too, to avoid hammering an unavailable backend.
                with self.db:
                    self.db.execute(
                        "UPDATE alert_watches SET last_checked_at=? WHERE id=?", (now, row["id"])
                    )
                try:
                    async with asyncio.timeout(90):
                        args, state = json.loads(row["args"]), json.loads(row["state"])
                        if row["kind"] == "media_available":
                            notice = await self.available(args, state)
                        else:
                            notice = await self.stalled(args, state, now)
                    with self.db:
                        fired = None
                        if notice:
                            title, body, kind = notice
                            if args.get("note"):
                                body += "\n" + sanitize_text(args["note"], 200)
                            ids = self.core.alerts.queue(
                                f"watch:{row['id']}",
                                {"title": title, "body": body, "kind": kind},
                                now,
                            )
                            if ids:
                                fired = now
                        self.db.execute(
                            "UPDATE alert_watches SET state=?, fired_at=? WHERE id=?",
                            (json.dumps(state), fired, row["id"]),
                        )
                except Exception:
                    # No payloads or exception text: a failed evaluation never fires.
                    continue

    async def available(self, args, state):
        if args["media"] == "movie":
            for row in await library_rows(self.core, "movie"):
                if row.get("tmdb_id") == args["tmdb_id"] and bool(row.get("has_file")):
                    title = sanitize_text(row["title"], 200)
                    return f"[{title}] [✅ Available]", "The movie is now in Plex.", "available"
            return None
        target = (args["season"], args["episode"]) if "season" in args else None
        title, episode, present = await show_watch_snapshot(
            self.core, args["tmdb_id"], baseline=state.get("baseline"), target=target
        )
        if present:
            body = f"S{episode[0]}E{episode[1]} is now in Plex."
            return f"[{title}] [✅ Available]", body, "available"
        return None

    async def stalled(self, args, state, now):
        data = await active_downloads(self.core)
        matches = []
        for row in data["downloads"]:
            if not isinstance(row.get("title"), str):
                raise CapabilityError("backend_invalid", "Invalid download title")
            if args["title_contains"].strip().casefold() in row["title"].casefold():
                matches.append(row)
        if not matches:
            since = state.setdefault("missing_since", now)
            state["items"] = {}
            if now - since >= 86400:
                title = sanitize_text(args["title_contains"], 200)
                return (
                    "[Download] [⚠️ Gone]",
                    f"{title}: the download is gone (no match for 24 h).",
                    "gone",
                )
            return None
        state.pop("missing_since", None)
        previous, items = state.get("items", {}), {}
        notice = None
        for row in matches:
            title = sanitize_text(row["title"], 200)
            status = row.get("status")
            if not isinstance(status, str):
                raise CapabilityError("backend_invalid", "Invalid download status")
            if status.casefold() in ("stalled", "warning", "failed"):
                notice = (
                    "[Download] [⚠️ Stalled]",
                    f"{title} is marked {status.casefold()}.",
                    "stalled",
                )
            ident = row.get("id")
            progress = row.get("progress")
            if (
                type(ident) not in (str, int)
                or type(progress) not in (int, float)
                or not math.isfinite(progress)
            ):
                if notice:
                    continue
                raise CapabilityError("backend_invalid", "Invalid download progress or identity")
            key = json.dumps([row.get("client"), ident])
            old = previous.get(key, {})
            changed = old.get("changed_at", now) if progress == old.get("progress") else now
            items[key] = {"progress": progress, "changed_at": changed}
            if now - changed >= 1800 and progress < 100:
                notice = (
                    "[Download] [⚠️ Stalled]",
                    f"{title} hasn't progressed for 30 min.",
                    "stalled",
                )
        state["items"] = items
        return notice
