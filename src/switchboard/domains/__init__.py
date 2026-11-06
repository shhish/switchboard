"""One registration module per domain; imports have no I/O side effects."""

from ..registry import Registry
from . import (
    approvals,
    books,
    calendar,
    context,
    feedback,
    home,
    jobs,
    media,
    ops,
    reminders,
    services,
    tasks,
    watch,
    weather,
)


def build_registry():
    registry = Registry()
    for domain in (ops, media, watch, home, calendar, reminders, tasks, jobs, approvals, feedback):
        domain.register(registry)
    services.register(registry)
    books.register(registry)
    context.register(registry)
    weather.register(registry)
    return registry
