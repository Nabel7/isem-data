"""Save SEMO's per-unit wind forecasts (report BM-012) exactly as they were published.

SEMO's "Four Day Rolling Wind Power Unit Forecast" gives EirGrid's forecast for every
wind (and solar) unit, every 15 minutes, from the start of the trading day to about four
days ahead. A new version comes out about four times a day (around 00:15, 06:15, 12:15
and 18:15), and SEMO keeps only about 90 days of them online.

To judge a forecast fairly you need to know what it said *before* the event, so every
version is kept, tagged with when it was published. A forecast for 14:00 tomorrow made
this morning is a different data point from one made tomorrow at 06:15.

    published_utc   when SEMO published this version (as SEMO states it; it looks like
                    UTC but isn't labelled, so it could be an hour off in summer)
    timestamp_utc   start of the 15 minutes the forecast is for
    unit, participant, region   the unit (GU_...), its market participant, ROI or NI
    forecast_mw     forecast output in MW

Each file is about 35 MB of XML (about 130,000 rows), so versions already saved are
never downloaded again. Saved as one Parquet file per publication day in
data/wind_unit_forecasts/, about 1.5 MB a day.
"""

import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from . import semo, storage

FILES = "PUB_4DayRollWindUnitFcst"
DATASET = "wind_unit_forecasts"
COLUMNS = ["published_utc", "timestamp_utc", "unit", "participant", "region", "forecast_mw"]
KEY = ["published_utc", "timestamp_utc", "unit"]


def list_publications(files: str = FILES) -> pd.DataFrame:
    """Every version SEMO lists: file name, trading day and publish time, oldest first."""
    rows = []
    page = 1
    while True:
        params = {
            "ResourceName": files,
            "sort_by": "PublishTime",
            "order_by": "ASC",
            "page_size": 100,
            "page": page,
        }
        response = requests.get(semo.LIST_URL, params=params, timeout=60)
        response.raise_for_status()
        answer = response.json()
        for item in answer["items"]:
            rows.append(
                {
                    "name": item["ResourceName"],
                    "day": date.fromisoformat(item["Date"][:10]),
                    "published_utc": pd.Timestamp(item["PublishTime"], tz="UTC"),
                }
            )
        if page >= answer["pagination"]["totalPages"]:
            return pd.DataFrame(rows, columns=["name", "day", "published_utc"])
        page += 1


def parse(text: str) -> pd.DataFrame:
    """Turn one forecast file into a table with our column names."""
    if not text:
        return pd.DataFrame(columns=COLUMNS)
    root = ET.fromstring(text)
    published = pd.Timestamp(root.attrib["PublishTime"], tz="UTC")
    rows = pd.DataFrame([element.attrib for element in root])
    return pd.DataFrame(
        {
            "published_utc": published,
            "timestamp_utc": pd.to_datetime(rows["StartTime"]).dt.tz_localize("UTC"),
            "unit": rows["ResourceName"],
            "participant": rows["ParticipantName"],
            "region": rows["Jurisdiction"],
            "forecast_mw": pd.to_numeric(rows["ForecastMW"]).astype(float),
        }
    )


def folder(data_dir: Path = storage.DATA_DIR) -> Path:
    return data_dir / DATASET


def saved(data_dir: Path = storage.DATA_DIR) -> set[pd.Timestamp]:
    """The publish times of the versions already saved."""
    times = set()
    for path in folder(data_dir).glob("*.parquet"):
        times.update(pd.read_parquet(path, columns=["published_utc"])["published_utc"].unique())
    return times


def fetch_and_save(start: date, end: date, data_dir: Path = storage.DATA_DIR) -> int:
    """Download and save every version for trading days `start` to `end` that isn't
    saved yet. Returns how many versions were added."""
    listed = list_publications()
    wanted = listed[(listed["day"] >= start) & (listed["day"] <= end)]
    have = saved(data_dir)
    new = wanted[~wanted["published_utc"].isin(have)]
    for item in new.itertuples():
        df = parse(semo.download(item.name))
        day = item.published_utc.strftime("%Y-%m-%d")
        storage.save(df, f"{DATASET}/{day}", data_dir=data_dir, key=KEY)
        print(f"{DATASET}: {item.name}, {len(df):,} rows")
        time.sleep(1)  # be polite: each file is about 35 MB
    return len(new)


def load(data_dir: Path = storage.DATA_DIR) -> pd.DataFrame:
    """Every saved version, as one table."""
    paths = sorted(folder(data_dir).glob("*.parquet"))
    if not paths:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
