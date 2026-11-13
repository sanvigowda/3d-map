#!/usr/bin/env python3
"""Capture one real pair of walking-route alternatives for the standalone demo.

The geometry, distance and duration are exactly what the public OpenStreetMap
foot router returned at capture time. No risk values are attached, because the
router knows nothing about risk: the demo shows them as "Data limited".

Usage:  python tools/fetch_sample_routes.py
"""
import datetime as dt
import json
import urllib.request
from pathlib import Path

START = {"lat": 12.9346, "lon": 77.6113, "label": "Nexus Mall Koramangala"}
END = {"lat": 12.9435, "lon": 77.6228, "label": "Koramangala Indoor Stadium"}
ROUTER = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
OUT = Path(__file__).resolve().parents[1] / "map3d" / "frontend" / "data" / "sample_routes.json"


def main():
    url = (f"{ROUTER}/{START['lon']},{START['lat']};{END['lon']},{END['lat']}"
           "?overview=full&geometries=geojson&alternatives=3&steps=false")
    req = urllib.request.Request(url, headers={"User-Agent": "safenisha-map-demo/1.0"})
    data = json.load(urllib.request.urlopen(req, timeout=60))
    if data.get("code") != "Ok":
        raise SystemExit(f"router said {data.get('code')}")
    routes = sorted(data["routes"], key=lambda r: r["distance"])
    out = {
        "source": "Public OpenStreetMap foot router (routing.openstreetmap.de, routed-foot)",
        "captured": dt.date.today().isoformat(),
        "note": "Real router output. No risk data attached.",
        "start": START,
        "destination": END,
        "routes": [],
    }
    for i, r in enumerate(routes[:2]):
        out["routes"].append({
            "id": "shortest" if i == 0 else "alternative",
            "role": "shortest" if i == 0 else "alternative",
            "label": "Shortest route" if i == 0 else "Alternative route",
            "distance_m": round(r["distance"]),
            "duration_s": round(r["duration"]),
            "risk_indicator": None,
            "coords": [[round(lat, 6), round(lon, 6)] for lon, lat in r["geometry"]["coordinates"]],
        })
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {OUT} with {len(out['routes'])} route(s):",
          [(r["label"], r["distance_m"], r["duration_s"]) for r in out["routes"]])


if __name__ == "__main__":
    main()
