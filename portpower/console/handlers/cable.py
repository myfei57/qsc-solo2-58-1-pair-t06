"""Cable inventory, lifecycle and thermal monitoring endpoints."""

from __future__ import annotations

from portpower.cable.lifecycle import derate_warning, normalize_timestamp
from portpower.cable.rating import classify
from portpower.console.http import BadRequest, Request, Response
from portpower.console.validation import (
    optional_bool,
    optional_number,
    optional_text,
    require_number,
    require_text,
    valid_temperature,
)
from portpower.disconn.trip import trip_breaker


def cables(runtime, request: Request) -> Response:
    span = runtime.lifecycle.current()
    identifiers = runtime.cables.identifiers()
    selected = (
        runtime.cables.select(span.rated_temperature) if span is not None else None
    )
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
            "in_service": span.cable_id if span is not None else None,
            "selected": selected.identifier if selected is not None else None,
        },
    )


def cable_current(runtime, request: Request) -> Response:
    span = runtime.lifecycle.require_current()
    temperature = runtime.sensor.read()
    return Response.json(
        200,
        {
            "span": span.as_dict(),
            "monitor": runtime.tracker.summary_for(span.cable_id),
            "temperature": temperature,
            "rating": classify(temperature, span.rated_temperature).value,
            "warning": derate_warning(span),
        },
    )


def cable_history(runtime, request: Request) -> Response:
    spans = runtime.lifecycle.spans()
    current = runtime.lifecycle.current()
    return Response.json(
        200,
        {
            "count": len(spans),
            "current": current.cable_id if current is not None else None,
            "spans": [span.as_dict() for span in spans],
        },
    )


def cable_replace(runtime, request: Request) -> Response:
    payload = request.json_object()
    cable_id = require_text(payload, "cable_id")
    existing = runtime.cables.get(cable_id)
    if existing is None:
        rated = require_number(payload, "rated_temperature")
    else:
        rated = optional_number(
            payload, "rated_temperature", existing.rated_temperature
        )
        if rated != existing.rated_temperature:
            raise BadRequest(
                f"rated_temperature {rated} does not match inventory "
                f"{existing.rated_temperature} for cable {cable_id!r}"
            )
    effective_at = optional_text(payload, "effective_at", "") or None
    derated = optional_bool(payload, "derated")

    span = runtime.lifecycle.replace(
        cable_id, rated, effective_at=effective_at, derated=derated
    )
    if existing is None:
        runtime.cables.add(cable_id, rated)
    runtime.ledger.append(
        "cable.replace",
        f"{cable_id} rated {span.rated_temperature} effective {span.effective_at}"
        + (" derated" if span.derated else ""),
    )
    spans = runtime.lifecycle.spans()
    closed = spans[-2] if len(spans) >= 2 else None
    return Response.json(
        200,
        {
            "replaced": True,
            "previous": closed.as_dict() if closed is not None else None,
            "span": span.as_dict(),
            "warning": derate_warning(span),
        },
    )


def cable_attribution(runtime, request: Request) -> Response:
    moment = request.query_one("at", "") or ""
    if not moment.strip():
        raise BadRequest("at is required")
    span = runtime.lifecycle.at(moment)
    if span is None:
        raise LookupError(f"no cable in service at {moment!r}")
    return Response.json(
        200,
        {
            "at": normalize_timestamp(moment),
            "span": span.as_dict(),
            "monitor": runtime.tracker.summary_for(span.cable_id),
            "exceedances": [
                item.as_dict()
                for item in runtime.tracker.exceedances_for(span.cable_id)
            ],
        },
    )


def cable_exceedances(runtime, request: Request) -> Response:
    cable_id = request.query_one("cable", "") or ""
    if cable_id:
        items = runtime.tracker.exceedances_for(cable_id)
    else:
        items = runtime.tracker.exceedances()
    return Response.json(
        200,
        {
            "count": len(items),
            "exceedances": [item.as_dict() for item in items],
        },
    )


def cable_monitor(runtime, request: Request) -> Response:
    temperature = runtime.sensor.read()
    span = runtime.lifecycle.require_current()
    return Response.json(
        200,
        {
            "monitor": runtime.tracker.summary_for(span.cable_id),
            "monitors": runtime.tracker.summaries(),
            "current": span.as_dict(),
            "filter_average": runtime.filter.average,
            "protection": {
                "threshold": runtime.protector.threshold,
                "tripped": runtime.protector.tripped,
                "breaches": runtime.protector.breaches,
            },
            "temperature": temperature,
            "rating": classify(temperature, span.rated_temperature).value,
            "warning": derate_warning(span),
        },
    )


def cable_sample(runtime, request: Request) -> Response:
    payload = request.json_object()
    temperature = require_number(payload, "temperature")
    if not valid_temperature(temperature):
        raise BadRequest("invalid temperature")
    runtime.sensor.set(temperature)
    span, rating = runtime.tracker.observe(temperature)
    breach = runtime.protector.verdict(runtime.sensor.read())
    if breach:
        trip_breaker(runtime.shore)
    return Response.json(
        200,
        {
            "temperature": temperature,
            "cable_id": span.cable_id,
            "rated_temperature": span.rated_temperature,
            "derated": span.derated,
            "warning": derate_warning(span),
            "filtered": runtime.filter.average,
            "rating": rating.value,
            "breach": breach,
            "breaker_state": runtime.shore.state.value,
        },
    )
