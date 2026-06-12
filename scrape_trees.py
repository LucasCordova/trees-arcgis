#!/usr/bin/env python3

from __future__ import annotations

import csv
import io
import sys

import numpy as np
import rasterio
import requests
from pyproj import Transformer

SERVICE_URL = (
    "https://gis.odf.oregon.gov/ags3/rest/services/"
    "LandUseLandCover/LULC_Willamette_Valley_Hardwood/ImageServer"
)

DATES = ("2008-10-01", "2009-04-01", "2009-10-01", "2010-04-01", "2010-10-01", "2011-04-01", "2011-10-01", "2012-04-01", "2012-10-01", "2013-04-01", "2013-10-01", "2014-04-01", "2014-10-01", "2015-04-01", "2015-10-01", "2016-04-01", "2016-10-01", "2017-04-01", "2017-10-01", "2018-04-01", "2018-10-01", "2019-04-01", "2019-10-01", "2020-04-01", "2020-10-01", "2021-04-01", "2021-10-01", "2022-04-01", "2022-10-01", "2023-04-01", "2023-10-01", "2024-04-01", "2024-10-01", "2025-04-01", "2025-10-01", "2026-04-01")

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
# Service access
# --------------------------------------------------------------------------- #

def export_raw_raster(
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
        f"{SERVICE_URL}/exportImage", params=params, timeout=REQUEST_TIMEOUT
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


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    extent = bbox_to_service_sr(BBOX)
    size = export_size(*extent)
    print(
        f"BBOX {BBOX} -> EPSG:{SERVICE_WKID} extent {extent} "
        f"({size[0]}x{size[1]} px @ {PIXEL_SIZE_M}m)"
    )

    all_rows: list[dict] = []
    for date in DATES:
        print(f"Querying date {date} ...")
        try:
            tiff_bytes = export_raw_raster(extent, size, date)
        except Exception as exc:  # noqa: BLE001 - report and continue other dates
            print(f"  ! failed for {date}: {exc}", file=sys.stderr)
            continue
        rows = raster_to_rows(tiff_bytes, date)
        print(f"  -> {len(rows)} classified pixels")
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
