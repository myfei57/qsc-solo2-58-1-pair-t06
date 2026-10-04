"""Rolling monitor that tracks peak, count and filtered average."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from portpower.cable.filter import MovingAverageFilter


class TemperatureMonitor:
    def __init__(self, window: int = 5) -> None:
        self._filter = MovingAverageFilter(window)
        self._peak: float | None = None
        self._peak_at: str | None = None
        self._count = 0
        self._total = 0.0
        self._last: float | None = None

    @classmethod
    def from_stats(
        cls,
        window: int,
        count: int = 0,
        total: float = 0.0,
        peak: float | None = None,
        peak_at: str | None = None,
        last: float | None = None,
    ) -> "TemperatureMonitor":
        monitor = cls(window)
        monitor._count = int(count)
        monitor._total = float(total)
        monitor._peak = None if peak is None else float(peak)
        monitor._peak_at = peak_at
        monitor._last = None if last is None else float(last)
        return monitor

    def observe(self, sample: float, at: str | None = None) -> float:
        value = float(sample)
        self._filter.add(value)
        if self._peak is None or value > self._peak:
            self._peak = value
            self._peak_at = at
        self._count += 1
        self._total += value
        self._last = value
        return self.filtered

    @property
    def filter_window(self) -> MovingAverageFilter:
        return self._filter

    def preview_filtered(self, sample: float) -> float:
        values = list(self._filter.values())
        values.append(float(sample))
        values = values[-self._filter.size :]
        return sum(values) / len(values)

    @property
    def filtered(self) -> float:
        return self._filter.average

    @property
    def peak(self) -> float:
        return 0.0 if self._peak is None else self._peak

    @property
    def peak_at(self) -> str | None:
        return self._peak_at

    @property
    def count(self) -> int:
        return self._count

    @property
    def total(self) -> float:
        return self._total

    @property
    def cumulative_average(self) -> float:
        if self._count == 0:
            return 0.0
        return self._total / self._count

    def stats(self) -> dict[str, Any]:
        return {
            "count": self._count,
            "total": self._total,
            "cumulative_average": self.cumulative_average,
            "peak": self.peak,
            "peak_at": self._peak_at,
            "last": self._last if self._last is not None else 0.0,
        }

    def summary(self) -> dict[str, Any]:
        summary = self.stats()
        summary["count"] = float(self._count)
        summary["average"] = self.filtered
        return summary

    def reset_filter(self) -> None:
        self._filter.reset()


Timestamp = str
MonitorFactory = Callable[[], TemperatureMonitor]


class ActiveTemperatureMonitor:
    """Temperature monitor view delegating to the active cable asset."""

    def __init__(self, provider: Callable[[], TemperatureMonitor]) -> None:
        self._provider = provider

    def observe(self, sample: float, at: str | None = None) -> float:
        return self._provider().observe(sample, at)

    @property
    def filtered(self) -> float:
        return self._provider().filtered

    @property
    def peak(self) -> float:
        return self._provider().peak

    @property
    def peak_at(self) -> str | None:
        return self._provider().peak_at

    @property
    def count(self) -> int:
        return self._provider().count

    @property
    def total(self) -> float:
        return self._provider().total

    @property
    def cumulative_average(self) -> float:
        return self._provider().cumulative_average

    def stats(self) -> dict[str, Any]:
        return self._provider().stats()

    def summary(self) -> dict[str, Any]:
        return self._provider().summary()

    def reset_filter(self) -> None:
        self._provider().reset_filter()
