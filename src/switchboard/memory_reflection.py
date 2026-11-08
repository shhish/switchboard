"""Bounded, domain-filtered inputs for an offline reflection worker; no inference here."""

import json
import time

from .memory_subjects import DOMAIN_SCOPES
from .secretscan import looks_secret

SCHEMA = """
    CREATE TABLE IF NOT EXISTS memory_reflection_refs (
        client TEXT NOT NULL, ref TEXT NOT NULL, issued_at REAL NOT NULL,
        PRIMARY KEY(client,ref)
    );
    CREATE TABLE IF NOT EXISTS memory_incidents (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        event TEXT NOT NULL, at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS memory_incidents_time ON memory_incidents(at,name);
"""
MAX_BYTES = 40000


def safe(value, limit=120):
    if isinstance(value, str):
        from .domains.common import sanitize_text

        return "[redacted]" if looks_secret(value) else sanitize_text(value, limit)
    return value


def capability_domains(memory, client):
    scopes = memory.core.policy.client(client)["scopes"]
    allowed = set(memory.domains(client))
    names = []
    for cap in memory.core.registry.capabilities.values():
        if cap.memory is not None:
            domain = cap.memory.domain
        elif cap.scope.startswith("memory:"):
            domain = "general"
        else:
            domain = next(
                (
                    d
                    for d, scope in DOMAIN_SCOPES.items()
                    if scope.split(":")[0] == cap.scope.split(":")[0]
                ),
                None,
            )
        if domain in allowed and (cap.memory is not None or cap.scope in scopes):
            names.append(cap.name)
    return names


def incident_domain(name):
    return "storage" if name.startswith(("storage:", "smart:")) else "ops"


def record_incident(memory, event, name):
    if not memory.core.config.memory["enabled"] or not isinstance(name, str) or looks_secret(name):
        return
    with memory.db:
        memory.db.execute(
            "INSERT INTO memory_incidents(name,event,at) VALUES(?,?,?)",
            (safe(name, 100), event, time.time()),
        )


def valid_evidence(memory, client, ref, *, issued=False, now=None):
    now = time.time() if now is None else now
    if not isinstance(ref, str) or not ref or looks_secret(ref):
        return False
    if (
        issued
        and not memory.db.execute(
            "SELECT 1 FROM memory_reflection_refs WHERE client=? AND ref=? AND issued_at>=?",
            (client, ref, now - 72 * 3600),
        ).fetchone()
    ):
        return False
    domains = set(memory.domains(client))
    caps = capability_domains(memory, client)
    if (
        caps
        and memory.db.execute(
            "SELECT 1 FROM audit_log WHERE trace_id=? AND ts_ms>=? AND ts_ms<=? "
            "AND capability IN (" + ",".join("?" for _ in caps) + ") LIMIT 1",
            (ref, (now - 72 * 3600) * 1000, now * 1000 + 1, *caps),
        ).fetchone()
    ):
        return True
    if incident_domain(ref) not in domains:
        return False
    return bool(
        memory.db.execute(
            (
                "SELECT 1 FROM memory_incidents WHERE name=? AND at>=? AND at<=? "
                "UNION ALL SELECT 1 FROM monitor_state WHERE name=? AND "
                "last_change_at>=? AND last_change_at<=? LIMIT 1"
            ),
            (ref, now - 72 * 3600, now, ref, now - 72 * 3600, now),
        ).fetchone()
    )


def reflect_input(memory, client, args):
    memory.ready()
    memory.core.policy.require_scope(client, "memory:reflect")
    domains = memory.domains(client)
    now = time.time()
    since = now - args.get("hours", 24) * 3600
    names = capability_domains(memory, client)
    marks = ",".join("?" for _ in names)
    report = {
        "status": "ok",
        "since": since,
        "until": now,
        "allowed_domains": domains,
        "audit": {"counts": [], "errors": [], "loops": [], "actions": []},
        "monitoring": {"incidents": [], "storage_alerts": []},
        "routine_runs": [],
        "memory_titles": {d: [] for d in domains},
        "evidence_refs": [],
        "truncated": False,
    }
    refs = set()

    def add(target, value):
        target.append(value)
        if len(json.dumps(report, ensure_ascii=False).encode()) > MAX_BYTES - 1000:
            target.pop()
            report["truncated"] = True
            return False
        return True

    where = "FROM audit_log WHERE ts_ms>=? AND ts_ms<=? AND capability IN (" + marks + ")"
    params = [since * 1000, now * 1000 + 1, *names]
    if names:
        for row in memory.db.execute(
            "SELECT client,capability,COUNT(*) n "
            + where
            + " GROUP BY 1,2 ORDER BY n DESC,1,2 LIMIT 80",
            params,
        ):
            add(report["audit"]["counts"], {k: safe(v) for k, v in dict(row).items()})
        for row in memory.db.execute(
            "SELECT client,capability,error_code,trace_id,ts_ms,tier,decision,outcome,args_hash "
            + where
            + " ORDER BY seq DESC LIMIT 400",
            params,
        ):
            row = dict(row)
            trace = row.pop("trace_id")
            row.pop("args_hash")
            event = {k: safe(v) for k, v in row.items()}
            if row["error_code"]:
                if add(report["audit"]["errors"], {**event, "evidence_ref": safe(trace, 160)}):
                    refs.add(trace)
            elif row["tier"] in ("write", "approval") and row["decision"] == "allowed":
                if add(report["audit"]["actions"], {**event, "evidence_ref": safe(trace, 160)}):
                    refs.add(trace)
            if len(report["audit"]["errors"]) + len(report["audit"]["actions"]) >= 40:
                break
        for row in memory.db.execute(
            "SELECT client,capability,COUNT(*) n,MIN(trace_id) evidence_ref "
            + where
            + (
                " GROUP BY client,capability,args_hash,ts_ms/600000 HAVING n>=5 "
                "ORDER BY n DESC LIMIT 20"
            ),
            params,
        ):
            value = {k: safe(v) for k, v in dict(row).items()}
            if add(report["audit"]["loops"], value):
                refs.add(value["evidence_ref"])
        # Read calls can be evidence too; never include their arguments or result bodies.
        for row in memory.db.execute(
            "SELECT DISTINCT trace_id " + where + " ORDER BY ts_ms DESC LIMIT 30", params
        ):
            if row[0]:
                refs.add(row[0])
    for row in memory.db.execute(
        (
            "SELECT name,event,at FROM memory_incidents WHERE at>=? AND at<=? "
            "ORDER BY at DESC LIMIT 100"
        ),
        (since, now),
    ):
        if incident_domain(row["name"]) not in domains:
            continue
        entry = {
            "name": safe(row["name"], 100),
            "state": "closed" if row["event"] == "service.recovered" else "open",
            "at": row["at"],
            "evidence_ref": safe(row["name"], 100),
        }
        target = report["monitoring"][
            "storage_alerts" if incident_domain(row["name"]) == "storage" else "incidents"
        ]
        if add(target, entry):
            refs.add(row["name"])
    for row in memory.db.execute(
        (
            "SELECT name,status,last_change_at FROM monitor_state WHERE "
            "last_change_at>=? AND last_change_at<=? ORDER BY name LIMIT 100"
        ),
        (since, now),
    ):
        if incident_domain(row["name"]) not in domains:
            continue
        target = report["monitoring"][
            "storage_alerts" if incident_domain(row["name"]) == "storage" else "incidents"
        ]
        if add(
            target,
            {
                "name": safe(row["name"], 100),
                "state": row["status"],
                "at": row["last_change_at"],
                "evidence_ref": safe(row["name"], 100),
            },
        ):
            refs.add(row["name"])
    if "routines" in domains:
        for row in memory.db.execute(
            (
                "SELECT run_id,routine,status,started_at,finished_at,trace_id "
                "FROM routine_runs WHERE started_at>=? AND started_at<=? ORDER BY "
                "started_at DESC LIMIT 30"
            ),
            (since, now),
        ):
            add(report["routine_runs"], {k: safe(v) for k, v in dict(row).items()})
    for domain in domains:
        for row in memory.db.execute(
            (
                "SELECT id,title FROM memory WHERE domain=? AND status='active' "
                "ORDER BY pinned DESC,use_count DESC,id LIMIT 200"
            ),
            (domain,),
        ):
            if not add(
                report["memory_titles"][domain], {"id": row["id"], "title": safe(row["title"])}
            ):
                break
    for ref in sorted(r for r in refs if isinstance(r, str) and r and not looks_secret(r)):
        add(report["evidence_refs"], ref)
    with memory.db:
        for ref in report["evidence_refs"]:
            memory.db.execute(
                (
                    "INSERT INTO memory_reflection_refs VALUES(?,?,?) ON "
                    "CONFLICT(client,ref) DO UPDATE SET issued_at=excluded.issued_at"
                ),
                (client, ref, now),
            )
        memory.db.execute(
            "DELETE FROM memory_reflection_refs WHERE issued_at<?", (now - 72 * 3600,)
        )
    return report
