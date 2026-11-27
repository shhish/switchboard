"""The README's tool counts must match the registry, so they can't silently go stale."""

import re
from pathlib import Path

from switchboard.domains import build_registry

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
NAMES = sorted(build_registry().capabilities)
TOTAL = len(NAMES)


def test_headline_counts_match_registry():
    found = {
        "hero": re.findall(r"same (\d+) tools", README),
        "badge": re.findall(r"tools-(\d+)-success", README),
        "nav": re.findall(r"All (\d+) tools", README),
        "diagram alt": re.findall(r"runs (\d+) typed tools", README),
    }
    for where, values in found.items():
        assert values, f"README has no {where} tool count"
        assert {int(v) for v in values} == {TOTAL}, (
            f"README {where} says {values}, registry has {TOTAL}"
        )
    for svg in ("how-it-fits-light.svg", "how-it-fits-dark.svg"):
        text = (ROOT / "docs" / "readme" / svg).read_text()
        assert re.findall(r">(\d+) typed tools<", text) == [str(TOTAL)], svg


def test_section_counts_match_their_prefixes():
    # Rows like: | ... <sub>`git_*` · 12 tools</sub> | ...
    rows = re.findall(r"<sub>((?:`[a-z_]+\*?`\s*)+)·\s*(\d+) tools</sub>", README)
    assert rows, "README has no per-section tool counts"
    for prefixes, count in rows:
        names = [p.rstrip("*") for p in re.findall(r"`([a-z_]+\*?)`", prefixes)]
        actual = [n for n in NAMES if any(n.startswith(p) for p in names)]
        assert len(actual) == int(count), (
            f"{prefixes.strip()} says {count}, registry has {len(actual)}"
        )
