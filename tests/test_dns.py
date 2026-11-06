"""AdGuard Home stats and bounded pause/resume through the remote host agent."""

import json

import pytest

from switchboard import hostcmd

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
