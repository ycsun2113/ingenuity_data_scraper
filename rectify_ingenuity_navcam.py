"""
Rectify Ingenuity navcam images from the CAHVORE2 fisheye model to an
ideal pinhole camera, for every flight in the dataset.

Input layout (produced by ingenuity_scraper.py):
    <raw-root>/flight_XXX/navcam/images/<sclk>.png
    <raw-root>/flight_XXX/navcam/frames.csv   (optional, maps to NASA names)

Output layout:
    <output-root>/flight_XXX/images/<sclk>.png   rectified frames
    <output-root>/flight_XXX/original/<sclk>.png (with --save-original)
    <output-root>/flight_XXX/{K.npy, camera.json, frames.json,
                              valid_mask.png, comparison.png}

Example:
    python rectify_ingenuity_navcam.py                  # all flights
    python rectify_ingenuity_navcam.py --flights 26 27 --save-original
"""

import argparse
import csv
import json
import re
from pathlib import Path

import cv2
import numpy as np


DEFAULT_RAW_ROOT = "/cedar/datasets/ingenuity_data/raw_data"
DEFAULT_OUTPUT_ROOT = "/cedar/datasets/ingenuity_data/rectified_images"

# HNM_0414_0703689840_866ECM_N0260001HELI00470_0000LUJ02.png
#     sol  sclk_sec   sclk_ms                     seq
NASA_FILENAME_RE = re.compile(
    r"^HNM_(?P<sol>\d{4})_(?P<sec>\d{10})_(?P<ms>\d{3})"
    r".*?HELI(?P<seq>\d+)_"
)
# 703689840.866.png
SCLK_FILENAME_RE = re.compile(r"^(?P<sclk>\d+(?:\.\d+)?)\.(?:png|jpe?g)$", re.I)


def parse_nasa_filename(name):
    m = NASA_FILENAME_RE.match(name)
    if m is None:
        return None
    return {
        "sol": int(m["sol"]),
        "sclk": int(m["sec"]) + int(m["ms"]) / 1000.0,
        "sequence": int(m["seq"]),
    }


def parse_filename(name):
    info = parse_nasa_filename(name)
    if info is not None:
        return info
    m = SCLK_FILENAME_RE.match(name)
    if m is not None:
        return {"sclk": float(m["sclk"])}
    return None


def sort_key(path):
    info = parse_filename(path.name)
    # Order by spacecraft clock; fall back to filename.
    return (info["sclk"] if info else float("inf"), path.name)


def load_frames_csv(navcam_dir):
    """Map image filename -> NASA metadata, if frames.csv exists."""
    csv_path = navcam_dir / "frames.csv"
    if not csv_path.exists():
        return {}
    table = {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            name = Path(row.get("file", "")).name
            if name:
                table[name] = row
    return table


def process_sequence(input_dir, output_dir, maps, save_original):
    map_x, map_y, mask, K, camera_meta = maps

    files = sorted(
        (p for p in input_dir.iterdir()
         if p.suffix.lower() in (".png", ".jpg", ".jpeg")),
        key=sort_key,
    )
    if not files:
        print(f"[skip] no images in {input_dir}")
        return

    csv_rows = load_frames_csv(input_dir.parent)

    rectified_dir = output_dir / "images"
    rectified_dir.mkdir(parents=True, exist_ok=True)
    original_dir = output_dir / "original"
    if save_original:
        original_dir.mkdir(parents=True, exist_ok=True)

    np.save(output_dir / "K.npy", K)
    cv2.imwrite(str(output_dir / "valid_mask.png"), mask)

    frames = []
    skipped = []
    t0 = None
    first = True

    print(f"\n== {input_dir}: {len(files)} frames -> {output_dir}")

    for i, path in enumerate(files):
        image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise IOError(f"Could not read {path}")

        h, w = image.shape[:2]
        if (w, h) != (camera_meta["width"], camera_meta["height"]):
            # e.g. 64x48 thumbnails mixed into some flights
            print(f"   [skip] {path.name}: size {(w, h)} does not match "
                  f"calibration {(camera_meta['width'], camera_meta['height'])}")
            skipped.append({"source": path.name, "size": [w, h]})
            continue

        rectified = cv2.remap(
            image, map_x, map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        # Keep the source filename (SCLK timestamp) so frames line up with
        # frames.csv and the TUM pose files in the raw navcam directory.
        filename = path.stem + ".png"
        cv2.imwrite(str(rectified_dir / filename), rectified)
        if save_original:
            cv2.imwrite(str(original_dir / filename), image)

        if first:
            first = False
            cv2.imwrite(
                str(output_dir / "comparison.png"),
                np.hstack([image, rectified]),
            )

        info = parse_filename(path.name) or {}
        row = csv_rows.get(path.name)
        if row is not None:
            nasa_name = row.get("nasa_filename")
            nasa_info = parse_nasa_filename(nasa_name or "") or {}
            info = {**nasa_info, **info, "nasa_filename": nasa_name}
            if row.get("frame_counter"):
                info["frame_counter"] = int(row["frame_counter"])

        if "sclk" in info and t0 is None:
            t0 = info["sclk"]
        frames.append({
            "index": len(frames),
            "frame": filename,
            "source": path.name,
            **info,
            "time_from_start": (
                round(info["sclk"] - t0, 3) if "sclk" in info else None
            ),
        })

    with open(output_dir / "camera.json", "w") as f:
        json.dump(camera_meta, f, indent=2)

    with open(output_dir / "frames.json", "w") as f:
        json.dump(frames, f, indent=2)

    if skipped:
        with open(output_dir / "skipped.json", "w") as f:
            json.dump(skipped, f, indent=2)

    if not frames:
        print("   no valid frames")
        return
    duration = frames[-1].get("time_from_start")
    print(f"   saved {len(frames)} frames"
          + (f", duration {duration:.1f} s" if duration is not None else "")
          + (f", skipped {len(skipped)}" if skipped else ""))


# ============================================================
# 1. Read CAHVORE camera model
# ============================================================

def load_cahvore(path):
    text = Path(path).read_text()

    def read_vector(name):
        match = re.search(
            rf"^{name}\s*=\s*([^\n]+)",
            text,
            flags=re.MULTILINE
        )
        if match is None:
            raise ValueError(f"Missing camera vector: {name}")

        values = np.fromstring(match.group(1), sep=" ")
        if len(values) != 3:
            raise ValueError(f"Invalid vector: {name}")

        return values.astype(np.float64)

    model_match = re.search(
        r"^Model\s*=\s*(.+)$",
        text,
        flags=re.MULTILINE
    )

    if not model_match or "CAHVORE2" not in model_match.group(1):
        raise ValueError("This script supports CAHVORE2 only.")

    dims_match = re.search(
        r"^Dimensions\s*=\s*(\d+)\s+(\d+)",
        text,
        flags=re.MULTILINE
    )

    if dims_match is None:
        raise ValueError("Missing image dimensions.")

    width, height = map(int, dims_match.groups())

    camera = {
        name: read_vector(name)
        for name in ("C", "A", "H", "V", "O", "R", "E")
    }

    camera["width"] = width
    camera["height"] = height

    # E = 0 allows central-ray rectification.
    if not np.allclose(camera["E"], 0.0, atol=1e-10):
        raise NotImplementedError(
            "Nonzero E requires a full CAHVORE entrance-pupil "
            "projection implementation."
        )

    return camera


# ============================================================
# 2. Build rectified camera basis
# ============================================================

def normalize(vector):
    return vector / np.linalg.norm(vector)


def camera_basis(camera):
    A = normalize(camera["A"])

    # Remove optical-axis components from H and V.
    H_perp = camera["H"] - np.dot(camera["H"], A) * A
    V_perp = camera["V"] - np.dot(camera["V"], A) * A

    right = normalize(H_perp)

    # Orthogonalize vertical basis.
    down = V_perp - np.dot(V_perp, right) * right
    down = normalize(down)

    return A, right, down


# ============================================================
# 3. CAHVORE2 forward projection
# ============================================================

def project_cahvore2(rays, camera):
    """
    Project 3D viewing rays into CAHVORE2 pixel coordinates.

    CAHVORE2 uses an equidistant angular variable chi=theta.
    R provides its radial polynomial correction.

    E=0 is assumed (central projection).

    rays: array with shape (..., 3)
    """

    O = normalize(camera["O"])
    R0, R1, R2 = camera["R"]

    omega = np.sum(rays * O, axis=-1)

    perpendicular = rays - omega[..., None] * O
    lam = np.linalg.norm(perpendicular, axis=-1)

    theta = np.arctan2(lam, omega)

    unit_perp = np.divide(
        perpendicular,
        lam[..., None],
        out=np.zeros_like(perpendicular),
        where=lam[..., None] > 1e-12
    )

    chi = theta

    chi_distorted = chi * (
        1.0
        + R0
        + R1 * chi**2
        + R2 * chi**4
    )

    # Project distorted ray through CAHV linear projection.
    distorted_ray = O + chi_distorted[..., None] * unit_perp

    alpha = np.sum(distorted_ray * camera["A"], axis=-1)

    alpha_safe = np.where(
        np.abs(alpha) > 1e-12,
        alpha,
        np.nan
    )

    u = (
        np.sum(distorted_ray * camera["H"], axis=-1)
        / alpha_safe
    )

    v = (
        np.sum(distorted_ray * camera["V"], axis=-1)
        / alpha_safe
    )

    return u, v, alpha


# ============================================================
# 4. Build inverse-remapping grid
# ============================================================

def build_rectification_map(camera, focal, width, height):
    A, right, down = camera_basis(camera)

    fx = fy = float(focal)
    cx = (width - 1) / 2.0
    cy = (height - 1) / 2.0

    K = np.array([
        [fx, 0, cx],
        [0, fy, cy],
        [0, 0, 1]
    ], dtype=np.float64)

    u, v = np.meshgrid(
        np.arange(width),
        np.arange(height)
    )

    x = (u - cx) / fx
    y = (v - cy) / fy

    rays = (
        A[None, None, :]
        + x[..., None] * right
        + y[..., None] * down
    )

    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)

    source_u, source_v, alpha = project_cahvore2(
        rays, camera
    )

    valid = (
        np.isfinite(source_u)
        & np.isfinite(source_v)
        & (alpha > 0)
        & (source_u >= 0)
        & (source_u <= camera["width"] - 1)
        & (source_v >= 0)
        & (source_v <= camera["height"] - 1)
    )

    map_x = np.where(valid, source_u, -1).astype(np.float32)
    map_y = np.where(valid, source_v, -1).astype(np.float32)

    mask = (valid.astype(np.uint8) * 255)

    return map_x, map_y, mask, K



def find_flight_dirs(raw_root, flights=None):
    """Return [(flight_name, navcam_images_dir), ...] sorted by flight."""
    wanted = None
    if flights:
        wanted = set()
        for f in flights:
            m = re.search(r"(\d+)$", f)
            if m is None:
                raise ValueError(f"Bad flight id: {f}")
            wanted.add(int(m.group(1)))

    result = []
    for flight_dir in sorted(raw_root.glob("flight_*")):
        m = re.fullmatch(r"flight_(\d+)", flight_dir.name)
        if m is None or (wanted is not None and int(m.group(1)) not in wanted):
            continue
        images_dir = flight_dir / "navcam" / "images"
        if not images_dir.is_dir():
            print(f"[skip] {flight_dir.name}: no navcam/images")
            continue
        result.append((flight_dir.name, images_dir))
    return result


def main():
    here = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-root", default=DEFAULT_RAW_ROOT,
                        help="Dir containing flight_*/navcam/images")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT,
                        help="Rectified output goes to <output-root>/flight_*")
    parser.add_argument("--flights", nargs="+", default=None,
                        help="Subset of flights, e.g. 26 27 or flight_026 "
                             "(default: all)")
    parser.add_argument("--camera", default=str(here / "ncam_fm.cahvore"))
    parser.add_argument("--focal", type=float, default=320.0)
    parser.add_argument("--save-original", action="store_true")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip flights whose output frames.json exists")
    args = parser.parse_args()

    raw_root = Path(args.raw_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()

    camera = load_cahvore(args.camera)
    width, height = camera["width"], camera["height"]

    print("Camera model: CAHVORE2")
    print(f"Resolution: {width} x {height}")

    # Same calibration for all flights -> build the remap grid once.
    map_x, map_y, mask, K = build_rectification_map(
        camera, args.focal, width, height
    )
    print(f"Valid output pixels: {100 * np.mean(mask > 0):.2f}%")

    camera_meta = {
        "source_model": "CAHVORE2",
        "output_model": "PINHOLE",
        "width": width,
        "height": height,
        "fx": float(K[0, 0]),
        "fy": float(K[1, 1]),
        "cx": float(K[0, 2]),
        "cy": float(K[1, 2]),
        "K": K.tolist(),
        "distortion": [0, 0, 0, 0, 0],
        "focal_choice": "user-selected output focal length",
        "valid_pixel_fraction": float(np.mean(mask > 0)),
    }
    maps = (map_x, map_y, mask, K, camera_meta)

    flights = find_flight_dirs(raw_root, args.flights)
    print(f"Found {len(flights)} flights with navcam images in {raw_root}")

    for flight_name, images_dir in flights:
        out_dir = output_root / flight_name
        if args.skip_existing and (out_dir / "frames.json").exists():
            print(f"[skip] {flight_name}: already rectified")
            continue
        process_sequence(images_dir, out_dir, maps, args.save_original)

    print("\nDone!")
    print("Camera intrinsics:\n", K)


if __name__ == "__main__":
    main()