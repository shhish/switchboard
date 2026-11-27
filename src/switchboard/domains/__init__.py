"""One registration module per domain; imports have no I/O side effects."""

from ..registry import Registry
from . import (
    activity,
    alerts,
    approvals,
    audit,
    board,
    books,
    calendar,
    console,
    context,
    dns,
    elevate,
    feedback,
    git,
    home,
    jobs,
    mac_ops,
    media,
    memory,
    notes,
    ops,
    recipes,
    reminders,
    routines,
    services,
    storage,
    tasks,
    watch,
    weather,
)


def build_registry():
    registry = Registry()
    for domain in (ops, media, watch, home, calendar, reminders, tasks, jobs, approvals, feedback):
        domain.register(registry)
    mac_ops.register(registry)
    services.register(registry)
    storage.register(registry)
    books.register(registry)
    context.register(registry)
    weather.register(registry)
    alerts.register(registry)
    recipes.register(registry)
    notes.register(registry)
    console.register(registry)
    dns.register(registry)
    routines.register(registry)
    audit.register(registry)
    activity.register(registry)
    git.register(registry)
    git.register_land(registry)
    board.register(registry)
    elevate.register(registry)
    memory.register(registry)
    return registry
