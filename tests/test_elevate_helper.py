"""The root helper (native/elevate) runs only what a paired key signed.

Builds the helper's test variant (swiftc -D ELEVATE_TEST: scratch paths, no root) and drives
it over its socket with keys made by openssl. Skipped where swiftc or openssl is missing.
"""

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "elevate" / "main.swift"
pytestmark = pytest.mark.skipif(
    not (shutil.which("swiftc") and shutil.which("openssl")), reason="needs swiftc and openssl"
)

RECIPES = {
    "recipes": [
        {
            "id": "hello",
            "title": "Echo",
            "steps": [["/bin/echo", "hello", "{who}"]],
            "args": [{"name": "who", "pattern": "[a-z]{1,10}"}],
            "timeout": 10,
        },
        {"id": "where", "title": "Home", "steps": [["/bin/echo", "{home}", "{owner}"]]},
        {"id": "slow", "title": "Sleep", "steps": [["/bin/sleep", "30"]], "timeout": 5},
        {
            "id": "pick",
            "title": "Choice",
            "steps": [["/bin/echo", "{x}"]],
            "args": [{"name": "x", "choices": ["a", "b"]}],
        },
    ]
}


class Key:
    def __init__(self, folder: Path, name: str):
        self.pem = folder / f"{name}.pem"
        subprocess.run(
            ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", self.pem],
            check=True,
            capture_output=True,
        )
        der = subprocess.run(
            [
                "openssl",
                "ec",
                "-in",
                self.pem,
                "-pubout",
                "-conv_form",
                "uncompressed",
                "-outform",
                "DER",
            ],
            check=True,
            capture_output=True,
        ).stdout
        self.x963 = der[-65:]
        self.name = name

    def sign(self, data: bytes) -> str:
        return base64.b64encode(
            subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", self.pem],
                input=data,
                check=True,
                capture_output=True,
            ).stdout
        ).decode()


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    out = tmp_path_factory.mktemp("build") / "elevate-test"
    subprocess.run(
        ["swiftc", "-swift-version", "5", "-D", "ELEVATE_TEST", "-o", out, SOURCE],
        check=True,
        capture_output=True,
    )
    return out


@pytest.fixture
def helper(binary, tmp_path):
    # Unix socket paths are short; keep the scratch folder near /tmp.
    base = Path(f"/tmp/elv-{secrets.token_hex(4)}")
    (base / "support").mkdir(parents=True)
    key = Key(tmp_path, "phone")
    machine = secrets.token_hex(8)
    (base / "support" / "config.json").write_text(
        json.dumps({"owner_uid": os.getuid(), "machine": machine})
    )
    (base / "support" / "keys.json").write_text(
        json.dumps({"keys": [{"name": "phone", "x963": base64.b64encode(key.x963).decode()}]})
    )
    (base / "support" / "recipes.json").write_text(json.dumps(RECIPES))
    proc = subprocess.Popen([binary], env={"ELEVATE_TEST_DIR": str(base)})
    for _ in range(100):
        if (base / "elevate.sock").exists():
            break
        time.sleep(0.05)

    class Helper:
        pass

    h = Helper()
    h.base, h.key, h.machine, h.tmp = base, key, machine, tmp_path

    def call(request):
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(base / "elevate.sock"))
            s.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)

    def payload(**fields):
        body = {
            "v": 1,
            "machine": machine,
            "nonce": secrets.token_hex(16),
            "expires": int(time.time()) + 3600,
            **fields,
        }
        return json.dumps(body).encode()

    def run(data, signer=None):
        signer = signer or key
        return call(
            {
                "op": "run",
                "payload": base64.b64encode(data).decode(),
                "signature": signer.sign(data),
            }
        )

    def wait(job):
        for _ in range(200):
            result = call({"op": "result", "job": job})
            if result["state"] != "running":
                return result
            time.sleep(0.05)
        return result

    h.call, h.payload, h.run, h.wait = call, payload, run, wait
    yield h
    proc.terminate()
    proc.wait(5)
    shutil.rmtree(base, ignore_errors=True)


def recipe(h, rid, args, steps, **extra):
    return h.payload(kind="recipe", recipe=rid, args=args, steps=steps, **extra)


def test_status_lists_keys_and_recipes(helper):
    status = helper.call({"op": "status"})
    assert status["ok"] and status["machine"] == helper.machine
    assert [r["id"] for r in status["recipes"]] == ["hello", "where", "slow", "pick"]
    assert len(status["keys"]) == 1 and len(status["keys"][0]["fingerprint"]) == 19


def test_signed_recipe_runs_and_reports(helper):
    reply = helper.run(recipe(helper, "hello", {"who": "mac"}, [["/bin/echo", "hello", "mac"]]))
    assert reply["ok"] and reply["state"] == "running"
    result = helper.wait(reply["job"])
    assert result["state"] == "done" and result["exit"] == 0
    assert "hello mac" in result["output"]


def test_home_and_owner_tokens(helper):
    status = helper.call({"op": "status"})
    steps = [["/bin/echo", status["home"], status["owner"]]]
    result = helper.wait(helper.run(recipe(helper, "where", {}, steps))["job"])
    assert result["state"] == "done"


def test_unsigned_or_foreign_signature_refused(helper):
    data = recipe(helper, "hello", {"who": "mac"}, [["/bin/echo", "hello", "mac"]])
    stranger = Key(helper.tmp, "stranger")
    assert helper.run(data, stranger) == {
        "ok": False,
        "error": "signature not from a paired iPhone",
    }
    tampered = data.replace(b"mac", b"max")
    reply = helper.call(
        {
            "op": "run",
            "payload": base64.b64encode(tampered).decode(),
            "signature": helper.key.sign(data),
        }
    )
    assert reply["ok"] is False


def test_replay_refused(helper):
    data = recipe(helper, "hello", {"who": "mac"}, [["/bin/echo", "hello", "mac"]])
    assert helper.run(data)["ok"]
    assert helper.run(data) == {"ok": False, "error": "payload already used"}


def test_steps_must_match_recipe(helper):
    reply = helper.run(recipe(helper, "hello", {"who": "mac"}, [["/bin/echo", "goodbye", "mac"]]))
    assert reply["error"] == "steps differ from recipe hello"


def test_args_checked_against_rules(helper):
    bad = recipe(helper, "hello", {"who": "MAC; rm"}, [["/bin/echo", "hello", "MAC; rm"]])
    assert helper.run(bad)["error"] == "arg who not allowed"
    extra = recipe(helper, "hello", {"who": "a", "x": "b"}, [["/bin/echo", "hello", "a"]])
    assert helper.run(extra)["error"] == "wrong args"
    choice = recipe(helper, "pick", {"x": "c"}, [["/bin/echo", "c"]])
    assert helper.run(choice)["error"] == "arg x not allowed"


def test_value_cannot_inject_a_token(helper):
    # Rendering is one pass: "{home}" inside a value would stay literal, and rules refuse it.
    data = recipe(helper, "pick", {"x": "{home}"}, [["/bin/echo", "{home}"]])
    assert helper.run(data)["error"] == "arg x not allowed"


def test_custom_runs_verbatim_and_reports_failure(helper):
    ok = helper.wait(helper.run(helper.payload(kind="custom", steps=[["/bin/echo", "a b"]]))["job"])
    assert ok["state"] == "done" and "a b" in ok["output"]
    bad = helper.wait(helper.run(helper.payload(kind="custom", steps=[["/usr/bin/false"]]))["job"])
    assert bad["state"] == "failed" and bad["exit"] == 1
    relative = helper.run(helper.payload(kind="custom", steps=[["echo", "x"]]))
    assert relative["error"] == "bad steps"


def test_expiry_and_machine_checked(helper):
    old = helper.payload(kind="custom", steps=[["/bin/echo"]], expires=int(time.time()) - 1)
    assert helper.run(old)["error"] == "payload expired"
    far = helper.payload(
        kind="custom", steps=[["/bin/echo"]], expires=int(time.time()) + 40 * 86400
    )
    assert helper.run(far)["error"] == "payload expired"
    other = helper.payload(kind="custom", steps=[["/bin/echo"]], machine="elsewhere")
    assert helper.run(other)["error"] == "payload not for this Mac"


def test_timeout_kills(helper):
    result = helper.wait(helper.run(recipe(helper, "slow", {}, [["/bin/sleep", "30"]]))["job"])
    assert result["state"] == "failed" and "timed out" in result["output"]


def test_add_key_is_itself_signed(helper):
    second = Key(helper.tmp, "second")
    data = helper.payload(kind="custom", steps=[["/bin/echo", "x"]])
    assert helper.run(data, second)["ok"] is False
    add = helper.payload(
        kind="add-key", key={"name": "iPad", "x963": base64.b64encode(second.x963).decode()}
    )
    assert helper.run(add)["state"] == "done"
    assert helper.run(helper.payload(kind="custom", steps=[["/bin/echo", "y"]]), second)["ok"]
    keys = json.loads((helper.base / "support" / "keys.json").read_text())["keys"]
    assert [k["name"] for k in keys] == ["phone", "iPad"]


def test_recipe_update_needs_matching_hash(helper):
    new = helper.tmp / "recipes.json"
    new.write_text(json.dumps({"recipes": [RECIPES["recipes"][0]]}))
    wrong = helper.payload(kind="update-recipes", file={"path": str(new), "sha256": "0" * 64})
    assert helper.run(wrong)["error"] == "sha256 mismatch"
    digest = hashlib.sha256(new.read_bytes()).hexdigest()
    good = helper.payload(kind="update-recipes", file={"path": str(new), "sha256": digest})
    assert helper.run(good)["state"] == "done"
    assert [r["id"] for r in helper.call({"op": "status"})["recipes"]] == ["hello"]


def test_garbage_is_refused(helper):
    assert helper.call({"op": "nope"})["ok"] is False
    assert helper.call({"op": "run", "payload": "!!", "signature": "!!"})["ok"] is False
    assert helper.call({"op": "result", "job": "../x"})["ok"] is False


def test_shipped_recipes_load(helper):
    # The real recipes file must load in the helper as shipped (a bad rule stops startup).
    shipped = (ROOT / "native" / "elevate" / "recipes.json").read_bytes()
    path = helper.tmp / "shipped.json"
    path.write_bytes(shipped)
    data = helper.payload(
        kind="update-recipes",
        file={"path": str(path), "sha256": hashlib.sha256(shipped).hexdigest()},
    )
    assert helper.run(data)["state"] == "done"
    ids = [r["id"] for r in helper.call({"op": "status"})["recipes"]]
    assert ids == [r["id"] for r in json.loads(shipped)["recipes"]] and "privacy-reset" in ids


def test_shipped_rules(helper):
    shipped = (ROOT / "native" / "elevate" / "recipes.json").read_bytes()
    path = helper.tmp / "shipped.json"
    path.write_bytes(shipped)
    digest = hashlib.sha256(shipped).hexdigest()
    helper.run(helper.payload(kind="update-recipes", file={"path": str(path), "sha256": digest}))
    home = helper.call({"op": "status"})["home"]

    def allowed(rid, args, steps):
        return helper.run(recipe(helper, rid, args, steps)).get("error") != "arg pkg not allowed"

    pkg = f"{home}/Downloads/Thing 1.2.pkg"
    steps = [
        ["/usr/sbin/pkgutil", "--check-signature", pkg],
        ["/usr/sbin/installer", "-pkg", pkg, "-target", "/"],
    ]
    reply = helper.run(recipe(helper, "install-pkg", {"pkg": pkg}, steps))
    assert reply["ok"]  # runs pkgutil on a missing file and fails harmlessly
    escape = f"{home}/Downloads/../x.pkg"
    bad = helper.run(recipe(helper, "install-pkg", {"pkg": escape}, steps))
    assert bad["error"] == "arg pkg not allowed"
    elsewhere = "/tmp/Downloads/x.pkg"
    assert helper.run(recipe(helper, "install-pkg", {"pkg": elsewhere}, steps))["ok"] is False


def test_busy_helper_keeps_the_approval(helper):
    for _ in range(2):
        assert helper.run(recipe(helper, "slow", {}, [["/bin/sleep", "30"]]))["ok"]
    data = recipe(helper, "hello", {"who": "mac"}, [["/bin/echo", "hello", "mac"]])
    assert helper.run(data) == {"ok": False, "error": "busy, try again"}
    for _ in range(200):
        reply = helper.run(data)
        if reply.get("error") != "busy, try again":
            break
        time.sleep(0.1)
    assert reply["ok"], reply  # the same signed request runs once a slot frees


def test_recipes_cannot_shadow_home_or_owner(helper):
    bad = {
        "recipes": [
            {
                "id": "x",
                "title": "X",
                "steps": [["/bin/echo", "{home}"]],
                "args": [{"name": "home", "pattern": ".*"}],
            }
        ]
    }
    path = helper.tmp / "bad.json"
    path.write_text(json.dumps(bad))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    reply = helper.run(
        helper.payload(kind="update-recipes", file={"path": str(path), "sha256": digest})
    )
    assert reply["ok"] is False
