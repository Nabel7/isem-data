"""Fetch wind and demand data from EirGrid's Smart Grid Dashboard.

The dashboard at https://www.smartgriddashboard.com draws its charts from a
JSON API. The API is not officially documented, so everything we know about it
lives in this one file: if EirGrid changes it, only this file needs fixing.
"""

import time
from datetime import date, timedelta

import pandas as pd

from . import web

API_URL = "https://www.smartgriddashboard.com/api/chart/"

# The datasets we download:
# our name -> (the API's chart type, the API's area names, the series we expect back).
DATASETS = {
    "wind": ("wind", "windactual,windforecast", ["wind_actual_mw", "wind_forecast_mw"]),
    "demand": ("demand", "demandactual", ["demand_actual_mw"]),
}

# The API's field names -> the series names we use in our tables.
SERIES_NAMES = {
    "WIND_ACTUAL": "wind_actual_mw",
    "WIND_FCAST": "wind_forecast_mw",
    "SYSTEM_DEMAND": "demand_actual_mw",
}

COLUMNS = ["timestamp_utc", "region", "series", "value"]

# EirGrid publishes one reading every 15 minutes.
INTERVAL = pd.Timedelta(minutes=15)


def fetch_rows(
    dataset: str, start: date, end: date, region: str = "ALL", attempts: int = 3
) -> list[dict]:
    """Download the raw rows for one dataset, from `start` to `end` inclusive.

    Each row looks like:
    {"EffectiveTime": "01-Nov-2026 00:00:00", "FieldName": "WIND_ACTUAL",
     "Region": "ALL", "Value": 958}

    The API sometimes answers with no rows for a request that works a second
    later, so an empty answer is retried a few times before giving up.
    """
    chart_type, areas, _ = DATASETS[dataset]
    params = {
        "region": region,  # "ALL" (whole island), "ROI" or "NI"
        "chartType": chart_type,
        "dateRange": "day",
        "dateFrom": start.strftime("%d-%b-%Y"),  # the API wants dates like 01-Nov-2026
        "dateTo": end.strftime("%d-%b-%Y"),
        "areas": areas,
    }
    for _ in range(attempts):
        response = web.get(API_URL, params=params, timeout=30)  # raises if the server says no
        rows = response.json()["Rows"]
        if rows:
            return rows
        time.sleep(2)
    return []


def to_dataframe(rows: list[dict]) -> pd.DataFrame:
    """Turn raw API rows into a tidy table with one row per timestamp and series."""
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    raw = pd.DataFrame(rows)

    # Timestamps are Irish local time. Storing UTC avoids clock-change problems later.
    # When the clocks go back in October, 01:00-01:59 happens twice and the API gives
    # no way to tell the two apart, so those rows become NaT (not a time) and are dropped.
    local_time = pd.to_datetime(raw["EffectiveTime"], format="%d-%b-%Y %H:%M:%S")
    local_time = local_time.dt.tz_localize("Europe/Dublin", ambiguous="NaT", nonexistent="NaT")

    tidy = pd.DataFrame(
        {
            "timestamp_utc": local_time.dt.tz_convert("UTC"),
            "region": raw["Region"],
            "series": raw["FieldName"].map(SERIES_NAMES),  # unknown fields become NaN
            "value": pd.to_numeric(raw["Value"], errors="coerce").astype(float),
        }
    )
    return tidy.dropna().reset_index(drop=True)


def fetch_wind_regions(start: date, end: date) -> pd.DataFrame:
    """Wind for Ireland and Northern Ireland separately (the `wind` dataset is the whole island)."""
    frames = [fetch("wind", start, end, region=region) for region in ["ROI", "NI"]]
    return pd.concat(frames, ignore_index=True)


def fetch(
    dataset: str, start: date, end: date, region: str = "ALL", chunk_days: int = 7
) -> pd.DataFrame:
    """Fetch a date range a week at a time and return one tidy table.

    Prints a warning for any stretch of missing readings, so gaps in EirGrid's
    data don't pass unnoticed.
    """
    if start > end:
        raise ValueError(f"start must be on or before end (got start={start}, end={end})")

    frames = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
        rows = fetch_rows(dataset, chunk_start, chunk_end, region)
        if not rows:
            print(f"warning: no {dataset} data for {chunk_start} to {chunk_end}")
        frames.append(to_dataframe(rows))
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(1)  # be polite: this is a free public service
    df = pd.concat(frames, ignore_index=True)

    for gap in find_gaps(df, dataset, start, end).itertuples():
        print(
            f"warning: {gap.series} is missing {gap.readings} reading(s) "
            f"from {gap.first_missing} to {gap.last_missing}"
        )
    return df


def find_gaps(df: pd.DataFrame, dataset: str, start: date, end: date) -> pd.DataFrame:
    """List the readings missing from `df`, one row per unbroken run of missing readings.

    Every series should have a reading every 15 minutes from midnight Irish time on
    `start` to 23:45 on `end`. Readings can be missing because EirGrid didn't send
    them, sent them empty, or (once a year) because they fell in the repeated hour
    when the clocks go back.

    Columns: series, first_missing, last_missing (both UTC), readings (how many).
    """
    # Midnight is never affected by clock changes in Ireland, so these are unambiguous.
    first = pd.Timestamp(start).tz_localize("Europe/Dublin")
    after_last = pd.Timestamp(end + timedelta(days=1)).tz_localize("Europe/Dublin")
    expected = pd.date_range(first, after_last, freq=INTERVAL, inclusive="left")
    expected = expected.tz_convert("UTC")

    gaps = []
    for series in DATASETS[dataset][2]:
        present = df.loc[df["series"] == series, "timestamp_utc"]
        missing = expected[~expected.isin(present)].to_series()
        # A new run starts wherever the step from the previous missing reading isn't 15 min.
        run_number = (missing.diff() != INTERVAL).cumsum()
        for _, run in missing.groupby(run_number):
            gaps.append(
                {
                    "series": series,
                    "first_missing": run.iloc[0],
                    "last_missing": run.iloc[-1],
                    "readings": len(run),
                }
            )
    return pd.DataFrame(gaps, columns=["series", "first_missing", "last_missing", "readings"])
