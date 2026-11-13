"""SafeNisha 3D map: a Streamlit component that shows Bengaluru in 3D.

The map only *draws*. It never computes or guesses risk: routes, hazard
reports and risk values appear exactly as the app passes them in, and a
route without a ``risk_indicator`` is labelled "Data limited".

    from map3d import safenisha_map

    event = safenisha_map(
        start=(12.9346, 77.6113),
        destination=(12.9435, 77.6228),
        route_result=route_result,          # Member 1's shortest / lower_risk dict
        hazards=get_active_hazards(),       # Member 4's hazard dicts
        pick_mode="hazard",                 # what the next map click means
        key="map",
    )
    if event and event["type"] == "click":
        lat, lon = event["lat"], event["lon"]
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import streamlit as st
import streamlit.components.v1 as components

__all__ = ["safenisha_map", "routes_from_osrm", "routes_from_route_result"]

# Windows registries sometimes map .js to text/plain, which browsers refuse
# for module scripts. Pin the types the frontend needs.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")

_FRONTEND = Path(__file__).resolve().parent / "frontend"
_component = components.declare_component("safenisha_map3d", path=str(_FRONTEND))

_PICK_MODES = {None, "start", "destination", "hazard"}
_ROLE_LABEL = {
    "shortest": "Shortest route",
    "lower_risk": "Lower estimated risk",
    "alternative": "Alternative route",
    "preferred": "Preferred route",
}


def _point(p: Any, label: Optional[str] = None) -> Optional[dict]:
    """Accept (lat, lon), [lat, lon] or a dict with lat/lon or latitude/longitude."""
    if p is None:
        return None
    if isinstance(p, Mapping):
        lat = p.get("lat", p.get("latitude"))
        lon = p.get("lon", p.get("lng", p.get("longitude")))
        label = p.get("label", label)
    else:
        lat, lon = p[0], p[1]
    if lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"not a latitude/longitude pair: {p!r}")
    out = {"lat": lat, "lon": lon}
    if label:
        out["label"] = str(label)
    return out


def _coords(coords: Iterable[Sequence[float]]) -> list[list[float]]:
    return [[round(float(c[0]), 6), round(float(c[1]), 6)] for c in coords]


def _route(r: Mapping[str, Any], default_role: str, index: int) -> Optional[dict]:
    coords = r.get("coords") or r.get("coordinates") or r.get("path")
    if not coords or len(coords) < 2:
        return None
    role = r.get("role", default_role)
    risk = r.get("risk_indicator")
    return {
        "id": r.get("id", role if role else f"route-{index}"),
        "role": role,
        "label": r.get("label") or _ROLE_LABEL.get(role, "Route"),
        "coords": _coords(coords),
        "distance_m": None if r.get("distance_m") is None else float(r["distance_m"]),
        "duration_s": None if r.get("duration_s") is None else float(r["duration_s"]),
        # None stays None: the map then shows "Data limited" instead of a number
        "risk_indicator": None if risk is None else float(risk),
        **({"color": r["color"]} if r.get("color") else {}),
    }


def routes_from_route_result(route_result: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """Turn the team's ``route_result`` contract into map routes.

    Expects ``{"shortest": {...}, "lower_risk": {...}}`` where each entry has
    ``distance_m``, ``risk_indicator`` (or None) and ``coords`` as a list of
    ``(lat, lon)`` pairs. If both entries follow the same path, one line is drawn.
    """
    out = []
    lower = route_result.get("lower_risk")
    short = route_result.get("shortest")
    same = bool(lower and short and lower.get("coords") and _coords(lower["coords"]) == _coords(short.get("coords") or []))
    if lower:
        r = _route(lower, "lower_risk", 0)
        if r:
            if same:
                r["label"] = lower.get("label") or "Shortest and lower estimated risk"
            out.append(r)
    if short and not same:
        r = _route(short, "shortest", 1)
        if r:
            out.append(r)
    return out


def routes_from_osrm(response: Mapping[str, Any], max_routes: int = 2) -> list[dict]:
    """Turn a response from the public OSM foot router into map routes.

    Works with ``geometries=geojson``. The router knows nothing about risk, so
    every route comes back with ``risk_indicator=None``; attach your own values
    before passing them to :func:`safenisha_map` if you have them.
    """
    routes = sorted(response.get("routes") or [], key=lambda r: r.get("distance", 0))[:max_routes]
    out = []
    for i, r in enumerate(routes):
        role = "shortest" if i == 0 else "alternative"
        out.append({
            "id": role if i < 2 else f"alternative-{i}",
            "role": role,
            "label": _ROLE_LABEL[role],
            "coords": [[round(lat, 6), round(lon, 6)] for lon, lat in r["geometry"]["coordinates"]],
            "distance_m": float(r["distance"]),
            "duration_s": float(r["duration"]),
            "risk_indicator": None,
        })
    return out


def _hazard(h: Mapping[str, Any]) -> Optional[dict]:
    lat = h.get("latitude", h.get("lat"))
    lon = h.get("longitude", h.get("lon"))
    if lat is None or lon is None:
        return None
    return {
        "id": h.get("id"),
        "latitude": float(lat),
        "longitude": float(lon),
        "hazard_type": h.get("hazard_type", "other_infrastructure"),
        "description": (h.get("description") or "")[:280],
        "status": h.get("status", "new"),
    }


def safenisha_map(
    *,
    start: Any = None,
    destination: Any = None,
    routes: Optional[Sequence[Mapping[str, Any]]] = None,
    route_result: Optional[Mapping[str, Mapping[str, Any]]] = None,
    hazards: Optional[Iterable[Mapping[str, Any]]] = None,
    risk_cells: Optional[Iterable[Mapping[str, Any]]] = None,
    risk_synthetic: bool = False,
    user_location: Any = None,
    pick_mode: Optional[str] = None,
    selected_route: Optional[str] = None,
    fit: str = "auto",
    fly_to: Optional[Mapping[str, Any]] = None,
    hud: str = "minimal",
    show_route_card: bool = True,
    labels: Optional[bool] = None,
    lamps: Optional[bool] = None,
    legend: Optional[bool] = None,
    stats: Optional[bool] = None,
    quality: str = "auto",
    height: int = 640,
    only_new_events: bool = True,
    key: Optional[str] = None,
) -> Optional[dict]:
    """Render the 3D map and return the latest map event, if any.

    Parameters
    ----------
    start, destination
        ``(lat, lon)`` or a dict with ``lat``/``lon`` (optionally ``label``).
    routes
        List of dicts: ``coords`` as ``(lat, lon)`` pairs, plus optional ``id``,
        ``role`` (``"lower_risk"``, ``"shortest"``, ``"alternative"``), ``label``,
        ``distance_m``, ``duration_s``, ``risk_indicator`` (0..1 or None).
        The lower-risk / alternative route is drawn solid teal, the shortest
        dashed coral.
    route_result
        Shortcut for the team contract ``{"shortest": {...}, "lower_risk": {...}}``;
        each entry needs ``coords``. Ignored when ``routes`` is given.
    hazards
        Hazard dicts as returned by ``hazards.get_active_hazards()``.
        ``status == "new"`` is drawn as an unverified report, ``"confirmed"`` as
        a confirmed one, ``"resolved"`` is not drawn.
    risk_cells
        Optional overlay: dicts with ``lat``, ``lon``, ``value`` (0..1) and
        optional ``size_m``. Drawn as coral columns. Pass real estimates only,
        or set ``risk_synthetic=True`` so the map labels them as a synthetic demo.
    user_location
        ``(lat, lon)`` or dict (optionally ``accuracy_m``) for the walker's dot.
    pick_mode
        ``"start"``, ``"destination"``, ``"hazard"`` or None. Shows a prompt and
        tags the next click event with that mode. Clicks are reported either way.
    fit
        ``"auto"`` flies the camera to new routes/endpoints, ``"none"`` leaves it.
    fly_to
        One-shot camera move, e.g. ``{"lat": .., "lon": .., "distance": 900}``.
        Runs again only when the dict changes (add a ``"nonce"`` to repeat).
    hud
        ``"minimal"`` (default, for embedding next to Streamlit controls),
        ``"full"`` (title, search, fly-to chips) or ``"none"``.
    stats
        Show a live readout of frame rate, triangles drawn and tile processing time.
    only_new_events
        Streamlit re-sends the last component value on every rerun. With this
        on (default) an event is returned once and ``None`` afterwards.

    Returns
    -------
    dict or None
        ``{"type": "click", "lat", "lon", "mode", "inside", "seq", "ts"}``,
        ``{"type": "hazard_click", "id", ...}`` or
        ``{"type": "route_select", "id", ...}`` or, after a benchmark run from the
        map's About panel, ``{"type": "benchmark", "result": {...}}``.
    """
    if pick_mode not in _PICK_MODES:
        raise ValueError(f"pick_mode must be one of {sorted(m for m in _PICK_MODES if m)} or None")
    if routes is None and route_result is not None:
        route_list = routes_from_route_result(route_result)
    else:
        route_list = [r for i, raw in enumerate(routes or []) if (r := _route(raw, "lower_risk" if i == 0 else "shortest", i))]

    user = _point(user_location)
    if user and isinstance(user_location, Mapping) and user_location.get("accuracy_m") is not None:
        user["accuracy_m"] = float(user_location["accuracy_m"])

    cells = []
    for c in risk_cells or []:
        p = _point(c)
        if p is None or c.get("value") is None:
            continue
        cells.append({**p, "value": max(0.0, min(1.0, float(c["value"]))), **({"size_m": float(c["size_m"])} if c.get("size_m") else {})})

    args = {
        "start": _point(start),
        "destination": _point(destination),
        "routes": route_list,
        "hazards": [x for h in hazards or [] if (x := _hazard(h))],
        "risk_cells": cells,
        "risk_synthetic": bool(risk_synthetic),
        "user_location": user,
        "pick_mode": pick_mode,
        "selected_route": selected_route,
        "fit": fit,
        "fly_to": dict(fly_to) if fly_to else None,
        "hud": hud,
        "show_route_card": bool(show_route_card),
        "quality": quality,
        "height": int(height),
    }
    for name, value in (("labels", labels), ("lamps", lamps), ("legend", legend), ("stats", stats)):
        if value is not None:
            args[name] = bool(value)

    event = _component(**args, key=key, default=None)
    if not event or not only_new_events:
        return event
    seen_key = f"_map3d_seen_{key or 'default'}"
    stamp = (event.get("ts"), event.get("seq"))
    if st.session_state.get(seen_key) == stamp:
        return None
    st.session_state[seen_key] = stamp
    return event
