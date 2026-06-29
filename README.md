# trees-arcgis

Willamette Valley hardwood land-cover snapshots from ArcGIS Map Viewer, scraped to CSV.

## Setup

You need Python 3.9+ and Google Chrome installed (Selenium Manager fetches chromedriver).

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run everything (recommended)

Interactive driver that asks for dates, step size, then captures snapshots and builds `trees.csv`:

```bash
python up_and_up.py
```

You'll be prompted for:

- **Start date** and **end date** (`YYYY-MM-DD`)
- **Iteration length in days** — e.g. `7` for weekly, `30` for roughly monthly

It runs headless Chrome in batches (with progress on stdout), optionally clears `snapshot/` and `trees.csv` first, then calls `scrape_trees.py` on whatever HTML landed there.

## Run pieces separately

### 1. Capture HTML snapshots (Selenium)

The script lives in `selenium/` which shadows the pip `selenium` package if you run from the repo root. Run it from `/tmp` (or anywhere outside the repo):

```bash
cd /tmp
python /path/to/trees-arcgis/selenium/capture_snapshots.py \
  --start 2024-01-01 --end 2024-03-01 --step 7 --headless --overwrite
```

Output: `snapshot/<YYYY-MM-DD>.html`

For debugging the Map Viewer UI (visible browser):

```bash
cd /tmp
python /path/to/trees-arcgis/selenium/capture_snapshots.py --debug
```

### 2. Scrape snapshots to CSV

Processes every `snapshot/YYYY-MM-DD.html` on disk (no hardcoded date list):

```bash
python scrape_trees.py
```

Optional: only dates in a range (must match files you captured):

```bash
python scrape_trees.py --start 2024-01-01 --end 2024-03-01 --step 7
```

Output: `trees.csv` with columns `date`, `tree_type`, `class_value`, `latitude`, `longitude`.

## Notes

- Headless capture often fails WebGL on ArcGIS; `up_and_up.py` uses headless anyway. If batches fail, try capture without `--headless`.
- Snapshots must include the layer time controls (~200KB+ HTML). Tiny files mean the map didn't load.
- BBOX and class names are hardcoded in `scrape_trees.py` for a small Corvallis-ish window.
