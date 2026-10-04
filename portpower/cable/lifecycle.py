"""Asset lifecycle and per-cable temperature history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import RLock

from portpower.cable.cable import Cable
from portpower.cable.inventory import CableInventory
from portpower.cable.monitor import TemperatureMonitor
from portpower.cable.rating import Rating, classify
from portpower.store.kvstore import RecordStore

LIFECYCLE_KEY = "cable.lifecycle"
LIFECYCLE_VERSION = 1


class DuplicateCableRegistration(ValueError):
    """Raised when a replacement registration is already present."""


class CableReplacementFailed(ValueError):
    """Raised when a cable cannot become the active asset."""


@dataclass(frozen=True)
class CableInstallation:
    sequence: int
    cable_id: str
    rated_temperature: float
    installed_at: str
    replaced_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "CableInstallation":
        if not isinstance(payload, dict):
            raise ValueError("installation must be an object")
        sequence = payload.get("sequence")
        cable_id = payload.get("cable_id")
        rated_temperature = payload.get("rated_temperature")
        installed_at = payload.get("installed_at")
        replaced_at = payload.get("replaced_at")
        if not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("invalid installation sequence")
        if not isinstance(cable_id, str) or not cable_id.strip():
            raise ValueError("invalid cable id")
        if not isinstance(rated_temperature, (int, float)) or rated_temperature <= 0:
            raise ValueError("invalid rated temperature")
        if not isinstance(installed_at, str) or not installed_at:
            raise ValueError("invalid installation time")
        if replaced_at is not None and (
            not isinstance(replaced_at, str) or not replaced_at
        ):
            raise ValueError("invalid replacement time")
        return cls(
            sequence=sequence,
            cable_id=cable_id.strip(),
            rated_temperature=float(rated_temperature),
            installed_at=installed_at,
            replaced_at=replaced_at,
        )


@dataclass(frozen=True)
class ThermalEvent:
    at: str
    cable_id: str
    kind: str
    temperature: float
    filtered: float
    rated_temperature: float
    rating: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "ThermalEvent":
        if not isinstance(payload, dict):
            raise ValueError("thermal event must be an object")
        values = {}
        for key in ("at", "cable_id", "kind", "rating"):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"invalid {key}")
            values[key] = value
        for key in ("temperature", "filtered", "rated_temperature"):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"invalid {key}")
            values[key] = float(value)
        return cls(**values)


@dataclass(frozen=True)
class ThermalObservation:
    at: str
    cable: Cable
    temperature: float
    filtered: float
    rating: Rating
    over_limit: bool
    breach: bool
    degraded: bool
    warning: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "cable": self.cable.describe(),
            "temperature": self.temperature,
            "filtered": self.filtered,
            "rating": self.rating.value,
            "over_limit": self.over_limit,
            "breach": self.breach,
            "degraded": self.degraded,
            "warning": self.warning,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is required")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class CableLifecycle:
    def __init__(
        self,
        inventory: CableInventory,
        initial_cable_id: str,
        initial_rated_temperature: float,
        store: RecordStore | None = None,
        window: int = 3,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._inventory = inventory
        self._store = store
        self._window = window
        self._clock = clock
        self._lock = RLock()
        self._installations: list[CableInstallation] = []
        self._monitors: dict[str, TemperatureMonitor] = {}
        self._events: list[ThermalEvent] = []

        document = self._store.get_json(LIFECYCLE_KEY) if store else None
        if document is None:
            self._baseline = float(initial_rated_temperature)
            self._install(
                initial_cable_id,
                initial_rated_temperature,
                clock(),
                initial=True,
            )
        else:
            self._load(document)

    @property
    def baseline_rated_temperature(self) -> float:
        return self._baseline

    @property
    def current(self) -> Cable:
        with self._lock:
            return self._inventory.get(self.current_installation.cable_id)  # type: ignore[return-value]

    @property
    def current_installation(self) -> CableInstallation:
        with self._lock:
            return self._installations[-1]

    @property
    def current_monitor(self) -> TemperatureMonitor:
        with self._lock:
            return self._monitors[self.current_installation.cable_id]

    @property
    def active_filter(self):
        return self.current_monitor.filter_window

    @property
    def rated_temperature(self) -> float:
        return self.current.rated_temperature

    def is_degraded(self, cable: Cable | None = None) -> bool:
        active = cable or self.current
        return active.rated_temperature < self._baseline

    def warning_for(self, cable: Cable | None = None) -> str | None:
        active = cable or self.current
        if active.rated_temperature < self._baseline:
            return (
                f"cable {active.identifier} is rated {active.rated_temperature}; "
                f"explicit downgrade from baseline {self._baseline}"
            )
        return None

    def install(
        self,
        cable_id: str,
        rated_temperature: float,
        effective_at: str | None = None,
    ) -> CableInstallation:
        timestamp = (
            self._clock() if effective_at is None else normalize_timestamp(effective_at)
        )
        return self._install(cable_id, rated_temperature, timestamp)

    def register_replacement(
        self,
        cable_id: str,
        rated_temperature: float,
        effective_at: str | None = None,
    ) -> CableInstallation:
        return self.install(cable_id, rated_temperature, effective_at)

    def replace(
        self,
        cable_id: str,
        rated_temperature: float,
        effective_at: str | None = None,
    ) -> CableInstallation:
        """Readable alias used by the replacement HTTP flow."""

        return self.install(cable_id, rated_temperature, effective_at)

    def observe(
        self,
        temperature: float,
        at: str | None = None,
    ) -> ThermalObservation:
        observed_at = self._clock() if at is None else normalize_timestamp(at)
        with self._lock:
            installation = self.current_installation
            if observed_at < installation.installed_at:
                raise CableReplacementFailed(
                    "sample time cannot precede the active cable installation"
                )
            cable = self.current
            monitor = self.current_monitor
            filtered = monitor.observe(temperature, observed_at)
            rating = classify(temperature, cable.rated_temperature)
            over_limit = rating.is_alarm
            breach = filtered > cable.rated_temperature
            if over_limit:
                self._events.append(
                    ThermalEvent(
                        at=observed_at,
                        cable_id=cable.identifier,
                        kind="over_limit",
                        temperature=float(temperature),
                        filtered=float(filtered),
                        rated_temperature=cable.rated_temperature,
                        rating=rating.value,
                    )
                )
            if breach:
                self._events.append(
                    ThermalEvent(
                        at=observed_at,
                        cable_id=cable.identifier,
                        kind="breach",
                        temperature=float(temperature),
                        filtered=float(filtered),
                        rated_temperature=cable.rated_temperature,
                        rating=rating.value,
                    )
                )
            self._save()
            return ThermalObservation(
                at=observed_at,
                cable=cable,
                temperature=float(temperature),
                filtered=float(filtered),
                rating=rating,
                over_limit=over_limit,
                breach=breach,
                degraded=self.is_degraded(cable),
                warning=self.warning_for(cable),
            )

    def reset_protection(self) -> None:
        with self._lock:
            self.current_monitor.reset_filter()

    def installations(self, cable_id: str | None = None) -> list[CableInstallation]:
        with self._lock:
            installations = list(self._installations)
        if cable_id is not None:
            installations = [
                installation
                for installation in installations
                if installation.cable_id == cable_id
            ]
        return installations

    def installation_at(self, at: str) -> CableInstallation | None:
        moment = normalize_timestamp(at)
        with self._lock:
            active: CableInstallation | None = None
            for installation in self._installations:
                if installation.installed_at <= moment:
                    active = installation
                else:
                    break
        return active

    def cable_at(self, at: str) -> Cable | None:
        installation = self.installation_at(at)
        if installation is None:
            return None
        return self._inventory.get(installation.cable_id)

    def events(
        self,
        cable_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[ThermalEvent]:
        start_at = None if start is None else normalize_timestamp(start)
        end_at = None if end is None else normalize_timestamp(end)
        with self._lock:
            events = list(self._events)
        result = []
        for event in events:
            if cable_id is not None and event.cable_id != cable_id:
                continue
            if start_at is not None and event.at < start_at:
                continue
            if end_at is not None and event.at > end_at:
                continue
            result.append(event)
        return result

    def stats(self, cable_id: str | None = None) -> dict[str, object]:
        target = cable_id or self.current_installation.cable_id
        with self._lock:
            monitor = self._monitors.get(target)
            if monitor is None:
                raise LookupError(f"unknown cable {target!r}")
            return {
                "cable_id": target,
                "rated_temperature": self._rated_temperature(target),
                **monitor.summary(),
            }

    def all_stats(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                cable_id: {
                    "cable_id": cable_id,
                    "rated_temperature": self._rated_temperature(cable_id),
                    **monitor.summary(),
                }
                for cable_id, monitor in self._monitors.items()
            }

    def describe_current(self) -> dict[str, object]:
        cable = self.current
        return {
            "cable": cable.describe(),
            "installation": self.current_installation.as_dict(),
            "baseline_rated_temperature": self._baseline,
            "degraded": self.is_degraded(cable),
            "warning": self.warning_for(cable),
            "monitor": self.stats(cable.identifier),
        }

    def _install(
        self,
        cable_id: str,
        rated_temperature: float,
        effective_at: str,
        initial: bool = False,
    ) -> CableInstallation:
        identifier = self._validate_cable_id(cable_id)
        rating = float(rated_temperature)
        if rating <= 0:
            raise ValueError("rated temperature must be positive")
        effective_at = normalize_timestamp(effective_at)

        with self._lock:
            if not self._installations:
                if not initial:
                    raise CableReplacementFailed("no initial cable is registered")
                self._baseline = rating
                installation = CableInstallation(
                    sequence=1,
                    cable_id=identifier,
                    rated_temperature=rating,
                    installed_at=effective_at,
                )
            else:
                self._validate_replacement(identifier, rating, effective_at)

                def mutate(document: object) -> tuple[object, None]:
                    installations, events, stats, _baseline = self._decode_document(
                        document
                    )
                    current = installations[-1]
                    self._validate_candidate(
                        installations,
                        CableInstallation(
                            sequence=current.sequence + 1,
                            cable_id=identifier,
                            rated_temperature=rating,
                            installed_at=effective_at,
                        ),
                    )
                    installations[-1] = CableInstallation(
                        sequence=current.sequence,
                        cable_id=current.cable_id,
                        rated_temperature=current.rated_temperature,
                        installed_at=current.installed_at,
                        replaced_at=effective_at,
                    )
                    installations.append(
                        CableInstallation(
                            sequence=current.sequence + 1,
                            cable_id=identifier,
                            rated_temperature=rating,
                            installed_at=effective_at,
                        )
                    )
                    return (
                        self._encode_document(installations, events, stats),
                        None,
                    )

                if self._store is not None:
                    self._store.update_json(LIFECYCLE_KEY, mutate)

                previous = self._installations[-1]
                self._installations[-1] = CableInstallation(
                    sequence=previous.sequence,
                    cable_id=previous.cable_id,
                    rated_temperature=previous.rated_temperature,
                    installed_at=previous.installed_at,
                    replaced_at=effective_at,
                )
                installation = CableInstallation(
                    sequence=previous.sequence + 1,
                    cable_id=identifier,
                    rated_temperature=rating,
                    installed_at=effective_at,
                )

            self._inventory.add(identifier, rating)
            self._installations.append(installation)
            monitor = self._monitors.setdefault(
                identifier, TemperatureMonitor(self._window)
            )
            if not initial and installation.sequence > 1:
                monitor.reset_filter()
            self._save()
            return installation

    def _validate_replacement(
        self, cable_id: str, rated_temperature: float, effective_at: str
    ) -> None:
        current = self._installations[-1]
        if effective_at <= current.installed_at:
            raise CableReplacementFailed(
                "effective time must be after the current installation"
            )
        if any(item.installed_at == effective_at for item in self._installations):
            raise DuplicateCableRegistration("duplicate cable registration time")
        existing = self._inventory.get(cable_id)
        if existing is not None and (
            existing.rated_temperature != rated_temperature
        ):
            raise DuplicateCableRegistration(
                f"cable {cable_id!r} is already registered with a different rating"
            )
        if current.cable_id == cable_id:
            raise DuplicateCableRegistration(f"cable {cable_id!r} is already active")

    def _validate_candidate(
        self,
        installations: list[CableInstallation],
        candidate: CableInstallation,
    ) -> None:
        current = installations[-1]
        if candidate.installed_at <= current.installed_at:
            raise CableReplacementFailed(
                "effective time must be after the current installation"
            )
        if any(item.installed_at == candidate.installed_at for item in installations):
            raise DuplicateCableRegistration("duplicate cable registration time")
        if candidate.cable_id == current.cable_id:
            raise DuplicateCableRegistration(
                f"cable {candidate.cable_id!r} is already active"
            )
        existing = self._inventory.get(candidate.cable_id)
        if (
            existing is not None
            and existing.rated_temperature != candidate.rated_temperature
        ):
            raise DuplicateCableRegistration(
                f"cable {candidate.cable_id!r} is already registered with a different rating"
            )

    def _load(self, document: object) -> None:
        installations, events, stats, baseline = self._decode_document(document)
        self._baseline = baseline
        for installation in installations:
            self._inventory.add(
                installation.cable_id, installation.rated_temperature
            )
            cable_stats = stats.get(installation.cable_id, {})
            monitor = TemperatureMonitor.from_stats(
                self._window,
                count=self._number(cable_stats, "count", 0),
                total=self._number(cable_stats, "total", 0.0),
                peak=self._optional_number(cable_stats, "peak"),
                peak_at=cable_stats.get("peak_at"),
                last=self._optional_number(cable_stats, "last"),
            )
            self._monitors[installation.cable_id] = monitor
        self._installations = installations
        self._events = events

    @staticmethod
    def _number(payload: dict[str, object], key: str, default: float) -> float:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"invalid {key}")
        return float(value)

    @staticmethod
    def _optional_number(
        payload: dict[str, object], key: str
    ) -> float | None:
        if key not in payload or payload[key] is None:
            return None
        return CableLifecycle._number(payload, key, 0.0)

    def _decode_document(
        self, document: object
    ) -> tuple[
        list[CableInstallation], list[ThermalEvent], dict[str, dict[str, object]], float
    ]:
        if not isinstance(document, dict):
            raise ValueError("invalid cable lifecycle document")
        if document.get("version") != LIFECYCLE_VERSION:
            raise ValueError("unknown cable lifecycle version")
        baseline = document.get("baseline_rated_temperature")
        if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
            raise ValueError("invalid baseline rated temperature")
        raw_installations = document.get("installations")
        raw_events = document.get("events", [])
        raw_stats = document.get("stats", {})
        if not isinstance(raw_installations, list) or not raw_installations:
            raise ValueError("cable lifecycle has no installations")
        if not isinstance(raw_events, list) or not isinstance(raw_stats, dict):
            raise ValueError("invalid cable lifecycle history")
        installations = [
            CableInstallation.from_dict(item) for item in raw_installations
        ]
        installations.sort(key=lambda item: item.sequence)
        if [item.sequence for item in installations] != list(
            range(1, len(installations) + 1)
        ):
            raise ValueError("installation sequences must be contiguous")
        if installations[0].replaced_at is not None and len(installations) == 1:
            raise ValueError("first installation cannot already be replaced")
        for previous, current in zip(installations, installations[1:]):
            if previous.replaced_at != current.installed_at:
                raise ValueError("installation boundary is inconsistent")
            if current.installed_at <= previous.installed_at:
                raise ValueError("installation times must be ordered")
        if installations[-1].replaced_at is not None:
            raise ValueError("active installation cannot already be replaced")
        events = [ThermalEvent.from_dict(item) for item in raw_events]
        events.sort(key=lambda item: (item.at, item.cable_id, item.kind))
        stats: dict[str, dict[str, object]] = {}
        for cable_id, payload in raw_stats.items():
            if not isinstance(cable_id, str) or not isinstance(payload, dict):
                raise ValueError("invalid cable statistics")
            stats[cable_id] = payload
        return installations, events, stats, float(baseline)

    def _encode_document(
        self,
        installations: list[CableInstallation] | None = None,
        events: list[ThermalEvent] | None = None,
        stats: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, object]:
        installations = installations or self._installations
        events = events if events is not None else self._events
        stats = stats if stats is not None else {
            cable_id: monitor.stats()
            for cable_id, monitor in self._monitors.items()
        }
        return {
            "version": LIFECYCLE_VERSION,
            "baseline_rated_temperature": self._baseline,
            "installations": [item.as_dict() for item in installations],
            "stats": stats,
            "events": [event.as_dict() for event in events],
        }

    def _save(self) -> None:
        if self._store is not None:
            self._store.put_json(LIFECYCLE_KEY, self._encode_document())

    def _rated_temperature(self, cable_id: str) -> float:
        for installation in reversed(self._installations):
            if installation.cable_id == cable_id:
                return installation.rated_temperature
        raise LookupError(f"unknown cable {cable_id!r}")

    @staticmethod
    def _validate_cable_id(cable_id: str) -> str:
        if not isinstance(cable_id, str) or not cable_id.strip():
            raise ValueError("cable id is required")
        return cable_id.strip()
