"""Run the standalone Pi agent against fakes only; never SSH or contact AdGuard."""

import io
import json
import runpy
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from switchboard import hostcmd

from .test_dns import BAD_DOMAINS, BAD_REQUESTS

AGENT = Path(__file__).parents[1] / "pi-agent" / "switchboard-pi-agent"


@pytest.fixture
def agent(monkeypatch):
    namespace = runpy.run_path(str(AGENT))
    # runpy function globals belong to the executed module, not its returned copy.
    namespace = namespace["main"].__globals__
    monkeypatch.setitem(namespace, "dns_auth", lambda: {"user": "fake", "password": "fake"})
    return namespace


@pytest.mark.parametrize("line", BAD_REQUESTS)
def test_agent_rejects_bad_requests_without_backend(agent, monkeypatch, capsys, line):
    def unexpected(*args):
        pytest.fail("invalid request reached AdGuard")

    monkeypatch.setitem(agent, "dns_auth", unexpected)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", line)
    assert agent["main"]() == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize("domain", BAD_DOMAINS)
def test_domain_validation_agrees(agent, domain):
    assert agent["dns_domain"](domain) is False
    assert hostcmd.dns_domain(domain) is False


def test_agent_query_projection_and_bounds(agent, monkeypatch):
    calls = []
    row = {
        "time": "2026-11-23T12:00:00Z",
        "client": "192.0.2.5",
        "client_info": {"name": "Test TV", "private": "dropped"},
        "question": {"name": "ads.example.com", "type": "A"},
        "status": "NXDOMAIN",
        "reason": "FilteredBlackList",
        "rules": [{"text": "||ads.example.com^", "filter_list_id": 42}],
        "answer": [{"value": "PRIVATE"}],
    }

    def get(route, auth, params):
        calls.append((route, params))
        return {"data": [row] * 201}

    monkeypatch.setitem(agent, "adguard_get", get)
    result = agent["dns_querylog"](["1", "blocked", "ads.example.com"])
    assert calls == [
        (
            "/control/querylog",
            {"limit": 200, "search": "ads.example.com"},
        )
    ]
    assert result["truncated"] and len(result["queries"]) == 1
    query = result["queries"][0]
    assert query["client"] == {"ip": "192.0.2.5", "name": "Test TV"}
    assert query["question"] == {"domain": "ads.example.com", "type": "A"}
    assert query["reason"] == "FilteredBlackList" and query["answer_status"] == "NXDOMAIN"
    assert query["rules"] == [{"rule": "||ads.example.com^", "filter_id": 42}]
    assert "PRIVATE" not in json.dumps(result)
    row["rules"] = [{"text": "é" * 512, "filter_list_id": -1}] * 9
    result = agent["dns_querylog"](["200", "all"])
    assert result["truncated"] and len(json.dumps(result).encode()) < hostcmd.OUTPUT_LIMIT


def query_row(index, reason="FilteredBlackList", **fields):
    return {
        "time": f"2026-11-23T12:00:00.{1000000 - index:09d}Z",
        "client": "192.0.2.5",
        "question": {"name": f"q-{index}.example.com", "type": "A"},
        "reason": reason,
        **fields,
    }


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"rules": [{"text": "||modern.example^", "filter_list_id": 42}]},
            [{"rule": "||modern.example^", "filter_id": 42}],
        ),
        (
            {"rule": "||legacy.example^", "filterId": -1},
            [{"rule": "||legacy.example^", "filter_id": -1}],
        ),
        (
            {"rules": [{"text": "modern", "filter_list_id": 7}], "rule": "old", "filterId": 99},
            [{"rule": "modern", "filter_id": 7}],
        ),
        ({"rules": [], "rule": "old", "filterId": 99}, []),
        ({"rules": None, "rule": "old", "filterId": 99}, [{"rule": "old", "filter_id": 99}]),
        ({}, []),
        ({"rule": "", "filterId": 99}, []),
    ],
    ids=[
        "v1-rules",
        "v0107-legacy",
        "prefer-modern",
        "empty-modern",
        "null-legacy",
        "no-rule",
        "empty-legacy",
    ],
)
def test_querylog_current_and_legacy_rules(agent, monkeypatch, fields, expected):
    def get(route, auth, params):
        assert route == "/control/querylog" and params == {"limit": 1}
        return {"data": [query_row(0, **fields)]}

    monkeypatch.setitem(agent, "adguard_get", get)
    result = agent["dns_querylog"](["1", "all"])
    assert result["queries"][0]["rules"] == expected


@pytest.mark.parametrize("mode", ["blocked", "all"])
def test_querylog_reason_filtering(agent, monkeypatch, mode):
    blocked = [
        "FilteredBlackList",
        "FilteredBlockedService",
        "FilteredParental",
        "FilteredSafeBrowsing",
        "FilteredSafeSearch",
        "FilteredInvalid",
        "FilteredFuture",
    ]
    other = [
        "NotFilteredAllowList",
        "NotFilteredNotFound",
        "NotFilteredWhiteList",
        "Rewrite",
        "RewriteRule",
        "RewriteEtcHosts",
        "",
        None,
        1,
    ]
    rows = [query_row(i, reason) for i, reason in enumerate(blocked + other)]
    calls = []

    def get(route, auth, params):
        calls.append(dict(params))
        assert "response_status" not in params
        return {"data": [] if "older_than" in params else rows}

    monkeypatch.setitem(agent, "adguard_get", get)
    result = agent["dns_querylog"](["200", mode])
    expected = rows[: len(blocked)] if mode == "blocked" else rows
    assert [row["time"] for row in result["queries"]] == [row["time"] for row in expected]
    assert not result["truncated"]
    assert calls == [{"limit": 200}, {"limit": 200, "older_than": rows[-1]["time"]}]


def test_querylog_blocked_follows_cursors_and_stops_at_limit(agent, monkeypatch):
    pages = [
        {"data": [query_row(i, "NotFilteredNotFound") for i in range(200)]},
        {
            "data": [query_row(i, "NotFilteredAllowList") for i in range(200, 399)]
            + [query_row(399)]
        },
        {"data": [query_row(400), query_row(401), query_row(402)]},
    ]
    pages[0]["oldest"] = "2026-11-23T11:59:59Z"
    pages[1]["oldest"] = "2026-11-23T11:59:58Z"
    calls = []

    def get(route, auth, params):
        assert route == "/control/querylog" and "response_status" not in params
        calls.append(params)
        assert len(calls) <= 3
        return pages[len(calls) - 1]

    monkeypatch.setitem(agent, "adguard_get", get)
    result = agent["dns_querylog"](["3", "blocked", "example.com"])
    assert [q["question"]["domain"] for q in result["queries"]] == [
        "q-399.example.com",
        "q-400.example.com",
        "q-401.example.com",
    ]
    assert calls == [
        {"limit": 200, "search": "example.com"},
        {"limit": 200, "search": "example.com", "older_than": pages[0]["oldest"]},
        {"limit": 200, "search": "example.com", "older_than": pages[1]["oldest"]},
    ]
    assert result["truncated"]


@pytest.mark.parametrize("hits", [False, True])
def test_querylog_stops_after_five_pages(agent, monkeypatch, hits):
    calls = []

    def get(route, auth, params):
        page = len(calls)
        assert page < 5
        calls.append(dict(params))
        rows = [query_row(i, "NotFilteredNotFound") for i in range(page * 200, (page + 1) * 200)]
        if hits:
            rows[-1]["reason"] = "FilteredBlackList"
        if page:
            assert params["older_than"] == query_row(page * 200 - 1)["time"]
        return {"data": rows}

    monkeypatch.setitem(agent, "adguard_get", get)
    result = agent["dns_querylog"](["10", "blocked"])
    assert len(calls) == 5 and result["truncated"]
    assert len(result["queries"]) == (5 if hits else 0)


@pytest.mark.parametrize("cursor", [None, "", ["invalid"], "x" * 101])
def test_querylog_missing_cursor_is_bounded(agent, monkeypatch, cursor):
    calls = []

    def get(*args):
        calls.append(args)
        return {"data": [{"reason": "NotFilteredNotFound"}], "oldest": cursor}

    monkeypatch.setitem(agent, "adguard_get", get)
    result = agent["dns_querylog"](["10", "blocked"])
    assert len(calls) == 1 and result["queries"] == [] and result["truncated"]


def test_querylog_repeated_cursor_does_not_repeat_results(agent, monkeypatch):
    calls = []
    page = {"data": [query_row(0)], "oldest": query_row(0)["time"]}

    def get(*args):
        calls.append(args)
        return page

    monkeypatch.setitem(agent, "adguard_get", get)
    result = agent["dns_querylog"](["10", "blocked"])
    assert len(calls) == 2 and len(result["queries"]) == 1 and result["truncated"]


def test_querylog_empty_log_is_complete(agent, monkeypatch):
    calls = []

    def get(*args):
        calls.append(args)
        return {"data": []}

    monkeypatch.setitem(agent, "adguard_get", get)
    assert agent["dns_querylog"](["10", "blocked"]) == {
        "queries": [],
        "limit": 10,
        "truncated": False,
    }
    assert len(calls) == 1


@pytest.mark.parametrize(
    "data", [{}, {"data": None}, {"data": [None]}, {"data": [query_row(0, rules="bad")]}]
)
def test_querylog_malformed_pages_are_refused(agent, monkeypatch, data):
    monkeypatch.setitem(agent, "adguard_get", lambda *_: data)
    with pytest.raises(agent["Refused"]):
        agent["dns_querylog"](["10", "all"])


@pytest.mark.parametrize(
    "route",
    [
        "/control/querylog_config",
        "/control/stats_config",
        "/control/querylog_info",
        "/control/stats_info",
        "/control/blocked_services/list",
        "/control/blocked_services/services",
        "/control/blocked_services/set",
        "/control/clients/find",
        "/control/safesearch/enable",
        "/control/safesearch/disable",
    ],
)
def test_removed_adguard_api_routes_are_not_callable(agent, monkeypatch, route):
    monkeypatch.setattr(
        agent["urllib"].request, "build_opener", lambda *_: pytest.fail("removed API")
    )
    assert route not in agent["ADGUARD_ROUTES"]
    with pytest.raises(ValueError):
        agent["adguard_get"](route, agent["dns_auth"]())
    with pytest.raises(ValueError):
        agent["adguard_post"](route, agent["dns_auth"](), {})


def test_rules_hide_url_secrets_and_bound_output(agent, monkeypatch):
    data = {
        "enabled": True,
        "user_rules": ["!" + "é" * 1000] * 1001,
        "filters": [
            {
                "id": 42,
                "name": "Test list",
                "enabled": True,
                "rules_count": 100,
                "url": "https://user:PRIVATE@filters.example.com/PRIVATE?key=PRIVATE",
                "last_updated": "2026-11-23T12:00:00Z",
            }
        ],
    }
    monkeypatch.setitem(agent, "adguard_get", lambda *_: data)
    result = agent["dns_rules"]([])
    assert result["enabled"] and result["truncated"] and result["user_rules_count"] == 1001
    assert result["filters"] == [
        {
            "id": 42,
            "name": "Test list",
            "url_host": "filters.example.com",
            "enabled": True,
            "rules_count": 100,
            "last_updated": "2026-11-23T12:00:00Z",
        }
    ]
    assert "PRIVATE" not in json.dumps(result)
    assert len(json.dumps(result).encode()) < hostcmd.OUTPUT_LIMIT


def test_rule_edits_are_exact_reversible_and_idempotent(agent, monkeypatch):
    original = [
        "! keep comment",
        "||other.example^",
        "||sub.example.com^",
        "||*.example.com^",
        "@@||example.com.evil^",
        "/example[.]com/",
        "||example.com^$important",
        "@@||EXAMPLE.com^",
        "0.0.0.0 example.com",
    ]
    state, posts = {"user_rules": list(original)}, []
    monkeypatch.setitem(agent, "adguard_get", lambda *_: state)

    def post(route, auth, body):
        assert route == "/control/filtering/set_rules"
        posts.append(body)
        state["user_rules"] = list(body["rules"])
        return {}

    monkeypatch.setitem(agent, "adguard_post", post)
    for action, expected in (("allow", "@@||example.com^"), ("block", "||example.com^")):
        result = agent["dns_rule"]([action, "Example.com"])
        assert result["changed"] and result["rule"] == expected
        assert state["user_rules"] == original[:6] + [expected]
        assert result["rules_count"] == 7
        assert not agent["dns_rule"]([action, "example.com"])["changed"]
    result = agent["dns_rule"](["remove", "example.com"])
    assert result["changed"] and result["rules_count"] == 6
    assert state["user_rules"] == original[:6]
    assert not agent["dns_rule"](["remove", "example.com"])["changed"]
    assert len(posts) == 3
    # The wildcard target is distinct from the parent and its subdomain rules.
    agent["dns_rule"](["remove", "*.example.com"])
    assert state["user_rules"] == original[:3] + original[4:6]


@pytest.mark.parametrize(
    "bad", [{}, {"user_rules": None}, {"user_rules": "x"}, {"user_rules": [None]}]
)
def test_bad_rule_state_never_written(agent, monkeypatch, bad):
    monkeypatch.setitem(agent, "adguard_get", lambda *_: bad)
    monkeypatch.setitem(agent, "adguard_post", lambda *_: pytest.fail("unsafe write"))
    with pytest.raises(agent["Refused"]):
        agent["dns_rule"](["block", "example.com"])


def test_rule_write_preserves_full_list_and_long_rules(agent, monkeypatch):
    rules = ["!" + "x" * 2000] * 1200 + ["||example.com^"]
    monkeypatch.setitem(agent, "adguard_get", lambda *_: {"user_rules": rules})
    writes = []
    monkeypatch.setitem(agent, "adguard_post", lambda route, auth, body: writes.append(body))
    result = agent["dns_rule"](["remove", "example.com"])
    assert result["changed"] and result["rule"] == "||example.com^"
    assert result["rules_count"] == 1200
    assert writes == [{"rules": rules[:-1]}]


def test_filters_refresh_payload_and_count(agent, monkeypatch):
    calls = []

    def post(route, auth, body):
        calls.append((route, body))
        return {"updated": 3}

    monkeypatch.setitem(agent, "adguard_post", post)
    assert agent["dns_filters_refresh"]([]) == {"updated": 3}
    assert calls == [("/control/filtering/refresh", {"whitelist": False})]


def test_http_routes_payloads_and_redirects_are_fixed(agent, monkeypatch):
    calls = []

    class FakeOpener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return io.BytesIO(b'{"updated":2}')

    def opener(*handlers):
        assert any(isinstance(h, agent["NoRedirect"]) for h in handlers)
        assert any(
            isinstance(h, agent["urllib"].request.ProxyHandler) and h.proxies == {}
            for h in handlers
        )
        return FakeOpener()

    monkeypatch.setattr(agent["urllib"].request, "build_opener", opener)
    auth = agent["dns_auth"]()
    agent["adguard_get"]("/control/querylog", auth, {"limit": 10, "search": "2001:db8::1"})
    req, timeout = calls[-1]
    assert req.full_url.startswith("http://127.0.0.1:3000/control/querylog?") and timeout == 8
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)["search"] == [
        "2001:db8::1"
    ]
    assert "response_status" not in urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
    before = len(calls)
    with pytest.raises(ValueError):
        agent["adguard_get"]("/control/querylog", auth, {"limit": 10, "response_status": "all"})
    assert len(calls) == before
    agent["adguard_post"]("/control/filtering/set_rules", auth, {"rules": ["||example.com^"]})
    req, _ = calls[-1]
    assert req.method == "POST" and json.loads(req.data) == {"rules": ["||example.com^"]}
    assert req.full_url == "http://127.0.0.1:3000/control/filtering/set_rules"
    assert agent["adguard_post"]("/control/filtering/refresh", auth, {"whitelist": False}) == {
        "updated": 2
    }
    assert calls[-1][1] == 60
    for route in ("http://example.com", "/control/filtering/set_rules?x", "/control/status"):
        with pytest.raises(ValueError):
            agent["adguard_post"](route, auth, {})
    with pytest.raises(ValueError):
        agent["adguard_get"]("/control/querylog?url=bad", auth)
    assert (
        agent["NoRedirect"]().redirect_request(None, None, 302, "", {}, "https://example.com")
        is None
    )


@pytest.mark.parametrize(
    "code, expected", [(401, "dns_auth_failed"), (403, "dns_auth_failed"), (500, "dns_unavailable")]
)
def test_http_failures_do_not_leak_backend_details(agent, monkeypatch, capsys, code, expected):
    class FakeOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "PRIVATE", {}, None)

    monkeypatch.setattr(agent["urllib"].request, "build_opener", lambda *_: FakeOpener())
    for line in ("dns-rules", "dns-filters-refresh"):
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", line)
        assert agent["main"]() == 3
        out = capsys.readouterr().out
        assert json.loads(out)["code"] == expected and "PRIVATE" not in out


def test_agent_version_and_success_envelope(agent, monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "version")
    assert agent["main"]() == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "agent_version": "5", "version": "5"}


def test_blank_user_rules_are_not_counted(agent, monkeypatch):
    # AdGuard reports an empty custom-rule list as [""].
    data = {"enabled": True, "user_rules": ["", "  ", "||ads.example.com^"], "filters": []}
    monkeypatch.setitem(agent, "adguard_get", lambda *_: data)
    result = agent["dns_rules"]([])
    assert result["user_rules"] == ["||ads.example.com^"]
    assert result["user_rules_count"] == 1 and not result["truncated"]
    data["user_rules"] = [""]
    assert agent["dns_rules"]([])["user_rules_count"] == 0
