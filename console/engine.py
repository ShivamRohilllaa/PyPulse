"""Shared entry points used by the scheduler and CI API."""
from .benchrunner import benchmark, freeze
from .benchstats import pace, quantile, summarize
from .models import Run
from .uptime_engine import check_monitor, notify

__all__ = ["benchmark", "check_monitor", "enqueue", "notify", "pace", "quantile", "summarize"]


def enqueue(exercise):
    return Run.objects.create(exercise=exercise, config=freeze(exercise))
