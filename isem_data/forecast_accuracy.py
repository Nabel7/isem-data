"""How good is EirGrid's wind forecast? Each saved version, scored against what the
farms could have produced.

SEMO's report BM-012 gives EirGrid's wind forecast for every farm, about four times a
day, up to four days ahead; forecasts.py keeps every version. This scores them.

What a forecast is compared with matters:

- **Available power, not output.** Each farm's available power is SEMO's availability
  figure, checked against its output the same way as for dispatch-down (see
  dispatch_down.py). Output is lower whenever the grid turns farms down; that is a
  grid decision, not a forecast error, and counting it would make the forecast look
  about twice as bad as it is.
- **The same farms on both sides.** Only farms with both a forecast and available
  power in a half-hour are added up, so the totals compare like with like. That is
  the market's wind farms (about 210); the smaller farms outside the market have a
  forecast but no availability figure and are left out.

Hours ahead is the time from a version's publication to the start of the half-hour.
SEMO's publish times may be an hour off in summer (see forecasts.py), which moves
hours ahead by up to an hour, never the forecasts themselves.

    from isem_data import dispatch_down, forecast_accuracy, storage

    farms = dispatch_down.farm_half_hours(...)            # as in dispatch_down.py
    totals = forecast_accuracy.version_totals(farms)      # every version, every half-hour
    forecast_accuracy.by_hours_ahead(totals)              # error against hours ahead
    forecast_accuracy.day_ahead(totals)                   # the version the auction had
"""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import forecasts, storage

LOCAL_TIME = "Europe/Dublin"

# The day-ahead auction for day D closes at 11:00 Irish time on D-1 (10:00 UTC in
# summer, 11:00 in winter). A version counts as known before the auction if it was
# published by 09:00 UTC on D-1: an hour early in either season, and still safe if
# SEMO's publish times are an hour off. If none came out that morning, the newest older
# version is used, up to two days older. (The same rule as isem-forecast's price tests.)
CUTOFF_HOUR_UTC = 9
OLDEST_VERSION = pd.Timedelta(days=2)

TOTALS = [
    "published_utc",
    "timestamp_utc",
    "hours_ahead",
    "forecast_mw",
    "available_mw",
    "output_mw",
    "farms",
]


def trading_day(times) -> np.ndarray:
    """The trading day of each time: it runs 23:00 to 23:00 Irish time."""
    local = pd.DatetimeIndex(times).tz_convert(LOCAL_TIME) + pd.Timedelta(hours=1)
    return np.array(local.date)


def version_totals(farms: pd.DataFrame, data_dir: Path = storage.DATA_DIR) -> pd.DataFrame:
    """Every saved forecast version against what happened, for the island.

    `farms` is dispatch_down.farm_half_hours(): timestamp_utc, unit, available_mw,
    output_mw. Returns one row per version and half-hour, from the time of publication
    on (columns TOTALS). The forecast files are read one at a time: all of them at once
    would be tens of millions of rows.
    """
    truth = farms[["timestamp_utc", "unit", "available_mw", "output_mw"]]
    units = set(truth["unit"])
    parts = []
    for path in sorted(forecasts.folder(data_dir).glob("*.parquet")):
        table = pd.read_parquet(
            path, columns=["published_utc", "timestamp_utc", "unit", "forecast_mw"]
        )
        table = table[table["unit"].isin(units)]
        parts.append(totals_for(table, truth))
    if not parts:
        return pd.DataFrame(columns=TOTALS)
    return pd.concat(parts, ignore_index=True).sort_values(["published_utc", "timestamp_utc"])


def totals_for(table: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    """The island totals for some forecast rows (published_utc, timestamp_utc, unit,
    forecast_mw), against `truth` (timestamp_utc, unit, available_mw, output_mw)."""
    # Forecasts are every 15 minutes, availability every 30: average each pair.
    half_hour = table.assign(timestamp_utc=table["timestamp_utc"].dt.floor("30min"))
    half_hour = (
        half_hour.groupby(["published_utc", "timestamp_utc", "unit"])["forecast_mw"]
        .mean()
        .reset_index()
    )
    both = half_hour.merge(truth, on=["timestamp_utc", "unit"])  # farms with both only
    island = (
        both.groupby(["published_utc", "timestamp_utc"])
        .agg(
            forecast_mw=("forecast_mw", "sum"),
            available_mw=("available_mw", "sum"),
            output_mw=("output_mw", "sum"),
            farms=("unit", "nunique"),
        )
        .reset_index()
    )
    ahead = island["timestamp_utc"] - island["published_utc"]
    island.insert(2, "hours_ahead", ahead.dt.total_seconds() / 3600)
    return island[island["hours_ahead"] >= 0][TOTALS]


def by_hours_ahead(totals: pd.DataFrame, step: float = 1.0) -> pd.DataFrame:
    """Average error against hours ahead, in bands of `step` hours.

    mae_mw    average size of the miss
    bias_mw   average of forecast minus available: positive means it expected too much
    """
    band = (totals["hours_ahead"] // step * step).rename("hours_ahead")
    error = totals["forecast_mw"] - totals["available_mw"]
    return (
        pd.DataFrame({"error": error, "available": totals["available_mw"]})
        .groupby(band)
        .agg(
            mae_mw=("error", lambda e: e.abs().mean()),
            bias_mw=("error", "mean"),
            available_mw=("available", "mean"),
            half_hours=("error", "size"),
        )
        .reset_index()
    )


def auction_cutoff(day: date) -> pd.Timestamp:
    """The latest publish time a version may have to count as known before `day`'s auction."""
    return pd.Timestamp(day - timedelta(days=1), tz="UTC") + pd.Timedelta(hours=CUTOFF_HOUR_UTC)


def day_ahead(totals: pd.DataFrame) -> pd.DataFrame:
    """For each trading day, the half-hours of the version the auction had.

    Same columns as `totals`, plus `day`. Days with no version from before the cutoff
    (within OLDEST_VERSION) are left out.
    """
    totals = totals.assign(day=trading_day(totals["timestamp_utc"]))
    versions = pd.DatetimeIndex(totals["published_utc"].unique()).sort_values()
    chosen = []
    for day in sorted(totals["day"].unique()):
        cutoff = auction_cutoff(day)
        usable = versions[(versions <= cutoff) & (versions >= cutoff - OLDEST_VERSION)]
        if len(usable):
            chosen.append((day, usable.max()))
    if not chosen:
        return totals.iloc[0:0]
    pick = pd.DataFrame(chosen, columns=["day", "published_utc"])
    pick["published_utc"] = pd.to_datetime(pick["published_utc"], utc=True)
    return totals.merge(pick, on=["day", "published_utc"]).sort_values("timestamp_utc")


def latest_before(totals: pd.DataFrame) -> pd.DataFrame:
    """For each half-hour, the last version published before it: the best EirGrid had."""
    newest = totals.sort_values("published_utc").groupby("timestamp_utc").tail(1)
    return newest.sort_values("timestamp_utc").reset_index(drop=True)
