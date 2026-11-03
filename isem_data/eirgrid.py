"""Fetch wind and demand data from EirGrid's Smart Grid Dashboard.

The dashboard at https://www.smartgriddashboard.com draws its charts from a
JSON API. The API is not officially documented, so everything we know about it
lives in this one file: if EirGrid changes it, only this file needs fixing.
"""

import time
from datetime import date, timedelta

import pandas as pd
import requests

API_URL = "https://www.smartgriddashboard.com/api/chart/"

# The datasets we download: our name -> (the API's chart type, the API's area names).
DATASETS = {
    "wind": ("wind", "windactual,windforecast"),
    "demand": ("demand", "demandactual"),
}

# The API's field names -> the series names we use in our tables.
SERIES_NAMES = {
    "WIND_ACTUAL": "wind_actual",
    "WIND_FCAST": "wind_forecast",
    "SYSTEM_DEMAND": "demand_actual",
}

COLUMNS = ["timestamp_utc", "region", "series", "value_mw"]


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
    chart_type, areas = DATASETS[dataset]
    params = {
        "region": region,  # "ALL" (whole island), "ROI" or "NI"
        "chartType": chart_type,
        "dateRange": "day",
        "dateFrom": start.strftime("%d-%b-%Y"),  # the API wants dates like 01-Nov-2026
        "dateTo": end.strftime("%d-%b-%Y"),
        "areas": areas,
    }
    for _ in range(attempts):
        response = requests.get(API_URL, params=params, timeout=30)
        response.raise_for_status()  # stop with an error if the server said no
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
            "value_mw": pd.to_numeric(raw["Value"], errors="coerce").astype(float),
        }
    )
    return tidy.dropna().reset_index(drop=True)


def fetch(
    dataset: str, start: date, end: date, region: str = "ALL", chunk_days: int = 7
) -> pd.DataFrame:
    """Fetch a date range a week at a time and return one tidy table."""
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
    return pd.concat(frames, ignore_index=True)
