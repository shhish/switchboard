"""Unanswered macOS privacy (TCC) prompts, read from the unified log.

When a privacy prompt sits unanswered on the Mac's screen, every process that needs
that permission blocks until the kernel's approval watchdog gives up (~1 s at a time,
repeatedly). Symptoms elsewhere look unrelated: EINTR reading external drives, Docker
stuck starting, hung reads of ~/Documents, CI jobs timing out. Only the owner can
answer the prompts, so the monitor's job is to say so early and plainly.
"""

import re
from collections import Counter

from . import hostcmd

MARKER = hostcmd.PRIVACY_PROMPT_MARKER
ARGV = hostcmd.PRIVACY_PROMPT_ARGV
# e.g. "... watchdog expired for approval entry 193863 (kTCCServicePhotos, pid 98839)"
ENTRY = re.compile(re.escape(MARKER) + r" \d+ \(kTCCService([A-Za-z]{1,64}), pid (\d{1,7})\)")
MAX_SERVICES = 6


def reading(text):
    """Counts of expired approval waits in the window, by permission, plus process count."""
    services = Counter()
    pids = set()
    for service, pid in ENTRY.findall(text):
        services[service] += 1
        pids.add(pid)
    return {
        "waits": sum(services.values()),
        "processes": len(pids),
        "services": dict(services.most_common(MAX_SERVICES)),
    }


async def sample():
    """One reading, or None when the log can't be read (never guesses)."""
    code, text, _ = await hostcmd.run(ARGV, timeout_s=30)
    if code != 0:
        return None
    return reading(text)


LABELS = {
    "SystemPolicyRemovableVolumes": "external drives",
    "SystemPolicyNetworkVolumes": "network drives",
    "SystemPolicyAllFiles": "Full Disk Access",
    "SystemPolicyAppBundles": "App Management",
    "SystemPolicyAppData": "other apps' data",
    "SystemPolicyDocumentsFolder": "Documents",
    "SystemPolicyDesktopFolder": "Desktop",
    "SystemPolicyDownloadsFolder": "Downloads",
    "FileProviderDomain": "cloud files",
    "Photos": "Photos",
}


def describe(detail):
    return ", ".join(
        f"{LABELS.get(name, name)} ({count})" for name, count in detail["services"].items()
    )
