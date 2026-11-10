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
  python ingenuity_scraper.py --flights 2 --name-by sol # name the folder sol_0061 instead of flight_002
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


def capture_time(imageid):
    """Capture time (SCLK seconds) from the product name, e.g. HNM_0061_0672355852_234ECM_... -> 672355852.234.

    The name's SCLK is used rather than extended.sclk / date_taken_utc from the API: those are not in
    capture order (e.g. on Sol 61 frame HELI00485 has an API sclk 47 s later than frame HELI00537), while the
    name's SCLK increases with the nav camera frame counter at ~30 Hz.
    """
    p = imageid.split("_")
    return int(p[2]) + int(p[3][:3]) / 1000.0


def frame_counter(imageid):
    i = imageid.find("HELI")
    return int(imageid[i + 4:i + 9]) if i >= 0 else -1


def download_images(sol, out_dir, size):
    """Images go to <camera>/images/<sclk>.<ext>, a folder that holds only images, named by capture time.

    That layout matches what VGGT-SLAM expects: main.py globs every file in --image_folder (metadata.json
    would be loaded as an image), sorts by the last number in the name, and writes the first number in the
    name as the TUM timestamp. NASA names would give the sol (e.g. 61) as every timestamp and a wrong order.
    """
    for camera, folder in CAMERAS.items():
        imgs = query_images(sol, camera)
        print(f"  {camera}: {len(imgs)} images")
        if not imgs:
            continue
        d = out_dir / folder
        img_dir = d / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        imgs = sorted(imgs, key=lambda im: capture_time(im["imageid"]))
        (d / "metadata.json").write_text(json.dumps(imgs, indent=2))
        rows, seen = [], set()
        for i, im in enumerate(imgs, 1):
            url = im["image_files"].get(size) or im["image_files"]["full_res"]
            orig = url.rsplit("/", 1)[-1]
            ts = capture_time(im["imageid"])
            if ts in seen:
                print(f"    ! duplicate capture time {ts:.3f}, skipping {orig}")
                continue
            seen.add(ts)
            dest = img_dir / f"{ts:.3f}{Path(orig).suffix}"
            rows.append({"timestamp": f"{ts:.3f}", "file": f"images/{dest.name}", "nasa_filename": orig,
                         "imageid": im["imageid"], "frame_counter": frame_counter(im["imageid"]),
                         "api_sclk": im["extended"]["sclk"], "api_date_taken_utc": im["date_taken_utc"]})
            legacy = d / orig  # layout written by earlier versions of this script
            if legacy.exists() and not dest.exists():
                legacy.rename(dest)
            if dest.exists() and dest.stat().st_size > 0:
                continue
            r = get(url)
            if r.status_code != 200:
                print(f"    ! {r.status_code} {url}")
                rows.pop()
                continue
            dest.write_bytes(r.content)
            print(f"    [{i}/{len(imgs)}] {dest.name}  ({orig})")
        with open(d / "frames.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["timestamp"])
            w.writeheader()
            w.writerows(rows)
        write_camera_models(imgs, {float(r["timestamp"]) for r in rows}, d)


# ----------------------------------------------------------------------------- SLAM reference data
MARS_RADIUS_M = 3396190.0  # sphere used by MMGIS; reproduces the Length_m of every flight path


def parse_cahvore(s):
    parts = s.split(";")
    vecs = [tuple(float(v) for v in p.strip("()").split(",")) for p in parts[:7]]
    return vecs, [float(p) for p in parts[7:]]


def cahv_to_pose(C, A, H, V):
    """CAHV -> pinhole intrinsics and camera-to-world pose (OpenCV axes: x right, y down, z forward)."""
    def dot(a, b): return sum(x * y for x, y in zip(a, b))
    def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    def norm(a): return math.sqrt(dot(a, a))
    hc, vc = dot(A, H), dot(A, V)
    hs, vs = norm(cross(A, H)), norm(cross(A, V))
    x = [(h - hc * a) / hs for h, a in zip(H, A)]
    y = [(v - vc * a) / vs for v, a in zip(V, A)]
    Rcw = [[x[i], y[i], A[i]] for i in range(3)]  # columns = camera axes in world frame
    return (hs, vs, hc, vc), Rcw, C


def rot_to_quat(R):
    """3x3 rotation -> (qx, qy, qz, qw)."""
    t = R[0][0] + R[1][1] + R[2][2]
    if t > 0:
        s = math.sqrt(t + 1) * 2
        q = ((R[2][1] - R[1][2]) / s, (R[0][2] - R[2][0]) / s, (R[1][0] - R[0][1]) / s, s / 4)
    elif R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s = math.sqrt(1 + R[0][0] - R[1][1] - R[2][2]) * 2
        q = (s / 4, (R[0][1] + R[1][0]) / s, (R[0][2] + R[2][0]) / s, (R[2][1] - R[1][2]) / s)
    elif R[1][1] > R[2][2]:
        s = math.sqrt(1 + R[1][1] - R[0][0] - R[2][2]) * 2
        q = ((R[0][1] + R[1][0]) / s, s / 4, (R[1][2] + R[2][1]) / s, (R[0][2] - R[2][0]) / s)
    else:
        s = math.sqrt(1 + R[2][2] - R[0][0] - R[1][1]) * 2
        q = ((R[0][2] + R[2][0]) / s, (R[1][2] + R[2][1]) / s, s / 4, (R[1][0] - R[0][1]) / s)
    return q


def write_camera_models(imgs, kept, d):
    """intrinsics.json + cahv_poses_tum.txt (per-image camera model from the raw-image API, see README)."""
    poses, intr = [], None
    for im in imgs:
        ts = capture_time(im["imageid"])
        cam = im["camera"]
        if ts not in kept or not cam.get("camera_model_component_list", "UNK").startswith("("):
            continue
        vecs, extra = parse_cahvore(cam["camera_model_component_list"])
        (fx, fy, cx, cy), Rcw, C = cahv_to_pose(*vecs[:4])
        poses.append((ts, *C, *rot_to_quat(Rcw)))
        if intr is None:
            w, h = (int(v) for v in im["extended"]["dimension"].strip("()").split(","))
            intr = {"model": cam["camera_model_type"], "width": w, "height": h,
                    "pinhole_approx": {"fx": fx, "fy": fy, "cx": cx, "cy": cy},
                    "cahvore": dict(zip("CAHVORE", vecs)), "cahvore_extra": extra,
                    "note": "fx,fy,cx,cy are the CAHV linear part only; CAHVORE adds strong fisheye "
                            "distortion (nav camera). Valid for the image size above (full_res)."}
    if intr:
        (d / "intrinsics.json").write_text(json.dumps(intr, indent=2))
    with open(d / "cahv_poses_tum.txt", "w") as fh:
        fh.write("# timestamp(SCLK s) tx ty tz qx qy qz qw  camera-to-world, OpenCV camera axes, "
                 "frame of the API camera model (UNVERIFIED, see README)\n")
        for p in poses:
            fh.write(" ".join(f"{v:.6f}" for v in p) + "\n")


def flight_path_local(path, origin):
    """MMGIS path -> list of (t, east_m, north_m) relative to origin (lon, lat).

    The path has no timestamps; its points are assumed evenly spaced in time over [SCLK_START, SCLK_END]
    (they come at ~29.5 points/s for every flight, consistent with a fixed-rate telemetry log).
    """
    pts = path["geometry"]["coordinates"]
    t0, t1 = path["properties"]["SCLK_START"], path["properties"]["SCLK_END"]
    lon0, lat0 = origin
    k = math.radians(1) * MARS_RADIUS_M
    out = []
    for i, c in enumerate(pts):
        t = t0 + (t1 - t0) * i / max(len(pts) - 1, 1)
        out.append((t, (c[0] - lon0) * k * math.cos(math.radians(lat0)), (c[1] - lat0) * k))
    return out


def interp_path(traj, t):
    if t <= traj[0][0]:
        return traj[0][1:]
    if t >= traj[-1][0]:
        return traj[-1][1:]
    lo, hi = 0, len(traj) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if traj[mid][0] <= t else (lo, mid)
    (ta, ea, na), (tb, eb, nb) = traj[lo], traj[hi]
    u = (t - ta) / (tb - ta) if tb > ta else 0
    return ea + u * (eb - ea), na + u * (nb - na)


def write_reference(sol_dir, paths):
    """Per camera: gt_path_tum.txt = flight path interpolated at each image's capture time."""
    if not paths:
        return
    paths = sorted(paths, key=lambda p: p["properties"]["SCLK_START"])
    origin = paths[0]["geometry"]["coordinates"][0][:2]  # takeoff point of the sol's first flight
    traj = [pt for p in paths for pt in flight_path_local(p, origin)]
    t_start, t_end = traj[0][0], traj[-1][0]
    with open(sol_dir / "map" / "flight_path_local_tum.txt", "w") as fh:
        fh.write("# timestamp(SCLK s, ASSUMED uniform) east_m north_m 0 0 0 0 1  (no altitude/attitude)\n")
        for t, e, n in traj:
            fh.write(f"{t:.6f} {e:.6f} {n:.6f} 0 0 0 0 1\n")
    for folder in CAMERAS.values():
        fcsv = sol_dir / folder / "frames.csv"
        if not fcsv.exists():
            continue
        with open(fcsv) as fh:
            stamps = [float(r["timestamp"]) for r in csv.DictReader(fh)]
        with open(sol_dir / folder / "gt_path_tum.txt", "w") as fh:
            fh.write("# timestamp(SCLK s) east_m north_m 0 0 0 0 1  flight path at image times; "
                     "z and orientation UNKNOWN (set to 0 / identity); clamped to path ends outside "
                     f"flight window [{t_start:.3f}, {t_end:.3f}]\n")
            for t in stamps:
                e, n = interp_path(traj, t)
                fh.write(f"{t:.3f} {e:.6f} {n:.6f} 0 0 0 0 1\n")
        n_in = sum(t_start <= t <= t_end for t in stamps)
        print(f"  {folder}: gt_path_tum.txt ({n_in}/{len(stamps)} frames inside the flight window)")


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
    write_reference(out_dir, paths)
    if render:
        render_map(paths, start_wp, wps, d / f"flight_map_{out_dir.name}.png")


# ----------------------------------------------------------------------------- main
def folder_name(sol, flights, name_by):
    """flight_002 for one flight, flight_010-011 for several on one sol, sol_0061 if no flight (or --name-by sol)."""
    if name_by == "sol" or not flights:
        return f"sol_{sol:04d}"
    if len(flights) == 1:
        return f"flight_{flights[0]:03d}"
    return f"flight_{flights[0]:03d}-{flights[-1]:03d}"


def migrate_folder(old, new):
    """Rename a folder written under the other naming scheme, so nothing is downloaded twice."""
    if old == new or not old.exists() or new.exists():
        return
    old.rename(new)
    for png in (new / "map").glob("flight_map_*.png"):
        png.rename(png.with_name(f"flight_map_{new.name}.png"))
    print(f"  renamed {old.name}/ -> {new.name}/")


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
    ap.add_argument("--name-by", default="flight", choices=["flight", "sol"],
                    help="output folder name: flight_NNN (default; sol_NNNN if the sol has no flight) or sol_NNNN")
    a = ap.parse_args()

    out = Path(a.out)
    cache = out / "_cache"
    cache.mkdir(parents=True, exist_ok=True)

    wp = load_geojson(WAYPOINTS_URL, cache)
    f2s = {f["properties"]["Flight"]: f["properties"]["Sol"] for f in wp["features"]}
    s2f = {}
    for fl, s in f2s.items():
        s2f.setdefault(s, []).append(fl)
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
        flights = sorted(s2f.get(sol, []))
        print(f"=== Sol {sol}" + (f" (Flight {', '.join(map(str, flights))})" if flights else ""))
        sd = out / folder_name(sol, flights, a.name_by)
        migrate_folder(out / f"sol_{sol:04d}", sd)
        if not a.no_images:
            download_images(sol, sd, a.size)
        if not a.no_map:
            download_map(sol, sd, cache, render=not a.no_render)


if __name__ == "__main__":
    main()
