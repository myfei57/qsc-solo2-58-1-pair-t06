"""Asset lifecycle: which cable is in service at any moment.

Every replacement is registered with the moment it takes effect.  All
validation runs before any state changes, so a failed or duplicated
registration never moves temperature attribution onto the wrong asset.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone

from portpower.store.kvstore import RecordStore

LIFECYCLE_KEY = "cable.lifecycle"


class ReplacementError(ValueError):
    """Raised when a cable replacement cannot be registered."""


class DuplicateReplacement(ReplacementError):
    """Raised when the same cable is registered while already known."""


class DerateNotAcknowledged(ReplacementError):
    """Raised when a lower-rated substitute is not explicitly derated."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_timestamp(moment: str) -> str:
    """Canonicalize an ISO-8601 string so timestamps compare as text."""

    if not isinstance(moment, str) or not moment.strip():
        raise ValueError("timestamp must be a non-empty ISO string")
    text = moment.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {moment!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class ServiceSpan:
    """One cable's unbroken period in service."""

    cable_id: str
    rated_temperature: float
    effective_at: str
    ended_at: str | None
    derated: bool
    derated_from: float | None

    @property
    def active(self) -> bool:
        return self.ended_at is None

    def covers(self, moment: str) -> bool:
        if moment < self.effective_at:
            return False
        return self.ended_at is None or moment < self.ended_at

    def as_dict(self) -> dict[str, object]:
        return {
            "cable_id": self.cable_id,
            "rated_temperature": self.rated_temperature,
            "effective_at": self.effective_at,
            "ended_at": self.ended_at,
            "active": self.active,
            "derated": self.derated,
            "derated_from": self.derated_from,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "ServiceSpan | None":
        if not isinstance(payload, dict):
            return None
        cable_id = payload.get("cable_id")
        rated = payload.get("rated_temperature")
        effective = payload.get("effective_at")
        ended = payload.get("ended_at")
        if not isinstance(cable_id, str) or not cable_id:
            return None
        if isinstance(rated, bool) or not isinstance(rated, (int, float)):
            return None
        if not isinstance(effective, str) or not effective:
            return None
        if ended is not None and not isinstance(ended, str):
            return None
        derated_from = payload.get("derated_from")
        if isinstance(derated_from, bool) or not isinstance(
            derated_from, (int, float, type(None))
        ):
            return None
        return cls(
            cable_id=cable_id,
            rated_temperature=float(rated),
            effective_at=effective,
            ended_at=ended,
            derated=bool(payload.get("derated", False)),
            derated_from=None if derated_from is None else float(derated_from),
        )


def derate_warning(span: ServiceSpan) -> str | None:
    """Human-readable reminder shown while a derated substitute serves."""

    if not span.derated:
        return None
    return (
        f"cable {span.cable_id!r} is a derated substitute: "
        f"rated {span.rated_temperature} below the previous {span.derated_from}"
    )


class CableLifecycle:
    """Ordered registry of which cable served when."""

    def __init__(self, store: RecordStore | None = None) -> None:
        self._store = store
        self._spans: list[ServiceSpan] = []
        if store is not None:
            payload = store.get_json(LIFECYCLE_KEY)
            if isinstance(payload, list):
                for item in payload:
                    span = ServiceSpan.from_dict(item)
                    if span is not None:
                        self._spans.append(span)

    def replace(
        self,
        cable_id: str,
        rated_temperature: float,
        effective_at: str | None = None,
        derated: bool = False,
    ) -> ServiceSpan:
        """Register a replacement.  Nothing mutates unless every check passes."""

        if not isinstance(cable_id, str) or not cable_id.strip():
            raise ReplacementError("cable id is required")
        cable_id = cable_id.strip()
        if isinstance(rated_temperature, bool) or not isinstance(
            rated_temperature, (int, float)
        ):
            raise ReplacementError("rated temperature must be a number")
        rated = float(rated_temperature)
        if rated <= 0:
            raise ReplacementError("rated temperature must be positive")
        moment = (
            normalize_timestamp(effective_at) if effective_at is not None else utc_now()
        )

        head = self._spans[-1] if self._spans else None
        if head is not None and head.cable_id == cable_id:
            raise DuplicateReplacement(f"cable {cable_id!r} is already in service")
        if any(span.cable_id == cable_id for span in self._spans):
            raise DuplicateReplacement(f"cable {cable_id!r} was already retired")
        if head is not None and moment <= head.effective_at:
            raise ReplacementError(
                f"effective_at {moment!r} must be after {head.effective_at!r}"
            )
        is_derate = head is not None and rated < head.rated_temperature
        if is_derate and not derated:
            raise DerateNotAcknowledged(
                f"cable {cable_id!r} is rated {rated} below the in-service "
                f"{head.rated_temperature}; pass derated=true to acknowledge"
            )

        span = ServiceSpan(
            cable_id=cable_id,
            rated_temperature=rated,
            effective_at=moment,
            ended_at=None,
            derated=is_derate,
            derated_from=head.rated_temperature if is_derate else None,
        )
        if head is not None:
            self._spans[-1] = dataclasses.replace(head, ended_at=moment)
        self._spans.append(span)
        self._persist()
        return span

    def current(self) -> ServiceSpan | None:
        """The span whose effective window covers right now, if any."""

        return self.at(utc_now())

    def require_current(self) -> ServiceSpan:
        span = self.current()
        if span is None:
            raise LookupError("no cable in service")
        return span

    def at(self, moment: str) -> ServiceSpan | None:
        """Which cable was in service at a past or present moment."""

        instant = normalize_timestamp(moment)
        for span in reversed(self._spans):
            if span.covers(instant):
                return span
        return None

    def spans(self) -> list[ServiceSpan]:
        return list(self._spans)

    def _persist(self) -> None:
        if self._store is not None:
            self._store.put_json(
                LIFECYCLE_KEY, [span.as_dict() for span in self._spans]
            )
