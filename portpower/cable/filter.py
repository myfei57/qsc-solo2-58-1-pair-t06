"""Fixed-window moving average used to damp temperature spikes."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable


class MovingAverageFilter:
    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("filter size must be positive")
        self._size = size
        self._window: deque[float] = deque(maxlen=size)

    def add(self, sample: float) -> float:
        self._window.append(float(sample))
        return self.average

    @property
    def size(self) -> int:
        return self._size

    def values(self) -> list[float]:
        return list(self._window)

    @property
    def average(self) -> float:
        if not self._window:
            return 0.0
        return sum(self._window) / len(self._window)

    def reset(self) -> None:
        self._window.clear()


class ActiveAverageFilter:
    """Moving-average view delegating to the currently installed cable."""

    def __init__(self, provider: Callable[[], MovingAverageFilter]) -> None:
        self._provider = provider

    def add(self, sample: float) -> float:
        return self._provider().add(sample)

    @property
    def average(self) -> float:
        return self._provider().average

    @property
    def size(self) -> int:
        return self._provider().size

    def values(self) -> list[float]:
        return self._provider().values()

    def reset(self) -> None:
        self._provider().reset()
