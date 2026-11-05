"""Command line entry point.

python -m isem_data                                   # the last 7 days
python -m isem_data --start 2026-10-01 --end 2026-10-31
python -m isem_data --datasets wind
python -m isem_data --datasets prices --start 2025-11-07   # back-fill SEMO's year of prices
python -m isem_data --data-dir ~/grid-data            # save somewhere else
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

from . import eirgrid, semo, storage

# Every dataset the command can download: EirGrid's, plus day-ahead prices from SEMO.
DATASETS = [*eirgrid.DATASETS, "prices"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Irish grid data as Parquet files.")
    parser.add_argument("--start", type=date.fromisoformat, help="first day, e.g. 2026-10-01")
    parser.add_argument("--end", type=date.fromisoformat, help="last day (default: yesterday)")
    parser.add_argument("--datasets", nargs="+", choices=DATASETS, default=DATASETS)
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

    for dataset in args.datasets:
        if dataset == "prices":
            df = semo.fetch(start, end)
        else:
            df = eirgrid.fetch(dataset, start, end)
        path = storage.save(df, dataset, data_dir=args.data_dir.expanduser())
        print(f"{dataset}: {len(df)} rows for {start} to {end} -> {path}")


if __name__ == "__main__":
    main()
