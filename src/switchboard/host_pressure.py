"""Local macOS pressure readings shared by monitoring and host diagnostics."""

import asyncio
import os
import re

from . import hostcmd

KERNEL_PRESSURE = "kern.memorystatus_vm_pressure_level"


def reading(kernel, load_averages, cpu_count, swap):
    kernel_level = int(kernel.strip()) if kernel.strip() in {"1", "2", "4"} else None
    load_per_core = load_averages[0] / cpu_count if load_averages and cpu_count else None
    swap_mb = {
        key: float(value) * {"K": 1 / 1024, "M": 1, "G": 1024}[unit]
        for key, value, unit in re.findall(r"(total|used)\s*=\s*([\d.]+)([KMG])", swap)
    }
    swap_used_pct = (
        round(100 * swap_mb["used"] / swap_mb["total"], 2)
        if swap_mb.get("total", 0) > 0 and "used" in swap_mb
        else 0.0
        if swap_mb.get("total") == 0
        else None
    )
    return {
        "level": severity(kernel_level, load_per_core)[0],
        "kernel_level": kernel_level,
        "load_per_core": load_per_core,
        "swap_used_pct": swap_used_pct,
    }


def severity(kernel_level, load_per_core):
    # Evaluate both critical conditions before warnings; swap never affects status.
    if kernel_level == 4:
        return "critical", "memory_critical"
    if load_per_core is not None and load_per_core > 10:
        return "critical", "load_critical"
    if kernel_level == 2:
        return "busy", "memory_warning"
    if load_per_core is not None and load_per_core > 4:
        return "busy", "load_high"
    if kernel_level is None or load_per_core is None:
        return None, "pressure_unavailable"
    return "ok", None


async def sample():
    results = await asyncio.gather(
        hostcmd.run([hostcmd.SYSCTL, "-n", KERNEL_PRESSURE]),
        hostcmd.run([hostcmd.SYSCTL, "-n", "vm.swapusage"]),
    )
    kernel, swap = (text if code == 0 else "" for code, text, _ in results)
    try:
        load = os.getloadavg()
    except OSError:
        load = ()
    return reading(kernel, load, os.cpu_count(), swap)
