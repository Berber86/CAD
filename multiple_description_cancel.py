"""Cooperative cancellation for the experimental PDF workflow.

Already-started HTTP calls cannot be recalled; no new paid calls are started
when cancellation is observed, and a cancelled run is not cached as a result.
"""
from collections.abc import Callable


class Cancelled(RuntimeError):
    """The caller invalidated the current job."""


def check(current: Callable[[], bool]) -> None:
    if not current():
        raise Cancelled('Job cancelled')
