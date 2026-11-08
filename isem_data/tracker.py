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
| `farm_locations.csv` | wind farm: matched farm name and map position (if found) |
| `farms/<id>.json`  | farm page: units, daily, monthly, by hour of day, last 14 days |
| `meta.json`        | when it was built, period covered, missing days, credit lines |

A farm page can cover several units: units matched to the same OpenStreetMap farm
(phases such as Oweninny 1 and 2) share one. `farms.csv` says which page each unit
is on (`farm_id`, `farm_name`).

farm_locations.csv is only written when the farm_names and osm_wind_farms datasets
have been downloaded. It is built from OpenStreetMap, so it is shared under the
Open Database Licence with the credit "© OpenStreetMap contributors".

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
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import dispatch_down, locations, storage

LOCAL_TIME = "Europe/Dublin"

# The credit the EirGrid Group Open Data Licence asks for.
CREDIT = "Supported by EirGrid Group Data"
MAP_CREDIT = "Farm positions © OpenStreetMap contributors (ODbL)"

# How many days of half-hours each farm page shows.
RECENT_DAYS = 14

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


def farm_list(
    farms: pd.DataFrame, units: pd.DataFrame, names: pd.DataFrame | None = None
) -> pd.DataFrame:
    """One row per wind farm over the whole period, with its company, worst first.

    With `names` (the farm_names dataset), also SEMO's name for each unit and whether
    it is wind or solar (judged from that name)."""
    table = sums(farms, ["unit"])
    seen = farms.groupby("unit").agg(
        region=("region", "first"), first_day=("day", "min"), last_day=("day", "max")
    )
    owners = units.drop_duplicates("unit").set_index("unit")[["company"]]
    table = table.join(seen, on="unit").join(owners, on="unit")
    semo_names = pd.Series(dtype=str) if names is None else names.set_index("unit")["semo_name"]
    table["semo_name"] = [semo_names.get(unit, "") for unit in table["unit"]]
    table["technology"] = (
        table["semo_name"].str.lower().str.contains("solar").map({True: "solar", False: "wind"})
    )
    columns = ["unit", "semo_name", "technology", "company", "region", "first_day", "last_day"]
    return table[[*columns, *MEASURES]].sort_values(["lost_mwh", "unit"], ascending=[False, True])


def farm_locations(
    farms: pd.DataFrame, units: pd.DataFrame, names: pd.DataFrame, osm: pd.DataFrame
) -> pd.DataFrame:
    """Each farm with data: the OpenStreetMap farm it was matched to and its position."""
    found = locations.match(
        names, osm, sorted(farms["unit"].unique()), locations.read_corrections()
    )
    columns = ["unit", "farm_name", "lat", "lon", "capacity_mw", "located_by"]
    return found[columns]


def slug(name: str) -> str:
    """A web-address-safe version of a name: "Oweninny Wind Farm" -> "oweninny-wind-farm"."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def farm_groups(table: pd.DataFrame, places: pd.DataFrame | None = None) -> pd.DataFrame:
    """Which farm page each unit goes on: unit, farm_id, farm_name.

    `table` is farm_list's output; `places` is farm_locations's. A unit placed on the map
    takes the OpenStreetMap farm's name, so phases matched to the same farm share a page.
    Otherwise it takes SEMO's name for it, and failing that its unit code."""
    name = table["semo_name"].fillna("").where(table["semo_name"].fillna("") != "", table["unit"])
    if places is not None:
        placed = places[places["located_by"] != ""].set_index("unit")["farm_name"]
        name = table["unit"].map(placed).fillna(name)
    groups = pd.DataFrame({"unit": table["unit"].values, "farm_name": name.values})

    # Two different names could give the same id ("X Wind Farm", "X Wind-Farm"); number them.
    ids = {}
    for farm_name in sorted(groups["farm_name"].unique()):
        base = slug(farm_name) or "farm"
        farm_id, n = base, 1
        while farm_id in ids.values():
            n += 1
            farm_id = f"{base}-{n}"
        ids[farm_name] = farm_id
    return groups.assign(farm_id=groups["farm_name"].map(ids))[["unit", "farm_id", "farm_name"]]


def records(table: pd.DataFrame) -> list[dict]:
    """Rows as plain dicts for JSON: 3 decimals, missing values as null."""
    return json.loads(table.to_json(orient="records", double_precision=3))


def recent_half_hours(farms: pd.DataFrame, since: pd.Timestamp, until: pd.Timestamp) -> dict:
    """One farm's half-hours from `since` to `until` (UTC), as columns, for a chart.

    Every half-hour in the range is there, so `start` plus the position gives the time;
    half-hours with no data are null. `reason` is one letter per half-hour: C for
    constraint, U for curtailment, - for neither, ? for no data."""
    times = pd.date_range(since, until, freq="30min")
    if "farm_id" in farms:
        farms = farms.drop(columns="farm_id")
    by_time = farms.groupby("timestamp_utc").agg(
        available_mw=("availability_mw", "sum"),
        output_mw=("output_mw", "sum"),
        constrained=("reason", lambda r: (r == "constraint").any()),
        curtailed=("reason", lambda r: (r == "curtailment").any()),
    )
    by_time = by_time.reindex(times)
    letters = [
        "?" if pd.isna(c) else "C" if c else "U" if u else "-"
        for c, u in zip(by_time["constrained"], by_time["curtailed"], strict=True)
    ]

    def column(name):
        return [None if pd.isna(v) else round(float(v), 1) for v in by_time[name]]

    return {
        "start": since.strftime("%Y-%m-%dT%H:%MZ"),
        "available_mw": column("available_mw"),
        "output_mw": column("output_mw"),
        "reason": "".join(letters),
    }


def farm_pages(
    farms: pd.DataFrame,
    table: pd.DataFrame,
    places: pd.DataFrame | None = None,
    recent_days: int = RECENT_DAYS,
) -> dict[str, dict]:
    """Each farm page's data, keyed "farms/<farm_id>.json".

    `farms` is the half-hours with local dates; `table` is farm_list's output with
    farm_groups's columns; `places` is farm_locations's output, if there is one."""
    farms = farms.merge(table[["unit", "farm_id"]], on="unit")
    local = farms["timestamp_utc"].dt.tz_convert(LOCAL_TIME)
    farms = farms.assign(hour=local.dt.hour)
    daily = sums(farms, ["farm_id", "day"])
    monthly = sums(farms, ["farm_id", "month"])
    hours = sums(farms, ["farm_id", "month", "hour"])[
        ["farm_id", "month", "hour", "constraint_mwh", "curtailment_mwh",
         "half_hours", "held_down_half_hours"]
    ]  # fmt: skip
    until = farms["timestamp_utc"].max()
    since = until.floor("D") - pd.Timedelta(days=recent_days - 1)
    recent = farms[farms["timestamp_utc"] >= since]
    where = None if places is None else places[places["located_by"] != ""].set_index("unit")

    def part(by_farm, farm_id):
        rows = by_farm[by_farm["farm_id"] == farm_id].drop(columns="farm_id")
        return records(rows)

    pages = {}
    for farm_id, units in table.groupby("farm_id", sort=False):
        units = units.sort_values("lost_mwh", ascending=False)
        page = {
            "id": farm_id,
            "name": units["farm_name"].iloc[0],
            "technology": "solar" if (units["technology"] == "solar").all() else "wind",
            "regions": sorted(units["region"].dropna().unique().tolist()),
            "companies": [c for c in units["company"].fillna("").unique() if c],
            "units": records(units[["unit", "semo_name", "company", "lost_mwh"]]),
            "first_day": units["first_day"].min(),
            "last_day": units["last_day"].max(),
            "lat": None,
            "lon": None,
            "capacity_mw": None,
        }
        if where is not None:
            placed = [u for u in units["unit"] if u in where.index]
            if placed:
                spot = where.loc[placed[0]]
                page["lat"], page["lon"] = float(spot["lat"]), float(spot["lon"])
                if pd.notna(spot["capacity_mw"]):
                    page["capacity_mw"] = float(spot["capacity_mw"])
        page["monthly"] = part(monthly, farm_id)
        page["daily"] = part(daily, farm_id)
        page["hours"] = part(hours, farm_id)
        page["recent"] = recent_half_hours(recent[recent["farm_id"] == farm_id], since, until)
        pages[f"farms/{farm_id}.json"] = page
    return pages


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
        "map_credit": MAP_CREDIT,
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
    names: pd.DataFrame | None = None,
    osm: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame | dict]:
    """Every file's contents, keyed by file name. farm_locations.csv needs `names` and
    `osm` (the farm_names and osm_wind_farms datasets)."""
    farms = dispatch_down.farm_half_hours(availability, metered, instructions, units)
    farms = dispatch_down.add_market_value(farms, prices)
    farms = with_local_dates(farms)
    table = farm_list(farms, units, names)
    places = None
    if names is not None and osm is not None:
        places = farm_locations(farms, units, names, osm)
    groups = farm_groups(table, places)
    table = groups.merge(table, on="unit")
    files = {
        "monthly.csv": monthly(farms),
        "daily.csv": daily(farms),
        "farms.csv": table,
        "farm_monthly.csv": farm_monthly(farms),
        "farm_daily.csv": farm_daily(farms),
    }
    if places is not None:
        files["farm_locations.csv"] = places
    files["meta.json"] = meta(farms)
    files.update(farm_pages(farms, table, places))
    return files


def write(files: dict[str, pd.DataFrame | dict], out_dir: Path) -> list[Path]:
    """Write the files, rounded so they stay small: 3 decimals, 2 for euros.

    Old farm pages are removed first, so a farm that has gone doesn't linger."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(name.startswith("farms/") for name in files):
        for old in (out_dir / "farms").glob("*.json"):
            old.unlink()
    paths = []
    for name, content in files.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            # meta.json is for people to read; farm pages are kept compact.
            indent = None if name.startswith("farms/") else 2
            path.write_text(json.dumps(content, indent=indent) + "\n")
        else:
            rounded = content.round(3)
            if "lost_value_eur" in content:
                rounded["lost_value_eur"] = content["lost_value_eur"].round(2)
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
    names = osm = None
    if all((data_dir / f"{name}.parquet").exists() for name in ["farm_names", "osm_wind_farms"]):
        names = storage.load("farm_names", data_dir)
        osm = storage.load("osm_wind_farms", data_dir)
    else:
        print("note: no farm_names / osm_wind_farms data, so no farm_locations.csv. Download")
        print("      them with: python -m isem_data --datasets farm_names osm_wind_farms")
    files = build(
        storage.load("availability", data_dir),
        storage.load("metered", data_dir),
        storage.load("dispatch_instructions", data_dir),
        storage.load("units", data_dir),
        storage.load("prices", data_dir),
        names,
        osm,
    )
    paths = write(files, args.out.expanduser())
    pages = [path for path in paths if path.parent.name == "farms"]
    for path in paths:
        if path not in pages:
            print(f"{path}  ({path.stat().st_size / 1000:,.0f} kB)")
    pages_kb = sum(path.stat().st_size for path in pages) / 1000
    print(f"{len(pages)} farm pages in {args.out / 'farms'}  ({pages_kb:,.0f} kB in all)")
    info = files["meta.json"]
    print(f"{info['first_day']} to {info['last_day']}, {info['farms']} farms")
    if "farm_locations.csv" in files:
        placed = (files["farm_locations.csv"]["located_by"] != "").sum()
        print(f"{placed} of {info['farms']} farms placed on the map")
    if info["missing_days"]:
        print(f"warning: no data for {', '.join(info['missing_days'])}")


if __name__ == "__main__":
    main()
