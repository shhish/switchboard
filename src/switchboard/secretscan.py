"""Recognise values that look like credentials, whatever field they arrive in.

Used by the audit log (so a secret pasted into free text is never stored) and,
later, by shared memory writes. Conservative: false positives only cost a
"[REDACTED:secret]" marker in a log line.
"""

import re

PATTERNS = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{16,}"  # OpenAI/Anthropic-style keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # JWT
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/-]{20,}"
    r"|(?i:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S{6,}"
    r")"
)
TOKENISH = re.compile(r"[A-Za-z0-9_+/=-]{32,}")


def looks_secret(value):
    if not isinstance(value, str) or len(value) < 12:
        return False
    if PATTERNS.search(value):
        return True
    for chunk in TOKENISH.findall(value):
        # Long hex runs: Switchboard's own bearer tokens are 64 lower-case hex characters.
        # Short hex (trace IDs, 32) and numeric IDs stay; a 40+ run is treated as a secret.
        if re.fullmatch(r"[0-9a-fA-F]{40,}", chunk):
            return True
        # Long runs mixing upper, lower AND digits look like base64/random keys. IDs we keep
        # (UUIDs, hex hashes, job_<hex>, upper-case HomeKit IDs) never mix both cases.
        if all(re.search(p, chunk) for p in (r"[a-z]", r"[A-Z]", r"[0-9]")):
            return True
    return False
