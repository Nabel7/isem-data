# isem-data

[![CI](https://github.com/Nabel7/isem-data/actions/workflows/ci.yml/badge.svg)](https://github.com/Nabel7/isem-data/actions/workflows/ci.yml)

Download and store data from the Irish electricity market (I-SEM) in one tidy format, ready for analysis and forecasting.

## What it collects

| Dataset | Series | Source | Resolution |
|---|---|---|---|
| `wind` | `wind_actual_mw`, `wind_forecast_mw` | EirGrid Smart Grid Dashboard | 15 min |
| `wind_regions` | the same as `wind`, for `ROI` and `NI` separately | EirGrid Smart Grid Dashboard | 15 min |
| `demand` | `demand_actual_mw` | EirGrid Smart Grid Dashboard | 15 min |
| `prices` | `dam_price_eur_mwh`, `dam_volume_mw` | SEMO day-ahead market results | 30 min |
| `orders` | each company's cleared orders (see below) | SEMO day-ahead market results | 30 min |
| `dispatch_instructions` | every instruction EirGrid gave each unit (see below) | SEMO report BM-037 | per instruction |
| `availability` | MW each unit could have produced | SEMO report BM-101 | 30 min |
| `metered` | MWh each unit actually produced in each half-hour | SEMO report BM-086 | 30 min |
| `units` | each unit's company, type, region and fuel (e.g. `WIND`) | SEMO report BM-091 | latest list |
| `wind_unit_forecasts` | EirGrid's wind forecast for each unit, **every version as published** (see below) | SEMO report BM-012 | 15 min, ~4 versions a day |

EirGrid values are for the whole island (Ireland and Northern Ireland), except `wind_regions`. Day-ahead prices and volumes are per bidding zone, `ROI` and `NI`.

SEMO only keeps about a year of day-ahead files online, so back-fill early:

```bash
python -m isem_data --datasets prices orders --start 2025-11-07   # about 8 minutes each for a year
```

SEMO posts each delivery day's day-ahead results file at about 01:00 the *following* day, so yesterday is the newest day the daily run can get. SEMO's aggregated bid and ask curves for tomorrow (`BidAskCurves_SEM-DA`) do appear around 13:40 on the day of the auction; they are not downloaded yet.

### Per-farm wind forecasts, as published

`wind_unit_forecasts` saves SEMO's *Four Day Rolling Wind Power Unit Forecast* (BM-012): EirGrid's forecast for every wind and solar unit, every 15 minutes, up to about four days ahead. A new version comes out about four times a day, and SEMO keeps only about 90 days online, so the daily run saves every version, tagged with `published_utc`. That makes fair forecast scoring possible later: you can ask what the forecast said *before* the event.

Each version is a ~35 MB file, so versions already saved are never downloaded again. They are kept as one Parquet file per publication day in `data/wind_unit_forecasts/` (about 1.5 MB a day); `forecasts.load()` reads them all. To collect everything still online:

```bash
python -m isem_data --datasets wind_unit_forecasts --start 2026-08-11   # ~360 files, about 20 minutes
```

### Company orders

`orders` has one row per company order and half-hour: how much each order bought or sold at the day-ahead clearing price. These are the **cleared results, not the bids**: the prices companies offered aren't published here.

| Column | Example | Meaning |
|---|---|---|
| `timestamp_utc` | 2026-11-01 23:00 UTC | start of the half-hour |
| `region` | `ROI`, `NI` | bidding zone |
| `participant` | `SSE` | the company's short code in SEMO |
| `unit` | `GU_400770` | the unit; the prefix is its type: `GU` generator, `SU` supplier, `AU` trader (assetless), `DSU` demand side unit |
| `order_type` | `linear`, `scalable_complex` | the kind of order |
| `order_name` | `SSE-U2` | the order's name; one unit can have a linear and a scalable complex order with the same name |
| `quantity_mw` | `-50.5` | **+ bought, − sold** |

In every half-hour the buys add up exactly to the sells, which is a handy check on the data. A week is about 140,000 rows (0.27 MB as Parquet), so a year is about 7 million rows and roughly 14 MB.

### Per-unit balancing reports (dispatch-down)

Three SEMO reports show, for every unit, what it was told to do, what it could have produced and what it did produce. For a wind farm, **availability minus metered output** is roughly its dispatch-down: output EirGrid told it not to produce. The dispatch instructions say why.

| Dataset | Columns |
|---|---|
| `dispatch_instructions` | `timestamp_utc` (when it takes effect), `issued_utc`, `participant`, `unit`, `instruction_code`, `combination_code`, `instruction_mw` |
| `availability` | `timestamp_utc`, `participant`, `unit`, `availability_mw` |
| `metered` | `timestamp_utc`, `participant`, `unit`, `unit_type`, `region`, `metered_mwh` |
| `units` | `trade_date`, `participant`, `company`, `unit`, `unit_type`, `region`, `fuel_type` (one row per unit, the latest seen) |

Wind farm instructions have `instruction_code` `WIND`, and `combination_code` gives the reason: `LOCL` constraint on (the local grid can't carry the power), `LCLO` constraint lifted, `CURL` curtailment on (too much wind for the whole island), `CRLO` curtailment lifted. Units are anonymised codes like `GU_407510`, not wind farm names, but `units` says which company owns each one and its fuel type, so `fuel_type == "WIND"` picks out the wind farms.

**Watch the units.** SEMO calls the meter column `MeteredMW`, but it is energy per half-hour (MWh): a unit running steadily at 340 MW meters about 170. We store it as `metered_mwh`; multiply by 2 for the average MW over the half-hour, to compare with `availability_mw`.

**SEMO only keeps these files online for about 2-3 months**, so start the daily updates early and back-fill what is still there:

```bash
python -m isem_data --datasets dispatch_instructions availability metered units --start 2026-08-07
```

The metered and availability files appear about two days after the trading day. Each is roughly 17,000 rows a day.

### Calculating dispatch-down

`isem_data/dispatch_down.py` turns the four datasets into one row per wind farm and half-hour, with the energy produced, the energy lost and why:

```python
from isem_data import dispatch_down, storage

farms = dispatch_down.farm_half_hours(
    storage.load("availability"),
    storage.load("metered"),
    storage.load("dispatch_instructions"),
    storage.load("units"),
)
dispatch_down.totals(farms)  # GWh produced, lost to constraint / curtailment, %
farms.groupby("region").apply(dispatch_down.totals)  # the same, split by ROI and NI
```

Constraint and curtailment are tracked as two separate switches, since a farm can be under both; when both are on, the loss counts as a constraint. Energy only counts as lost while an instruction is in force. Checked against EirGrid's official monthly figures in [`notebooks/02_dispatch_down_checks.ipynb`](notebooks/02_dispatch_down_checks.ipynb): September 2026 all-island 24.3% against EirGrid's 22.7%.

`dispatch_down.add_market_value(farms, storage.load("prices"))` adds `lost_value_eur`: the lost energy at the day-ahead price for the farm's zone. That's the market value of the energy, not what the owner lost (some dispatch-down is compensated), and it can be negative.

### Farm names and positions

SEMO's per-unit data uses anonymous codes (`GU_405140`). `isem_data/locations.py` names them and puts them on a map:

1. **Names** from SEMO's own PDFs: the yearly loss adjustment factor (CLAF) publication for Irish units (with each unit's substation), and lists of registered units, which add Northern Ireland. Reading them needs `pdftotext` (`brew install poppler`).
2. **Positions** of wind farms from OpenStreetMap, through the Overpass API (retried with back-off when the public server is busy).
3. **Matching** each named wind unit to the OpenStreetMap farm with the most similar name ("Oweninney Windfarm 1" -> "Oweninny Wind Farm"). [`isem_data/corrections.csv`](isem_data/corrections.csv) fixes the ones names can't, e.g. Seecon and Uggool -> Galway Wind Park.

These change rarely, so they're not part of the daily run. Refresh them now and then:

```bash
python -m isem_data --datasets farm_names osm_wind_farms
```

In November 2026 this named 164 of 217 farms with data (82% of the energy) and placed 135 on the map. The rest are mostly suppliers' grouped units that don't name a farm. OpenStreetMap data is © OpenStreetMap contributors under the Open Database Licence.

### Data files for the tracker website

`isem_data/tracker.py` writes the small summary files a website reads, so the raw data never has to leave this machine:

```bash
python -m isem_data.tracker --out ../dispatch-down-tracker/src/dispatch-down/data
```

| File | One row per |
|---|---|
| `monthly.csv` | month and region (`ALL`, `ROI`, `NI`), with how many days of the month have data |
| `daily.csv` | day and region |
| `farms.csv` | wind farm over the whole period, with its company, worst first |
| `farm_monthly.csv` | wind farm and month |
| `farm_daily.csv` | wind farm and day |
| `farm_locations.csv` | wind farm: matched OpenStreetMap farm and position (needs the two datasets above) |
| `meta.json` | when it was built, the period, missing days, and the licence credits |
| `farms/<id>.json` | one farm's web page: its units, daily, monthly and hour-of-day figures, and its last 14 days half-hour by half-hour |

A farm page can cover several units: units matched to the same OpenStreetMap farm (phases such as Oweninny 1 and 2) share one. `farms.csv` says which page each unit is on (`farm_id`, `farm_name`). The farm pages are about 6 MB for two months; old ones are removed on each run.

`monthly.csv` and `daily.csv` describe the island's wind power, so they leave out solar farms (SEMO lists them as wind; `solar_units` finds them by name, or by having no availability at night), and their `produced_mwh` is all wind generation from EirGrid's readings. The units in SEMO's reports make only about 85-88% of it, and using their output alone overstated the percentage by about 2 points. `units_produced_mwh` keeps what those units made. The farm tables use each farm's own metered output.

Each table has `produced_mwh`, `constraint_mwh`, `curtailment_mwh`, `lost_mwh`, `dispatch_down_pct`, `lost_value_eur`, `half_hours` and `held_down_half_hours`. Days and months are Irish clock time, as SEMO's trading days and EirGrid's monthly figures are. Two months of data is under 1 MB; `farm_daily.csv` is most of it.

## Quickstart

```bash
git clone https://github.com/Nabel7/isem-data.git
cd isem-data
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m isem_data                                     # the last 7 days
python -m isem_data --start 2026-10-01 --end 2026-10-31 # a date range
```

Data is saved to `data/wind.parquet` and `data/demand.parquet` at the top of the project, whichever folder you run the command from. Use `--data-dir` to save somewhere else:

```python
from isem_data import storage

wind = storage.load("wind")
```

```
              timestamp_utc region          series   value
0 2026-10-19 23:00:00+00:00    ALL  wind_actual_mw  1843.0
1 2026-10-19 23:15:00+00:00    ALL  wind_actual_mw  1785.0
2 2026-10-19 23:30:00+00:00    ALL  wind_actual_mw  1779.0
```

## Exploring the data

[`notebooks/01_prices_and_drivers.ipynb`](notebooks/01_prices_and_drivers.ipynb) looks at a year of day-ahead prices next to wind and demand: how prices moved, what a typical day looks like, how much wind and net demand (demand minus wind) move the price, and what that means for forecasting.

[`notebooks/02_dispatch_down_checks.ipynb`](notebooks/02_dispatch_down_checks.ipynb) checks the per-unit balancing data: which units are wind farms, what units the numbers are in, whether the farms add up to EirGrid's island total, and a first estimate of dispatch-down (constraint vs curtailment, by month, region and farm) against outside figures.

```bash
pip install -e ".[notebooks]"
jupyter lab notebooks/
```

## Daily updates (macOS)

`scripts/daily_update.sh` downloads the last 7 days of every dataset. Going back a week means days the Mac was off are filled in on the next run, and re-downloading days that are already saved changes nothing.

To run it every day at 14:00 with macOS's built-in scheduler (launchd):

```bash
scripts/install_daily_update.sh
```

| To | Run |
|---|---|
| run it now | `launchctl kickstart gui/$(id -u)/com.isem-data.daily-update` |
| read the log | `tail -n 20 logs/daily_update.log` |
| check it is loaded | `launchctl print gui/$(id -u)/com.isem-data.daily-update \| head` |
| remove it | `launchctl bootout gui/$(id -u)/com.isem-data.daily-update` and delete `~/Library/LaunchAgents/com.isem-data.daily-update.plist` |

If the Mac is asleep at 14:00 the job runs when it wakes. If it is off for more than a week, catch up by hand with `python -m isem_data --start <first missed day>`.

### When something fails

- **A slow or unavailable site:** every request is tried up to four times, with a longer pause each time (`isem_data/web.py`).
- **A dataset that still fails:** the other datasets download anyway. The run then ends with `FAILED` in the log, a notification on the Mac and a non-zero exit code.
- **How stale is each dataset?** `data/status.json` records when each one last downloaded successfully, and its last error if any.
- **An interrupted save:** files are written beside the real one and swapped in, so a crash or full disk leaves the old file intact.

### Backing up the archive

SEMO deletes most of its reports after 2 to 3 months, so `data/` can't be downloaded again. `scripts/backup_data.sh` keeps a second copy somewhere that isn't this disk, and the daily update runs it after each download. Say where the copy goes, once:

```bash
echo "github:YourName/isem-data-archive" > .backup_to   # a private GitHub repository
# or
echo "/Volumes/MyDrive/isem-backup" > .backup_to        # a folder: an external or synced drive
```

For GitHub, create a private repository first (`gh repo create YourName/isem-data-archive --private --add-readme`); `gh` must be logged in. Files are attached to releases, which keeps them out of the repository's history:

| Release | What it holds |
|---|---|
| `latest` | every main dataset as it is now, replaced on each run |
| `forecasts-2026-11` (one per month) | the per-farm wind forecasts, one file per day; only new or changed days are sent |
| `weekly-2026-W46` (8 newest kept) | the main datasets once a week, in case a bad file replaces a good one in `latest` |

Each run ends by checking every file on GitHub against the local one. Until a place is set, each daily run ends with `NOT BACKED UP` in the log and a notification.

To restore onto a new machine:

```bash
gh release download latest -R YourName/isem-data-archive -D data
for month in 2026-08 2026-09 2026-10 2026-11; do   # every forecasts-* release
  gh release download forecasts-$month -R YourName/isem-data-archive -D data/wind_unit_forecasts
done
```

## How it works

```
isem_data/
├── eirgrid.py    # download from EirGrid and turn the JSON into a tidy table
├── semo.py       # list, download and read SEMO day-ahead results files
├── balancing.py  # read SEMO's per-unit balancing reports (dispatch-down)
├── dispatch_down.py  # per-farm, per-half-hour dispatch-down from those reports
├── tracker.py    # summaries for the tracker website: python -m isem_data.tracker --out <folder>
├── locations.py  # farm names (SEMO PDFs) and positions (OpenStreetMap), matched by name
├── corrections.csv  # hand fixes to that matching
├── storage.py    # save to / load from Parquet, without duplicate rows (each dataset has a key)
└── __main__.py   # the command line: python -m isem_data
```

- **One tidy format.** The market-wide datasets (wind, demand, prices) have the same four columns: `timestamp_utc`, `region`, `series`, `value`. The unit is part of the series name (`wind_actual_mw`), so MW, prices and volumes all fit the same shape. Per-unit datasets (orders and the balancing reports) keep their own columns, with the unit in the column name (`metered_mwh`).
- **UTC everywhere.** EirGrid reports Irish local time. On the night the clocks go back, 01:00–01:59 happens twice and the two hours can't be told apart, so those rows are dropped rather than guessed.
- **Gaps are reported, not hidden.** Readings that are missing or empty are left out of the table, and the download prints a warning for each stretch of missing readings, e.g. `warning: wind_actual_mw is missing 4 reading(s) from 2026-11-01 14:00:00+00:00 to 2026-11-01 14:45:00+00:00`. The repeated hour on the night the clocks go back always shows up here.
- **Safe to re-run.** Saving replaces rows with the same timestamp, region and series, so downloading the same days twice changes nothing. Newer downloads win, which matters because forecasts get revised.
- **Polite.** Requests go a week at a time, with a pause between them.

## Data sources and caveats

- **EirGrid Smart Grid Dashboard** ([smartgriddashboard.com](https://www.smartgriddashboard.com)): the JSON API behind its charts is public but not officially documented, so it may change without notice. Everything that depends on it is in `eirgrid.py`.
- **SEMO** ([sem-o.com](https://www.sem-o.com)): day-ahead results files, one per delivery day, listed through SEMO's public reports API for about a year before they drop off. A file appears in the list a day or two after its auction. Timestamps are already UTC, and prices use a comma as the decimal point. The per-unit balancing reports come through the same API as XML files, kept for only about 2-3 months; their times are UTC too (the day the clocks go back has 50 half-hours, none repeated). `tests/fixtures/` holds excerpts of real files for testing.
- Check each provider's terms before redistributing the data. This repo stores code, not data.

## Development

```bash
pre-commit install   # ruff lint + format on every commit
pytest -q            # tests use made-up data and never touch the internet
```

## Roadmap

- [x] Day-ahead market prices from SEMO
- [x] Company order results from the same SEMO files
- [x] Per-unit dispatch instructions, availability and metered output (dispatch-down)
- [x] Dispatch-down per farm and half-hour, checked against EirGrid's monthly figures
- [x] Summary files for a dispatch-down tracker website, with the market value of lost energy
- [x] Farm names and map positions for the wind units
- [x] Daily updates on a Mac (launchd)
- [ ] Daily updates in the cloud: a scheduled GitHub Action writing to object storage
- [ ] Query the data with DuckDB
- [ ] Wind and price forecasting on top of the data, in a separate repo

## License

MIT
