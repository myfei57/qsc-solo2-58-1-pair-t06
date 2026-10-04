"""Thermal protection that trips the breaker on filtered temperature."""

from __future__ import annotations

from collections.abc import Callable

from portpower.cable.filter import MovingAverageFilter
from portpower.disconn.breaker import Breaker
from portpower.disconn.open import open_breaker


class Protector:
    def __init__(
        self,
        threshold: float | Callable[[], float],
        filter_: MovingAverageFilter,
    ) -> None:
        self._threshold = threshold
        self._filter = filter_
        self._tripped = False
        self._breaches = 0

    @property
    def threshold(self) -> float:
        value = self._threshold() if callable(self._threshold) else self._threshold
        if value <= 0:
            raise ValueError("protection threshold must be positive")
        return value

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def breaches(self) -> int:
        return self._breaches

    def reset_filter(self) -> None:
        self._filter.reset()

    def reset(self) -> None:
        self.reset_filter()
        self._tripped = False
        self._breaches = 0

    def assess(self, filtered: float) -> bool:
        if filtered > self.threshold:
            self._tripped = True
            self._breaches += 1
            return True
        return False

    def verdict(self, sample: float) -> bool:
        filtered = self._filter.add(sample)
        return self.assess(filtered)

    def open_and_reset(self, breaker: Breaker) -> None:
        if breaker.closed:
            open_breaker(breaker)
        self._tripped = False
        self._breaches = 0
        self.reset_filter()
