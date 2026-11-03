"""Command line entry point.

python -m isem_data                                   # the last 7 days
python -m isem_data --start 2026-10-01 --end 2026-10-31
python -m isem_data --datasets wind
"""

import argparse
from datetime import date, timedelta

from . import eirgrid, storage


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Irish grid data into data/.")
    parser.add_argument("--start", type=date.fromisoformat, help="first day, e.g. 2026-10-01")
    parser.add_argument("--end", type=date.fromisoformat, help="last day (default: yesterday)")
    parser.add_argument(
        "--datasets", nargs="+", choices=list(eirgrid.DATASETS), default=list(eirgrid.DATASETS)
    )
    args = parser.parse_args()

    end = args.end or date.today() - timedelta(days=1)
    start = args.start or end - timedelta(days=6)

    for dataset in args.datasets:
        df = eirgrid.fetch(dataset, start, end)
        path = storage.save(df, dataset)
        print(f"{dataset}: {len(df)} rows for {start} to {end} -> {path}")


if __name__ == "__main__":
    main()
