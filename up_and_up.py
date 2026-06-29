#!/usr/bin/env python3
"""ArcGIS tree scraper that's totally on the up and up.

Prompts for a date range and step size, captures Map Viewer HTML snapshots
via Selenium, then runs scrape_trees.py on the results.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CAPTURE_SCRIPT = REPO_ROOT / "selenium" / "capture_snapshots.py"
SCRAPE_SCRIPT = REPO_ROOT / "scrape_trees.py"
TREES_CSV = REPO_ROOT / "trees.csv"
SNAPSHOT_DIR = REPO_ROOT / "snapshot"

# Repo has a selenium/ folder that shadows the pip package if cwd is the repo.
CAPTURE_CWD = "/tmp"

# Snapshots per Chrome session before we restart the browser (memory leaks are real).
SNAPSHOTS_PER_BATCH = 6


def _banner() -> None:
    print(
        """
┌─────────────────────────────────────────────────────────────┐
│  ArcGIS Scraper — Totally On The Up And Up™                 │
│  public data · polite HTTP · headless Chrome (pray for GL)  │
└─────────────────────────────────────────────────────────────┘
"""
    )


def _prompt_date(label: str) -> dt.date:
    while True:
        raw = input(f"{label} (YYYY-MM-DD): ").strip()
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            print("  nah, that's not a date. YYYY-MM-DD please.")


def _prompt_step_days() -> int:
    print(
        "How many days between snapshots?\n"
        "  7  = weekly (what we usually run)\n"
        "  14 = biweekly\n"
        "  30 = roughly monthly (ArcGIS doesn't care about your calendar)"
    )
    while True:
        raw = input("Iteration length in days: ").strip()
        try:
            step = int(raw)
        except ValueError:
            print("  need a whole number, boss.")
            continue
        if step < 1:
            print("  zero-day iterations are a philosophy problem, not a GIS one.")
            continue
        return step


def _iter_dates(start: dt.date, end: dt.date, step: int) -> list[dt.date]:
    out: list[dt.date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += dt.timedelta(days=step)
    return out


def _batch_ranges(dates: list[dt.date], size: int) -> list[tuple[dt.date, dt.date]]:
    batches: list[tuple[dt.date, dt.date]] = []
    for i in range(0, len(dates), size):
        chunk = dates[i : i + size]
        batches.append((chunk[0], chunk[-1]))
    return batches


def _run_streaming(cmd: list[str], *, cwd: str | None = None) -> int:
    """Run a subprocess and tee stdout/stderr to the terminal."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"  {line}", end="")
    return proc.wait()


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _existing_outputs() -> list[Path]:
    paths: list[Path] = []
    if SNAPSHOT_DIR.is_dir():
        paths.extend(sorted(SNAPSHOT_DIR.glob("*.html")))
        paths.extend(sorted(SNAPSHOT_DIR.glob("*.png")))
    if TREES_CSV.is_file():
        paths.append(TREES_CSV)
    return paths


def _maybe_clear_outputs() -> None:
    """Ask before deleting old snapshot files and trees.csv."""
    existing = _existing_outputs()
    if not existing:
        return

    snap_count = sum(1 for p in existing if p.parent == SNAPSHOT_DIR)
    has_csv = TREES_CSV in existing
    parts = []
    if snap_count:
        parts.append(f"{snap_count} file(s) in snapshot/")
    if has_csv:
        parts.append("trees.csv")
    print(f"\n  Already on disk: {', '.join(parts)}.")
    print(
        "  A fresh run will mix old and new data unless we wipe those first."
    )
    ans = input(
        "  Delete snapshot/ and trees.csv before capturing? [y/N]: "
    ).strip().lower()
    if ans not in ("y", "yes"):
        print("  Keeping old files — stale rows may linger in trees.csv.")
        return

    removed = 0
    for path in existing:
        path.unlink()
        removed += 1
    print(f"  cleared {removed} file(s). clean slate.\n")


def main() -> int:
    _banner()
    print("Phase 0: paperwork (you type, I judge quietly)\n")

    start = _prompt_date("Start date")
    end = _prompt_date("End date")
    if end < start:
        print("End date is before start date. Time travel module not installed.")
        return 1

    step = _prompt_step_days()
    dates = _iter_dates(start, end, step)
    batches = _batch_ranges(dates, SNAPSHOTS_PER_BATCH)

    print(
        f"\nPlan: {len(dates)} snapshot(s), {step}-day step, "
        f"{len(batches)} browser session(s), headless Chrome.\n"
        "Legal disclaimer: we're downloading public map pages and raster "
        "exports like a normal person with a bbox.\n"
    )

    if input("Look good? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("Aborted. The trees remain un-scraped.")
        return 0

    _maybe_clear_outputs()

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    capture_failures = 0

    print("\n── Phase 1: Selenium snapshot capture ──")
    print("(if WebGL dies, blame headless mode, not me)\n")

    for i, (batch_start, batch_end) in enumerate(batches, start=1):
        print(
            f"[batch {i}/{len(batches)}] "
            f"{batch_start} → {batch_end} "
            f"(elapsed {_fmt_duration(time.time() - t0)})"
        )
        cmd = [
            sys.executable,
            str(CAPTURE_SCRIPT),
            "--start",
            batch_start.isoformat(),
            "--end",
            batch_end.isoformat(),
            "--step",
            str(step),
            "--headless",
            "--overwrite",
        ]
        rc = _run_streaming(cmd, cwd=CAPTURE_CWD)
        if rc != 0:
            capture_failures += 1
            print(f"  ! batch {i} exited {rc} — moving on anyway\n")
        else:
            print(f"  batch {i} done.\n")

    missing = [
        d for d in dates
        if not (SNAPSHOT_DIR / f"{d.isoformat()}.html").is_file()
    ]
    if missing:
        print(
            f"  ! {len(missing)} snapshot(s) still missing after capture "
            f"(first few: {', '.join(d.isoformat() for d in missing[:5])})"
        )
    else:
        print(f"  all {len(dates)} snapshot file(s) present. miracle achieved.")

    print("\n── Phase 2: scrape_trees.py (pixels → CSV) ──")
    print("(reading whatever landed in snapshot/)\n")
    scrape_cmd = [sys.executable, str(SCRAPE_SCRIPT)]
    scrape_rc = _run_streaming(scrape_cmd, cwd=str(REPO_ROOT))

    elapsed = _fmt_duration(time.time() - t0)
    csv_path = TREES_CSV

    print("\n── Debrief ──")
    if scrape_rc == 0 and csv_path.is_file():
        rows = sum(1 for _ in csv_path.open(encoding="utf-8")) - 1  # minus header
        print(f"  trees.csv: {rows:,} data row(s)")
    print(f"  capture batches with errors: {capture_failures}")
    print(f"  scrape_trees exit code: {scrape_rc}")
    print(f"  total wall time: {elapsed}")
    print("\nTotally on the up and up. Go touch grass.")

    return 1 if scrape_rc != 0 else (1 if capture_failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())
