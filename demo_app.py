"""Wiring example for the SafeNisha 3D map.

Run:  python -m streamlit run demo_app.py

This is not the SafeNisha app. It shows how app.py can drive the map:
click to set start / destination, fetch walking alternatives from the public
OpenStreetMap foot router, and place hazard reports. There is no risk model
here, so every route shows "Data limited" until core/ supplies risk indicators.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.request

import streamlit as st

from map3d import routes_from_osrm, safenisha_map

ROUTER = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
HAZARD_TYPES = ["broken_streetlight", "dark_path", "blocked_footpath", "other_infrastructure"]

st.set_page_config(page_title="SafeNisha map demo", layout="wide")
ss = st.session_state
ss.setdefault("start", None)
ss.setdefault("destination", None)
ss.setdefault("routes", [])
ss.setdefault("hazards", [])
ss.setdefault("pending_hazard", None)
ss.setdefault("message", "")


@st.cache_data(show_spinner=False, ttl=600)
def fetch_routes(a: tuple[float, float], b: tuple[float, float]) -> dict:
    url = f"{ROUTER}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson&alternatives=3&steps=false"
    req = urllib.request.Request(url, headers={"User-Agent": "safenisha-prototype/0.1"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)


def refresh_routes() -> None:
    ss.routes = []
    if not (ss.start and ss.destination):
        return
    try:
        data = fetch_routes(ss.start, ss.destination)
        if data.get("code") != "Ok":
            ss.message = "No walking route was found between these points."
            return
        ss.routes = routes_from_osrm(data)
        ss.message = "" if len(ss.routes) > 1 else "The router returned one route, so there is nothing to compare."
    except Exception as err:  # network down, router busy
        ss.message = f"The public walking router could not be reached ({err})."


st.title("SafeNisha 3D map: wiring demo")
st.caption("Prototype. Routes come from the public OpenStreetMap foot router. No risk data is loaded, so risk shows as “Data limited”.")

left, right = st.columns([1, 3], gap="large")

with left:
    mode = st.radio("A map click sets", ["start", "destination", "hazard", "nothing"], horizontal=True)
    pick_mode = None if mode == "nothing" else mode
    if st.button("Clear route"):
        ss.start = ss.destination = None
        ss.routes = []
        ss.message = ""

    if ss.pending_hazard:
        lat, lon = ss.pending_hazard
        with st.form("hazard"):
            st.write(f"Report at {lat:.5f}, {lon:.5f}")
            hazard_type = st.selectbox("Hazard type", HAZARD_TYPES)
            description = st.text_input("Description (optional)", max_chars=280)
            if st.form_submit_button("Submit report"):
                ss.hazards.append({
                    "id": len(ss.hazards) + 1, "latitude": lat, "longitude": lon,
                    "hazard_type": hazard_type, "description": description,
                    "reported_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "new", "expires_at": None,
                })
                ss.pending_hazard = None
                st.rerun()

    if ss.hazards:
        st.subheader("Reports this session")
        for h in ss.hazards:
            c1, c2 = st.columns([3, 2])
            c1.write(f"#{h['id']} {h['hazard_type'].replace('_', ' ')} · **{h['status']}**")
            if h["status"] == "new" and c2.button("Confirm", key=f"c{h['id']}"):
                h["status"] = "confirmed"
                st.rerun()
            elif h["status"] == "confirmed" and c2.button("Resolve", key=f"r{h['id']}"):
                h["status"] = "resolved"
                st.rerun()

    if ss.message:
        st.info(ss.message)

    if ss.routes:
        st.subheader("Routes")
        for r in ss.routes:
            st.markdown(f"**{r['label']}**  \n{r['distance_m'] / 1000:.2f} km · about {round(r['duration_s'] / 60)} min on foot")
            st.caption("Estimated risk: data limited")

with right:
    event = safenisha_map(
        start=ss.start,
        destination=ss.destination,
        routes=ss.routes,
        hazards=ss.hazards,
        pick_mode=pick_mode,
        hud="full",
        height=680,
        key="map",
    )

if event and event["type"] == "click" and event.get("mode"):
    point = (event["lat"], event["lon"])
    if not event.get("inside", True):
        ss.message = "That point is outside the mapped area."
    elif event["mode"] == "hazard":
        ss.pending_hazard = point
    else:
        ss[event["mode"]] = point
        refresh_routes()
    st.rerun()
