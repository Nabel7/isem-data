"""Summaries of dispatch-down for the tracker website, and a command that writes them.

python -m isem_data.tracker --out ../dispatch-down-tracker/src/data

The website only reads these small files, never the raw data:

| File               | One row per                         |
|--------------------|-------------------------------------|
| `monthly.csv`      | month and region (ALL, ROI, NI)     |
| `daily.csv`        | day and region                      |
| `farms.csv`        | wind farm, over the whole period    |
| `farm_monthly.csv` | wind farm and month                 |
| `farm_daily.csv`   | wind farm and day                   |
| `meta.json`        | when it was built, period covered, missing days, credit line |

Every table has the same measures:

    produced_mwh, constraint_mwh, curtailment_mwh, lost_mwh   energy
    dispatch_down_pct      lost / (produced + lost), as a percentage
    lost_value_eur         lost energy at the day-ahead price (market value, not the
                           owners' loss; can be negative)
    half_hours             farm half-hours behind the row
    held_down_half_hours   ...of which under a constraint or curtailment

Days and months are Irish clock time (Europe/Dublin), the way SEMO's trading
days and EirGrid's monthly figures run, not UTC.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import dispatch_down, storage

LOCAL_TIME = "Europe/Dublin"

# The credit the EirGrid Group Open Data Licence asks for.
CREDIT = "Supported by EirGrid Group Data"

MEASURES = [
    "produced_mwh",
    "constraint_mwh",
    "curtailment_mwh",
    "lost_mwh",
    "dispatch_down_pct",
    "lost_value_eur",
    "half_hours",
    "held_down_half_hours",
]


def with_local_dates(farms: pd.DataFrame) -> pd.DataFrame:
    """Add each half-hour's Irish calendar day ("2026-10-25") and month ("2026-10")."""
    local = farms["timestamp_utc"].dt.tz_convert(LOCAL_TIME)
    return farms.assign(day=local.dt.strftime("%Y-%m-%d"), month=local.dt.strftime("%Y-%m"))


def sums(farms: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Add up farm half-hours by `keys`: the same numbers as dispatch_down.totals, in MWh."""
    held_down = farms["reason"] != "none"
    parts = farms.assign(
        constraint_mwh=farms["lost_mwh"].where(farms["reason"] == "constraint", 0.0),
        curtailment_mwh=farms["lost_mwh"].where(farms["reason"] == "curtailment", 0.0),
        held_down=held_down,
    )
    if "lost_value_eur" not in parts:
        parts["lost_value_eur"] = float("nan")
    table = parts.groupby(keys).agg(
        produced_mwh=("produced_mwh", "sum"),
        constraint_mwh=("constraint_mwh", "sum"),
        curtailment_mwh=("curtailment_mwh", "sum"),
        lost_value_eur=("lost_value_eur", "sum"),
        half_hours=("reason", "size"),
        held_down_half_hours=("held_down", "sum"),
    )
    table["lost_mwh"] = table["constraint_mwh"] + table["curtailment_mwh"]
    available = table["produced_mwh"] + table["lost_mwh"]
    table["dispatch_down_pct"] = 100 * table["lost_mwh"] / available.where(available > 0)
    return table[MEASURES].reset_index()


def by_region(farms: pd.DataFrame, period: str) -> pd.DataFrame:
    """Sums per `period` ("day" or "month") for the whole island (ALL) and each region."""
    whole_island = sums(farms, [period]).assign(region="ALL")
    regions = sums(farms, [period, "region"])
    table = pd.concat([whole_island, regions], ignore_index=True)
    return table[[period, "region", *MEASURES]].sort_values([period, "region"])


def monthly(farms: pd.DataFrame) -> pd.DataFrame:
    """By month and region, with how many days of data each month has."""
    table = by_region(farms, "month")
    days = farms.groupby("month")["day"].nunique().rename("days_with_data")
    in_month = pd.PeriodIndex(days.index, freq="M").days_in_month
    days = days.to_frame().assign(days_in_month=in_month)
    return table.merge(days, left_on="month", right_index=True)


def daily(farms: pd.DataFrame) -> pd.DataFrame:
    return by_region(farms, "day")


def farm_list(farms: pd.DataFrame, units: pd.DataFrame) -> pd.DataFrame:
    """One row per wind farm over the whole period, with its company, worst first."""
    table = sums(farms, ["unit"])
    seen = farms.groupby("unit").agg(
        region=("region", "first"), first_day=("day", "min"), last_day=("day", "max")
    )
    owners = units.drop_duplicates("unit").set_index("unit")[["company"]]
    table = table.join(seen, on="unit").join(owners, on="unit")
    columns = ["unit", "company", "region", "first_day", "last_day", *MEASURES]
    return table[columns].sort_values(["lost_mwh", "unit"], ascending=[False, True])


def farm_monthly(farms: pd.DataFrame) -> pd.DataFrame:
    return sums(farms, ["unit", "month"])


def farm_daily(farms: pd.DataFrame) -> pd.DataFrame:
    return sums(farms, ["unit", "day"])


def missing_days(farms: pd.DataFrame) -> list[str]:
    """Days between the first and last with no data at all (e.g. no SEMO files)."""
    if farms.empty:
        return []
    days = pd.to_datetime(farms["day"].unique())
    expected = pd.date_range(days.min(), days.max(), freq="D")
    return [day.strftime("%Y-%m-%d") for day in expected.difference(days)]


def meta(farms: pd.DataFrame) -> dict:
    return {
        "built_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_day": farms["day"].min(),
        "last_day": farms["day"].max(),
        "missing_days": missing_days(farms),
        "farms": int(farms["unit"].nunique()),
        "credit": CREDIT,
        "note": (
            "Estimates from public SEMO data, calibrated against EirGrid's official monthly "
            "figures. lost_value_eur is the day-ahead market value of the lost energy, "
            "not the owners' loss. Independent; not affiliated with EirGrid, SONI or SEMO."
        ),
    }


def build(
    availability: pd.DataFrame,
    metered: pd.DataFrame,
    instructions: pd.DataFrame,
    units: pd.DataFrame,
    prices: pd.DataFrame,
) -> dict[str, pd.DataFrame | dict]:
    """Every file's contents, keyed by file name."""
    farms = dispatch_down.farm_half_hours(availability, metered, instructions, units)
    farms = dispatch_down.add_market_value(farms, prices)
    farms = with_local_dates(farms)
    return {
        "monthly.csv": monthly(farms),
        "daily.csv": daily(farms),
        "farms.csv": farm_list(farms, units),
        "farm_monthly.csv": farm_monthly(farms),
        "farm_daily.csv": farm_daily(farms),
        "meta.json": meta(farms),
    }


def write(files: dict[str, pd.DataFrame | dict], out_dir: Path) -> list[Path]:
    """Write the files, rounded so they stay small: 3 decimals, 2 for euros."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in files.items():
        path = out_dir / name
        if isinstance(content, dict):
            path.write_text(json.dumps(content, indent=2) + "\n")
        else:
            rounded = content.round(3).assign(lost_value_eur=content["lost_value_eur"].round(2))
            rounded.to_csv(path, index=False)
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the dispatch-down tracker's data files.")
    parser.add_argument("--out", type=Path, required=True, help="folder to write the files into")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=storage.DATA_DIR,
        help=f"where the downloaded datasets are (default: {storage.DATA_DIR})",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.expanduser()
    files = build(
        storage.load("availability", data_dir),
        storage.load("metered", data_dir),
        storage.load("dispatch_instructions", data_dir),
        storage.load("units", data_dir),
        storage.load("prices", data_dir),
    )
    for path in write(files, args.out.expanduser()):
        size = path.stat().st_size / 1000
        print(f"{path}  ({size:,.0f} kB)")
    info = files["meta.json"]
    print(f"{info['first_day']} to {info['last_day']}, {info['farms']} farms")
    if info["missing_days"]:
        print(f"warning: no data for {', '.join(info['missing_days'])}")


if __name__ == "__main__":
    main()
