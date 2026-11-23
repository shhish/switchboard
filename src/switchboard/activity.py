"""Bounded owner-facing projections of local audit and monitor history.

Never render audit arguments wholesale. Only identifier scalars are selected;
board prose comes from the local cache and the existing answer renderer.
"""

import json
import re
import time
from collections import defaultdict
from datetime import UTC, datetime

from .domains.common import sanitize_text
from .errors import CapabilityError
from .monitor import STATE_RANK, SYSTEM_PROBE, display_name
from .secretscan import PATTERNS, TOKENISH, looks_secret

DAY = 86400
SUCCESS = {"ok", "queued", "approved", "done", "completed", "succeeded"}
TOOLS = (
    "board_add",
    "board_move",
    "board_ask",
    "board_answer",
    "board_comment",
    "git_pr_merge",
    "git_tag_create",
    "ops_service_restart",
    "approvals_confirm",
    "approvals_approve",
)


def text(value, limit=140):
    if not isinstance(value, str):
        return ""
    # The shared sanitizer handles controls/whitespace; presentation also removes
    # Markdown and credential/path-shaped content, including in old cached rows.
    value = PATTERNS.sub("[redacted]", value)
    value = TOKENISH.sub(lambda m: "[redacted]" if looks_secret(m[0]) else m[0], value)
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(
        r"(?:https?://|file://)\S+|(?<![\w])(?:~?/|\./|[A-Za-z]:\\)\S+", "[redacted]", value
    )
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[`*_~]", "", value)
    return sanitize_text(value, limit)


def iso(at):
    return datetime.fromtimestamp(at, UTC).isoformat()


def clock(at, *, minutes=False):
    # astimezone() deliberately uses the host zone, including historical DST.
    value = datetime.fromtimestamp(at, UTC).astimezone()
    hour = value.hour % 12 or 12
    return f"{hour}{value.strftime(':%M') if minutes else ''} {'AM' if value.hour < 12 else 'PM'}"


def elapsed(seconds, *, short=False):
    minutes = max(0, int(seconds // 60))
    if minutes == 0:
        return "under a minute" if not short else "<1 min"
    return f"{minutes} min" if short else f"{minutes} minute{'s' if minutes != 1 else ''}"


def since_argument(value, now):
    if value is None:
        return now - DAY
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        since = parsed.timestamp()
        if not now - 14 * DAY <= since <= now:
            raise ValueError
        return since
    except (ValueError, TypeError, OverflowError):
        raise CapabilityError(
            "invalid_arguments", "since must include a time zone and be within the last 14 days"
        ) from None


def probe_names(core):
    return {p["name"] for p in core.config.probes} | core.monitor.storage_probes() | {SYSTEM_PROBE}


def require_probe(core, name):
    if name not in probe_names(core):
        raise CapabilityError("not_found", "Unknown configured probe")


def label(core, name):
    probe = next((p for p in core.config.probes if p["name"] == name), {"name": name})
    return text(display_name(probe), 60)


def event(
    id, at, kind, actor, verb, title, *, detail=None, card=None, service=None, severity="info"
):
    return {
        "id": id,
        "at": iso(at),
        "kind": kind,
        "actor": text(actor, 64) if actor else None,
        "verb": verb,
        "title": text(title),
        "detail": text(detail, 200) if detail else None,
        "card": identifier(card, r"[\w.-]+#[1-9][0-9]*"),
        "service": identifier(service, r"[@\w:.-]+"),
        "severity": severity,
    }


def identifier(value, pattern):
    if not isinstance(value, str) or looks_secret(value) or not re.fullmatch(pattern, value):
        return None
    return sanitize_text(value, 240)


def scalar(data, key):
    value = data.get(key)
    return value if isinstance(value, str) and not looks_secret(value) else None


def audit_events(core, since, now, *, service=None):
    db = core.storage.db
    rows = db.execute(
        f"SELECT id, ts_ms, client, capability, args_json FROM audit_log "
        f"WHERE ts_ms>=? AND ts_ms<=? AND decision='allowed' AND error_code IS NULL "
        f"AND tier IN ('write', 'approval') AND outcome IN ({','.join('?' for _ in SUCCESS)}) "
        f"AND capability IN ({','.join('?' for _ in TOOLS)}) ORDER BY ts_ms DESC, seq DESC",
        (int(since * 1000), int(now * 1000), *sorted(SUCCESS), *TOOLS),
    )
    for row in rows:
        tool, actor = row["capability"], row["client"]
        owner = actor in {"cortex", "owner"}
        who = "You" if owner else text((actor or "Agent").capitalize(), 64)
        kind, severity, detail, target = "owner" if owner else "agent", "info", None, None
        args = json.loads(row["args_json"] or "{}")
        args = args if isinstance(args, dict) else {}
        refs = args.get("_activity", {})
        refs = refs if isinstance(refs, dict) else {}
        card = scalar(refs, "card") or scalar(args, "card")
        ask_id = scalar(refs, "ask_id") or scalar(args, "ask_id")
        ask_row = db.execute("SELECT * FROM board_asks WHERE ask_id=?", (ask_id,)).fetchone()
        if ask_row:
            card = ask_row["card"]
        cached = db.execute("SELECT title FROM board_cards WHERE card=?", (card,)).fetchone()
        title = (text(cached["title"]) if cached else "") or text(card) or "a card"
        if tool == "board_add":
            verb, title = "added", f"{who} added {title}"
        elif tool == "board_move":
            column = scalar(refs, "column") or scalar(args, "column")
            verb = {"In progress": "started", "Done": "finished", "Needs you": "needs_you"}.get(
                column, "moved"
            )
            severity = {"finished": "good", "needs_you": "warning"}.get(verb, "info")
            title = f"{who} needs you: {title}" if verb == "needs_you" else f"{who} {verb} {title}"
        elif tool in {"board_ask", "board_answer"}:
            prompt = text(json.loads(ask_row["spec"]).get("prompt"), 200) if ask_row else title
            if tool == "board_ask":
                verb, title, severity = "asked", f"{who} asked: {prompt}", "warning"
            else:
                kind, verb = "owner", "answered"
                # validate_answer is the board's canonical answer_text renderer. Never
                # display the raw answer object, form values or hashed audit text.
                answer_text = core.board.ask_object(ask_row)["answer_text"] if ask_row else None
                title = f"You answered: {text(answer_text) or 'answer recorded'}"
                asker = ask_row["asker"] if ask_row else None
                asker = "You" if asker in {"owner", "cortex"} else (asker or "Agent").capitalize()
                detail = f"{asker} asked: {prompt}"
        elif tool == "board_comment":
            if actor != "cortex":
                continue
            verb, title = "replied", f"You replied on {title}"
        elif tool in {"git_pr_merge", "git_tag_create"}:
            repo = text(scalar(args, "repo")) or "a repository"
            verb, severity = ("merged" if tool == "git_pr_merge" else "released"), "good"
            title = (
                f"{who} merged a change into {repo}"
                if verb == "merged"
                else f"{who} released {repo} {text(scalar(args, 'tag'))}"
            )
        elif tool == "ops_service_restart":
            target = scalar(args, "service")
            target = core.config.services.get(target, {}).get("probe", target)
            verb, title = (
                "restarted",
                f"{who} restarted {label(core, target) if target else 'a service'}",
            )
        else:
            verb, title = "allowed", f"{who} allowed an action"
        if service is None or target == service:
            yield event(
                "audit:" + row["id"],
                row["ts_ms"] / 1000,
                kind,
                actor,
                verb,
                title,
                detail=detail,
                card=card,
                service=target,
                severity=severity,
            )


def monitor_events(core, since, now, *, service=None):
    db = core.storage.db
    groups = defaultdict(list)
    for row in db.execute(
        "SELECT * FROM monitor_events WHERE at>=? AND at<=? "
        + ("AND name=? " if service else "")
        + "ORDER BY name, at, id",
        (since, now, service) if service else (since, now),
    ):
        groups[row["name"]].append(dict(row))
    for name, rows in groups.items():
        previous = db.execute(
            "SELECT * FROM monitor_events WHERE name=? AND at<? ORDER BY at DESC, id DESC LIMIT 1",
            (name, since),
        ).fetchone()
        down = previous if previous and previous["status"] == "down" else None
        items = []
        for row in rows:
            at, state = row["at"], row["status"]
            verb, severity, detail = state, "info", None
            title = f"{label(core, name)} is back up"
            if state == "down":
                down = row
                title, severity = f"{label(core, name)} went down", "critical"
            elif state in {"up", "degraded"} and down is not None:
                verb, severity = "recovered", "good" if state == "up" else "warning"
                title = f"{label(core, name)} was down for {elapsed(at - down['at'])}"
                detail = f"Back at {clock(at, minutes=True)}"
                # Collapse only a down event actually inside the requested window.
                items = [item for item in items if item[0]["id"] != down["id"]]
                down = None
            elif state == "degraded":
                verb, title, severity = "slow", f"{label(core, name)} is slow", "warning"
            elif state == "unknown":
                title, severity = f"{label(core, name)} state is unknown", "warning"
            else:
                verb, severity = "recovered", "good"
                if row["previous"] == "unknown" and not row["incident_id"]:
                    continue  # Initial healthy observation is not an owner activity.
            items.append(
                (
                    row,
                    event(
                        f"monitor:{row['id']}",
                        at,
                        "server",
                        None,
                        verb,
                        title,
                        detail=detail,
                        service=name,
                        severity=severity,
                    ),
                )
            )
        # Group changes within a ten-minute span (not an unbounded chain of flaps).
        clusters = []
        for row in rows:
            if row["status"] == "up" and row["previous"] == "unknown" and not row["incident_id"]:
                continue  # Do not count the initial healthy observation as a flap.
            if not clusters or row["at"] - clusters[-1][0]["at"] > 600:
                clusters.append([])
            clusters[-1].append(row)
        for cluster in clusters:
            states = [r["status"] for r in cluster]
            if (
                len(cluster) < 3
                or len(set(states)) == len(states)
                or not any(s in {"down", "degraded"} for s in states)
            ):
                continue
            ids = {r["id"] for r in cluster}
            collapsed = [item for row, item in items if row["id"] in ids]
            if not collapsed:
                continue
            last = collapsed[-1]
            last["detail"] = f"Flapped {len(cluster)} times"
            items = [(row, item) for row, item in items if row["id"] not in ids]
            items.append((cluster[-1], last))
        yield from (item for _, item in items)


def feed(core, args, now=None):
    now = time.time() if now is None else now
    since = since_argument(args.get("since"), now)
    kinds = args.get("kinds", ["agent", "owner", "server"])
    events = []
    if "agent" in kinds or "owner" in kinds:
        events.extend(e for e in audit_events(core, since, now) if e["kind"] in kinds)
    if "server" in kinds:
        events.extend(monitor_events(core, since, now))
    events.sort(key=lambda e: (e["at"], e["id"]), reverse=True)
    limit = args.get("limit", 100)
    return {
        "status": "ok",
        "since": iso(since),
        "events": events[:limit],
        "truncated": len(events) > limit,
        "limit": limit,
    }


def downtime(db, name, start, end):
    previous = db.execute(
        "SELECT status FROM monitor_events WHERE name=? AND at<=? "
        "ORDER BY at DESC, id DESC LIMIT 1",
        (name, start),
    ).fetchone()
    rows = db.execute(
        "SELECT at, status, previous FROM monitor_events WHERE name=? AND at>? AND at<? "
        "ORDER BY at, id",
        (name, start, end),
    ).fetchall()
    state = previous[0] if previous else rows[0]["previous"] if rows else "unknown"
    total, cursor, first = 0, start, start if state == "down" else None
    for row in rows:
        if state == "down":
            total += row["at"] - cursor
        state, cursor = row["status"], row["at"]
        if state == "down" and first is None:
            first = cursor
    return total + (end - cursor if state == "down" else 0), first


def pulse(core, args, now=None):
    now = time.time() if now is None else now
    hours, name = args.get("hours", 24), args.get("name")
    if name:
        require_probe(core, name)
    end_hour = now // 3600 * 3600
    start = end_hour - (hours - 1) * 3600
    db = core.storage.db
    samples = defaultdict(list)
    for row in db.execute(
        "SELECT * FROM monitor_hours WHERE hour>=? AND hour<=? " + ("AND name=?" if name else ""),
        (start, end_hour, name) if name else (start, end_hour),
    ):
        samples[row["hour"]].append(row)
    buckets, outages = [], defaultdict(float)
    first_down = {}
    for offset in range(hours):
        hour = start + offset * 3600
        rows = samples[hour]
        worst = max((r["worst"] for r in rows), key=STATE_RANK.__getitem__, default="unknown")
        notes = []
        for row in sorted(rows, key=lambda r: r["name"]):
            state = row["worst"]
            if state == "up":
                continue
            note = f"{label(core, row['name'])} {'slow' if state == 'degraded' else state}"
            if state == "down":
                seconds, at = downtime(db, row["name"], hour, min(hour + 3600, now))
                if seconds:
                    note += " " + elapsed(seconds, short=True)
                    outages[row["name"]] += seconds
                    first_down.setdefault(row["name"], at)
            notes.append(note)
        buckets.append(
            {
                "start": iso(hour),
                "state": "slow" if worst == "degraded" else worst,
                "note": text(", ".join(notes), 80) if notes else None,
            }
        )
    slow = [b for b in buckets if b["state"] == "slow"]
    down = [b for b in buckets if b["state"] == "down"]
    unknown = sum(b["state"] == "unknown" for b in buckets)
    if outages:
        name = max(outages, key=outages.get)
        summary = (
            f"{label(core, name)} was down for {elapsed(outages[name])} "
            f"at {clock(first_down[name])}"
        )
    elif down:
        summary = f"{len(down)} hour{'s' if len(down) != 1 else ''} with outages"
    elif len(slow) == 1:
        summary = f"One slow hour at {clock(datetime.fromisoformat(slow[0]['start']).timestamp())}"
    elif slow:
        summary = f"{len(slow)} slow hours"
    elif unknown == hours:
        summary = "No health data yet"
    elif unknown:
        observed = hours - unknown
        summary = f"Healthy in {observed} observed hour{'s' if observed != 1 else ''}"
    else:
        summary = "Healthy all day" if hours == 24 else f"Healthy for {hours} hours"
    return {"status": "ok", "hours": hours, "buckets": buckets, "summary": text(summary, 200)}


def service_history(core, args, now=None):
    now = time.time() if now is None else now
    name = args["name"]
    require_probe(core, name)
    since = now - args.get("days", 7) * DAY
    db = core.storage.db
    current = db.execute("SELECT * FROM monitor_state WHERE name=?", (name,)).fetchone()
    events = [
        *monitor_events(core, since, now, service=name),
        *audit_events(core, since, now, service=name),
    ]
    events.sort(key=lambda e: (e["at"], e["id"]), reverse=True)
    counts = db.execute(
        "SELECT SUM(checks), SUM(failures) FROM monitor_hours "
        "WHERE name=? AND hour>=? AND hour<=? AND worst!='unknown'",
        (name, since // 3600 * 3600, now),
    ).fetchone()
    checks, failures = counts
    state = current["status"] if current else "unknown"
    return {
        "status": "ok",
        "name": name,
        "state": "slow" if state == "degraded" else state,
        "since": iso(current["last_change_at"]) if current else None,
        "latency_ms": current["latency_ms"] if current else None,
        "events": events[:30],
        "truncated": len(events) > 30,
        "uptime_percent": round(100 * (checks - failures) / checks, 2) if checks else 0,
    }
