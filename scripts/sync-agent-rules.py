#!/usr/bin/env python3
"""Copy docs/agent-rules.md into every agent's global instruction file.

The block sits between markers, so re-running replaces it and leaves the rest of each
file alone. Files that don't exist yet are created only where the agent reads them.
"""

import sys
from pathlib import Path

BEGIN = "<!-- switchboard:agent-rules begin (synced from switchboard/docs/agent-rules.md) -->"
END = "<!-- switchboard:agent-rules end -->"
HOME = Path.home()
TARGETS = [
    HOME / ".claude/CLAUDE.md",  # Claude Code, every project
    HOME / ".codex/AGENTS.md",  # Codex, every project
    HOME / ".gemini/config/AGENTS.md",  # Antigravity global rules
    HOME / "workspace/projects/AGENTS.md",  # OpenClaw workspace and DeepSeek runs here
]


def sync(rules: str, path: Path) -> str:
    block = f"{BEGIN}\n{rules.strip()}\n{END}\n"
    text = path.read_text() if path.exists() else ""
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        new = head + block + ("\n" + tail if tail else "")
    else:
        new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    if new == text:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    return "updated"


def main() -> int:
    rules = (Path(__file__).resolve().parents[1] / "docs/agent-rules.md").read_text()
    for path in TARGETS:
        print(f"{sync(rules, path)}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
