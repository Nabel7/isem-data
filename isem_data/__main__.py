"""Command line entry point.

python -m isem_data                                   # the last 7 days
python -m isem_data --start 2026-10-01 --end 2026-10-31
python -m isem_data --datasets wind
python -m isem_data --datasets prices --start 2025-11-07   # back-fill SEMO's year of prices
python -m isem_data --datasets orders --start 2025-11-07   # ...and of company orders
python -m isem_data --datasets dispatch_instructions availability metered units --start 2026-08-07
                                                      # SEMO's per-unit reports (~2-3 months online)
python -m isem_data --datasets farm_names osm_wind_farms  # farm names and map positions;
                                                      # not in the daily run: refresh now and then
python -m isem_data --datasets wind_unit_forecasts --start 2026-08-11
                                                      # every version of SEMO's per-farm
                                                      # wind forecasts (~90 days online)
python -m isem_data --datasets entsoe_prices --start 2025-11-07
                                                      # ENTSO-E's hourly day-ahead prices,
                                                      # up to tomorrow (needs a token)
python -m isem_data --datasets entsoe_wind_forecasts   # ENTSO-E's wind forecast, a few days
                                                      # ahead; each run keeps what changed
python -m isem_data --data-dir ~/grid-data            # save somewhere else
"""

import argparse
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

from . import balancing, eirgrid, entsoe, entsoe_wind, forecasts, locations, semo, storage

# Every dataset the command can download: EirGrid's, SEMO's day-ahead prices and
# company orders (both read from the same SEMO files), and SEMO's per-unit
# balancing reports (dispatch instructions, availability, metered output, unit list),
# and ENTSO-E's copy of the day-ahead prices, which has tomorrow's a day early.
DATASETS = [
    *eirgrid.DATASETS,
    "wind_regions",
    "prices",
    "orders",
    *balancing.DATASETS,
    forecasts.DATASET,
    entsoe.DATASET,
    entsoe_wind.DATASET,
]

# Reference data that barely changes, so it's left out of the default (daily) run:
# SEMO's names for unit codes, and wind farm positions from OpenStreetMap.
REFERENCE = {
    "farm_names": (locations.fetch_names, ["unit"]),
    "osm_wind_farms": (locations.fetch_osm, ["osm_id"]),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Irish grid data as Parquet files.")
    parser.add_argument("--start", type=date.fromisoformat, help="first day, e.g. 2026-10-01")
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        help="last day (default: yesterday)",
    )
    parser.add_argument("--datasets", nargs="+", choices=[*DATASETS, *REFERENCE], default=DATASETS)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=storage.DATA_DIR,
        help=f"folder to save into (default: {storage.DATA_DIR})",
    )
    args = parser.parse_args()

    end = args.end or date.today() - timedelta(days=1)
    start = args.start or end - timedelta(days=6)
    if start > end:
        parser.error(f"start must be on or before end (got start={start}, end={end})")

    failed = run(args.datasets, start, end, args.data_dir.expanduser())
    if failed:
        print(f"{len(failed)} of {len(args.datasets)} dataset(s) failed: {', '.join(failed)}")
        sys.exit(1)


def run(datasets: list[str], start: date, end: date, data_dir: Path) -> list[str]:
    """Download each dataset; return the ones that failed.

    Anything that fails gets one more try at the end of the run. By then several minutes
    have passed, which is usually enough for a source that answered empty because it was
    asked too often (EirGrid's dashboard does this) or that had a moment's outage.
    """
    failed = try_each(datasets, start, end, data_dir, last_try=False)
    if failed:
        print(f"Trying again at the end: {', '.join(failed)}")
        failed = try_each(failed, start, end, data_dir, last_try=True)
    return failed


def try_each(datasets, start, end, data_dir, last_try: bool) -> list[str]:
    failed = []
    for dataset in datasets:
        # One dataset failing (a site being down, say) mustn't stop the others.
        try:
            summary = download(dataset, start, end, data_dir)
        except Exception as error:
            traceback.print_exc()
            print(f"FAILED {dataset}: {type(error).__name__}: {error}")
            if last_try:  # only a failure that stands goes into status.json
                storage.record_status(dataset, data_dir, error=f"{type(error).__name__}: {error}")
            failed.append(dataset)
            continue
        print(f"{dataset}: {summary}")
        storage.record_status(dataset, data_dir, start=str(start), end=str(end), result=summary)
    return failed


class NoData(RuntimeError):
    """A source answered, but with nothing for the whole date range."""


def download(dataset: str, start: date, end: date, data_dir: Path) -> str:
    """Download one dataset for `start` to `end`, save it, and say what was saved."""
    if dataset == forecasts.DATASET:
        added = forecasts.fetch_and_save(start, end, data_dir=data_dir)
        return f"{added} new version(s) for {start} to {end}"
    if dataset == entsoe_wind.DATASET:
        # Forecasts run days ahead, and only what changed since the last run is kept, so
        # the record says when each value was first seen (see entsoe_wind.py).
        end = entsoe_wind.default_end(end)
        added = entsoe_wind.fetch_and_save(start, end, data_dir=data_dir)
        return f"{added} new or changed hourly value(s) for {start} to {end}"
    if dataset in REFERENCE:
        fetch, key = REFERENCE[dataset]
        df = fetch()
        path = storage.save(df, dataset, data_dir=data_dir, key=key)
        return f"{len(df)} rows -> {path}"
    key = storage.KEY
    if dataset == "wind_regions":
        df = eirgrid.fetch_wind_regions(start, end)
    elif dataset == "prices":
        df = semo.fetch(start, end)
    elif dataset == entsoe.DATASET:
        # ENTSO-E has the next day's prices from the afternoon before, so its range runs
        # on to tomorrow (and never further: later days can't have been published).
        end = min(end + timedelta(days=2), date.today() + timedelta(days=1))
        df = entsoe.fetch(start, end)
    elif dataset == "orders":
        df = semo.fetch(start, end, parse=semo.parse_orders)
        key = semo.ORDER_KEY  # orders are identified differently from the tidy datasets
    elif dataset in balancing.REPORTS:
        report = balancing.REPORTS[dataset]
        df = semo.fetch(
            start,
            end,
            parse=balancing.parser(dataset),
            files=report["files"],
            lag_days=report.get("lag_days", 0),
        )
        key = report["key"]
    else:
        df = eirgrid.fetch(dataset, start, end)
    if df.empty:
        # Every dated dataset has data for every day, so nothing at all for the range
        # means the source failed quietly. Count it as a failure, so status.json and the
        # daily script's notification say so, rather than as a download of 0 rows.
        raise NoData(f"no rows at all for {start} to {end}")
    path = storage.save(df, dataset, data_dir=data_dir, key=key)
    return f"{len(df)} rows for {start} to {end} -> {path}"


if __name__ == "__main__":
    main()
