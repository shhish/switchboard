---
name: switchboard-routines
description: "Draft home automations (routines) for the owner to approve: triggers, conditions and steps in a small checked YAML language, run by Switchboard against Apple Home through HomeRelay."
---

A routine is data, not code: a YAML definition Switchboard checks, the owner
approves by hash, and the engine runs. Agents only propose. Nothing runs until
the owner approves that exact version (`sb routines approve <name> <hash>`).

## Workflow

1. Find devices with `home_list`. Refer to them as `Room/Name`, by id, or by
   `{room: Hall, type: motion}` (all motion sensors in the Hall).
2. Write the YAML. Call `routines_plan {yaml}` to see resolved devices, the next
   scheduled time, whether conditions pass right now, and every call it would make.
3. `routines_propose {yaml}` stores it and tells the owner. Show the user the
   returned `scopes`, `warnings` and `approve` command. Never claim it's running.
4. After approval, check behaviour with `routines_list` (last run, last skip
   reason) and `routines_runs {run_id}` (each journaled step).
5. If one misbehaves, `routines_disable {routine, reason}`. Re-enabling needs a
   new proposal and approval.

## Language

```yaml
routine: home.hall_motion_light      # dotted lowercase name
description: Hall light with motion at night.
mode: restart                        # single (default) | restart | queued | parallel
max_runs: 1                          # queued/parallel cap
cooldown: 30s                        # optional
triggers:                            # any one fires the routine
  - device: {device: {room: Hall, type: motion}, to: detected}
conditions:                          # all must be true (a list means all)
  - sun: night
steps:
  - home.set: {device: Hall/Ceiling, on: true, brightness: 40}
  - wait_until: {condition: {device: {room: Hall, type: motion}, is: clear}, for: 3m, timeout: 30m}
  - home.set: {device: Hall/Ceiling, on: false}
until:                               # optional: stops a running run
  - device: Hall/Switch
    is: off
```

**Triggers** (each may have an `id:` for `trigger:` conditions):
- `device: {device, to, from, above, below}`: a change. `above`/`below` fire on crossing.
- `held: {device, is|above|below, for: 15m}`: true continuously for the duration; fires once per hold. Unknown time (unreachable) never counts.
- `reachability: {device, reachable: false, for: 10m}`
- `time: "07:00"` or `time: {at: "07:00", days: weekdays}` (local time, DST-safe)
- `every: 30m`
- `sun: {event: sunset, offset: -30m, days: weekends}` (sunrise, sunset, dawn, dusk)
- `manual`: runnable with `routines_run` (conditions are skipped then).

**Conditions** (`all`, `any`, `not` to combine): `device` with `is`/`above`/`below` (and `for`);
`reachable`; `time: {after: "22:00", before: "06:00", days}` (wraps midnight);
`sun: day|night` or `sun: {elevation_below: 5}`;
`history: {device, window: 10m, on: {above: 0}}` (also `mean`, `min`, `max`, `changes`);
`trigger: <id>`. A device that's unreachable or never seen is **unknown**, which
fails `all` and is reported as the skip reason. A selector naming several
devices needs all of them; add `match: any` to need any.

**Steps:** `home.set {device, on, <value>: …}` (value names as HomeRelay shows,
e.g. `brightness`, `targetTemperature`); `home.set_secure {device, target: closed|locked|…}`
(asks the owner every run); `home.scene: Movie`; `state.set: {state: Guest mode, on: true}`; `call: {tool, args}` (any
Switchboard tool the routine's scopes allow; not routine or approval tools);
`notify: {title, message}` (ntfy and email); `wait: 5m`;
`wait_until: {condition, for, timeout, on_timeout: continue|stop}`;
`if: <condition>` with `then:` and `else:`; `repeat: 3` with `steps:`; `stop`.

**States** are HomeRelay's virtual switches and sensors (`home_states`; create
one with `home_state_create`). In triggers and conditions they're devices in a
room called States: `device: States/Guest mode`, `is: on`. A State named
**Routines paused** that's on pauses every routine, so the owner can flip it in
the Home app.

**Values by word:** `contact` closed/open, `doorState` open/closed/opening/closing/stopped,
`lockState` unlocked/locked/jammed, `motion`/`occupancy`/`leakDetected`/`smoke`
detected/clear, `on`/`active` on/off, `buttonEvent` single/double/long.

**Templates** in `notify` and `call` args: `{{ trigger.accessory }}`,
`{{ trigger.value }}`, `{{ trigger.duration }}` (held), `{{ routine.name }}`, `{{ run.id }}`.

## Safety the engine enforces

- Every step runs through Switchboard as the routine's author, with policy,
  allowlists and audit, limited to the scopes the owner approved. Revoking the
  author's scope stops the routine at its next step.
- Writes carry the run as their cause: a routine never re-triggers itself;
  chains stop at depth 3; at most 20 writes a minute per device; five failed
  runs in a row turn a routine off and tell the owner.
- Runs, waits and approvals survive restarts; a non-repeatable step interrupted
  mid-way marks the run `indeterminate` instead of repeating it.
- `routines_pause` (owner) stops everything at the next step.
- Keep anything safety-critical (leak → shut the water) as an Apple Home
  automation too: routines pause while HomeRelay or Switchboard is down.
