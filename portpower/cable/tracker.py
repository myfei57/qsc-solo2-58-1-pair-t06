"""Attributes every temperature sample to the cable in service at that moment.

Each cable gets its own monitor, so accumulation and peaks never mix
across a replacement.  Exceedances are logged with the cable that was
in service, allowing later attribution by timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass

from portpower.cable.lifecycle import (
    CableLifecycle,
    ServiceSpan,
    normalize_timestamp,
    utc_now,
)
from portpower.cable.monitor import TemperatureMonitor
from portpower.cable.rating import Rating, classify
from portpower.store.kvstore import RecordStore

EXCEEDANCES_KEY = "cable.exceedances"


@dataclass(frozen=True)
class Exceedance:
    """One over-temperature sample pinned to the cable that saw it."""

    at: str
    cable_id: str
    temperature: float
    rated_temperature: float
    rating: str

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "cable_id": self.cable_id,
            "temperature": self.temperature,
            "rated_temperature": self.rated_temperature,
            "rating": self.rating,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "Exceedance | None":
        if not isinstance(payload, dict):
            return None
        at = payload.get("at")
        cable_id = payload.get("cable_id")
        temperature = payload.get("temperature")
        rated = payload.get("rated_temperature")
        rating = payload.get("rating")
        if not isinstance(at, str) or not at:
            return None
        if not isinstance(cable_id, str) or not cable_id:
            return None
        for value in (temperature, rated):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
        if not isinstance(rating, str) or not rating:
            return None
        return cls(
            at=at,
            cable_id=cable_id,
            temperature=float(temperature),
            rated_temperature=float(rated),
            rating=rating,
        )


class CableTracker:
    """Per-cable temperature accumulation driven by the lifecycle."""

    def __init__(
        self,
        lifecycle: CableLifecycle,
        window: int = 5,
        store: RecordStore | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._window = window
        self._store = store
        self._monitors: dict[str, TemperatureMonitor] = {}
        self._exceedances: list[Exceedance] = []
        if store is not None:
            payload = store.get_json(EXCEEDANCES_KEY)
            if isinstance(payload, list):
                for item in payload:
                    exceedance = Exceedance.from_dict(item)
                    if exceedance is not None:
                        self._exceedances.append(exceedance)

    def observe(self, sample: float, at: str | None = None) -> tuple[ServiceSpan, Rating]:
        """Attribute one sample to the cable currently in service."""

        span = self._lifecycle.require_current()
        monitor = self._monitors.get(span.cable_id)
        if monitor is None:
            monitor = TemperatureMonitor(self._window)
            self._monitors[span.cable_id] = monitor
        monitor.observe(sample)
        rating = classify(sample, span.rated_temperature)
        if rating.is_alarm:
            moment = normalize_timestamp(at) if at is not None else utc_now()
            self._record(
                Exceedance(
                    at=moment,
                    cable_id=span.cable_id,
                    temperature=float(sample),
                    rated_temperature=span.rated_temperature,
                    rating=rating.value,
                )
            )
        return span, rating

    def summary_for(self, cable_id: str) -> dict[str, float] | None:
        monitor = self._monitors.get(cable_id)
        return monitor.summary() if monitor is not None else None

    def summaries(self) -> dict[str, dict[str, float]]:
        return {
            cable_id: monitor.summary()
            for cable_id, monitor in sorted(self._monitors.items())
        }

    def exceedances(self) -> list[Exceedance]:
        return list(self._exceedances)

    def exceedances_for(self, cable_id: str) -> list[Exceedance]:
        return [item for item in self._exceedances if item.cable_id == cable_id]

    def _record(self, exceedance: Exceedance) -> None:
        self._exceedances.append(exceedance)
        if self._store is not None:
            self._store.put_json(
                EXCEEDANCES_KEY, [item.as_dict() for item in self._exceedances]
            )
