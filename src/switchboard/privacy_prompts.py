"""macOS privacy (TCC) checks that time out, read from the unified log.

When the kernel asks tccd whether a process may use a protected resource and gets no
answer, it waits for its approval watchdog and then fails the call (EINTR). Symptoms
elsewhere look unrelated: external drives unreadable, Docker stuck starting, hung reads
of ~/Documents, CI steps stalling seconds per process. The usual cause is not a visible
prompt: on 2026-11-23 the system tccd itself had wedged (constant CPU, no answers, no
prompt on screen) and only a restart (`sudo killall -9 tccd sandboxd`; launchd relaunches it)
cleared it. Occasional timeouts from background system daemons are normal, so the
monitor pages only on a sustained run.
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
