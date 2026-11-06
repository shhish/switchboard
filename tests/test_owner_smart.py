"""Run a relocated copy of the owner script against fake executables only."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path("scripts/owner-enable-smart.sh")
FIXTURES = Path(__file__).parent / "fixtures" / "storage"


@pytest.fixture
def owner_script(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for name in ("physical.plist", "info.json"):
        (data / name).write_bytes((FIXTURES / name).read_bytes())
    helper = tmp_path / "fake-host"
    helper.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """
import json
import plistlib
import sys
from pathlib import Path
root = Path(__file__).parent / 'data'
args = sys.argv[1:]
with (root / 'calls').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if args == ['list', '-plist', 'physical']:
    sys.stdout.buffer.write((root / 'physical.plist').read_bytes())
elif args[:2] == ['info', '-plist']:
    infos = json.loads((root / 'info.json').read_text())
    sys.stdout.buffer.write(plistlib.dumps(infos[args[-1]]))
elif args[:2] == ['-s', 'on']:
    disk = args[-1]
    if (root / ('fail-' + disk)).exists():
        print('Enable failed')
        sys.exit(1)
    (root / ('enabled-' + disk)).touch()
    print('SMART enabled')
elif args[:2] == ['-j', '-n']:
    enabled = args[-1] == 'disk6' or (root / ('enabled-' + args[-1])).exists()
    print(json.dumps({'smart_support': {'enabled': enabled}}))
else:
    raise AssertionError('Unexpected fake command')
"""
    )
    helper.chmod(0o700)
    script = tmp_path / "owner-enable-smart.sh"
    script.write_text(
        SCRIPT.read_text()
        .replace("/usr/sbin/diskutil", str(helper))
        .replace("/opt/homebrew/bin/smartctl", str(helper))
        .replace("/opt/homebrew/bin/python3", shlex.quote(sys.executable))
    )
    return script, data


def calls(data):
    return [json.loads(line) for line in (data / "calls").read_text().splitlines()]


def test_owner_script_external_disabled_only_and_idempotent(owner_script):
    script, data = owner_script
    first = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert [c for c in calls(data) if c[:2] == ["-s", "on"]] == [
        ["-s", "on", "disk7"],
        ["-s", "on", "disk9"],
    ]
    assert not any(c[-1] == "disk0" and c[0] == "-j" for c in calls(data))
    second = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert second.returncode == 0, second.stderr
    assert len([c for c in calls(data) if c[:2] == ["-s", "on"]]) == 2
    assert "No external physical disks" in second.stdout


def test_owner_script_reports_failure_and_continues_other_disks(owner_script):
    script, data = owner_script
    (data / "fail-disk7").touch()
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert (data / "enabled-disk9").exists()
    assert not (data / "enabled-disk7").exists()


def test_owner_script_rejects_arguments_before_discovery(owner_script):
    script, data = owner_script
    result = subprocess.run(
        ["/bin/bash", str(script), "disk0"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert not (data / "calls").exists()
