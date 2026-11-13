"""Checks for the map3d Python helpers and the baked data. Run: python -m pytest tests"""
import gzip
import json
import struct
from pathlib import Path

import pytest

import map3d
from map3d import routes_from_osrm, routes_from_route_result

DATA = Path(map3d.__file__).parent / "frontend" / "data"


def test_point_accepts_tuples_and_dicts():
    assert map3d._point((12.93, 77.61)) == {"lat": 12.93, "lon": 77.61}
    assert map3d._point({"latitude": 12.93, "longitude": 77.61, "label": "Home"}) == {"lat": 12.93, "lon": 77.61, "label": "Home"}
    assert map3d._point(None) is None


def test_point_rejects_swapped_or_invalid_coordinates():
    with pytest.raises(ValueError):
        map3d._point((120.0, 77.61))


def test_route_result_contract_keeps_missing_risk_as_none():
    result = {
        "shortest": {"distance_m": 850, "risk_indicator": None, "coords": [(12.93, 77.61), (12.94, 77.62)]},
        "lower_risk": {"distance_m": 1100, "risk_indicator": 0.25, "coords": [(12.93, 77.61), (12.935, 77.618), (12.94, 77.62)]},
    }
    routes = routes_from_route_result(result)
    assert [r["role"] for r in routes] == ["lower_risk", "shortest"]
    assert routes[0]["risk_indicator"] == 0.25
    assert routes[1]["risk_indicator"] is None          # shown as "Data limited", never as 0


def test_route_result_with_identical_paths_draws_one_line():
    path = [(12.93, 77.61), (12.94, 77.62)]
    routes = routes_from_route_result({
        "shortest": {"distance_m": 850, "risk_indicator": 0.4, "coords": path},
        "lower_risk": {"distance_m": 850, "risk_indicator": 0.4, "coords": path},
    })
    assert len(routes) == 1


def test_osrm_response_is_converted_to_lat_lon_and_sorted_by_distance():
    response = {"code": "Ok", "routes": [
        {"distance": 1751.2, "duration": 1400.0, "geometry": {"coordinates": [[77.6113, 12.9346], [77.6228, 12.9435]]}},
        {"distance": 1658.4, "duration": 1326.0, "geometry": {"coordinates": [[77.6113, 12.9346], [77.62, 12.94], [77.6228, 12.9435]]}},
    ]}
    routes = routes_from_osrm(response)
    assert [r["role"] for r in routes] == ["shortest", "alternative"]
    assert routes[0]["distance_m"] == 1658.4
    assert routes[0]["coords"][0] == [12.9346, 77.6113]
    assert all(r["risk_indicator"] is None for r in routes)


def test_manifest_lists_every_tile_and_tiles_are_readable():
    manifest = json.loads((DATA / "manifest.json").read_text())
    assert manifest["stats"]["buildings"]["total"] > 1_000_000
    counted = 0
    for c in manifest["chunks"]:
        f = DATA / f"c_{c['cx']}_{c['cy']}.bin"
        assert f.exists(), f.name
        if c["cx"] == 10 and c["cy"] == 6:                      # Koramangala
            body = gzip.decompress(f.read_bytes())
            magic, n_buildings = struct.unpack_from("<4sI", body, 0)
            assert magic == b"SNC1" and n_buildings == c["nb"]
        counted += c["nb"]
    assert counted == manifest["stats"]["buildings"]["total"]


def test_sample_routes_carry_no_invented_risk():
    sample = json.loads((DATA / "sample_routes.json").read_text())
    assert sample["routes"] and all(r["risk_indicator"] is None for r in sample["routes"])
