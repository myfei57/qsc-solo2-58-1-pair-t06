"""Cable inventory, lifecycle and thermal monitoring endpoints."""

from __future__ import annotations

from portpower.cable.rating import classify
from portpower.console.http import BadRequest, Request, Response
from portpower.console.validation import (
    normalize,
    optional_text,
    require_number,
    require_text,
    valid_temperature,
)
from portpower.disconn.trip import trip_breaker


def cables(runtime, request: Request) -> Response:
    lifecycle = runtime.cable_lifecycle
    identifiers = runtime.cables.identifiers()
    selected = runtime.cables.select(lifecycle.rated_temperature)
    detail = {}
    for identifier in identifiers:
        cable = runtime.cables.get(identifier)
        if cable is not None:
            detail[identifier] = cable.describe()
    return Response.json(
        200,
        {
            "count": runtime.cables.count(),
            "ids": identifiers,
            "ratings": runtime.cables.ratings(),
            "detail": detail,
            "selected": selected.identifier if selected is not None else None,
            "current": lifecycle.describe_current(),
            "assets": lifecycle.all_stats(),
            "installations": [
                item.as_dict() for item in lifecycle.installations()
            ],
        },
    )


def cable_monitor(runtime, request: Request) -> Response:
    lifecycle = runtime.cable_lifecycle
    cable = lifecycle.current
    temperature = runtime.sensor.read()
    return Response.json(
        200,
        {
            "cable_id": cable.identifier,
            "installation": lifecycle.current_installation.as_dict(),
            "degraded": lifecycle.is_degraded(cable),
            "warning": lifecycle.warning_for(cable),
            "monitor": lifecycle.stats(cable.identifier),
            "filter_average": runtime.filter.average,
            "protection": {
                "threshold": runtime.protector.threshold,
                "tripped": runtime.protector.tripped,
                "breaches": runtime.protector.breaches,
            },
            "temperature": temperature,
            "rating": classify(temperature, cable.rated_temperature).value,
        },
    )


def cable_sample(runtime, request: Request) -> Response:
    payload = request.json_object()
    temperature = require_number(payload, "temperature")
    if not valid_temperature(temperature):
        raise BadRequest("invalid temperature")
    at = optional_text(payload, "at") or None
    runtime.sensor.set(temperature)
    observation = runtime.cable_lifecycle.observe(temperature, at)
    runtime.protector.assess(observation.filtered)
    breach = observation.breach
    if breach:
        trip_breaker(runtime.shore)
    return Response.json(
        200,
        {
            "at": observation.at,
            "cable_id": observation.cable.identifier,
            "temperature": temperature,
            "filtered": observation.filtered,
            "average": observation.filtered,
            "rating": observation.rating.value,
            "over_limit": observation.over_limit,
            "breach": breach,
            "degraded": observation.degraded,
            "warning": observation.warning,
            "breaker_state": runtime.shore.state.value,
        },
    )


def cable_replace(runtime, request: Request) -> Response:
    payload = request.json_object()
    cable_id = (
        require_text(payload, "cable_id")
        if "cable_id" in payload
        else require_text(payload, "id")
    )
    rated_temperature = (
        require_number(payload, "rated_temperature")
        if "rated_temperature" in payload
        else require_number(payload, "rating")
    )
    if rated_temperature <= 0:
        raise BadRequest("rated temperature must be positive")
    effective_at = optional_text(payload, "effective_at") or None
    if payload.get("installed_at") is not None:
        effective_at = optional_text(payload, "installed_at")
    if payload.get("at") is not None:
        effective_at = optional_text(payload, "at")

    installation = runtime.cable_lifecycle.replace(
        cable_id, rated_temperature, effective_at
    )
    runtime.protector.reset()
    cable = runtime.cable_lifecycle.current
    degraded = runtime.cable_lifecycle.is_degraded(cable)
    warning = runtime.cable_lifecycle.warning_for(cable)
    runtime.ledger.append(
        "cable_replace",
        f"{cable.identifier} effective {installation.installed_at} rated "
        f"{cable.rated_temperature}",
    )
    return Response.json(
        200,
        {
            "replaced": True,
            "cable": cable.describe(),
            "installation": installation.as_dict(),
            "degraded": degraded,
            "warning": warning,
            "monitor": runtime.cable_lifecycle.stats(cable.identifier),
        },
    )


def cable_history(runtime, request: Request) -> Response:
    cable_id = request.query_one("cable")
    at = request.query_one("at")
    start = request.query_one("start")
    end = request.query_one("end")
    lifecycle = runtime.cable_lifecycle

    resolved_cable = cable_id
    installation = None
    if at is not None:
        installation = lifecycle.installation_at(at)
        if installation is None:
            raise BadRequest("no cable installed at requested time")
        resolved_cable = installation.cable_id

    events = lifecycle.events(resolved_cable, start, end)
    return Response.json(
        200,
        {
            "at": at,
            "cable_id": resolved_cable,
            "installation_at": (
                installation.as_dict() if installation is not None else None
            ),
            "installations": [
                item.as_dict()
                for item in lifecycle.installations(resolved_cable)
            ],
            "events": [event.as_dict() for event in events],
            "stats": lifecycle.stats(resolved_cable) if resolved_cable else None,
            "all_stats": lifecycle.all_stats(),
        },
    )


def cable_asset(runtime, request: Request) -> Response:
    cable_id = normalize(request.query_one("cable", "") or "")
    if not cable_id:
        raise BadRequest("cable is required")
    cable = runtime.cables.get(cable_id)
    if cable is None:
        raise LookupError(f"unknown cable {cable_id!r}")
    return Response.json(
        200,
        {
            "cable": cable.describe(),
            "installations": [
                item.as_dict() for item in runtime.cable_lifecycle.installations(cable_id)
            ],
            "events": [
                event.as_dict() for event in runtime.cable_lifecycle.events(cable_id)
            ],
            "monitor": runtime.cable_lifecycle.stats(cable_id),
        },
    )
