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

## Output

```
data/
├── _cache/                          # cached flight path / waypoint GeoJSON
└── sol_0061/
    ├── navcam/                      # Navigation Camera images + metadata.json
    ├── colorcam/                    # Color Camera images + metadata.json
    └── map/
        ├── flight_path.geojson      # flight trajectory (lon, lat)
        ├── flight_path.csv
        ├── waypoints.geojson        # takeoff and landing points with flight stats
        ├── waypoints.csv
        └── flight_map_sol0061.png   # basemap with flight path
```

- `metadata.json` holds the full API record for each image (capture time, SCLK, camera attitude, etc.).
- Files that already exist are skipped, so you can rerun the script to resume an interrupted download.
- On the map, **green** marks the takeoff point, **red** marks the landing point, and **cyan** is the flight path.

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
