"""Routines end to end against a fake HomeRelay: propose, approve, trigger, run."""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from aiohttp import web

from switchboard.core import Core
from switchboard.routines import engine as engine_module
from switchboard.routines.evaluate import HomeIndex, check_value, crossed, tri_all
from switchboard.routines.language import RoutineError, definition_hash, parse

from .conftest import running_app

HALL_LIGHT = """
routine: home.hall_motion_light
description: Hall light on with motion at night, off after it clears.
mode: restart
triggers:
  - device: {device: {room: Hall, type: motion}, to: detected}
steps:
  - home.set: {device: Hall/Ceiling, on: true, brightness: 40}
  - wait_until: {condition: {device: {room: Hall, type: motion}, is: clear}, timeout: 30m}
  - home.set: {device: Hall/Ceiling, on: false}
"""

GARAGE = """
routine: home.garage_left_open
mode: single
triggers:
  - held: {device: Garage/Door, is: open, for: 1s}
steps:
  - notify: {title: Garage, message: "Garage open for {{ trigger.duration }}"}
  - home.set_secure: {device: Garage/Door, target: closed}
"""


def accessory(aid, name, room, category, services, reachable=True):
    return {
        "id": aid,
        "name": name,
        "room": room,
        "home": "Home",
        "category": category,
        "reachable": reachable,
        "bridged": False,
        "state": {},
        "services": services,
    }


def char(cid, ctype, value, writable=False):
    return {
        "id": cid,
        "type": ctype,
        "label": ctype,
        "value": value,
        "readable": True,
        "writable": writable,
        "notifies": True,
    }


def home_rows():
    return [
        accessory(
            "motion-1",
            "Motion",
            "Hall",
            "sensor",
            [
                {
                    "id": "s-m",
                    "type": "motionSensor",
                    "name": "Motion",
                    "primary": True,
                    "characteristics": [char("c-motion", "motion", False)],
                },
            ],
        ),
        accessory(
            "light-1",
            "Ceiling",
            "Hall",
            "lightbulb",
            [
                {
                    "id": "s-l",
                    "type": "lightbulb",
                    "name": "Ceiling",
                    "primary": True,
                    "characteristics": [
                        char("c-on", "on", False, True),
                        char("c-bri", "brightness", 10, True),
                    ],
                },
            ],
        ),
        accessory(
            "garage-1",
            "Door",
            "Garage",
            "garageDoor",
            [
                {
                    "id": "s-g",
                    "type": "garageDoor",
                    "name": "Door",
                    "primary": True,
                    "characteristics": [
                        char("c-door", "doorState", 1),
                        char("c-target", "doorTarget", 1, True),
                    ],
                },
            ],
        ),
    ]


class FakeHomeRelay:
    def __init__(self):
        self.rows = home_rows()
        self.states = [
            {"id": "guest-mode", "name": "Guest mode", "kind": "switch", "on": False},
            {"id": "routines-paused", "name": "Routines paused", "kind": "switch", "on": False},
        ]
        self.writes = []
        self.events = asyncio.Queue()
        self.seq = 0
        self.streams = 0
        self.since = []

    def row(self, aid):
        return next(r for r in self.rows if r["id"] == aid)

    def push(self, cid, value, **extra):
        self.seq += 1
        aid = next(
            r["id"]
            for r in self.rows
            for s in r["services"]
            for c in s["characteristics"]
            if c["id"] == cid
        )
        event = {
            "type": "characteristic",
            "accessoryId": aid,
            "accessory": self.row(aid)["name"],
            "room": self.row(aid)["room"],
            "characteristicId": cid,
            "value": value,
            "origin": "notification",
            "seq": self.seq,
            "ts": datetime.now(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            **extra,
        }
        self.events.put_nowait((self.seq, event))
        return self.seq

    def app(self):
        async def accessories(request):
            return web.json_response(self.rows)

        async def one(request):
            aid = request.match_info["aid"]
            if request.method == "POST":
                body = await request.json()
                self.writes.append(("POST", aid, body, request.headers.get("X-HomeRelay-Cause")))
            return web.json_response(self.row(aid))

        async def characteristic(request):
            cid = request.match_info["cid"]
            body = await request.json()
            self.writes.append(("PUT", cid, body, request.headers.get("X-HomeRelay-Cause")))
            return web.json_response({"id": cid, "value": body["value"]})

        async def states(request):
            return web.json_response(self.states)

        async def set_state(request):
            sid = request.match_info["sid"]
            body = await request.json()
            self.writes.append(("STATE", sid, body, request.headers.get("X-HomeRelay-Cause")))
            row = next(s for s in self.states if s["id"] == sid)
            row["on"] = body["on"]
            return web.json_response(row)

        async def scenes(request):
            return web.json_response(
                [{"id": "sc-1", "name": "Movie", "home": "Home", "kind": "user"}]
            )

        async def summary(request):
            return web.json_response({"summary": {"mean": 21.5, "on": 120, "transitions": 2}})

        async def events(request):
            self.streams += 1
            self.since.append(request.query.get("since"))
            response = web.StreamResponse(
                headers={"Content-Type": "text/event-stream", "X-HomeRelay-Store": "STORE-1"}
            )
            await response.prepare(request)
            await response.write(b": homerelay events\n\n")
            while True:
                seq, event = await self.events.get()
                await response.write(
                    f"id: {seq}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                )

        app = web.Application()
        app.router.add_get("/api/v1/accessories", accessories)
        app.router.add_route("*", "/api/v1/accessories/{aid}", one)
        app.router.add_put("/api/v1/characteristics/{cid}", characteristic)
        app.router.add_get("/api/v1/scenes", scenes)
        app.router.add_get("/api/v1/states", states)
        app.router.add_put("/api/v1/states/{sid}", set_state)
        app.router.add_get("/api/v1/history/summary", summary)
        app.router.add_get("/api/v1/events", events)
        return app


@pytest.fixture
async def home(config, monkeypatch):
    monkeypatch.setattr(engine_module, "TICK_S", 0.05)
    monkeypatch.setattr(engine_module, "APPROVAL_POLL_S", 0.05)
    fake = FakeHomeRelay()
    token = config.config_dir / "home.secret"
    token.write_text("fake-home")
    token.chmod(0o600)
    policy = yaml.safe_load((config.config_dir / "policy.yaml").read_text())
    agent = [
        "home:read",
        "home:control",
        "home:secure",
        "routines:read",
        "routines:propose",
        "routines:run",
        "routines:disable",
    ]
    policy["clients"]["claude"]["scopes"] = agent
    policy["clients"]["dot"]["scopes"] = ["routines:read"]
    policy["clients"]["owner"]["scopes"] = agent + ["routines:approve", "approvals:confirm"]
    policy["allowlists"]["home"] = {"allow_all_safe": True, "secure_accessories": ["garage-1"]}
    (config.config_dir / "policy.yaml").write_text(yaml.safe_dump(policy))
    async with running_app(fake.app()) as url:
        config.backends["home"].update(
            url=url + "/api/v1", token_file=str(token), control_token_file=str(token)
        )
        core = Core(config)
        await core.start()
        try:
            yield core, fake
        finally:
            await core.close()


async def until(predicate, seconds=5):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def activate(core, source, author="claude"):
    proposed = await core.call(author, "routines_propose", {"yaml": source})
    assert proposed["status"] == "proposed", proposed
    approved = await core.call(
        "owner", "routines_approve", {"routine": proposed["routine"], "hash": proposed["hash"]}
    )
    assert approved["status"] == "approved", approved
    await until(lambda: core.routines.connected)
    return proposed


# MARK: - Language


def test_parses_examples_and_hashes_stably():
    a = parse(HALL_LIGHT)
    assert a["mode"] == "restart"
    assert a["triggers"][0] == {
        "kind": "device",
        "device": {"room": "Hall", "type": "motion"},
        "to": "detected",
    }
    assert a["steps"][1]["timeout_s"] == 1800
    # Comments and key order don't change the hash; scopes do.
    b = parse("# a comment\n" + HALL_LIGHT.replace("mode: restart\n", "") + "mode: restart\n")
    assert definition_hash(a, {"home:control"}) == definition_hash(b, {"home:control"})
    assert definition_hash(a, {"home:control"}) != definition_hash(
        a, {"home:control", "home:secure"}
    )


@pytest.mark.parametrize(
    "source,problem",
    [
        ("routine: Bad Name\ntriggers: [manual]\nsteps: [stop]", "dotted lowercase"),
        ("routine: a.b\ntriggers: []\nsteps: [stop]", "1 to 10 triggers"),
        ("routine: a.b\ntriggers: [manual]\nsteps: [{shell: ls}]", "exactly one of"),
        ("routine: a.b\ntriggers: [{time: '25:00'}]\nsteps: [stop]", "24-hour time"),
        ("routine: a.b\ntriggers: [manual]\nsteps: [{wait: forever}]", "duration"),
        ("routine: a.b\ntriggers: [manual]\nmode: whenever\nsteps: [stop]", "mode"),
        ("routine: a.b\nextra: 1\ntriggers: [manual]\nsteps: [stop]", "unknown key 'extra'"),
        (
            "routine: a.b\ntriggers: [manual]\n"
            "steps: [{home.set_secure: {device: X, target: smash}}]",
            "target",
        ),
        ("[unclosed", "Not valid YAML"),
    ],
)
def test_rejects_bad_routines(source, problem):
    with pytest.raises(RoutineError) as exc:
        parse(source)
    assert any(problem in p for p in exc.value.problems), exc.value.problems


# MARK: - Index and conditions


def test_index_resolves_names_rooms_and_types():
    index = HomeIndex()
    index.load(home_rows())
    assert index.resolve({"ref": "Hall/Ceiling"}) == [("light-1", "c-on")]
    assert index.resolve({"room": "hall", "type": "motion"}) == [("motion-1", "c-motion")]
    assert index.resolve({"ref": "Garage/Door"}) == [("garage-1", "c-door")]
    changes = index.apply({"type": "characteristic", "characteristicId": "c-motion", "value": True})
    assert changes == [("value", "c-motion", False, True)]
    assert index.apply(
        {"type": "characteristic", "characteristicId": "c-motion", "value": True}
    ) == [("repeat", "c-motion", True)]
    assert (
        index.apply(
            {
                "type": "characteristic",
                "characteristicId": "c-motion",
                "value": False,
                "origin": "sample",
            }
        )
        == []
    )


def test_comparisons_words_and_crossings():
    assert check_value("doorState", 0, {"is": "open"}) is True
    assert check_value("contact", 1, {"is": "open"}) is True
    assert check_value("motion", True, {"is": "detected"}) is True
    assert check_value("currentTemperature", None, {"above": 20}) is None
    assert crossed("currentTemperature", 19, 21, {"above": 20})
    assert not crossed("currentTemperature", 21, 22, {"above": 20})  # already above: no crossing
    assert crossed("motion", False, True, {"to": "detected"})
    assert not crossed("motion", True, False, {"to": "detected"})
    assert tri_all([True, None]) is None and tri_all([False, None]) is False


# MARK: - Engine


async def test_propose_checks_devices_and_scopes(home):
    core, fake = home
    bad = await core.call(
        "claude", "routines_propose", {"yaml": HALL_LIGHT.replace("Hall/Ceiling", "Attic/Nothing")}
    )
    assert bad["error"]["code"] == "invalid_routine"
    assert "Attic/Nothing" in bad["error"]["message"]
    denied = await core.call("dot", "routines_propose", {"yaml": HALL_LIGHT})
    assert denied["error"]["code"] == "scope_denied"
    proposed = await core.call("claude", "routines_propose", {"yaml": HALL_LIGHT})
    assert proposed["scopes"] == ["home:control", "home:read"]
    assert proposed["plan"]["steps"][0]["devices"] == ["Hall/Ceiling"]
    # Proposing activates nothing, and agents can't approve.
    assert core.routines.versions == {}
    refused = await core.call(
        "claude", "routines_approve", {"routine": proposed["routine"], "hash": proposed["hash"]}
    )
    assert refused["error"]["code"] == "scope_denied"
    # The owner was told.
    outbox = core.storage.db.execute("SELECT payload FROM alert_outbox").fetchall()
    assert (
        any(
            "routines approve home.hall_motion_light" in json.loads(r["payload"])["body"]
            for r in outbox
        )
        or not core.alerts.channels()
    )


async def test_motion_turns_the_light_on_and_off(home):
    core, fake = home
    await activate(core, HALL_LIGHT)
    fake.push("c-motion", True)
    await until(lambda: len(fake.writes) >= 2)
    assert fake.writes[0][:3] == ("POST", "light-1", {"on": True})
    assert fake.writes[1][:3] == ("PUT", "c-bri", {"value": 40})
    assert all(w[3] and w[3].startswith("routine:run_") for w in fake.writes)
    fake.push("c-motion", False)
    await until(lambda: len(fake.writes) >= 3)
    assert fake.writes[2][:3] == ("POST", "light-1", {"on": False})
    await until(
        lambda: core.routines.store.runs("home.hall_motion_light")[0]["status"] == "succeeded"
    )
    run = core.routines.store.runs("home.hall_motion_light")[0]
    result = await core.call("claude", "routines_runs", {"run_id": run["run_id"]})
    assert "steps" in result, result.get("error")
    steps = result["steps"]
    assert [s["status"] for s in steps] == ["done", "done", "done"]
    # Audited as the author, with the run as the cause.
    rows = core.storage.db.execute(
        "SELECT client, capability, origin, causation_id FROM audit_log WHERE capability='home_set'"
    ).fetchall()
    assert {(r["client"], r["origin"], r["causation_id"]) for r in rows} == {
        ("claude", "routine", run["run_id"])
    }


async def test_restart_mode_supersedes_and_own_echoes_are_ignored(home):
    core, fake = home
    await activate(core, HALL_LIGHT)
    fake.push("c-motion", True)
    await until(lambda: len(fake.writes) >= 2)
    first = core.routines.store.runs()[0]["run_id"]
    # An echo of the routine's own write never re-triggers it.
    fake.push("c-motion", False, cause=f"routine:{first}")
    fake.push("c-motion", True, cause=f"routine:{first}")
    await asyncio.sleep(0.2)
    assert len(core.routines.store.runs()) == 1
    # A real new motion restarts it.
    fake.push("c-motion", False)
    await until(lambda: len(fake.writes) >= 3)
    fake.push("c-motion", True)
    await until(lambda: len(core.routines.store.runs()) == 2)
    statuses = {r["run_id"]: r["status"] for r in core.routines.store.runs()}
    assert statuses[first] in ("succeeded", "superseded")


async def test_held_trigger_and_secure_step_waits_for_approval(home):
    core, fake = home
    await activate(core, GARAGE)
    fake.push("c-door", 0)  # open
    await until(
        lambda: core.routines.store.runs() and core.routines.store.runs()[0]["status"] == "waiting",
        seconds=5,
    )
    pending = [a for a in core.approvals.list() if a["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["capability"] == "home_set_secure"
    assert not any(w[1] == "c-target" for w in fake.writes)
    core.resolve("owner", pending[0]["id"], True)
    await until(lambda: any(w[1] == "c-target" for w in fake.writes))
    assert [w[2] for w in fake.writes if w[1] == "c-target"] == [{"value": 1}]
    await until(lambda: core.routines.store.runs()[0]["status"] == "succeeded")
    # Held for the duration once; doesn't fire again while still open.
    await asyncio.sleep(0.3)
    assert len(core.routines.store.runs()) == 1


async def test_denied_approval_stops_the_run(home):
    core, fake = home
    await activate(core, GARAGE)
    fake.push("c-door", 0)
    await until(lambda: [a for a in core.approvals.list() if a["status"] == "pending"])
    pending = [a for a in core.approvals.list() if a["status"] == "pending"][0]
    core.resolve("owner", pending["id"], False)
    await until(lambda: core.routines.store.runs()[0]["status"] == "denied")
    assert not any(w[1] == "c-target" for w in fake.writes)


async def test_conditions_unknown_and_pause(home):
    core, fake = home
    source = HALL_LIGHT.replace(
        "steps:", "conditions:\n  - device: Garage/Door\n    is: closed\nsteps:", 1
    )
    await activate(core, source)
    # Garage unreachable: unknown, so the routine doesn't run.
    core.routines.index.accessories["garage-1"]["reachable"] = False
    fake.push("c-motion", True)
    await asyncio.sleep(0.3)
    assert core.routines.store.runs() == []
    assert "unknown" in core.routines.skips["home.hall_motion_light"]["reason"]
    core.routines.index.accessories["garage-1"]["reachable"] = True
    paused = await core.call("owner", "routines_pause", {"paused": True})
    assert paused["paused"] is True
    fake.push("c-motion", False)
    fake.push("c-motion", True)
    await asyncio.sleep(0.3)
    assert core.routines.store.runs() == []


async def test_resumes_waits_after_a_restart(home, config):
    core, fake = home
    source = """
routine: home.later
triggers: [manual]
steps:
  - wait: 1s
  - home.set: {device: Hall/Ceiling, on: true}
"""
    await activate(core, source)
    run = await core.call("claude", "routines_run", {"routine": "home.later"})
    assert run["status"] == "running"
    await until(lambda: core.routines.store.step(run["run_id"], "0"))
    # Switchboard stops mid-wait.
    await core.routines.close()
    assert core.routines.store.run(run["run_id"])["status"] == "running"
    assert fake.writes == []
    # A new engine resumes the run from its journal and finishes it.
    fresh = engine_module.RoutineEngine(core)
    core.routines = fresh
    await fresh.start()
    await until(lambda: fresh.store.run(run["run_id"])["status"] == "succeeded", seconds=5)
    assert [w[:3] for w in fake.writes] == [("POST", "light-1", {"on": True})]


async def test_stream_resumes_from_saved_position(home):
    core, fake = home
    await activate(core, HALL_LIGHT)
    fake.push("c-motion", True)
    await until(lambda: core.routines.store.meta("homerelay_seq") == "1")
    assert core.routines.store.meta("homerelay_store") == "STORE-1"
    await core.routines.close()
    await core.routines.start()
    await until(lambda: fake.streams == 2)
    assert fake.since[-1] == "1"


async def test_disable_and_breaker(home):
    core, fake = home
    await activate(core, HALL_LIGHT)
    result = await core.call(
        "claude", "routines_disable", {"routine": "home.hall_motion_light", "reason": "testing"}
    )
    assert result["status"] == "disabled"
    assert core.routines.versions == {}
    listing = await core.call("dot", "routines_list", {})
    assert listing["routines"][0]["status"] == "disabled"


def test_time_triggers_follow_local_time_across_dst(config):
    from switchboard.core import Core as CoreClass

    core = CoreClass(config)
    eng = core.routines
    eng.zone = ZoneInfo("America/New_York")
    trigger = parse("routine: a.b\ntriggers: [{time: '07:00'}]\nsteps: [stop]")["triggers"][0]
    # The day before the fall-back change, then the day of it: 07:00 local both times.
    after = int(datetime(2026, 10, 31, 8, 0, tzinfo=eng.zone).timestamp() * 1000)
    first = eng.next_fire(trigger, after)
    assert (
        datetime.fromtimestamp(first / 1000, eng.zone).strftime("%Y-%m-%d %H:%M")
        == "2026-11-01 07:00"
    )
    second = eng.next_fire(trigger, first)
    assert (
        datetime.fromtimestamp(second / 1000, eng.zone).strftime("%Y-%m-%d %H:%M")
        == "2026-11-02 07:00"
    )
    weekdays = parse(
        "routine: a.b\ntriggers: [{time: {at: '07:00', days: weekdays}}]\nsteps: [stop]"
    )["triggers"][0]
    saturday = int(datetime(2026, 11, 7, 6, 0, tzinfo=eng.zone).timestamp() * 1000)
    monday = eng.next_fire(weekdays, saturday)
    assert datetime.fromtimestamp(monday / 1000, eng.zone).strftime("%a %H:%M") == "Mon 07:00"
    core.storage.close()


async def test_states_trigger_set_and_pause_from_the_home_app(home):
    core, fake = home
    source = """
routine: home.guest_lights
triggers:
  - device: {device: States/Guest mode, to: on}
steps:
  - home.set: {device: Hall/Ceiling, on: true}
  - state.set: {state: Guest mode, on: false}
"""
    await activate(core, source)
    assert core.routines.index.resolve({"ref": "States/Guest mode"}) == [
        ("state:guest-mode", "state:guest-mode:on")
    ]
    fake.seq += 1
    fake.events.put_nowait(
        (
            fake.seq,
            {
                "type": "characteristic",
                "accessoryId": "state:guest-mode",
                "accessory": "Guest mode",
                "room": "States",
                "characteristicId": "state:guest-mode:on",
                "characteristic": "on",
                "value": True,
                "origin": "bridge",
            },
        )
    )
    await until(lambda: any(w[0] == "STATE" for w in fake.writes))
    state_write = next(w for w in fake.writes if w[0] == "STATE")
    assert state_write[1:3] == ("guest-mode", {"on": False})
    assert state_write[3].startswith("routine:run_")
    # The owner flips "Routines paused" in the Home app: nothing fires.
    core.routines.index.chars["state:routines-paused:on"]["value"] = True
    assert core.routines.paused()
    listing = await core.call("claude", "routines_list", {})
    assert listing["engine"]["paused"] is True


async def test_review_hardening(home):
    core, fake = home
    # Only the owner flips or recreates the pause State.
    denied = await core.call("claude", "home_state_set", {"state": "Routines paused", "on": False})
    assert denied["error"]["code"] == "owner_required"
    recreate = await core.call(
        "claude", "home_state_create", {"name": "Routines Paused", "kind": "switch"}
    )
    assert recreate["error"]["code"] == "owner_required"
    # Routines can't touch it either.
    pausing = """
routine: home.sneaky
triggers: [manual]
steps:
  - state.set: {state: Routines paused, on: false}
"""
    bad = await core.call("claude", "routines_propose", {"yaml": pausing})
    assert "Routines paused" in bad["error"]["message"]
    # on must be a boolean; a lock target needs home.set_secure.
    with pytest.raises(RoutineError):
        parse("routine: a.b\ntriggers: [manual]\nsteps: [{home.set: {device: X, on: yes}}]")
    lock = """
routine: home.lock_by_set
triggers: [manual]
steps:
  - home.set: {device: Garage/Door, doorTarget: 1}
"""
    refused = await core.call("claude", "routines_propose", {"yaml": lock})
    assert "home.set_secure" in refused["error"]["message"]
    # A read-only client can dry-run what it couldn't author.
    plan = await core.call("dot", "routines_plan", {"yaml": HALL_LIGHT})
    assert plan["status"] == "ok", plan


async def test_two_routines_cannot_ping_pong(home):
    core, fake = home
    a = await activate(core, HALL_LIGHT)
    assert a
    run_a = core.routines.start_run(
        core.routines.versions["home.hall_motion_light"], {"kind": "manual"}, None, 0
    )
    other = """
routine: home.echo
triggers:
  - device: {device: Hall/Ceiling, to: on}
steps:
  - notify: {message: echo}
"""
    await activate(core, other)
    run_b = core.routines.start_run(
        core.routines.versions["home.echo"], {"kind": "device"}, f"routine:{run_a}", 1
    )
    # A change caused by B (whose chain contains A) must not fire A again.
    result = await core.routines.fire(
        "home.hall_motion_light", {"kind": "device"}, f"routine:{run_b}"
    )
    assert result is None
    assert "another routine" in core.routines.skips["home.hall_motion_light"]["reason"]


async def test_held_counts_time_already_held(home):
    core, fake = home
    long = GARAGE.replace("for: 1s", "for: 1h")
    core.routines.index.chars.clear()
    await activate(core, long)
    # The door has been open (per the index) for two hours already.
    char = core.routines.index.chars["c-door"]
    char["value"] = 0
    char["since_ms"] = int(core.routines.clock() * 1000) - 2 * 3_600_000
    core.routines.store.clear_triggers("home.garage_left_open")
    core.routines.reconcile_held()
    held = core.routines.store.held("home.garage_left_open", 0)
    assert held["garage-1"]["since_ms"] == char["since_ms"]


async def test_weather_and_calendar_conditions(home, monkeypatch):
    core, fake = home
    weather = {"status": "ok", "temp_f": 28.0, "conditions": "Light Snow", "stale": False}
    busy_until = datetime.now(ZoneInfo("UTC")).timestamp() + 600

    async def tools(author, tool, args):
        assert author == "claude"
        if tool == "weather_now":
            return weather
        if tool == "weather_alerts":
            return {"status": "ok", "alerts": [{"event": "Winter Storm Warning"}]}
        start = datetime.fromtimestamp(busy_until - 1800, ZoneInfo("UTC")).isoformat()
        end = datetime.fromtimestamp(busy_until, ZoneInfo("UTC")).isoformat()
        return {"status": "ok", "events": [{"title": "Call", "start": start, "end": end}]}

    core.routines.evaluator.tools = tools
    cond = parse(
        "routine: a.b\ntriggers: [manual]\n"
        "conditions:\n  - weather: {temp_f: {below: 32}, conditions: snow, alert: true}\n"
        "  - calendar: busy\nsteps: [stop]"
    )["conditions"]
    run = {"author": "claude"}
    assert await core.routines.evaluator.evaluate(cond, run) is True
    weather["temp_f"] = 40.0
    assert await core.routines.evaluator.evaluate(cond, run) is False
    weather["stale"] = True
    # Stale weather is unknown: never passes.
    assert await core.routines.evaluator.evaluate(cond, run) is None
    # Without a routine to run as (a plan), weather is unknown.
    assert await core.routines.evaluator.evaluate(cond, None) is None


async def test_service_events_fire_routines(home):
    core, fake = home
    source = """
routine: ops.homerelay_down
triggers:
  - event: {name: service.down, service: homerelay}
steps:
  - home.set: {device: Hall/Ceiling, on: true}
"""
    await activate(core, source)
    core.monitor.emit("service.down", "plex")  # another service: ignored
    await asyncio.sleep(0.1)
    assert core.routines.store.runs("ops.homerelay_down") == []
    core.monitor.emit("service.down", "homerelay")
    await until(lambda: core.routines.store.runs("ops.homerelay_down"))
    run = core.routines.store.runs("ops.homerelay_down")[0]
    assert run["trigger"]["service"] == "homerelay"
