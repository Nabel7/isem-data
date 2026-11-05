# isem-data

[![CI](https://github.com/Nabel7/isem-data/actions/workflows/ci.yml/badge.svg)](https://github.com/Nabel7/isem-data/actions/workflows/ci.yml)

Download and store data from the Irish electricity market (I-SEM) in one tidy format, ready for analysis and forecasting.

## What it collects

| Dataset | Series | Source | Resolution |
|---|---|---|---|
| `wind` | `wind_actual_mw`, `wind_forecast_mw` | EirGrid Smart Grid Dashboard | 15 min |
| `demand` | `demand_actual_mw` | EirGrid Smart Grid Dashboard | 15 min |
| `prices` | `dam_price_eur_mwh`, `dam_volume_mw` | SEMO day-ahead market results | 30 min |
| `orders` | each company's cleared orders (see below) | SEMO day-ahead market results | 30 min |

EirGrid values are for the whole island (Ireland and Northern Ireland). Day-ahead prices and volumes are per bidding zone, `ROI` and `NI`.

SEMO only keeps about a year of day-ahead files online, so back-fill early:

```bash
python -m isem_data --datasets prices orders --start 2025-11-07   # about 8 minutes each for a year
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

## How it works

```
isem_data/
├── eirgrid.py    # download from EirGrid and turn the JSON into a tidy table
├── semo.py       # list, download and read SEMO day-ahead results files
├── storage.py    # save to / load from Parquet, without duplicate rows (each dataset has a key)
└── __main__.py   # the command line: python -m isem_data
```

- **One tidy format.** Every dataset has the same four columns: `timestamp_utc`, `region`, `series`, `value`. The unit is part of the series name (`wind_actual_mw`), so MW, prices and volumes all fit the same shape. New sources only need to produce it.
- **UTC everywhere.** EirGrid reports Irish local time. On the night the clocks go back, 01:00–01:59 happens twice and the two hours can't be told apart, so those rows are dropped rather than guessed.
- **Gaps are reported, not hidden.** Readings that are missing or empty are left out of the table, and the download prints a warning for each stretch of missing readings, e.g. `warning: wind_actual_mw is missing 4 reading(s) from 2026-11-01 14:00:00+00:00 to 2026-11-01 14:45:00+00:00`. The repeated hour on the night the clocks go back always shows up here.
- **Safe to re-run.** Saving replaces rows with the same timestamp, region and series, so downloading the same days twice changes nothing. Newer downloads win, which matters because forecasts get revised.
- **Polite.** Requests go a week at a time, with a pause between them.

## Data sources and caveats

- **EirGrid Smart Grid Dashboard** ([smartgriddashboard.com](https://www.smartgriddashboard.com)): the JSON API behind its charts is public but not officially documented, so it may change without notice. Everything that depends on it is in `eirgrid.py`.
- **SEMO** ([sem-o.com](https://www.sem-o.com)): day-ahead results files, one per delivery day, listed through SEMO's public reports API for about a year before they drop off. A file appears in the list a day or two after its auction. Timestamps are already UTC, and prices use a comma as the decimal point. `tests/fixtures/` holds the first lines of one real file for testing.
- Check each provider's terms before redistributing the data. This repo stores code, not data.

## Development

```bash
pre-commit install   # ruff lint + format on every commit
pytest -q            # tests use made-up data and never touch the internet
```

## Roadmap

- [x] Day-ahead market prices from SEMO
- [x] Company order results from the same SEMO files
- [x] Daily updates on a Mac (launchd)
- [ ] Daily updates in the cloud: a scheduled GitHub Action writing to object storage
- [ ] Query the data with DuckDB
- [ ] Wind and price forecasting on top of the data, in a separate repo

## License

MIT
