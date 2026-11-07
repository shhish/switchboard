import os
import time

import pytest
import yaml

from switchboard.errors import CapabilityError
from switchboard.policy import Policy


def write(path, scopes):
    path.write_text(yaml.safe_dump({"clients": {"codex": {"scopes": scopes}}}))


def test_policy_parses_once_while_unchanged_and_reloads_on_edit(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    write(path, ["ops:read"])
    policy = Policy(path)
    parses = []
    real = yaml.safe_load
    monkeypatch.setattr(yaml, "safe_load", lambda text: parses.append(1) or real(text))
    for _ in range(50):
        assert policy.client("codex")["scopes"] == ["ops:read"]
    assert len(parses) == 1
    write(path, ["ops:read", "media:read"])
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    assert policy.client("codex")["scopes"] == ["ops:read", "media:read"]
    assert len(parses) == 2


def test_cached_policy_cannot_be_mutated_by_callers(tmp_path):
    path = tmp_path / "policy.yaml"
    write(path, ["ops:read"])
    policy = Policy(path)
    policy.load()["clients"]["codex"]["scopes"].append("ops:admin")
    assert policy.client("codex")["scopes"] == ["ops:read"]


def test_removed_or_broken_policy_fails_closed_immediately(tmp_path):
    path = tmp_path / "policy.yaml"
    write(path, ["ops:read"])
    policy = Policy(path)
    policy.load()
    path.write_text("clients: [not, a, mapping")
    with pytest.raises(CapabilityError):
        policy.load()
    path.unlink()
    with pytest.raises(CapabilityError):
        policy.load()
