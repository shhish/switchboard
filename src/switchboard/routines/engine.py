"""Runs approved routines: listens to HomeRelay, fires triggers, executes steps.

One asyncio task follows HomeRelay's event stream from a saved position, so a
restart replays what it missed. A second task ticks every few seconds for
held-for timers and time and sun triggers. Each run is its own task; every
step is journaled before and after it runs, and goes through `Core.call` as
the routine's author, so policy, approvals and audit apply as for any agent.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import partial

import aiohttp

from ..errors import CapabilityError
from ..execution import ROUTINE_CAUSE, Execution, ulid
from .evaluate import Evaluator, HomeIndex, Unresolved, check_value, crossed, local_zone
from .language import SECURE_TARGETS, format_duration, walk_steps
from .store import RoutineStore
from .templates import render
from .validator import state_id

log = logging.getLogger("switchboard.routines")

TICK_S = 5
STALE_EVENT_S = 600
MISFIRE_S = 600
MAX_DEPTH = 3
WRITES_PER_MINUTE = 20
BREAKER_FAILURES = 5
APPROVAL_POLL_S = 2
# Steps that are safe to run again after a crash interrupted them.
RESUMABLE = {
    "home.set",
    "home.scene",
    "state.set",
    "notify",
    "wait",
    "wait_until",
    "if",
    "repeat",
    "stop",
}


class Stop(Exception):
    """Ends a run early without failing it."""

    def __init__(self, reason="stopped"):
        self.reason = reason


class StepFailed(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Indeterminate(Exception):
    pass


def parse_ts(value):
    """HomeRelay's ISO 8601 timestamps to epoch ms (None if unparseable)."""
    if not isinstance(value, str):
        return None
    try:
        text = value.replace("Z", "+00:00")
        if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
            text = text[:-2] + ":" + text[-2:]
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


class RoutineEngine:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.clock = clock
        self.store = RoutineStore(core.storage, clock)
        self.index = HomeIndex(clock)
        self.zone = local_zone()
        weather = getattr(core.config, "weather", None) or {}
        location = (
            (weather["lat"], weather["lon"])
            if weather.get("lat") is not None and weather.get("lon") is not None
            else None
        )

        async def tools(author, tool, args):
            return await self.core.call(author, tool, args, Execution.new("routine"))

        self.evaluator = Evaluator(
            self.index,
            location=location,
            zone=self.zone,
            history=self.history_summary,
            tools=tools,
            clock=clock,
        )
        self.scenes = []
        self.versions = {}
        self.device_watch = defaultdict(list)
        self.held_watch = defaultdict(list)
        self.reach_watch = defaultdict(list)
        self.tasks = {}
        self.running = defaultdict(set)
        self.queued = defaultdict(deque)
        self.cancel_reasons = {}
        self.writes = defaultdict(deque)
        self.skips = {}
        self.changed = asyncio.Event()
        self.loops = []
        self.connected = False
        self.last_error = None
        self.index_ready = False
        self.pending_seq = None
        self.next_cache = {}

    # MARK: - Lifecycle

    async def start(self):
        if hasattr(self.core, "monitor"):
            self.core.monitor.on_event(self.on_service_event)
        self.load_versions()
        if not self.versions and not self.store.open_runs():
            return  # Nothing to do until a routine is approved.
        await self.ensure_running()

    async def ensure_running(self):
        if self.loops:
            return
        await self.refresh_index()
        self.rebuild()
        self.reconcile_held()
        self.resume_runs()
        self.loops = [asyncio.create_task(self.follow()), asyncio.create_task(self.tick_loop())]

    def save_position(self):
        if self.pending_seq is not None:
            self.store.set_meta("homerelay_seq", self.pending_seq)
            self.pending_seq = None

    async def close(self):
        self.save_position()
        for task in [*self.loops, *self.tasks.values()]:
            task.cancel()
        await asyncio.gather(*self.loops, *self.tasks.values(), return_exceptions=True)
        self.loops = []
        self.tasks = {}

    def load_versions(self):
        self.versions = {v["routine"]: v for v in self.store.active()}

    async def reload(self):
        """After an approval or a disable: pick up active versions and watches."""
        self.load_versions()
        for routine in list(self.running):
            if routine not in self.versions:
                for run_id in list(self.running[routine]):
                    self.cancel(run_id, "disabled")
        if self.versions:
            await self.ensure_running()
            self.rebuild()
            self.reconcile_held()

    def status(self):
        return {
            "paused": self.paused(),
            "active": len(self.versions),
            "running": sum(len(v) for v in self.running.values()),
            "homerelay": "connected"
            if self.connected
            else ("idle" if not self.loops else "reconnecting"),
            "position": self.store.meta("homerelay_seq"),
            "last_error": self.last_error,
        }

    # MARK: - HomeRelay

    async def refresh_index(self):
        try:
            rows = await self.core.backends.get("home", "/accessories")
            try:
                states = await self.core.backends.get("home", "/states")
            except CapabilityError:
                states = []  # An older HomeRelay without States.
            self.index.load(
                [*(rows if isinstance(rows, list) else []), *self.index.state_rows(states)]
            )
            scenes = await self.core.backends.get("home", "/scenes")
            self.scenes = scenes if isinstance(scenes, list) else []
            self.index_ready = True
        except CapabilityError as exc:
            self.last_error = exc.code
            log.warning("Routine engine: HomeRelay unavailable (%s)", exc.code)

    async def history_summary(self, characteristic_id, window_s):
        return await self.core.backends.get(
            "home",
            "/history/summary",
            params={"series": characteristic_id, "window": f"{window_s}s"},
        )

    async def follow(self):
        """Keeps one event stream open, resuming from the last handled seq."""
        delay = 1
        while True:
            try:
                await self.stream_once()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means reconnect
                self.connected = False
                self.last_error = type(exc).__name__
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def stream_once(self):
        backends = self.core.backends
        settings = backends.settings("home")
        params = {}
        if (seq := self.store.meta("homerelay_seq")) is not None:
            params["since"] = seq
        if store := self.store.meta("homerelay_store"):
            params["store"] = store
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=60)
        async with backends.session.get(
            settings["url"] + "/events",
            params=params,
            headers=backends.headers("home"),
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise CapabilityError("backend_error", f"home: events HTTP {response.status}")
            if store_id := response.headers.get("X-HomeRelay-Store"):
                self.store.set_meta("homerelay_store", store_id)
            self.connected = True
            if not self.index_ready:
                await self.refresh_index()
                self.rebuild()
            frame = {}
            async for raw in response.content:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if "data" in frame:
                        await self.on_frame(frame)
                    frame = {}
                elif line.startswith(":"):
                    continue
                else:
                    key, _, value = line.partition(":")
                    frame[key] = value[1:] if value.startswith(" ") else value
        self.connected = False

    async def on_frame(self, frame):
        try:
            event = json.loads(frame["data"])
        except ValueError:
            return
        if frame.get("event") == "gap" or event.get("type") == "gap":
            # Missed events: start from the current state instead.
            await self.refresh_index()
            self.rebuild()
            self.reconcile_held()
        else:
            await self.on_event(event)
        if (seq := frame.get("id")) and seq.isdigit():
            # Saved every tick (and on close), not per event: replaying a few
            # seconds after a crash is harmless, a commit per event isn't free.
            self.pending_seq = seq

    async def on_event(self, event):
        now = int(self.clock() * 1000)
        ts = parse_ts(event.get("ts")) or now
        if event.get("type") == "homes":
            await self.refresh_index()
            self.rebuild()
            return
        changes = self.index.apply(event, ts)
        if not changes:
            return
        live = now - ts <= STALE_EVENT_S * 1000
        cause = event.get("cause")
        for change in changes:
            if change[0] in ("value", "repeat"):
                cid = change[1]
                self.update_held(cid, ts)
                if live:
                    await self.device_triggers(change, event, cause)
            elif change[0] == "reachable":
                await self.reachability(change, ts, live, cause)
        self.changed.set()
        self.changed = asyncio.Event()
        await self.check_until()

    # MARK: - Watches

    def rebuild(self):
        self.device_watch.clear()
        self.held_watch.clear()
        self.reach_watch.clear()
        if not self.index_ready:
            return
        for routine, version in self.versions.items():
            for i, t in enumerate(version["definition"]["triggers"]):
                try:
                    if t["kind"] == "device":
                        for _aid, cid in self.index.resolve(t["device"]):
                            self.device_watch[cid].append((routine, i))
                    elif t["kind"] == "held":
                        for aid, cid in self.index.resolve(t["device"]):
                            self.held_watch[cid].append((routine, i, aid))
                    elif t["kind"] == "reachability":
                        for aid in self.index.accessories_for(t["device"]):
                            self.reach_watch[aid].append((routine, i))
                except Unresolved as exc:
                    log.warning("Routine %s trigger %d: %s", routine, i, exc)

    def trigger_info(self, routine, i, **extra):
        t = self.versions[routine]["definition"]["triggers"][i]
        info = {"kind": t["kind"], "index": i, **extra}
        if "id" in t:
            info["id"] = t["id"]
        return info

    async def device_triggers(self, change, event, cause):
        cid = change[1]
        c = self.index.chars.get(cid)
        for routine, i in list(self.device_watch.get(cid, [])):
            t = self.versions[routine]["definition"]["triggers"][i]
            if change[0] == "value":
                old, new = change[2], change[3]
                if not crossed(c["type"], old, new, t):
                    continue
            else:
                old = new = change[2]
                if c["type"] != "buttonEvent" or not crossed(
                    c["type"], None, new, {k: v for k, v in t.items() if k == "to"}
                ):
                    continue
            await self.fire(
                routine,
                self.trigger_info(
                    routine,
                    i,
                    accessory=event.get("accessory"),
                    room=event.get("room"),
                    accessory_id=c["accessory_id"],
                    characteristic=c["type"],
                    value=new,
                    previous=old,
                ),
                cause,
            )

    def held_true(self, t, aid, cid):
        a = self.index.accessories.get(aid)
        c = self.index.chars.get(cid)
        if not a or not c or not a["reachable"] or not c["seen"]:
            return False  # Unknown time never counts towards a hold.
        return bool(check_value(c["type"], c["value"], t["test"]))

    def update_held(self, cid, ts):
        for routine, i, aid in self.held_watch.get(cid, []):
            t = self.versions[routine]["definition"]["triggers"][i]
            if self.held_true(t, aid, cid):
                self.store.hold(routine, i, aid, ts)
            else:
                self.store.release(routine, i, aid)

    def reconcile_held(self):
        """On start or after a gap: holds match current values (time already held is kept)."""
        now = int(self.clock() * 1000)
        for cid, entries in self.held_watch.items():
            for routine, i, aid in entries:
                t = self.versions[routine]["definition"]["triggers"][i]
                if self.held_true(t, aid, cid):
                    # Count from when the value last changed, as far as we know it.
                    since = min(now, self.index.chars[cid]["since_ms"])
                    self.store.hold(routine, i, aid, since)
                else:
                    self.store.release(routine, i, aid)
        for aid, entries in self.reach_watch.items():
            for routine, i in entries:
                t = self.versions[routine]["definition"]["triggers"][i]
                if t["for_s"] and self.index.accessories[aid]["reachable"] == t["reachable"]:
                    self.store.hold(routine, i, aid, now)
                else:
                    self.store.release(routine, i, aid)

    async def reachability(self, change, ts, live, cause):
        _, aid, _, reachable = change
        for cid, entries in self.held_watch.items():
            for _routine, _i, held_aid in entries:
                if held_aid == aid:
                    self.update_held(cid, ts)
        for routine, i in list(self.reach_watch.get(aid, [])):
            t = self.versions[routine]["definition"]["triggers"][i]
            if reachable != t["reachable"]:
                self.store.release(routine, i, aid)
            elif t["for_s"]:
                self.store.hold(routine, i, aid, ts)
            elif live:
                await self.fire(
                    routine,
                    self.trigger_info(
                        routine,
                        i,
                        accessory=self.index.label(aid),
                        accessory_id=aid,
                        reachable=reachable,
                    ),
                    cause,
                )

    # MARK: - Switchboard events

    def on_service_event(self, event, service):
        """From the monitor (synchronous): fire matching `event:` triggers."""
        matches = [
            (routine, i)
            for routine, version in self.versions.items()
            for i, t in enumerate(version["definition"]["triggers"])
            if t["kind"] == "event" and t["name"] == event and t.get("service") in (None, service)
        ]
        for routine, i in matches:
            info = self.trigger_info(routine, i, event=event, service=service)
            task = asyncio.get_running_loop().create_task(self.fire(routine, info, None))
            key = f"event:{id(task)}"
            self.tasks[key] = task
            task.add_done_callback(partial(self.forget_task, key))

    def forget_task(self, key, _task):
        self.tasks.pop(key, None)

    # MARK: - Time

    async def tick_loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not stop the clock
                log.exception("Routine tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self):
        self.save_position()
        now = int(self.clock() * 1000)
        for row in self.store.all_held():
            version = self.versions.get(row["routine"])
            if not version or row["fired"]:
                continue
            t = version["definition"]["triggers"][row["trigger"]]
            if now - row["since_ms"] >= t["for_s"] * 1000:
                self.store.mark_held_fired(row["routine"], row["trigger"], row["accessory"])
                await self.fire(
                    row["routine"],
                    self.trigger_info(
                        row["routine"],
                        row["trigger"],
                        accessory=self.index.label(row["accessory"]),
                        accessory_id=row["accessory"],
                        held_s=(now - row["since_ms"]) // 1000,
                        duration=format_duration((now - row["since_ms"]) // 1000),
                    ),
                    None,
                )
        for routine, version in list(self.versions.items()):
            for i, t in enumerate(version["definition"]["triggers"]):
                if t["kind"] not in ("time", "every", "sun"):
                    continue
                last = self.store.fired(routine, i) or int(
                    (version["approved_at"] or self.clock()) * 1000
                )
                key = (routine, version["hash"], i, last)
                if key not in self.next_cache:
                    if len(self.next_cache) > 500:
                        self.next_cache.clear()
                    self.next_cache[key] = self.next_fire(t, last)
                due = self.next_cache[key]
                if due is None or due > now:
                    continue
                self.store.set_fired(
                    routine, i, max(due, now) if now - due > MISFIRE_S * 1000 else due
                )
                if now - due <= MISFIRE_S * 1000:
                    await self.fire(
                        routine,
                        self.trigger_info(
                            routine,
                            i,
                            scheduled=datetime.fromtimestamp(due / 1000, self.zone).isoformat(),
                        ),
                        None,
                    )
                else:
                    log.info(
                        "Routine %s: skipped a %s trigger missed by over 10 minutes",
                        routine,
                        t["kind"],
                    )
        await self.check_until()

    def next_fire(self, t, after_ms):
        """The first scheduled time strictly after `after_ms`, in epoch ms."""
        after = datetime.fromtimestamp(after_ms / 1000, self.zone)
        if t["kind"] == "every":
            return after_ms + int(t["every_s"] * 1000)
        if t["kind"] == "time":
            hour, minute = (int(x) for x in t["at"].split(":"))
            for offset in range(9):
                day = (after + timedelta(days=offset)).date()
                if day.weekday() not in t["days"]:
                    continue
                at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.zone)
                ms = int(at.timestamp() * 1000)
                if ms > after_ms:
                    return ms
            return None
        if not self.evaluator.location:
            return None
        from .sun import next_event

        lat, lon = self.evaluator.location
        cursor = after
        for _ in range(9):
            hit = next_event(
                t["event"], cursor, lat, lon, self.zone, offset_minutes=int(t["offset_s"] // 60)
            )
            if hit is None:
                return None
            if hit.astimezone(self.zone).weekday() in t["days"]:
                return int(hit.timestamp() * 1000)
            cursor = hit
        return None

    # MARK: - Firing

    async def fire(self, routine, info, cause, *, manual=False):
        version = self.versions.get(routine)
        if version is None or self.paused():
            return None
        definition = version["definition"]
        depth = 0
        if isinstance(cause, str) and cause.startswith("routine:"):
            parent = self.store.run(cause.split(":", 1)[1])
            if parent:
                depth = parent["depth"] + 1
                if depth > MAX_DEPTH:
                    return self.skip(routine, f"chain deeper than {MAX_DEPTH}")
                # Never fire from a chain this routine is already part of.
                link, hops = parent, 0
                while link and hops <= MAX_DEPTH:
                    if link["routine"] == routine:
                        return self.skip(
                            routine, "its own action, directly or through another routine"
                        )
                    cause_up = link.get("cause") or ""
                    link = (
                        self.store.run(cause_up.split(":", 1)[1])
                        if cause_up.startswith("routine:")
                        else None
                    )
                    hops += 1
        if not manual and definition["cooldown_s"]:
            last = self.store.last_started(routine)
            if last and self.clock() - last < definition["cooldown_s"]:
                return self.skip(routine, "cooling down")
        if not manual:
            notes = []
            ok = await self.evaluator.evaluate(
                definition.get("conditions"), {"trigger": info, "author": version["author"]}, notes
            )
            if ok is not True:
                return self.skip(
                    routine,
                    "conditions " + ("unknown: " + "; ".join(notes) if ok is None else "not met"),
                )
        mode = definition["mode"]
        running = self.running[routine]
        if running:
            if mode == "single":
                return self.skip(routine, "already running")
            if mode == "restart":
                for run_id in list(running):
                    self.cancel(run_id, "superseded")
            elif mode == "queued":
                if len(self.queued[routine]) >= definition["max_runs"]:
                    return self.skip(routine, "queue full")
                self.queued[routine].append((info, cause, depth))
                return "queued"
            elif mode == "parallel" and len(running) >= definition["max_runs"]:
                return self.skip(routine, "at max_runs")
        return self.start_run(version, info, cause, depth)

    def skip(self, routine, reason):
        self.skips[routine] = {"at": self.clock(), "reason": reason}
        return None

    def start_run(self, version, info, cause, depth):
        run_id = "run_" + ulid()
        ctx = Execution.new("routine", causation_id=run_id)
        self.store.add_run(
            run_id, version["routine"], version["hash"], info, depth, cause, ctx.trace_id
        )
        run = self.store.run(run_id)
        self.launch(run, version)
        return run_id

    def launch(self, run, version):
        self.running[version["routine"]].add(run["run_id"])
        self.tasks[run["run_id"]] = asyncio.create_task(self.execute(run, version))

    def cancel(self, run_id, reason):
        if task := self.tasks.get(run_id):
            self.cancel_reasons[run_id] = reason
            task.cancel()

    def resume_runs(self):
        for run in self.store.open_runs():
            version = self.store.version(hashed=run["hash"])
            if version is None or version["status"] != "active":
                self.store.finish_run(run["run_id"], "cancelled", "routine no longer active")
                continue
            self.launch(run, version)

    async def check_until(self):
        for routine, run_ids in list(self.running.items()):
            version = self.versions.get(routine)
            until = version and version["definition"].get("until")
            if not until or not run_ids:
                continue
            if await self.evaluator.evaluate(until, {"author": version["author"]}, []) is True:
                for run_id in list(run_ids):
                    self.cancel(run_id, "stopped by until")

    # MARK: - Running

    async def execute(self, run, version):
        run_id = run["run_id"]
        routine = version["routine"]
        context = {
            "trigger": run["trigger"],
            "routine": {"name": routine, "version": version["version"]},
            "run": {"id": run_id},
        }
        ctx = Execution(
            trace_id=run["trace_id"] or Execution.new().trace_id,
            span_id=Execution.new().span_id,
            origin="routine",
            causation_id=run_id,
        )
        status, error = "succeeded", None
        try:
            await self.steps(run, version, version["definition"]["steps"], "", context, ctx)
        except Stop as stop:
            status = "succeeded" if stop.reason == "stopped" else stop.reason
        except asyncio.CancelledError:
            status = self.cancel_reasons.pop(run_id, "cancelled")
            if status == "cancelled":
                # Shutdown: leave it open so the next start resumes it.
                self.forget(routine, run_id)
                raise
        except StepFailed as exc:
            status, error = "failed", f"{exc.code}: {exc}"
        except Indeterminate as exc:
            status, error = "indeterminate", str(exc)
            self.notify(
                f"routine:{run_id}:indeterminate",
                f"Routine {routine} needs a look",
                f"A step was interrupted mid-way and isn't safe to repeat: {exc}. Run {run_id}.",
            )
        except Exception as exc:  # noqa: BLE001 - a bug must end the run, not the engine
            log.exception("Routine %s run %s crashed", routine, run_id)
            status, error = "failed", f"internal: {type(exc).__name__}"
        self.store.finish_run(run_id, status, error)
        self.forget(routine, run_id)
        if (
            status in ("failed", "indeterminate")
            and self.store.recent_failures(routine, version["hash"]) >= BREAKER_FAILURES
        ):
            self.store.set_status(routine, "disabled", f"{BREAKER_FAILURES} failed runs in a row")
            self.notify(
                f"routine:{routine}:breaker:{version['hash'][:12]}",
                f"Routine {routine} turned off",
                (
                    f"It failed {BREAKER_FAILURES} times in a row (last: {error}). "
                    "Fix it and approve a new version."
                ),
            )
            await self.reload()
        if self.queued[routine] and routine in self.versions:
            info, cause, depth = self.queued[routine].popleft()
            self.start_run(self.versions[routine], info, cause, depth)

    def forget(self, routine, run_id):
        self.running[routine].discard(run_id)
        self.tasks.pop(run_id, None)

    async def steps(self, run, version, items, prefix, context, ctx):
        for i, step in enumerate(items):
            await self.step(run, version, step, f"{prefix}{i}", context, ctx)

    def paused(self):
        """The owner's switch, or a State named "Routines paused" that's on
        (so routines can be paused from the Home app)."""
        if self.store.paused:
            return True
        for a in self.index.accessories.values():
            if a["room"] == "States" and a["name"].casefold() == "routines paused":
                c = self.index.chars.get(a["id"] + ":on")
                return bool(c and c["value"] is True)
        return False

    def check_switches(self, version):
        if self.paused():
            raise Stop("paused")
        current = self.store.version(hashed=version["hash"])
        if current is None or current["status"] != "active":
            raise Stop("disabled")

    async def step(self, run, version, step, path, context, ctx):
        self.check_switches(version)
        run_id = run["run_id"]
        kind = step["kind"]
        journal = self.store.step(run_id, path)
        if journal and journal["status"] == "done" and kind not in ("if", "repeat"):
            return journal["output"]
        waiting = bool(journal and (journal["detail"] or {}).get("approval_id"))
        if (
            journal
            and journal["status"] == "started"
            and not waiting
            and kind not in RESUMABLE
            and not self.idempotent(step)
        ):
            raise Indeterminate(f"step {path} ({kind}) was running when Switchboard stopped")
        if kind == "stop":
            self.store.start_step(run_id, path, kind)
            self.store.finish_step(run_id, path, "done")
            raise Stop()
        if kind == "if":
            if journal and journal["status"] == "done":
                branch = journal["output"]["branch"]
            else:
                notes = []
                result = await self.evaluator.evaluate(
                    step["condition"],
                    {"trigger": run["trigger"], "author": version["author"]},
                    notes,
                )
                branch = "then" if result is True else ("else" if "else" in step else "none")
                self.store.start_step(run_id, path, kind)
                self.store.finish_step(
                    run_id, path, "done", {"branch": branch, "result": result, "notes": notes[:5]}
                )
            if branch in ("then", "else"):
                await self.steps(run, version, step[branch], f"{path}.{branch}.", context, ctx)
            return None
        if kind == "repeat":
            for k in range(step["count"]):
                await self.steps(run, version, step["steps"], f"{path}#{k}.", context, ctx)
            return None
        wake_at = journal["wake_at"] if journal else None
        if kind == "wait":
            wake_at = wake_at or self.clock() + step["seconds"]
        elif kind == "wait_until":
            wake_at = wake_at or self.clock() + step["timeout_s"]
        detail = journal["detail"] if journal else None
        self.store.start_step(run_id, path, kind, detail, wake_at)
        try:
            output = await self.perform(run, version, step, path, context, ctx, wake_at, detail)
        except StepFailed as exc:
            self.store.finish_step(run_id, path, "failed", error=f"{exc.code}: {exc}")
            raise
        except Stop:
            self.store.finish_step(run_id, path, "done", {"stopped": True})
            raise
        self.store.finish_step(run_id, path, "done", output)
        return output

    def idempotent(self, step):
        if step["kind"] == "call":
            cap = self.core.registry.capabilities.get(step["tool"])
            return bool(cap and cap.idempotent and cap.tier == "read")
        return step["kind"] in RESUMABLE

    async def perform(self, run, version, step, path, context, ctx, wake_at, detail):
        kind = step["kind"]
        if kind == "wait":
            await asyncio.sleep(max(0, wake_at - self.clock()))
            return None
        if kind == "wait_until":
            return await self.wait_until(step, run, wake_at, version["author"])
        if kind == "notify":
            title = render(step["title"], context)
            body = render(step["message"], context)
            self.notify(f"routine:{run['run_id']}:{path}", str(title)[:80], str(body)[:500])
            return {"notified": True}
        if kind == "home.set":
            return await self.home_set(run, version, step, ctx)
        if kind == "home.set_secure":
            return await self.home_set_secure(run, version, step, path, ctx, detail)
        if kind == "state.set":
            try:
                sid = state_id(self.index, step["state"])
            except Unresolved as exc:
                raise StepFailed("state_not_found", str(exc)) from None
            await self.call_tool(
                run, version, "home_state_set", {"state": sid, "on": step["on"]}, ctx
            )
            return {"state": sid, "on": step["on"]}
        if kind == "home.scene":
            scene = self.scene_id(step["scene"])
            await self.call_tool(run, version, "home_run_scene", {"scene_id": scene}, ctx)
            return {"scene_id": scene}
        if kind == "call":
            args = render(step["args"], context)
            result = await self.call_tool(
                run, version, step["tool"], args, ctx, path=path, detail=detail
            )
            return {"status": result.get("status")}
        raise StepFailed("unknown_step", f"Unknown step {kind}")

    async def wait_until(self, step, run, deadline, author):
        since = None
        while True:
            notes = []
            result = await self.evaluator.evaluate(
                step["condition"], {"trigger": run["trigger"], "author": author}, notes
            )
            now = self.clock()
            if result is True:
                since = since or now
                if now - since >= step["for_s"]:
                    return {"met": True}
            else:
                since = None
            if now >= deadline:
                if step["on_timeout"] == "stop":
                    raise Stop("succeeded")
                return {"met": False, "timed_out": True}
            waiter = self.changed
            remaining = deadline - now
            if since is not None:
                remaining = min(remaining, step["for_s"] - (now - since))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter.wait(), timeout=max(0.05, min(remaining, TICK_S)))

    def accessories(self, sel):
        try:
            return self.index.accessories_for(sel)
        except Unresolved as exc:
            raise StepFailed("device_not_found", str(exc)) from None

    def rate_check(self, aid):
        now = self.clock()
        window = self.writes[aid]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= WRITES_PER_MINUTE:
            raise StepFailed(
                "rate_capped",
                f"over {WRITES_PER_MINUTE} routine writes a minute to {self.index.label(aid)}",
            )
        window.append(now)

    async def home_set(self, run, version, step, ctx):
        values = dict(step["values"])
        power = values.pop("on", None)
        done = []
        for aid in self.accessories(step["device"]):
            # Power on first, set values, power off last, so lights don't flash.
            actions = []
            if power is True:
                actions.append({"action": "on"})
            actions += [
                {"action": "set", "characteristic": k, "value": v} for k, v in values.items()
            ]
            if power is False:
                actions.append({"action": "off"})
            for action in actions:
                await self.call_tool(run, version, "home_set", {"accessory_id": aid, **action}, ctx)
            done.append(aid)
        return {"accessories": done}

    async def home_set_secure(self, run, version, step, path, ctx, detail):
        characteristic, value = SECURE_TARGETS[step["values"]["target"]]
        aids = self.accessories(step["device"])
        if len(aids) != 1:
            raise StepFailed("ambiguous", "home.set_secure acts on one device at a time")
        args = {
            "accessory_id": aids[0],
            "action": "set",
            "characteristic": characteristic,
            "value": value,
        }
        await self.call_tool(run, version, "home_set_secure", args, ctx, path=path, detail=detail)
        return {"accessory_id": aids[0], "target": step["values"]["target"]}

    def scene_id(self, ref):
        for scene in self.scenes:
            if ref.casefold() in (
                str(scene.get("id", "")).casefold(),
                str(scene.get("name", "")).casefold(),
            ):
                return scene["id"]
        raise StepFailed("scene_not_found", f"No scene '{ref}'")

    async def call_tool(self, run, version, tool, args, ctx, *, path=None, detail=None):
        cap = self.core.registry.capabilities.get(tool)
        if cap is None:
            raise StepFailed("unknown_capability", f"No tool {tool}")
        if cap.service == "home" and cap.tier != "read":
            target = args.get("accessory_id") or args.get("scene_id") or args.get("state") or tool
            self.rate_check(str(target))
        if cap.scope not in version["scopes"]:
            raise StepFailed(
                "scope_not_approved",
                f"{tool} needs {cap.scope}, which this version wasn't approved for",
            )
        token = ROUTINE_CAUSE.set(f"routine:{run['run_id']}")
        try:
            approval_id = (detail or {}).get("approval_id")
            result = (
                None if approval_id else await self.core.call(version["author"], tool, args, ctx)
            )
            if approval_id or (result or {}).get("status") == "approval_required":
                approval_id = approval_id or result["approval_id"]
                if path is not None:
                    self.store.start_step(
                        run["run_id"], path, "approval", {"approval_id": approval_id}
                    )
                self.store.set_run_status(run["run_id"], "waiting")
                if result:
                    self.notify(
                        f"routine:{run['run_id']}:{approval_id}",
                        f"Routine {version['routine']} needs your OK",
                        f"{result.get('summary', tool)}. Approve with: sb approve {approval_id}",
                    )
                await self.await_approval(approval_id)
                self.store.set_run_status(run["run_id"], "running")
                result = await self.core.call(
                    version["author"], tool, {**args, "approval_id": approval_id}, ctx
                )
        finally:
            ROUTINE_CAUSE.reset(token)
        if not isinstance(result, dict) or result.get("status") == "error" or "error" in result:
            code = ((result or {}).get("error") or {}).get("code", "error")
            raise StepFailed(code, f"{tool} failed: {code}")
        return result

    async def await_approval(self, approval_id):
        while True:
            try:
                row = self.core.approvals.get(approval_id)
            except CapabilityError:
                raise Stop("denied") from None
            if row["status"] == "approved":
                return
            if row["status"] == "used":
                # Approved and spent, but the run stopped before recording the result.
                raise Indeterminate(
                    f"approval {approval_id} was used but the result wasn't recorded"
                )
            if row["status"] == "denied" or row["expires_at"] <= self.clock():
                raise Stop("denied" if row["status"] == "denied" else "approval expired")
            await asyncio.sleep(APPROVAL_POLL_S)

    def notify(self, key, title, body):
        try:
            self.core.alerts.queue(key[:200], {"kind": "routine", "title": title, "body": body})
            self.core.storage.db.commit()
        except Exception:  # noqa: BLE001 - a notification failure must not fail the run
            log.exception("Routine notification failed")

    # MARK: - Manual runs and plans

    async def run_manual(self, routine, client):
        version = self.versions.get(routine)
        if version is None:
            raise CapabilityError("not_found", "No active routine by that name")
        if not any(t["kind"] == "manual" for t in version["definition"]["triggers"]):
            raise CapabilityError("not_manual", "This routine has no manual trigger")
        if self.paused():
            raise CapabilityError("paused", "Routines are paused")
        index = next(
            i for i, t in enumerate(version["definition"]["triggers"]) if t["kind"] == "manual"
        )
        await self.ensure_running()
        return await self.fire(
            routine, self.trigger_info(routine, index, by=client), None, manual=True
        )

    async def plan(self, definition):
        """A dry run: what would fire, whether conditions pass now, and the calls
        the steps would make. Nothing is written."""
        if not self.index_ready:
            await self.refresh_index()
        now_ms = int(self.clock() * 1000)
        triggers = []
        for i, t in enumerate(definition["triggers"]):
            entry = {"index": i, "kind": t["kind"]}
            try:
                if t["kind"] in ("device", "held"):
                    entry["devices"] = [
                        self.index.label(a) for a, _ in self.index.resolve(t["device"])
                    ]
                elif t["kind"] == "reachability":
                    entry["devices"] = [
                        self.index.label(a) for a in self.index.accessories_for(t["device"])
                    ]
                elif t["kind"] in ("time", "every", "sun"):
                    nxt = self.next_fire(t, now_ms)
                    entry["next"] = (
                        datetime.fromtimestamp(nxt / 1000, self.zone).isoformat() if nxt else None
                    )
            except Unresolved as exc:
                entry["problem"] = str(exc)
            triggers.append(entry)
        notes = []
        conditions = await self.evaluator.evaluate(definition.get("conditions"), None, notes)
        actions = []
        for path, step in walk_steps(definition["steps"]):
            entry = {"path": path, "kind": step["kind"]}
            try:
                if step["kind"] in ("home.set", "home.set_secure"):
                    entry["devices"] = [
                        self.index.label(a) for a in self.index.accessories_for(step["device"])
                    ]
                    entry["values"] = step["values"]
                    entry["tool"] = (
                        "home_set"
                        if step["kind"] == "home.set"
                        else "home_set_secure (asks you each time)"
                    )
                elif step["kind"] == "home.scene":
                    entry["scene_id"] = self.scene_id(step["scene"])
                elif step["kind"] == "state.set":
                    entry["state"] = state_id(self.index, step["state"])
                    entry["on"] = step["on"]
                elif step["kind"] == "call":
                    entry["tool"] = step["tool"]
                    entry["args"] = step["args"]
                elif step["kind"] == "if":
                    branch_notes = []
                    entry["now"] = await self.evaluator.evaluate(
                        step["condition"], None, branch_notes
                    )
                    if branch_notes:
                        entry["notes"] = branch_notes[:5]
                elif step["kind"] in ("wait", "wait_until"):
                    entry["seconds"] = step.get("seconds", step.get("timeout_s"))
            except (Unresolved, StepFailed) as exc:
                entry["problem"] = str(exc)
            actions.append(entry)
        return {
            "triggers": triggers,
            "conditions_now": conditions,
            "condition_notes": notes[:10],
            "steps": actions,
        }
