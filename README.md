# Ingenuity Data Scraper

A Python script that downloads Ingenuity Mars Helicopter data for selected Sols or flights:

- **Raw images**, saved separately for the Navigation Camera (`HELI_NAV`) and the Color Camera (`HELI_RTE`)
- **Flight path and waypoints** from the NASA Mars 2020 location map, as GeoJSON and CSV
- **A flight map** (PNG): the HiRISE color basemap with the flight path drawn on top

## Requirements

- Python 3.8+
- `requests`, `Pillow`

```bash
pip install requests Pillow
```

## Usage

```bash
python ingenuity_scraper.py --sols 58               # one Sol (Flight 1)
python ingenuity_scraper.py --sols 58 61 64-70      # several Sols and/or ranges
python ingenuity_scraper.py --flights 1-10          # by flight number (converted to Sols)
python ingenuity_scraper.py --list-flights          # print the Flight <-> Sol table
```

### Options

| Option | Description |
|---|---|
| `--sols` | Sols to download, e.g. `58 61 64-70` |
| `--flights` | Flight numbers to download, e.g. `1 2 10-15` |
| `--out DIR` | Output directory (default: `./data`) |
| `--size` | Image size: `full_res` (default), `large`, `medium`, `small` |
| `--no-images` | Skip raw images |
| `--no-map` | Skip flight path, waypoints and map |
| `--no-render` | Save GeoJSON/CSV but skip the map PNG |
| `--list-flights` | Print all flights with their Sols and exit |
| `--name-by` | Output folder name: `flight` (default, e.g. `flight_002`) or `sol` (e.g. `sol_0061`) |

## Output

```
data/
├── _cache/                          # cached flight path / waypoint GeoJSON
└── flight_002/                      # Flight 2 (Sol 61); sol_NNNN for a sol with no flight
    ├── navcam/
    │   ├── images/                  # ONLY images, named <capture SCLK>.png  -> VGGT-SLAM --image_folder
    │   ├── frames.csv               # timestamp <-> NASA filename, image id, nav-cam frame counter
    │   ├── metadata.json            # full raw-image API record per image
    │   ├── intrinsics.json          # CAHVORE model + pinhole approximation (fx, fy, cx, cy)
    │   ├── gt_path_tum.txt          # flight path at each image time (TUM format, see caveats)
    │   └── cahv_poses_tum.txt       # per-image pose from the API camera model (TUM, UNVERIFIED)
    ├── colorcam/                    # same layout (4208x3120 RGB, only a few frames per flight)
    └── map/
        ├── flight_path.geojson      # flight trajectory (lon, lat)
        ├── flight_path.csv
        ├── flight_path_local_tum.txt  # whole path in local east/north meters, assumed timestamps
        ├── waypoints.geojson        # takeoff and landing points with flight stats
        ├── waypoints.csv
        └── flight_map_flight_002.png  # basemap with flight path
```

- Each sol has at most one flight, so folders are named by flight number. A sol without a flight (e.g. images only) falls back to `sol_NNNN`. Folders from the other naming scheme are renamed on the next run, so nothing is downloaded twice.
- Files that already exist are skipped, so you can rerun the script to resume an interrupted download. Data downloaded by older versions (NASA filenames in `navcam/`) is renamed into `images/` on the next run.
- On the map, **green** marks the takeoff point, **red** marks the landing point, and **cyan** is the flight path.

## Using the data with VGGT-SLAM 2.0

```bash
python main.py --image_folder data/flight_002/navcam/images --log_results --log_path flight002_poses.txt
evo_ape tum data/flight_002/navcam/gt_path_tum.txt flight002_poses.txt -as --plot
```

Why the images are renamed: VGGT-SLAM loads **every** file in `--image_folder` (so `metadata.json` must not be there), sorts frames by the **last** number in the filename and writes the **first** number as the TUM timestamp. With NASA names (`HNM_0061_0672355852_234ECM_..._0000LUJ01.png`), every timestamp would be `61` (the sol) and the frames would be sorted by the version suffix. With `<SCLK>.png`, the timestamps in VGGT-SLAM's `poses.txt` are capture times in spacecraft-clock seconds, the same clock used by `gt_path_tum.txt`, so `evo` matches poses directly.

The capture time comes from the filename, not from `extended.sclk` / `date_taken_utc` in the API, which are not in capture order. The filename SCLK increases with the nav-camera frame counter (`HELI00537`, …) at about 30 Hz.

### Caveats for pose evaluation

There is **no full 6-DoF ground truth** in these sources. What exists:

| File | What it is | Limits |
|---|---|---|
| `gt_path_tum.txt` | MMGIS flight path, interpolated at each image time, in local east/north meters | **No altitude** (z = 0) and **no orientation** (identity quaternion). The path has no timestamps; its points are assumed evenly spaced over the flight's `SCLK_START`–`SCLK_END`. Outside that window the pose is held at the takeoff or landing point. Probably derived from the onboard navigation estimate, not an independent survey. |
| `cahv_poses_tum.txt` | Pose from each image's CAHVORE camera model (camera-to-world, OpenCV axes) | Does **not** match the flight path. On Flights 1–2, the hover height is about 2× the reported altitude (6.1 m vs 3 m, 10.1 m vs 5 m), the horizontal fit to the path leaves 0.72 m RMSE on a path about 2 m long, and the pose keeps drifting after landing. Use for sanity checks only. |
| `waypoints.csv` | Takeoff and landing positions, max altitude, final attitude quaternion | One pose per flight. |

Practical consequences:
- Only **translation APE after Sim(3) alignment** (`evo_ape ... -as`) is meaningful. Do not evaluate rotation or RPE against `gt_path_tum.txt`.
- Vertical motion counts as error against the z = 0 reference. Flights 1–3 are mostly vertical (takeoff, hover, landing over a few meters), so they are poor test cases. Longer flights with a long cruise at constant altitude give a far more meaningful horizontal evaluation. You can also evaluate only the cruise frames, or compare in the horizontal plane after alignment.
- Navigation-camera frames are sparse in this archive (about 1 frame every 5 s on early flights). `intrinsics.json` gives the camera model, which has strong fisheye distortion, if you want to compare it with VGGT's estimated intrinsics.

## Data Sources

| Data | Endpoint |
|---|---|
| Raw images | `https://mars.nasa.gov/rss/api/?feed=raw_images&category=ingenuity&...` |
| Flight path | `https://mars.nasa.gov/mmgis-maps/M20/Layers/json/m20_heli_flight_path.json` |
| Waypoints | `https://mars.nasa.gov/mmgis-maps/M20/Layers/json/m20_heli_waypoints.json` |
| Basemap tiles (TMS) | `https://mars.nasa.gov/mmgis-maps/M20/Layers/M20_HiRISE_RGB_CTX_mosaic_25cm_v7_Merge_LERC_clip/{z}/{x}/{y}.png` |

These are the same sources used by the [Raw Images page](https://mars.nasa.gov/mars2020/multimedia/raw-images/) and the [Mars 2020 location map](https://mars.nasa.gov/maps/location/?mission=M20). For the Sol of each flight, see the [Ingenuity flight log](https://science.nasa.gov/wp-content/uploads/2024/06/ingenuity-helicopter-flight-log.pdf), or run `--list-flights`.

## Notes

- Not every flight has color images. For example, Flight 1 (Sol 58) has Navigation Camera images only, so no `colorcam/` folder is created.
- Maps of very short flights (a few meters) are small and blurry, even at the basemap's highest zoom (19). For precise positions, use the coordinates in the GeoJSON or CSV files.
- Image credit: NASA/JPL-Caltech.
