#!/usr/bin/env python3
"""
Download Ingenuity Mars Helicopter data per Sol (or per Flight):
  * raw images, split into Navigation Camera (HELI_NAV) and Color Camera (HELI_RTE)
  * flight path + waypoints (GeoJSON / CSV) from the NASA MMGIS map
  * a rendered map PNG: HiRISE color basemap with the flight path drawn on top

Usage examples:
  python ingenuity_scraper.py --sols 58                 # Flight 1
  python ingenuity_scraper.py --sols 58 61 64-70
  python ingenuity_scraper.py --flights 1-5
  python ingenuity_scraper.py --sols 61 --no-map        # images only
  python ingenuity_scraper.py --sols 61 --no-images     # map only
  python ingenuity_scraper.py --list-flights            # print Flight <-> Sol table
"""
import argparse
import csv
import json
import math
import time
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw

RAW_API = "https://mars.nasa.gov/rss/api/"
FLIGHT_PATH_URL = "https://mars.nasa.gov/mmgis-maps/M20/Layers/json/m20_heli_flight_path.json"
WAYPOINTS_URL = "https://mars.nasa.gov/mmgis-maps/M20/Layers/json/m20_heli_waypoints.json"
BASEMAP_URL = ("https://mars.nasa.gov/mmgis-maps/M20/Layers/"
               "M20_HiRISE_RGB_CTX_mosaic_25cm_v7_Merge_LERC_clip/{z}/{x}/{y}.png")  # TMS scheme
CAMERAS = {"HELI_NAV": "navcam", "HELI_RTE": "colorcam"}
TILE = 256

session = requests.Session()
session.headers["User-Agent"] = "ingenuity-research-scraper/1.0 (python-requests)"


def get(url, retries=4, **kw):
    for i in range(retries):
        try:
            r = session.get(url, timeout=60, **kw)
            if r.status_code in (403, 404):
                return r
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == retries - 1:
                raise
            print(f"  retry ({e})")
            time.sleep(2 ** i)


def parse_ranges(items):
    out = set()
    for it in items:
        for part in str(it).split(","):
            if "-" in part:
                a, b = part.split("-")
                out.update(range(int(a), int(b) + 1))
            elif part:
                out.add(int(part))
    return sorted(out)


# ----------------------------------------------------------------------------- images
def query_images(sol, camera):
    """Return all image records for one sol and one helicopter camera."""
    images, page = [], 0
    while True:
        params = {
            "feed": "raw_images", "category": "ingenuity", "feedtype": "json",
            "num": 100, "page": page, "order": "sol asc",
            "condition_2": f"{sol}:sol:gte", "condition_3": f"{sol}:sol:lte",
            "search": f"|{camera}",
        }
        d = get(RAW_API, params=params).json()
        batch = d.get("images", [])
        images += batch
        if not batch or len(images) >= d.get("total_results", 0):
            return images
        page += 1


def download_images(sol, out_dir, size):
    for camera, folder in CAMERAS.items():
        imgs = query_images(sol, camera)
        print(f"  {camera}: {len(imgs)} images")
        if not imgs:
            continue
        d = out_dir / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps(imgs, indent=2))
        for i, im in enumerate(imgs, 1):
            url = im["image_files"].get(size) or im["image_files"]["full_res"]
            dest = d / url.rsplit("/", 1)[-1]
            if dest.exists() and dest.stat().st_size > 0:
                continue
            r = get(url)
            if r.status_code != 200:
                print(f"    ! {r.status_code} {url}")
                continue
            dest.write_bytes(r.content)
            print(f"    [{i}/{len(imgs)}] {dest.name}")


# ----------------------------------------------------------------------------- map
_cache = {}


def load_geojson(url, cache_dir):
    if url not in _cache:
        f = cache_dir / url.rsplit("/", 1)[-1]
        if not f.exists():
            f.write_bytes(get(url).content)
        _cache[url] = json.loads(f.read_text())
    return _cache[url]


def lonlat_to_px(lon, lat, z):
    """Global pixel coords (Web-Mercator / XYZ, as Leaflet uses for MMGIS)."""
    n = TILE * 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def render_map(paths, start_wp, waypoints, out_png, pad_px=200, max_px=2000):
    coords = [c for p in paths for c in p["geometry"]["coordinates"]]
    coords += [(w["properties"]["Lon"], w["properties"]["Lat"]) for w in [start_wp, *waypoints] if w]
    lons, lats = [c[0] for c in coords], [c[1] for c in coords]

    # highest zoom (<=19) where the flight extent fits in max_px
    z = 19
    while z > 12:
        x0, y0 = lonlat_to_px(min(lons), max(lats), z)
        x1, y1 = lonlat_to_px(max(lons), min(lats), z)
        if max(x1 - x0, y1 - y0) + 2 * pad_px <= max_px:
            break
        z -= 1
    x0, y0, x1, y1 = x0 - pad_px, y0 - pad_px, x1 + pad_px, y1 + pad_px

    tx0, ty0, tx1, ty1 = int(x0 // TILE), int(y0 // TILE), int(x1 // TILE), int(y1 // TILE)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            tms_y = 2 ** z - 1 - ty
            r = get(BASEMAP_URL.format(z=z, x=tx, y=tms_y))
            if r.status_code == 200:
                canvas.paste(Image.open(BytesIO(r.content)).convert("RGB"),
                             ((tx - tx0) * TILE, (ty - ty0) * TILE))
    ox, oy = tx0 * TILE, ty0 * TILE
    draw = ImageDraw.Draw(canvas)

    def P(lon, lat):
        x, y = lonlat_to_px(lon, lat, z)
        return x - ox, y - oy

    for p in paths:
        pts = [P(*c[:2]) for c in p["geometry"]["coordinates"]]
        draw.line(pts, fill=(0, 255, 255), width=4)
    for w, col in [(start_wp, (0, 255, 0))] + [(w, (255, 60, 60)) for w in waypoints]:
        if w:
            x, y = P(w["properties"]["Lon"], w["properties"]["Lat"])
            draw.ellipse([x - 9, y - 9, x + 9, y + 9], outline=col, width=4)
            draw.text((x + 12, y - 6), f"F{w['properties']['Flight']}", fill=col)

    canvas = canvas.crop((int(x0 - ox), int(y0 - oy), int(x1 - ox), int(y1 - oy)))
    canvas.save(out_png)
    print(f"  map: {out_png.name} (zoom {z}, {canvas.size[0]}x{canvas.size[1]} px; "
          f"green=start, red=landing, cyan=path)")


def download_map(sol, out_dir, cache_dir, render=True):
    fp = load_geojson(FLIGHT_PATH_URL, cache_dir)
    wp = load_geojson(WAYPOINTS_URL, cache_dir)
    paths = [f for f in fp["features"] if f["properties"]["Sol"] == sol]
    wps = [f for f in wp["features"] if f["properties"]["Sol"] == sol]
    if not paths and not wps:
        print("  no flight path / waypoint for this sol")
        return
    flights = sorted({f["properties"]["Flight"] for f in wps + paths})
    prev = [f for f in wp["features"] if f["properties"]["Flight"] == flights[0] - 1]
    start_wp = prev[0] if prev else None

    d = out_dir / "map"
    d.mkdir(parents=True, exist_ok=True)
    (d / "flight_path.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": paths}, indent=1))
    (d / "waypoints.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": ([start_wp] if start_wp else []) + wps}, indent=1))
    with open(d / "flight_path.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["flight", "point_idx", "lon", "lat"] + (["z"] if paths and len(paths[0]["geometry"]["coordinates"][0]) > 2 else []))
        for p in paths:
            for i, c in enumerate(p["geometry"]["coordinates"]):
                w.writerow([p["properties"]["Flight"], i, *c])
    with open(d / "waypoints.csv", "w", newline="") as fh:
        rows = [f["properties"] for f in ([start_wp] if start_wp else []) + wps]
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    for f in wps:
        p = f["properties"]
        print(f"  Flight {p['Flight']}: {p['FromAirfld']} -> {p['ToAirfld']}, "
              f"{p['Dist_m']} m, {p['Duration_s']} s, max alt {p['Max_Alt_m']} m")
    if render:
        render_map(paths, start_wp, wps, d / f"flight_map_sol{sol:04d}.png")


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sols", nargs="*", default=[], help="e.g. 58 61 64-70")
    ap.add_argument("--flights", nargs="*", default=[], help="e.g. 1 2 10-15 (converted to sols)")
    ap.add_argument("--out", default="data", help="output directory (default: ./data)")
    ap.add_argument("--size", default="full_res", choices=["full_res", "large", "medium", "small"])
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-map", action="store_true", help="skip GeoJSON/CSV and map rendering")
    ap.add_argument("--no-render", action="store_true", help="save GeoJSON/CSV but skip map PNG")
    ap.add_argument("--list-flights", action="store_true")
    a = ap.parse_args()

    out = Path(a.out)
    cache = out / "_cache"
    cache.mkdir(parents=True, exist_ok=True)

    if a.list_flights or a.flights:
        wp = load_geojson(WAYPOINTS_URL, cache)
        f2s = {f["properties"]["Flight"]: f["properties"]["Sol"] for f in wp["features"]}
        if a.list_flights:
            for f, s in sorted(f2s.items()):
                print(f"Flight {f:3d}  Sol {s}")
            return
    sols = set(parse_ranges(a.sols))
    for fl in parse_ranges(a.flights):
        if fl in f2s:
            sols.add(f2s[fl])
        else:
            print(f"Flight {fl} not found")
    if not sols:
        ap.error("give --sols and/or --flights")

    for sol in sorted(sols):
        print(f"=== Sol {sol}")
        sd = out / f"sol_{sol:04d}"
        if not a.no_images:
            download_images(sol, sd, a.size)
        if not a.no_map:
            download_map(sol, sd, cache, render=not a.no_render)


if __name__ == "__main__":
    main()
