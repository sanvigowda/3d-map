#!/usr/bin/env python3
"""Bake the Bengaluru 3D map data used by map3d/frontend.

Everything placed on the map comes from published open data:

* Building footprints, roads, rail/metro, water, parks, street lamps,
  stations and place names: Overture Maps Foundation (which redistributes
  OpenStreetMap, Google Open Buildings and Microsoft ML Buildings).
* Building heights, in order of preference:
    1. the mapped ``height`` tag,
    2. mapped floor count x 3.2 m,
    3. GHSL "average net building height" for the surrounding ~90 m cell
       (JRC, GHS-BUILT-H R2023A, epoch 2018),
    4. a 4 m default where none of the above exists.
  For 3 and 4 a small deterministic per-building variation is *generated*
  so a block does not render as one flat slab. That is disclosed in the
  map's About panel.

Nothing here invents incidents, lighting scores or risk values.

Usage:
    pip install -r tools/requirements-build.txt
    python tools/build_data.py --cache .cache --out map3d/frontend/data
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import shapely
from PIL import Image, ImageDraw

# --------------------------------------------------------------------------
# Model frame: a flat metric grid centred on Bengaluru (x east, y north).
# --------------------------------------------------------------------------
ORIGIN_LAT, ORIGIN_LON = 12.9716, 77.5946
EXTENT = (-18000.0, -16000.0, 20000.0, 18000.0)  # xmin, ymin, xmax, ymax in metres
CHUNK = 2000.0   # one binary file per 2 km square
SUB = 4          # each chunk is meshed as 4 x 4 sub-tiles of 500 m
Q = 0.25         # building / road vertex quantum in metres (int16)
BASE_Q = 1.0     # water / park vertex quantum in metres (int16)

_phi = math.radians(ORIGIN_LAT)
M_PER_DEG_LAT = 111132.92 - 559.82 * math.cos(2 * _phi) + 1.175 * math.cos(4 * _phi) - 0.0023 * math.cos(6 * _phi)
M_PER_DEG_LON = 111412.84 * math.cos(_phi) - 93.5 * math.cos(3 * _phi) + 0.118 * math.cos(5 * _phi)

NCX = int(round((EXTENT[2] - EXTENT[0]) / CHUNK))
NCY = int(round((EXTENT[3] - EXTENT[1]) / CHUNK))

OVERTURE_TYPES = ["building", "segment", "water", "land_use", "land", "division", "infrastructure", "place"]
GHSL_TILE = "GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_4326_3ss_V1_0_R8_C26"  # 10 deg tile holding Bengaluru
GHSL_URL = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_BUILT_H_GLOBE_R2023A/"
    "GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_4326_3ss/V1-0/tiles/" + GHSL_TILE + ".zip"
)

FLOOR_HEIGHT = 3.2
DEFAULT_HEIGHT = 4.0
MIN_BUILDING_AREA = 10.0

ROAD_CLASS = {
    "motorway": 0, "trunk": 1, "primary": 2, "secondary": 3, "tertiary": 4,
    "residential": 5, "unclassified": 5, "living_street": 5, "unknown": 5,
    "service": 6, "track": 6,
    "footway": 7, "path": 7, "pedestrian": 7, "steps": 7, "cycleway": 7, "bridleway": 7,
}
RAIL, METRO, WATERWAY = 8, 9, 10
# metres, used only for the far-view raster; the viewer has its own widths
RASTER_WIDTH = {0: 16, 1: 14, 2: 12, 3: 10, 4: 8, 5: 5, 6: 3, 7: 0, 8: 4, 9: 0, 10: 0}

WATER_SUBTYPES = {"lake", "reservoir", "pond", "water", "river"}
GREEN_LAND_USE = {"park", "garden", "pitch", "playground", "recreation_ground", "grass", "cemetery",
                  "grave_yard", "green", "golf_course", "fairway", "orchard", "plant_nursery", "village_green"}
GREEN_LAND = {"wood", "forest", "grassland", "grass"}

# Landmarks are looked up by exact name in the downloaded data so their
# positions are the mapped ones, never typed in by hand.
LANDMARKS = [
    ("building", "Vidhana Soudha", "Vidhana Soudha"),
    ("building", "Bangalore Palace", "Bangalore Palace"),
    ("building", "Sree Kanteerava Stadium", "Kanteerava Stadium"),
    ("building", "Karnataka High Court", "Karnataka High Court"),
    ("building", "Garuda Mall", "Garuda Mall"),
    ("building", "Phoenix Market City", "Phoenix Marketcity"),
    ("building", "Orion mall", "Orion Mall"),
    ("building", "Victoria Hospital", "Victoria Hospital"),
    ("land_use", "UB City", "UB City"),
    ("land_use", "M Chinnaswamy Stadium", "Chinnaswamy Stadium"),
    ("land_use", "Indian Institute of Science", "IISc"),
    ("land_use", "Manyata Tech Park", "Manyata Tech Park"),
    ("land_use", "Commercial Street Shopping Area", "Commercial Street"),
    ("land_use", "St. John's Medical College", "St. John's Hospital"),
    ("land_use", "HAL Airport", "HAL Airport"),
    ("land_use", "Krishna Rajendra City Market", "KR Market"),
    ("land_use", "Mantri Square", "Mantri Square"),
    ("land_use", "RMZ Ecospace", "RMZ Ecospace"),
    ("land_use", "Bagmane Tech Park", "Bagmane Tech Park"),
    # two independent place records agree on these to within 30 m
    ("place", "Nexus Mall Koramangala", "Nexus Mall Koramangala"),
    ("place", "MG Road Metro Station", "MG Road"),
]

# Fly-to presets: (chip label, label name to look up, kinds, camera distance m, heading deg, pitch deg)
PRESETS = [
    ("Koramangala", "Koramangala", ("area",), 1500, -24, 56),
    ("MG Road", "MG Road", ("poi",), 1300, 28, 58),
    ("Indiranagar", "Indiranagar", ("area",), 1700, 12, 56),
    ("Majestic", "KSR Bengaluru", ("rail",), 1500, -35, 57),
    ("Jayanagar", "Jayanagar", ("area",), 1700, 20, 55),
    ("HSR Layout", "HSR Layout", ("area",), 1700, -15, 55),
    ("Whitefield", "Whitefield", ("area",), 2000, 30, 55),
    ("Electronic City", "Electronic City", ("area",), 2200, -20, 56),
    ("Hebbal", "Hebbal", ("area",), 2000, 15, 55),
]


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------
# Coordinates
# --------------------------------------------------------------------------
def to_local(c: np.ndarray) -> np.ndarray:
    out = np.empty_like(c, dtype=np.float64)
    out[:, 0] = (c[:, 0] - ORIGIN_LON) * M_PER_DEG_LON
    out[:, 1] = (c[:, 1] - ORIGIN_LAT) * M_PER_DEG_LAT
    return out


def to_lonlat(x, y):
    return ORIGIN_LON + np.asarray(x) / M_PER_DEG_LON, ORIGIN_LAT + np.asarray(y) / M_PER_DEG_LAT


def bbox_lonlat(pad=0.004):
    lon0, lat0 = to_lonlat(EXTENT[0], EXTENT[1])
    lon1, lat1 = to_lonlat(EXTENT[2], EXTENT[3])
    return float(lon0) - pad, float(lat0) - pad, float(lon1) + pad, float(lat1) + pad


def chunk_index(x, y):
    cx = np.floor((x - EXTENT[0]) / CHUNK).astype(np.int64)
    cy = np.floor((y - EXTENT[1]) / CHUNK).astype(np.int64)
    ok = (cx >= 0) & (cx < NCX) & (cy >= 0) & (cy < NCY)
    return cx, cy, ok


def chunk_center(cid):
    cx = cid % NCX
    cy = cid // NCX
    return EXTENT[0] + (cx + 0.5) * CHUNK, EXTENT[1] + (cy + 0.5) * CHUNK


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------
def download_overture(cache: Path, release: str | None):
    raw = cache / "overture"
    raw.mkdir(parents=True, exist_ok=True)
    bbox = ",".join(f"{v:.5f}" for v in bbox_lonlat())
    for t in OVERTURE_TYPES:
        dst = raw / f"{t}.parquet"
        if dst.exists():
            continue
        log(f"  downloading Overture {t} for bbox {bbox}")
        cmd = [sys.executable, "-m", "overturemaps", "download", f"--bbox={bbox}", "-f", "geoparquet", "-t", t, "-o", str(dst)]
        if release:
            cmd += ["-r", release]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return raw


def overture_release(raw: Path, requested: str | None) -> str:
    if requested:
        return requested
    try:
        state = json.loads((raw / "building.parquet.state").read_text())
        for k in ("last_release", "release", "version"):
            if state.get(k):
                return str(state[k])
    except Exception:
        pass
    try:
        out = subprocess.run([sys.executable, "-m", "overturemaps", "releases", "latest"], capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().split()[-1]
    except Exception:
        pass
    return "latest at download time"


def download_ghsl(cache: Path):
    d = cache / "ghsl"
    d.mkdir(parents=True, exist_ok=True)
    tif = d / (GHSL_TILE + ".tif")
    if not tif.exists():
        log("  downloading GHSL building-height tile (60 MB)")
        z = d / "tile.zip"
        with urllib.request.urlopen(GHSL_URL, timeout=600) as r, open(z, "wb") as f:
            shutil.copyfileobj(r, f)
        with zipfile.ZipFile(z) as zf:
            zf.extract(GHSL_TILE + ".tif", d)
        z.unlink()
    return tif


class HeightGrid:
    """Bilinear sampler over the GHSL average-net-building-height raster."""

    def __init__(self, tif: Path):
        import tifffile

        with tifffile.TiffFile(tif) as tf:
            page = tf.pages[0]
            scale = page.tags["ModelPixelScaleTag"].value
            tie = page.tags["ModelTiepointTag"].value
            self.x0, self.y0 = tie[3], tie[4]
            self.dx, self.dy = scale[0], scale[1]
            lon0, lat0, lon1, lat1 = bbox_lonlat(0.01)
            c0 = int((lon0 - self.x0) / self.dx) - 2
            c1 = int((lon1 - self.x0) / self.dx) + 3
            r0 = int((self.y0 - lat1) / self.dy) - 2
            r1 = int((self.y0 - lat0) / self.dy) + 3
            self.r0, self.c0 = r0, c0
            self.a = np.nan_to_num(page.asarray()[r0:r1, c0:c1].astype(np.float32), nan=0.0)

    def sample(self, lon, lat):
        fx = (lon - self.x0) / self.dx - 0.5 - self.c0
        fy = (self.y0 - lat) / self.dy - 0.5 - self.r0
        x0 = np.clip(np.floor(fx).astype(np.int64), 0, self.a.shape[1] - 2)
        y0 = np.clip(np.floor(fy).astype(np.int64), 0, self.a.shape[0] - 2)
        tx = np.clip(fx - x0, 0, 1)
        ty = np.clip(fy - y0, 0, 1)
        a = self.a
        top = a[y0, x0] * (1 - tx) + a[y0, x0 + 1] * tx
        bot = a[y0 + 1, x0] * (1 - tx) + a[y0 + 1, x0 + 1] * tx
        return top * (1 - ty) + bot * ty


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def primary_names(col):
    return [(n or {}).get("primary") if isinstance(n, dict) else None for n in col.to_pylist()]


def hash01(x, y):
    """Deterministic pseudo-random [0,1) from a position (so re-bakes are stable)."""
    xi = np.round(np.asarray(x) * 4).astype(np.int64)
    yi = np.round(np.asarray(y) * 4).astype(np.int64)
    h = (xi * 73856093) ^ (yi * 19349663)
    h = (h ^ (h >> 13)) * 1274126177
    return ((h ^ (h >> 16)) & 0xFFFFFF).astype(np.float64) / float(0x1000000)


def pad4(b: bytes) -> bytes:
    return b + b"\0" * ((-len(b)) % 4)


def delta_encode(q: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """First vertex of each ring/line absolute, the rest as differences (compresses well)."""
    d = np.diff(q, axis=0, prepend=np.zeros((1, 2), dtype=q.dtype))
    d[starts] = q[starts]
    return d


def ring_arrays(rings):
    """Flatten closed rings to (coords without the repeated last vertex, per-ring counts)."""
    coords, idx = shapely.get_coordinates(rings, return_index=True)
    n = np.bincount(idx, minlength=len(rings))
    last = np.cumsum(n) - 1
    keep = np.ones(len(coords), dtype=bool)
    keep[last] = False
    return coords[keep], n - 1


# --------------------------------------------------------------------------
# Buildings
# --------------------------------------------------------------------------
def load_buildings(raw: Path, grid: HeightGrid):
    log("buildings: reading")
    tb = pq.read_table(raw / "building.parquet", columns=["geometry", "height", "num_floors", "is_underground"])
    geoms = shapely.from_wkb(tb["geometry"].to_numpy(zero_copy_only=False))
    h_tag = tb["height"].to_numpy(zero_copy_only=False).astype(np.float64)
    floors = tb["num_floors"].to_numpy(zero_copy_only=False).astype(np.float64)
    under = np.array([bool(v) for v in tb["is_underground"].to_pylist()])
    del tb

    parts, src = shapely.get_parts(geoms, return_index=True)
    del geoms
    is_poly = shapely.get_type_id(parts) == 3
    parts, src = parts[is_poly], src[is_poly]
    log(f"buildings: {len(parts):,} polygons, projecting + simplifying")
    rings = shapely.get_exterior_ring(parts)
    del parts
    rings = shapely.transform(rings, to_local)
    rings = shapely.simplify(rings, 0.35, preserve_topology=False)
    npts = shapely.get_num_coordinates(rings)
    ok = (npts >= 4) & ~shapely.is_empty(rings)
    rings, src, npts = rings[ok], src[ok], npts[ok]

    # very detailed outlines: simplify harder so a ring fits the u8 vertex count
    for tol in (1.0, 2.5, 6.0):
        big = npts > 250
        if not big.any():
            break
        rings[big] = shapely.simplify(rings[big], tol, preserve_topology=False)
        npts = shapely.get_num_coordinates(rings)
    ok = (npts >= 4) & (npts <= 256) & ~shapely.is_empty(rings)
    rings, src = rings[ok], src[ok]

    polys = shapely.polygons(rings)
    area = shapely.area(polys)
    cen = shapely.centroid(polys)
    cx_m, cy_m = shapely.get_x(cen), shapely.get_y(cen)
    del polys, cen
    gx, gy, inside = chunk_index(cx_m, cy_m)
    ok = inside & (area >= MIN_BUILDING_AREA) & ~under[src] & np.isfinite(cx_m)
    rings, src, area, cx_m, cy_m, gx, gy = rings[ok], src[ok], area[ok], cx_m[ok], cy_m[ok], gx[ok], gy[ok]

    cw = ~shapely.is_ccw(rings)
    rings[cw] = shapely.reverse(rings[cw])

    log(f"buildings: {len(rings):,} kept, assigning heights")
    lon, lat = to_lonlat(cx_m, cy_m)
    est = grid.sample(lon, lat).astype(np.float64)
    u = hash01(cx_m, cy_m)
    ht, fl = h_tag[src], floors[src]
    height = np.where(est >= 2.0, est * (0.85 + 0.30 * u), DEFAULT_HEIGHT * (0.875 + 0.375 * u))
    source = np.where(est >= 2.0, 1, 0)                       # 1 area estimate, 0 default
    has_fl = np.isfinite(fl) & (fl >= 1)
    height = np.where(has_fl, fl * FLOOR_HEIGHT, height)
    source = np.where(has_fl, 2, source)                      # 2 floor count
    has_h = np.isfinite(ht) & (ht >= 2.0)
    height = np.where(has_h, ht, height)
    source = np.where(has_h, 3, source)                       # 3 mapped height
    height = np.clip(height, 3.0, 300.0)

    cid = gy * NCX + gx
    ccx, ccy = chunk_center(cid)
    sx = np.clip(np.floor((cx_m - (ccx - CHUNK / 2)) / (CHUNK / SUB)).astype(np.int64), 0, SUB - 1)
    sy = np.clip(np.floor((cy_m - (ccy - CHUNK / 2)) / (CHUNK / SUB)).astype(np.int64), 0, SUB - 1)
    sub = sy * SUB + sx
    order = np.lexsort((sub, cid))
    rings, cid, sub, height, source = rings[order], cid[order], sub[order], height[order], source[order]

    coords, counts = ring_arrays(rings)
    stats = {
        "total": int(len(rings)),
        "measured": int((source == 3).sum()),
        "floors": int((source == 2).sum()),
        "area_estimate": int((source == 1).sum()),
        "default": int((source == 0).sum()),
    }
    log("buildings:", stats, f"vertices {len(coords):,}")
    return {"coords": coords, "counts": counts, "cid": cid, "sub": sub, "height": height, "rings": rings, "stats": stats}


# --------------------------------------------------------------------------
# Roads, rail, waterways
# --------------------------------------------------------------------------
def load_lines(raw: Path):
    log("roads: reading")
    tb = pq.read_table(raw / "segment.parquet", columns=["geometry", "subtype", "class"])
    geoms = shapely.from_wkb(tb["geometry"].to_numpy(zero_copy_only=False))
    subtype = np.array(tb["subtype"].to_pylist(), dtype=object)
    klass = np.array(tb["class"].to_pylist(), dtype=object)
    code = np.full(len(geoms), -1, dtype=np.int64)
    for i, (st, kc) in enumerate(zip(subtype, klass)):
        if st == "road":
            code[i] = ROAD_CLASS.get(kc, 5)
        elif st == "rail":
            code[i] = METRO if kc in ("subway", "light_rail", "monorail") else RAIL
    keep = code >= 0
    geoms, code = geoms[keep], code[keep]

    wt = pq.read_table(raw / "water.parquet", columns=["geometry", "subtype"])
    wg = shapely.from_wkb(wt["geometry"].to_numpy(zero_copy_only=False))
    wsub = np.array(wt["subtype"].to_pylist(), dtype=object)
    is_line = np.isin(shapely.get_type_id(wg), (1, 5)) & np.isin(wsub, ("canal", "stream", "river"))
    geoms = np.concatenate([geoms, wg[is_line]])
    code = np.concatenate([code, np.full(int(is_line.sum()), WATERWAY, dtype=np.int64)])

    parts, src = shapely.get_parts(geoms, return_index=True)
    is_ls = shapely.get_type_id(parts) == 1
    parts, code = parts[is_ls], code[src[is_ls]]
    parts = shapely.transform(parts, to_local)
    parts = shapely.simplify(parts, 0.4, preserve_topology=False)
    ok = shapely.get_num_coordinates(parts) >= 2
    parts, code = parts[ok], code[ok]
    log(f"roads: {len(parts):,} lines")
    return parts, code


def split_lines_by_chunk(lines, code):
    """Cut every line where it crosses into another chunk so each piece lives in exactly one."""
    coords, li = shapely.get_coordinates(lines, return_index=True)
    n = len(coords)
    gx, gy, inside = chunk_index(coords[:, 0], coords[:, 1])
    vcid = np.where(inside, gy * NCX + gx, -1)
    is_seg = np.zeros(n, dtype=bool)          # vertex i starts a segment (i, i+1)
    is_seg[:-1] = li[:-1] == li[1:]
    seg_idx = np.nonzero(is_seg)[0]
    seg_cid = vcid[seg_idx]
    seg_line = li[seg_idx]
    new_piece = np.ones(len(seg_idx), dtype=bool)
    new_piece[1:] = (seg_line[1:] != seg_line[:-1]) | (seg_cid[1:] != seg_cid[:-1])
    piece_of_seg = np.cumsum(new_piece) - 1
    last_of_piece = np.ones(len(seg_idx), dtype=bool)
    last_of_piece[:-1] = new_piece[1:]
    rep = 1 + last_of_piece.astype(np.int64)
    out_v = np.repeat(seg_idx, rep)
    ends = np.cumsum(rep) - 1
    out_v[ends[last_of_piece]] += 1
    out_piece = np.repeat(piece_of_seg, rep)
    piece_cid = seg_cid[new_piece]
    piece_code = code[seg_line[new_piece]]
    piece_n = np.bincount(out_piece, minlength=len(piece_cid))
    pts = coords[out_v]

    keep_piece = piece_cid >= 0
    keep_pt = keep_piece[out_piece]
    pts, piece_cid, piece_code, piece_n = pts[keep_pt], piece_cid[keep_piece], piece_code[keep_piece], piece_n[keep_piece]

    order = np.argsort(piece_cid, kind="stable")
    starts = np.cumsum(piece_n) - piece_n
    pt_order = np.concatenate([np.arange(s, s + c) for s, c in zip(starts[order], piece_n[order])]) if len(order) else np.zeros(0, dtype=np.int64)
    log(f"roads: {len(order):,} pieces after chunk split, {len(pts):,} points")
    return {"coords": pts[pt_order], "counts": piece_n[order], "cid": piece_cid[order], "code": piece_code[order]}


# --------------------------------------------------------------------------
# Lamps
# --------------------------------------------------------------------------
def load_infra(raw: Path):
    tb = pq.read_table(raw / "infrastructure.parquet", columns=["geometry", "class", "names"])
    klass = np.array(tb["class"].to_pylist(), dtype=object)
    names = primary_names(tb["names"])
    geoms = shapely.from_wkb(tb["geometry"].to_numpy(zero_copy_only=False))
    cen = shapely.centroid(geoms)
    xy = to_local(np.column_stack([shapely.get_x(cen), shapely.get_y(cen)]))
    return klass, names, xy


def lamps_from(klass, xy):
    pts = xy[klass == "street_lamp"]
    gx, gy, ok = chunk_index(pts[:, 0], pts[:, 1])
    pts, cid = pts[ok], (gy * NCX + gx)[ok]
    order = np.argsort(cid, kind="stable")
    log(f"lamps: {len(pts):,} mapped street lamps")
    return {"coords": pts[order], "cid": cid[order]}


# --------------------------------------------------------------------------
# Water + green polygons (one small global file)
# --------------------------------------------------------------------------
def load_base(raw: Path):
    log("base: water + green polygons")
    box = shapely.box(*EXTENT)
    out_geoms, out_kind, named = [], [], []

    def take(geoms, kind, names, min_area, label_kind, label_area):
        geoms = shapely.transform(geoms, to_local)
        geoms = shapely.make_valid(geoms)
        geoms = shapely.intersection(geoms, box)
        for g, nm in zip(geoms, names):
            if g is None or g.is_empty:
                continue
            a = g.area
            if nm and a >= label_area:
                p = g.representative_point()
                named.append({"n": nm, "x": round(p.x), "y": round(p.y), "k": label_kind, "a": round(a / 10000.0, 1)})
            for p in shapely.get_parts(g):
                if p.geom_type != "Polygon" or p.area < min_area:
                    continue
                p = shapely.simplify(p, 1.0, preserve_topology=True)
                if p.is_empty or p.geom_type != "Polygon":
                    continue
                out_geoms.append(p)
                out_kind.append(kind)

    wt = pq.read_table(raw / "water.parquet", columns=["geometry", "subtype", "class", "names"])
    wg = shapely.from_wkb(wt["geometry"].to_numpy(zero_copy_only=False))
    wsub = np.array(wt["subtype"].to_pylist(), dtype=object)
    wcls = np.array(wt["class"].to_pylist(), dtype=object)
    wn = np.array(primary_names(wt["names"]), dtype=object)
    m = np.isin(shapely.get_type_id(wg), (3, 6)) & np.isin(wsub, list(WATER_SUBTYPES)) & (wcls != "wastewater")
    take(wg[m], 0, wn[m], 150.0, "water", 60000.0)

    lt = pq.read_table(raw / "land_use.parquet", columns=["geometry", "class", "names"])
    lg = shapely.from_wkb(lt["geometry"].to_numpy(zero_copy_only=False))
    lcls = np.array(lt["class"].to_pylist(), dtype=object)
    ln = np.array(primary_names(lt["names"]), dtype=object)
    m = np.isin(shapely.get_type_id(lg), (3, 6)) & np.isin(lcls, list(GREEN_LAND_USE))
    park = np.isin(lcls, ("park", "garden", "recreation_ground"))
    take(lg[m & park], 1, ln[m & park], 300.0, "park", 60000.0)
    take(lg[m & ~park], 1, [None] * int((m & ~park).sum()), 300.0, "park", 1e18)

    nt = pq.read_table(raw / "land.parquet", columns=["geometry", "class"])
    ng = shapely.from_wkb(nt["geometry"].to_numpy(zero_copy_only=False))
    ncls = np.array(nt["class"].to_pylist(), dtype=object)
    m = np.isin(shapely.get_type_id(ng), (3, 6)) & np.isin(ncls, list(GREEN_LAND))
    take(ng[m], 1, [None] * int(m.sum()), 300.0, "park", 1e18)

    polys = np.array(out_geoms, dtype=object)
    kinds = np.array(out_kind, dtype=np.uint8)
    order = np.argsort(-kinds, kind="stable")  # green first, water last (drawn on top)
    polys, kinds = polys[order], kinds[order]
    log(f"base: {int((kinds == 0).sum()):,} water, {int((kinds == 1).sum()):,} green polygons")
    return polys, kinds, named


def write_base(out: Path, polys, kinds):
    rings, pidx = shapely.get_rings(polys, return_index=True)
    rings_per_poly = np.bincount(pidx, minlength=len(polys))
    coords, counts = ring_arrays(rings)
    q = np.round(coords / BASE_Q).astype(np.int64)
    starts = np.cumsum(counts) - counts
    d = delta_encode(q, starts)
    assert np.abs(d).max() < 32768
    body = struct.pack("<4sIII", b"SNB1", len(polys), len(rings), len(coords))
    body += pad4(kinds.astype(np.uint8).tobytes())
    body += pad4(rings_per_poly.astype("<u2").tobytes())
    body += counts.astype("<u4").tobytes()
    body += pad4(d.astype("<i2").tobytes())
    data = gzip.compress(body, compresslevel=9, mtime=0)
    (out / "base.bin").write_bytes(data)
    return len(data)


# --------------------------------------------------------------------------
# Far-view raster of the built fabric
# --------------------------------------------------------------------------
def write_fabric(out: Path, b, lines, code, width=2048, ss=4, lo_width=1024):
    log("fabric: rasterising far-view texture")
    w_m, h_m = EXTENT[2] - EXTENT[0], EXTENT[3] - EXTENT[1]
    W = width * ss
    H = int(round(W * h_m / w_m))
    s = W / w_m
    img = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(img)

    lc, li = shapely.get_coordinates(lines, return_index=True)
    px = np.column_stack([(lc[:, 0] - EXTENT[0]) * s, (EXTENT[3] - lc[:, 1]) * s]).astype(np.float32)
    bounds = np.concatenate([[0], np.cumsum(np.bincount(li, minlength=len(lines)))])
    for cls_code, lum in ((6, 60), (5, 78), (8, 70), (4, 104), (3, 116), (2, 128), (1, 138), (0, 146)):
        wpx = max(1, int(round(RASTER_WIDTH[cls_code] * s)))
        for i in np.nonzero(code == cls_code)[0]:
            draw.line(px[bounds[i]:bounds[i + 1]].ravel().tolist(), fill=lum, width=wpx)

    bc, cnt = b["coords"], b["counts"]
    bpx = np.column_stack([(bc[:, 0] - EXTENT[0]) * s, (EXTENT[3] - bc[:, 1]) * s]).astype(np.float32)
    st = np.concatenate([[0], np.cumsum(cnt)])
    for i in range(len(cnt)):
        draw.polygon(bpx[st[i]:st[i + 1]].ravel().tolist(), fill=215)

    # Two sizes: a small one the map shows at once, a sharper one loaded after
    # the first view is built. 2048 px is enough for a whole-city view on a laptop.
    big = img.resize((width, int(round(width * h_m / w_m))), Image.LANCZOS)
    big.save(out / "fabric.jpg", quality=84, optimize=True)
    big.resize((lo_width, int(round(lo_width * h_m / w_m))), Image.LANCZOS).save(out / "fabric_lo.jpg", quality=84, optimize=True)
    return big.size


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------
def build_labels(raw: Path, infra, base_named, b):
    labels = []
    box = EXTENT

    def inside(x, y, pad=300):
        return box[0] + pad <= x <= box[2] - pad and box[1] + pad <= y <= box[3] - pad

    dt_ = pq.read_table(raw / "division.parquet", columns=["geometry", "subtype", "class", "names"])
    dg = shapely.from_wkb(dt_["geometry"].to_numpy(zero_copy_only=False))
    dxy = to_local(np.column_stack([shapely.get_x(dg), shapely.get_y(dg)]))
    dsub = dt_["subtype"].to_pylist()
    dcls = dt_["class"].to_pylist()
    dn = primary_names(dt_["names"])

    areas = []
    for (x, y), st, kc, nm in zip(dxy, dsub, dcls, dn):
        if nm and inside(x, y) and (st == "macrohood" or (st == "locality" and kc == "town")):
            areas.append({"n": nm, "x": round(x), "y": round(y), "k": "area"})
    seen = {}
    for a in areas:
        seen.setdefault(a["n"].lower(), a)
    areas = list(seen.values())
    labels += areas

    def near_same(name, x, y, pool, dist):
        for o in pool:
            if o["n"].lower() == name.lower() and math.hypot(o["x"] - x, o["y"] - y) < dist:
                return True
        return False

    hoods = []
    rank = {"neighborhood": 0, "microhood": 1, "locality": 2}
    rows = sorted(
        [(rank[st], nm, x, y) for (x, y), st, kc, nm in zip(dxy, dsub, dcls, dn)
         if nm and st in rank and kc not in ("city", "town") and inside(x, y)],
        key=lambda r: r[0],
    )
    for _, nm, x, y in rows:
        if len(nm) > 32 or "Corporation" in nm or nm.endswith(" Ward"):
            continue
        if near_same(nm, x, y, areas, 2500) or near_same(nm, x, y, hoods, 1200):
            continue
        hoods.append({"n": nm, "x": round(x), "y": round(y), "k": "hood"})
    labels += hoods

    for it in base_named:
        if inside(it["x"], it["y"], 0):
            labels.append(it)

    klass, inames, ixy = infra
    for kind, cls_name in (("rail", "railway_station"), ("metro", "subway_station")):
        got = []
        for kc, nm, (x, y) in zip(klass, inames, ixy):
            if kc == cls_name and nm and inside(x, y, 0) and not near_same(nm, x, y, got, 400):
                if nm.startswith("Krantivira Sangolli Rayanna"):
                    nm = "KSR Bengaluru"
                got.append({"n": nm, "x": round(x), "y": round(y), "k": kind})
        labels += got

    def centroid_by_name(table, name, by_area=True):
        t = pq.read_table(raw / f"{table}.parquet", columns=["geometry", "names"])
        names = primary_names(t["names"])
        hits = [i for i, n in enumerate(names) if n == name]
        if not hits:
            return None
        gs = shapely.from_wkb(t["geometry"].to_numpy(zero_copy_only=False)[hits])
        g = max(gs, key=lambda q: q.area) if by_area else gs[0]
        p = g.representative_point() if g.geom_type != "Point" else g
        x, y = to_local(np.array([[p.x, p.y]]))[0]
        return float(x), float(y)

    cache = {}
    for table, name, shown in LANDMARKS:
        xy = centroid_by_name(table, name)
        if xy is None or not inside(*xy, 0):
            log(f"  landmark not found in data, skipped: {name}")
            continue
        labels.append({"n": shown, "x": round(xy[0]), "y": round(xy[1]), "k": "poi"})
        cache[shown] = xy

    presets = []
    for chip, lookup, kinds, dist, heading, pitch in PRESETS:
        hit = next((l for l in labels if l["n"] == lookup and l["k"] in kinds), None)
        if hit is None:
            log(f"  preset target not found, skipped: {lookup}")
            continue
        presets.append({"n": chip, "x": hit["x"], "y": hit["y"], "d": dist, "h": heading, "p": pitch})

    log("labels:", {k: sum(1 for l in labels if l["k"] == k) for k in sorted({l["k"] for l in labels})})
    return labels, presets


# --------------------------------------------------------------------------
# Chunk files
# --------------------------------------------------------------------------
def write_chunks(out: Path, b, r, lamps):
    for old in out.glob("c_*.bin"):
        old.unlink()
    b_cnt = b["counts"]
    b_starts = np.cumsum(b_cnt) - b_cnt
    b_lo = np.searchsorted(b["cid"], np.arange(NCX * NCY), "left")
    b_hi = np.searchsorted(b["cid"], np.arange(NCX * NCY), "right")
    r_cnt = r["counts"]
    r_starts = np.cumsum(r_cnt) - r_cnt
    r_lo = np.searchsorted(r["cid"], np.arange(NCX * NCY), "left")
    r_hi = np.searchsorted(r["cid"], np.arange(NCX * NCY), "right")
    l_lo = np.searchsorted(lamps["cid"], np.arange(NCX * NCY), "left")
    l_hi = np.searchsorted(lamps["cid"], np.arange(NCX * NCY), "right")

    entries, total = [], 0
    for cid in range(NCX * NCY):
        nb, nr, nl = b_hi[cid] - b_lo[cid], r_hi[cid] - r_lo[cid], l_hi[cid] - l_lo[cid]
        if nb == 0 and nr == 0 and nl == 0:
            continue
        ox, oy = chunk_center(cid)
        origin = np.array([ox, oy])

        # buildings
        if nb:
            v0 = b_starts[b_lo[cid]]
            v1 = b_starts[b_hi[cid] - 1] + b_cnt[b_hi[cid] - 1]
            bq = np.round((b["coords"][v0:v1] - origin) / Q).astype(np.int64)
            cnt = b_cnt[b_lo[cid]:b_hi[cid]]
            bd = delta_encode(bq, np.cumsum(cnt) - cnt)
            hq = np.round(b["height"][b_lo[cid]:b_hi[cid]] / Q).astype(np.int64)
            sub_counts = np.bincount(b["sub"][b_lo[cid]:b_hi[cid]], minlength=SUB * SUB)
        else:
            bd = np.zeros((0, 2), dtype=np.int64)
            cnt = np.zeros(0, dtype=np.int64)
            hq = np.zeros(0, dtype=np.int64)
            sub_counts = np.zeros(SUB * SUB, dtype=np.int64)
        # roads
        if nr:
            p0 = r_starts[r_lo[cid]]
            p1 = r_starts[r_hi[cid] - 1] + r_cnt[r_hi[cid] - 1]
            rq = np.round((r["coords"][p0:p1] - origin) / Q).astype(np.int64)
            rc = r_cnt[r_lo[cid]:r_hi[cid]]
            rd = delta_encode(rq, np.cumsum(rc) - rc)
            rcode = r["code"][r_lo[cid]:r_hi[cid]]
        else:
            rd = np.zeros((0, 2), dtype=np.int64)
            rc = np.zeros(0, dtype=np.int64)
            rcode = np.zeros(0, dtype=np.int64)
        lq = np.round((lamps["coords"][l_lo[cid]:l_hi[cid]] - origin) / Q).astype(np.int64) if nl else np.zeros((0, 2), dtype=np.int64)

        for arr in (bd, rd, lq):
            assert arr.size == 0 or np.abs(arr).max() < 32768, "coordinate overflow"
        assert rc.size == 0 or rc.max() < 65536

        body = struct.pack("<4s5I", b"SNC1", len(cnt), len(bd), len(rc), len(rd), len(lq))
        body += sub_counts.astype("<u4").tobytes()
        body += pad4(hq.astype("<u2").tobytes())
        body += pad4(cnt.astype(np.uint8).tobytes())
        body += pad4(bd.astype("<i2").tobytes())
        body += pad4(rcode.astype(np.uint8).tobytes())
        body += pad4(rc.astype("<u2").tobytes())
        body += pad4(rd.astype("<i2").tobytes())
        body += pad4(lq.astype("<i2").tobytes())
        data = gzip.compress(body, compresslevel=9, mtime=0)
        cx, cy = cid % NCX, cid // NCX
        (out / f"c_{cx}_{cy}.bin").write_bytes(data)
        total += len(data)
        entries.append({"cx": int(cx), "cy": int(cy), "nb": int(nb), "nr": int(nr), "nl": int(nl), "bytes": len(data)})
    log(f"chunks: {len(entries)} files, {total / 1e6:.1f} MB")
    return entries, total


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=".cache", help="where raw downloads are kept")
    ap.add_argument("--out", default="map3d/frontend/data", help="output folder served with the map")
    ap.add_argument("--release", default=None, help="Overture release (default: latest)")
    args = ap.parse_args()
    cache, out = Path(args.cache), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    log("downloads")
    raw = download_overture(cache, args.release)
    grid = HeightGrid(download_ghsl(cache))
    release = overture_release(raw, args.release)

    b = load_buildings(raw, grid)
    lines, code = load_lines(raw)
    r = split_lines_by_chunk(lines, code)
    infra = load_infra(raw)
    lamps = lamps_from(infra[0], infra[2])
    polys, kinds, base_named = load_base(raw)

    chunks, chunk_bytes = write_chunks(out, b, r, lamps)
    base_bytes = write_base(out, polys, kinds)
    fabric_size = write_fabric(out, b, lines, code)
    labels, presets = build_labels(raw, infra, base_named, b)

    manifest = {
        "version": 1,
        "name": "Bengaluru",
        "built": dt.date.today().isoformat(),
        "origin": {"lat": ORIGIN_LAT, "lon": ORIGIN_LON},
        "m_per_deg": {"lat": M_PER_DEG_LAT, "lon": M_PER_DEG_LON},
        "extent": list(EXTENT),
        "chunk": CHUNK, "sub": SUB, "q": Q, "base_q": BASE_Q,
        "ncx": NCX, "ncy": NCY,
        "chunks": chunks,
        "fabric": {"file": "fabric.jpg", "lo": "fabric_lo.jpg", "w": fabric_size[0], "h": fabric_size[1]},
        "base": {"file": "base.bin", "bytes": base_bytes},
        "stats": {
            "buildings": b["stats"],
            "road_pieces": int(len(r["counts"])),
            "lamps": int(len(lamps["cid"])),
            "water_polygons": int((kinds == 0).sum()),
            "green_polygons": int((kinds == 1).sum()),
            "bytes": int(chunk_bytes + base_bytes),
        },
        "road_classes": ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "service",
                         "footway", "rail", "metro", "waterway"],
        "labels": labels,
        "presets": presets,
        "sources": [
            f"Buildings, roads, rail, water, parks, street lamps, stations and place names: Overture Maps Foundation release {release} "
            "(includes OpenStreetMap, Google Open Buildings and Microsoft ML Buildings data). ODbL / CDLA-Permissive / CC BY 4.0.",
            "Building height estimates: GHSL GHS-BUILT-H R2023A, average net building height, epoch 2018, 3 arc-second grid "
            "(European Commission, Joint Research Centre). CC BY 4.0.",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False))
    log(f"done: {out} ({(chunk_bytes + base_bytes) / 1e6:.1f} MB of geometry)")


if __name__ == "__main__":
    main()
