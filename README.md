# isem-data

[![CI](https://github.com/Nabel7/isem-data/actions/workflows/ci.yml/badge.svg)](https://github.com/Nabel7/isem-data/actions/workflows/ci.yml)

Download and store data from the Irish electricity market (I-SEM) in one tidy format, ready for analysis and forecasting.

## What it collects

| Dataset | Series | Source | Resolution |
|---|---|---|---|
| `wind` | `wind_actual`, `wind_forecast` | EirGrid Smart Grid Dashboard | 15 min |
| `demand` | `demand_actual` | EirGrid Smart Grid Dashboard | 15 min |

All values are in MW for the whole island (Ireland and Northern Ireland). Day-ahead prices from SEMO are next.

## Quickstart

```bash
git clone https://github.com/Nabel7/isem-data.git
cd isem-data
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m isem_data                                     # the last 7 days
python -m isem_data --start 2026-10-01 --end 2026-10-31 # a date range
```

Data is saved to `data/wind.parquet` and `data/demand.parquet`:

```python
from isem_data import storage

wind = storage.load("wind")
```

```
              timestamp_utc region       series  value_mw
0 2026-10-19 23:00:00+00:00    ALL  wind_actual    1843.0
1 2026-10-19 23:15:00+00:00    ALL  wind_actual    1785.0
2 2026-10-19 23:30:00+00:00    ALL  wind_actual    1779.0
```

## How it works

```
isem_data/
├── eirgrid.py    # download from EirGrid and turn the JSON into a tidy table
├── storage.py    # save to / load from Parquet, without duplicate rows
└── __main__.py   # the command line: python -m isem_data
```

- **One tidy format.** Every dataset has the same four columns: `timestamp_utc`, `region`, `series`, `value_mw`. New sources only need to produce this shape.
- **UTC everywhere.** EirGrid reports Irish local time. On the night the clocks go back, 01:00–01:59 happens twice and the two hours can't be told apart, so those rows are dropped rather than guessed.
- **Safe to re-run.** Saving replaces rows with the same timestamp, region and series, so downloading the same days twice changes nothing. Newer downloads win, which matters because forecasts get revised.
- **Polite.** Requests go a week at a time, with a pause between them.

## Data sources and caveats

- **EirGrid Smart Grid Dashboard** ([smartgriddashboard.com](https://www.smartgriddashboard.com)): the JSON API behind its charts is public but not officially documented, so it may change without notice. Everything that depends on it is in `eirgrid.py`.
- Check each provider's terms before redistributing the data. This repo stores code, not data.

## Development

```bash
pre-commit install   # ruff lint + format on every commit
pytest -q            # tests use made-up data and never touch the internet
```

## Roadmap

- [ ] Day-ahead market prices from SEMO
- [ ] A scheduled GitHub Action that refreshes the data daily
- [ ] Query the data with DuckDB
- [ ] Wind and price forecasting on top of the data, in a separate repo

## License

MIT
