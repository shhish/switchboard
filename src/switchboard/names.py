"""Public tool identities; legacy dotted callers share the canonical trust binding."""

import re


def canonical_name(name):
    if isinstance(name, str) and re.fullmatch(r"[a-z]+\.[a-z][a-z0-9_]*", name):
        return name.replace(".", "_", 1)
    return name
