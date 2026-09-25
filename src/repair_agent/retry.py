"""Retry only transient model failures known not to have consumed token budget."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from .models import ModelError

T = TypeVar("T")


class RetryPolicy:
    def __init__(self, max_retries: int, *, base_delay: float = 0.2, max_delay: float = 2.0, jitter: float = 0.1, sleeper: Callable[[float], None] = time.sleep, random_value: Callable[[], float] = random.random) -> None:
        if max_retries < 0 or base_delay < 0 or max_delay < base_delay or not 0 <= jitter <= 1:
            raise ValueError("invalid retry policy")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.sleeper = sleeper
        self.random_value = random_value

    def run(self, operation: Callable[[], T], *, on_attempt: Callable[[], None], on_retry: Callable[[ModelError, float], None], before_attempt: Callable[[], str | None] | None = None, can_wait: Callable[[float], bool] | None = None) -> T:
        retries = 0
        while True:
            blocked = before_attempt() if before_attempt else None
            if blocked:
                raise ModelError(blocked, category="BUDGET_EXHAUSTED", usage_unknown=False)
            on_attempt()
            try:
                return operation()
            except ModelError as exc:
                if not exc.retryable or exc.usage_unknown or retries >= self.max_retries:
                    raise
                delay = min(self.max_delay, self.base_delay * (2 ** retries))
                delay *= 1 - self.jitter + 2 * self.jitter * self.random_value()
                if can_wait is not None and not can_wait(delay):
                    raise
                on_retry(exc, delay)
                self.sleeper(delay)
                retries += 1
