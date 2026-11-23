"""AdGuard Home stats and bounded pause/resume through the remote host agent."""

import json

import pytest

from switchboard import hostcmd, remote
from switchboard.errors import CapabilityError

from .conftest import change_policy
from .test_remote import pi, ssh_calls  # noqa: F401  (pi is a fixture)

STATS = {
    "ok": True,
    "protection_enabled": True,
    "paused_ms": 0,
    "version": "v0.107.80",
    "time_units": "days",
    "period_days": 14,
    "queries": 412318,
    "blocked": 51544,
    "blocked_percent": 12.5,
    "avg_ms": 6.2,
    "series": {"queries": [30000, 28000], "blocked": [4000, 3500]},
    "top_blocked": [{"name": "ads.example.com", "count": 900}],
    "top_queried": [{"name": "example.com", "count": 5000}],
    "top_clients": [{"name": "Living Room TV", "count": 120000}],
    "extra_field": "dropped",
}


async def test_dns_stats_projects_agent_reply(pi):  # noqa: F811
    core, calls, replies = pi
    replies["dns-stats"] = STATS
    result = await core.call("dot", "dns_stats", {})
    assert result["queries"] == 412318 and result["blocked_percent"] == 12.5
    assert result["top_clients"] == [{"name": "Living Room TV", "count": 120000}]
    assert "extra_field" not in result
    assert ssh_calls(calls) == ["dns-stats"]


async def test_dns_stats_not_set_up_names_owner_script(pi):  # noqa: F811
    core, calls, replies = pi
    replies["dns-stats"] = (
        3,
        json.dumps({"ok": False, "error": "PRIVATE", "code": "dns_not_configured"}),
        "",
    )
    result = await core.call("dot", "dns_stats", {})
    assert result["error"]["code"] == "owner_required"
    assert "owner-setup-adguard" in result["error"]["hint"]
    assert "PRIVATE" not in json.dumps(result)


async def test_dns_pause_and_resume_need_scope_and_stay_bounded(pi):  # noqa: F811
    core, calls, replies = pi
    args = {"minutes": 15, "reason": "site wrongly blocked"}
    assert (await core.call("codex", "dns_pause", args))["error"]["code"] == "scope_denied"
    assert (await core.call("codex", "dns_resume", {"reason": "x"}))["error"][
        "code"
    ] == "scope_denied"
    assert ssh_calls(calls) == []

    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("dns:control"))
    for minutes in (0, 121, "15"):
        bad = await core.call("codex", "dns_pause", {"minutes": minutes, "reason": "x"})
        assert bad["error"]["code"] == "invalid_arguments"
    assert ssh_calls(calls) == []

    replies["dns-pause 15"] = {"ok": True, "protection_enabled": False, "paused_minutes": 15}
    result = await core.call("codex", "dns_pause", args)
    assert result["protection_enabled"] is False and result["paused_minutes"] == 15
    replies["dns-resume"] = {"ok": True, "protection_enabled": True, "paused_minutes": 0}
    result = await core.call("codex", "dns_resume", {"reason": "done"})
    assert result["protection_enabled"] is True
    assert ssh_calls(calls) == ["dns-pause 15", "dns-resume"]


@pytest.mark.parametrize(
    ("request_line", "valid"),
    [
        ("dns-stats", True),
        ("dns-resume", True),
        ("dns-pause 1", True),
        ("dns-pause 120", True),
        ("dns-pause 0", False),
        ("dns-pause 121", False),
        ("dns-pause", False),
        ("dns-stats now", False),
        ("dns-pause 5 5", False),
    ],
)
def test_dns_requests_are_exact(request_line, valid):
    assert hostcmd.valid_request(request_line) is valid


@pytest.mark.parametrize(
    "request_line",
    [
        "dns-rules",
        "dns-filters-refresh",
        "dns-rule allow Example.com",
        "dns-rule block *.example.com",
        "dns-rule remove example.com",
        "dns-querylog 1 all",
        "dns-querylog 200 blocked ads.example.com",
        "dns-querylog 10 all 2001:db8::1",
    ],
)
async def test_new_dns_remote_requests(pi, request_line):  # noqa: F811
    core, calls, _ = pi
    assert hostcmd.valid_request(request_line)
    await remote.call(core, "remotehost", *request_line.split(" "))
    assert ssh_calls(calls) == [request_line]


BAD_DOMAINS = [
    "-example.com",
    "example-.com",
    "example..com",
    ".example.com",
    "example.com.",
    "a" * 64 + ".com",
    ".".join(["a" * 63] * 4),
    "foo.*.com",
    "*example.com",
    "example.com;id",
    "$(id)",
    "`id`",
    "example.com/",
    "example.com\n",
    "example.com extra",
    "example.com$important",
    "éxample.com",
    "",
]
BAD_REQUESTS = [
    "dns-rules extra",
    "dns-filters-refresh extra",
    "dns-rule",
    "dns-rule allow",
    "dns-rule unblock example.com",
    "dns-rule allow example.com extra",
    "dns-querylog",
    "dns-querylog 5",
    "dns-querylog 0 all",
    "dns-querylog 201 blocked",
    "dns-querylog -1 all",
    "dns-querylog 1.0 all",
    "dns-querylog ١ all",
    "dns-querylog 10 unknown",
    "dns-querylog 10 all x y",
    "dns-querylog 10 all x;id",
    "dns-querylog 10 all a_b",
    "dns-querylog 10 all " + "a" * 101,
    "dns-querylog 10 all ",
    "dns-querylog  10 all",
    "dns-querylog 10 all example.com\n",
    *["dns-rule allow " + domain for domain in BAD_DOMAINS],
]


@pytest.mark.parametrize("request_line", BAD_REQUESTS)
async def test_new_dns_remote_rejects_injection(pi, request_line):  # noqa: F811
    core, calls, _ = pi
    assert not hostcmd.valid_request(request_line)
    with pytest.raises(CapabilityError) as exc:
        await remote.call(core, "remotehost", *request_line.split(" "))
    assert exc.value.code == "invalid_arguments"
    assert ssh_calls(calls) == []


async def test_dns_new_capabilities_contracts_and_denials(pi):  # noqa: F811
    core, calls, replies = pi
    replies["dns-querylog 10 blocked example.com"] = {
        "ok": True,
        "queries": [],
        "limit": 10,
        "truncated": False,
        "private": "dropped",
    }
    result = await core.call(
        "dot", "dns_query_log", {"limit": 10, "filter": "blocked", "search": "example.com"}
    )
    assert result["queries"] == [] and "private" not in result
    replies["dns-rules"] = {
        "ok": True,
        "enabled": True,
        "user_rules": ["||example.com^"],
        "user_rules_count": 1,
        "filters": [],
        "truncated": False,
    }
    assert (await core.call("dot", "dns_rules", {}))["user_rules"] == ["||example.com^"]
    before = list(ssh_calls(calls))
    for name in ("dns_allow", "dns_block", "dns_rule_remove", "dns_filters_refresh"):
        args = {} if name == "dns_filters_refresh" else {"domain": "example.com"}
        assert (await core.call("codex", name, args))["error"]["code"] == "scope_denied"
        assert all(tool["name"] != name for tool in core.tools("codex"))
    assert ssh_calls(calls) == before
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("dns:control"))
    for name, action in (
        ("dns_allow", "allow"),
        ("dns_block", "block"),
        ("dns_rule_remove", "remove"),
    ):
        for domain in BAD_DOMAINS:
            assert (await core.call("codex", name, {"domain": domain}))["error"][
                "code"
            ] == "invalid_arguments"
        replies[f"dns-rule {action} *.example.com"] = {
            "ok": True,
            "domain": "*.example.com",
            "action": action,
            "rule": "",
            "changed": True,
            "rules_count": 0,
        }
        assert (await core.call("codex", name, {"domain": "*.example.com"}))["changed"]
    replies["dns-filters-refresh"] = {"ok": True, "updated": 2}
    assert (await core.call("codex", "dns_filters_refresh", {}))["updated"] == 2
    assert ssh_calls(calls)[2:] == [
        "dns-rule allow *.example.com",
        "dns-rule block *.example.com",
        "dns-rule remove *.example.com",
        "dns-filters-refresh",
    ]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"limit": 0, "filter": "all"},
        {"limit": 201, "filter": "all"},
        {"limit": True, "filter": "all"},
        {"limit": "1", "filter": "all"},
        {"limit": 10, "filter": "bad"},
        {"limit": 10, "filter": "all", "search": "x;id"},
        {"limit": 10, "filter": "all", "search": ""},
        {"limit": 10, "filter": "all", "search": "a" * 101},
        {"limit": 10, "filter": "all", "exec": "id"},
    ],
)
async def test_query_log_input_rejected_before_transport(pi, args):  # noqa: F811
    core, calls, _ = pi
    assert (await core.call("dot", "dns_query_log", args))["error"]["code"] == "invalid_arguments"
    assert ssh_calls(calls) == []


@pytest.mark.parametrize(
    "name, request_line", [("dns_query_log", "dns-querylog 1 all"), ("dns_rules", "dns-rules")]
)
async def test_dns_reads_reject_incomplete_agent_reply(pi, name, request_line):  # noqa: F811
    core, _, replies = pi
    replies[request_line] = {"ok": True}
    args = {"limit": 1, "filter": "all"} if name == "dns_query_log" else {}
    assert (await core.call("dot", name, args))["error"]["code"] == "backend_invalid"
