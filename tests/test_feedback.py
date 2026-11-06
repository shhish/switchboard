from .conftest import domain_payload


async def test_report_list_close_and_dedupe(core):
    first = await core.call(
        "dot",
        "switchboard_report_issue",
        {"kind": "bug", "title": "search misses year", "details": "Dune 2021 not matched"},
    )
    assert first["status"] == "ok" and first["duplicate"] is False
    again = await core.call(
        "openclaw",
        "switchboard_report_issue",
        {"kind": "bug", "title": "search misses year", "details": "same thing"},
    )
    assert domain_payload(again) == {
        "status": "ok",
        "issue_id": first["issue_id"],
        "duplicate": True,
    }

    listed = (await core.call("codex", "switchboard_list_issues", {}))["issues"]
    assert [i["issue_id"] for i in listed] == [first["issue_id"]]
    assert listed[0]["client"] == "dot"

    closed = await core.call(
        "codex",
        "switchboard_close_issue",
        {"issue_id": first["issue_id"], "resolution": "fixed in abc123"},
    )
    assert closed["status"] == "ok"
    assert (await core.call("codex", "switchboard_list_issues", {}))["issues"] == []
    reclose = await core.call(
        "codex", "switchboard_close_issue", {"issue_id": first["issue_id"], "resolution": "x"}
    )
    assert reclose["error"]["code"] == "issue_not_open"


async def test_feedback_scopes_enforced(core):
    # agy only has jobs:read in the test policy.
    denied = await core.call(
        "agy",
        "switchboard_report_issue",
        {"kind": "ops", "title": "t", "details": "d"},
    )
    assert "error" in denied


async def test_issue_flood_is_capped(core, monkeypatch):
    from switchboard.domains import feedback

    monkeypatch.setattr(feedback, "MAX_OPEN_PER_CLIENT", 2)
    for n in range(2):
        ok = await core.call(
            "dot", "switchboard_report_issue", {"kind": "bug", "title": f"t{n}", "details": "d"}
        )
        assert ok["status"] == "ok"
    capped = await core.call(
        "dot", "switchboard_report_issue", {"kind": "bug", "title": "t9", "details": "d"}
    )
    assert capped["error"]["code"] == "issue_limit"
