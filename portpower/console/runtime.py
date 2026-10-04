"""Composition root that wires every domain package together."""

from __future__ import annotations

from dataclasses import dataclass

from portpower.audit.ledger import Ledger
from portpower.berth.mapping import BerthMapping
from portpower.berth.registry import BerthRegistry
from portpower.berth.schedule import Scheduler
from portpower.cable.cable import ActiveCable, Cable
from portpower.cable.filter import ActiveAverageFilter, MovingAverageFilter
from portpower.cable.inventory import CableInventory
from portpower.cable.lifecycle import CableLifecycle
from portpower.cable.monitor import ActiveTemperatureMonitor, TemperatureMonitor
from portpower.cable.sensor import TemperatureSensor
from portpower.disconn.breaker import Breaker
from portpower.disconn.limit import LimitEnforcer
from portpower.disconn.protect import Protector
from portpower.freq.syncer import Syncer
from portpower.meter.binder import Binder
from portpower.meter.tariff import Tariff
from portpower.ns.catalog import Catalog
from portpower.ns.harbor import Harbor
from portpower.phase.state import PhaseState
from portpower.quota.manager import QuotaManager
from portpower.ship.fleet import Fleet
from portpower.ship.history import ProfileHistory
from portpower.ship.registry import ProfileRegistry
from portpower.store.kvstore import RecordStore

CATALOG_CODES: tuple[str, ...] = ("A1", "A2", "B1", "B2", "C1")
SHORE_CABLE = ("shore-1", 90.0)
PROTECTION_THRESHOLD = 90.0
FILTER_WINDOW = 3
MONITOR_WINDOW = 5
TARIFF_RATE = 0.95
SHORE_BREAKER_ID = "shore-main"


@dataclass
class Runtime:
    """All long-lived collaborators for one server process."""

    harbor: Harbor
    catalog: Catalog
    store: RecordStore
    ledger: Ledger
    quotas: QuotaManager
    profiles: ProfileRegistry
    fleet: Fleet
    mapping: BerthMapping
    berths: BerthRegistry
    binder: Binder
    syncer: Syncer
    sensor: TemperatureSensor
    filter: MovingAverageFilter | ActiveAverageFilter
    cable: Cable
    cables: CableInventory
    cable_lifecycle: CableLifecycle
    tariff: Tariff
    monitor: TemperatureMonitor | ActiveTemperatureMonitor
    scheduler: Scheduler
    history: ProfileHistory
    phase: PhaseState
    shore: Breaker
    protector: Protector
    limiter: LimitEnforcer


def build_runtime(harbor: Harbor, store: RecordStore) -> Runtime:
    catalog = Catalog(CATALOG_CODES)
    berths = BerthRegistry()
    for code in catalog.codes():
        berths.add(code)

    cables = CableInventory()
    cable_id, cable_rating = SHORE_CABLE
    cable_lifecycle = CableLifecycle(
        cables,
        cable_id,
        cable_rating,
        store=store,
        window=FILTER_WINDOW,
    )
    active_cable = ActiveCable(lambda: cable_lifecycle.current)
    active_filter = ActiveAverageFilter(lambda: cable_lifecycle.active_filter)
    active_monitor = ActiveTemperatureMonitor(
        lambda: cable_lifecycle.current_monitor
    )

    return Runtime(
        harbor=harbor,
        catalog=catalog,
        store=store,
        ledger=Ledger(store),
        quotas=QuotaManager(store),
        profiles=ProfileRegistry(store),
        fleet=Fleet(),
        mapping=BerthMapping(store),
        berths=berths,
        binder=Binder(store),
        syncer=Syncer(store),
        sensor=TemperatureSensor(),
        filter=active_filter,
        cable=active_cable,
        cables=cables,
        cable_lifecycle=cable_lifecycle,
        tariff=Tariff(TARIFF_RATE),
        monitor=active_monitor,
        scheduler=Scheduler(store),
        history=ProfileHistory(store),
        phase=PhaseState(),
        shore=Breaker(SHORE_BREAKER_ID),
        protector=Protector(lambda: cable_lifecycle.rated_temperature, active_filter),
        limiter=LimitEnforcer(ProfileRegistry(store)),
    )
