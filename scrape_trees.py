#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import re
import sys

import numpy as np
import rasterio
import requests
from pyproj import Transformer

# Folder of saved ArcGIS Map Viewer snapshots, one HTML file per date named
# "<date>.html" (e.g. snapshot/2008-10-01.html). scrape_trees reads whatever
# is in here unless --start/--end/--step pin a subset.
SNAPSHOT_DIR = "snapshot"

# Fallback service URL, used only if a snapshot does not reference a resolvable
# portal item / service.
SERVICE_URL = (
    "https://gis.odf.oregon.gov/ags3/rest/services/"
    "LandUseLandCover/LULC_Willamette_Valley_Hardwood/ImageServer"
)

BBOX = (-123.045, 44.940, -123.034, 44.949)


# Native grid spatial reference and pixel size of the service.
SERVICE_WKID = 32610  # UTM zone 10N
PIXEL_SIZE_M = 20.0

VALUE_TO_CLASSNAME = {
    1: "Bigleaf Maple",
    2: "Black Cottonwood",
    3: "Conifers",
    4: "Corylus avellana",
    5: "Oregon Ash",
    6: "Oregon White Oak",
    7: "Red Alder",
    8: "Urban Street Tree",
    9: "Burn Scar",
}

OUTPUT_CSV = "trees.csv"

# Guardrail: the service caps export dimensions at 100000 px per side.
MAX_EXPORT_DIM = 100000

# HTTP timeout (seconds) for the export request.
REQUEST_TIMEOUT = 120


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def bbox_to_service_sr(bbox_wgs84: tuple[float, float, float, float]):


    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    to_service = Transformer.from_crs(4326, SERVICE_WKID, always_xy=True)

    # Project all four corners and take the extremes (handles UTM skew safely).
    xs, ys = to_service.transform(
        [min_lon, max_lon, min_lon, max_lon],
        [min_lat, max_lat, max_lat, min_lat],
    )
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    # Snap outward to the pixel grid.
    xmin = np.floor(xmin / PIXEL_SIZE_M) * PIXEL_SIZE_M
    ymin = np.floor(ymin / PIXEL_SIZE_M) * PIXEL_SIZE_M
    xmax = np.ceil(xmax / PIXEL_SIZE_M) * PIXEL_SIZE_M
    ymax = np.ceil(ymax / PIXEL_SIZE_M) * PIXEL_SIZE_M
    return xmin, ymin, xmax, ymax


def export_size(xmin: float, ymin: float, xmax: float, ymax: float) -> tuple[int, int]:
    """Compute (width, height) in pixels for the snapped extent."""
    width = int(round((xmax - xmin) / PIXEL_SIZE_M))
    height = int(round((ymax - ymin) / PIXEL_SIZE_M))
    if width <= 0 or height <= 0:
        raise ValueError(f"Degenerate export size: {width}x{height}. Check BBOX.")
    if width > MAX_EXPORT_DIM or height > MAX_EXPORT_DIM:
        raise ValueError(
            f"Export size {width}x{height} exceeds service cap of "
            f"{MAX_EXPORT_DIM}px per side. Use a smaller BBOX or tile the request."
        )
    return width, height


# --------------------------------------------------------------------------- #
# Snapshot resolution
# --------------------------------------------------------------------------- #

# Matches the "saved from url" comment in a saved Map Viewer page, capturing the
# portal host and the 32-char portal item id from `...?layers=<itemid>`.
_SNAPSHOT_SRC_RE = re.compile(
    r"(https?://[^/\s]+)/apps/mapviewer/index\.html\?layers=([0-9a-fA-F]{32})"
)

# Cache of portal item id -> resolved service URL, to avoid repeat lookups.
_SERVICE_URL_CACHE: dict[str, str] = {}


def snapshot_path_for_date(date: str) -> str:
    """Return the snapshot file path for a date (snapshot/<date>.html)."""
    return os.path.join(SNAPSHOT_DIR, f"{date}.html")


def service_url_from_snapshot(path: str) -> str:
    """Derive the Image Service URL referenced by a saved Map Viewer snapshot.

    Reads the portal host and item id from the snapshot's "saved from url" comment,
    then resolves the item to its service URL via the portal item endpoint. Falls
    back to the module-level SERVICE_URL if anything cannot be resolved.
    """
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        # The reference lives in a comment near the top of the file.
        head = fh.read(8192)

    match = _SNAPSHOT_SRC_RE.search(head)
    if not match:
        print(
            f"  ! no portal item reference found in {path}; using fallback SERVICE_URL",
            file=sys.stderr,
        )
        return SERVICE_URL

    portal_host, item_id = match.group(1), match.group(2)
    if item_id in _SERVICE_URL_CACHE:
        return _SERVICE_URL_CACHE[item_id]

    item_url = f"{portal_host}/sharing/rest/content/items/{item_id}"
    try:
        resp = requests.get(
            item_url, params={"f": "json"}, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        service_url = resp.json().get("url")
    except Exception as exc:  # noqa: BLE001 - fall back and report
        print(
            f"  ! failed to resolve item {item_id} from {portal_host}: {exc}; "
            f"using fallback SERVICE_URL",
            file=sys.stderr,
        )
        service_url = None

    if not service_url:
        service_url = SERVICE_URL
    _SERVICE_URL_CACHE[item_id] = service_url
    return service_url


# --------------------------------------------------------------------------- #
# Service access
# --------------------------------------------------------------------------- #

def export_raw_raster(
    service_url: str,
    extent: tuple[float, float, float, float],
    size: tuple[int, int],
    date: str,
) -> bytes:
    """Call exportImage and return the raw (un-symbolized) class-value GeoTIFF bytes."""
    xmin, ymin, xmax, ymax = extent
    width, height = size
    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": SERVICE_WKID,
        "imageSR": SERVICE_WKID,
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "U8",
        "interpolation": "RSP_NearestNeighbor",
        # "None" returns source pixel values (1-9) instead of RGB symbology.
        "renderingRule": '{"rasterFunction":"None"}',
        # No-op on this non-time-enabled service; kept per the requested design.
        "time": date,
        "f": "image",
    }
    resp = requests.get(
        f"{service_url}/exportImage", params=params, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "image" not in content_type:
        # The service returns JSON (not an image) when something is wrong.
        raise RuntimeError(
            f"exportImage did not return an image (Content-Type={content_type!r}). "
            f"Response head: {resp.content[:500]!r}"
        )
    return resp.content


# --------------------------------------------------------------------------- #
# Pixel -> point extraction
# --------------------------------------------------------------------------- #

def raster_to_rows(tiff_bytes: bytes, date: str) -> list[dict]:
    """Convert classified pixels in a GeoTIFF to (date, tree_type, value, lat, lon) rows."""
    rows: list[dict] = []
    with rasterio.open(io.BytesIO(tiff_bytes)) as src:
        band = src.read(1)
        transform = src.transform

        # Reproject pixel centers from the raster CRS to WGS84.
        src_crs = src.crs.to_epsg() if src.crs else SERVICE_WKID
        to_wgs84 = Transformer.from_crs(src_crs, 4326, always_xy=True)

        # Only iterate pixels whose value is a known class (skip background/NoData).
        class_values = np.array(sorted(VALUE_TO_CLASSNAME), dtype=band.dtype)
        mask = np.isin(band, class_values)
        rows_idx, cols_idx = np.nonzero(mask)
        if rows_idx.size == 0:
            return rows

        # Pixel centers in raster CRS (offset by 0.5 px from the upper-left corner).
        xs, ys = rasterio.transform.xy(
            transform, rows_idx, cols_idx, offset="center"
        )
        lons, lats = to_wgs84.transform(np.asarray(xs), np.asarray(ys))

        values = band[rows_idx, cols_idx]
        for value, lat, lon in zip(values, lats, lons):
            value = int(value)
            rows.append(
                {
                    "date": date,
                    "tree_type": VALUE_TO_CLASSNAME[value],
                    "class_value": value,
                    "latitude": round(float(lat), 7),
                    "longitude": round(float(lon), 7),
                }
            )
    return rows


_DATE_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def list_snapshot_dates() -> list[str]:
    """Every YYYY-MM-DD.html in SNAPSHOT_DIR, sorted."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return []
    out: list[str] = []
    for entry in os.scandir(SNAPSHOT_DIR):
        if not entry.name.endswith(".html"):
            continue
        stem = entry.name[: -len(".html")]
        if _DATE_STEM_RE.fullmatch(stem):
            out.append(stem)
    return sorted(out)


def dates_for_run(
    start: dt.date | None,
    end: dt.date | None,
    step_days: int | None,
) -> list[str]:
    """Explicit --start/--end/--step range, or every snapshot/*.html on disk."""
    if start is not None and end is not None and step_days is not None:
        out: list[str] = []
        cur = start
        while cur <= end:
            out.append(cur.isoformat())
            cur += dt.timedelta(days=step_days)
        return out

    found = list_snapshot_dates()
    if not found:
        print(
            f"No snapshot/*.html files in {SNAPSHOT_DIR!r}. "
            "Run capture_snapshots.py first.",
            file=sys.stderr,
        )
    return found


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export classified tree pixels to trees.csv.")
    p.add_argument("--start", type=dt.date.fromisoformat, default=None)
    p.add_argument("--end", type=dt.date.fromisoformat, default=None)
    p.add_argument("--step", type=int, default=None, help="Days between dates (with --start/--end).")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if (args.start, args.end, args.step).count(None) not in (0, 3):
        print("Pass all of --start, --end, and --step, or none of them.", file=sys.stderr)
        return 2
    if args.step is not None and args.step < 1:
        print("--step must be >= 1", file=sys.stderr)
        return 2

    dates = dates_for_run(args.start, args.end, args.step)
    if not dates:
        return 1
    print(f"Processing {len(dates)} date(s) …")

    extent = bbox_to_service_sr(BBOX)
    size = export_size(*extent)
    print(
        f"BBOX {BBOX} -> EPSG:{SERVICE_WKID} extent {extent} "
        f"({size[0]}x{size[1]} px @ {PIXEL_SIZE_M}m)"
    )

    all_rows: list[dict] = []
    for date in dates:
        snapshot_path = snapshot_path_for_date(date)
        if not os.path.isfile(snapshot_path):
            print(f"Skipping {date}: no snapshot at {snapshot_path}")
            continue

        print(f"Processing {date} from {snapshot_path} ...")
        service_url = service_url_from_snapshot(snapshot_path)
        try:
            tiff_bytes = export_raw_raster(service_url, extent, size, date)
        except Exception as exc:  # noqa: BLE001 - report and continue other dates
            print(f"  ! failed for {date}: {exc}", file=sys.stderr)
            continue
        rows = raster_to_rows(tiff_bytes, date)
        print(f"  -> {len(rows)} classified pixels (service: {service_url})")
        all_rows.extend(rows)

    fieldnames = ["date", "tree_type", "class_value", "latitude", "longitude"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Wrote {len(all_rows)} rows to {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
