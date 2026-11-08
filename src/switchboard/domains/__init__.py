"""One registration module per domain; imports have no I/O side effects."""

from ..registry import Registry
from . import (
    alerts,
    approvals,
    audit,
    books,
    calendar,
    console,
    context,
    dns,
    feedback,
    git,
    home,
    jobs,
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
    git.register(registry)
    memory.register(registry)
    return registry
