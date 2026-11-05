"""Read SEMO day-ahead market results: prices, volumes and each company's orders.

SEMO (the Single Electricity Market Operator) publishes one results file per
day-ahead auction, named like
MarketResult_SEM-DA_PWR-MRC-D+1_20261101110000_20261101115501.csv.

The file is a semicolon-separated list of sections. The part we read looks like:

    Market;ROI-DA                          <- which bidding zone follows
    Index prices;30;EUR                    <- a section: half-hourly prices in EUR
    2026-11-01T23:00:00Z;2026-11-01T23:30:00Z;...   <- 48 timestamps, already UTC
    248,78;243,18;...                      <- 48 prices; comma is the decimal point
    Index volumes;30                       <- a section: half-hourly volumes
    2026-11-01T23:00:00Z;...
    4068,9;4011,8;...

After the NI and ROI market sections come about 2,400 lines of each company's
order results, one block per order:

    Bidding level;ROI-EIRGRID-DA           <- which bidding zone the orders below are in
    Portfolio;SSE;GU_400770;30;EUR;N       <- company, unit, 30-minute periods, currency
    Linear order;SSE-U2                    <- order type and order name
    2026-11-01T23:00:00Z;...               <- timestamps
    -75,0;-75,0;...                        <- MW cleared: + bought, - sold
    400000495306290;...                    <- two rows of SEMO's internal order IDs
    400000495306290;...

These are the cleared results, not the bids: how much each order bought or sold
at the clearing price, not the prices it offered.

SEMO keeps about one year of these files online, one per delivery day. Older
days drop off the list, so download them before they go.
"""

import time
from datetime import date, timedelta

import pandas as pd
import requests

LIST_URL = "https://reports.sem-o.com/api/v1/documents/static-reports"
FILE_URL = "https://reports.sem-o.com/documents/"

# Day-ahead results files all start with this name.
DAY_AHEAD_FILES = "MarketResult_SEM-DA"

# Same tidy shape as the EirGrid data.
COLUMNS = ["timestamp_utc", "region", "series", "value"]

# The SEMO zone names -> the region names we use.
REGIONS = {"ROI-DA": "ROI", "NI-DA": "NI"}

# The section headers we keep -> the series names we use.
# The volumes match demand levels (around 4,000 for ROI at night), so they read as MW.
SECTIONS = {
    "Index prices;30;EUR": "dam_price_eur_mwh",
    "Index volumes;30": "dam_volume_mw",
}


def to_number(text: str) -> float | None:
    """Turn '248,78' into 248.78. Empty cells become None."""
    if text == "":
        return None
    return float(text.replace(",", "."))


def parse_market_result(text: str) -> pd.DataFrame:
    """Turn the text of one results file into the tidy table."""
    lines = text.splitlines()
    records = []
    region = None

    for i, line in enumerate(lines):
        if line.startswith("Market;"):
            region = REGIONS.get(line.split(";")[1])  # None for zones we don't know
        elif line.startswith("Bidding level;"):
            break  # company order results start here; we are done
        elif line in SECTIONS and region is not None:
            timestamps = lines[i + 1].split(";")
            values = lines[i + 2].split(";")
            for timestamp, value in zip(timestamps, values, strict=True):
                records.append(
                    {
                        "timestamp_utc": timestamp,
                        "region": region,
                        "series": SECTIONS[line],
                        "value": to_number(value),
                    }
                )

    df = pd.DataFrame(records, columns=COLUMNS)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["value"] = df["value"].astype(float)
    return df.dropna().reset_index(drop=True)


# The company order results, one row per order and half-hour.
ORDER_COLUMNS = [
    "timestamp_utc",
    "region",
    "participant",
    "unit",
    "order_type",
    "order_name",
    "quantity_mw",
]

# One unit can have two orders with the same name (one linear, one scalable complex),
# so the order type is part of what identifies a row.
ORDER_KEY = ["timestamp_utc", "region", "unit", "order_name", "order_type"]

# The SEMO zone names used in the order section -> the region names we use.
ORDER_REGIONS = {"ROI-EIRGRID-DA": "ROI", "NI-SONI-DA": "NI"}

ORDER_TYPES = {"Linear order": "linear", "Scalable complex order": "scalable_complex"}


def parse_orders(text: str) -> pd.DataFrame:
    """Turn the text of one results file into a table of each company's cleared orders."""
    lines = text.splitlines()
    records = []
    region = participant = unit = None

    for i, line in enumerate(lines):
        cells = line.split(";")
        if cells[0] == "Bidding level":
            region = ORDER_REGIONS.get(cells[1])
        elif cells[0] == "Portfolio":
            participant, unit = cells[1], cells[2]
        elif cells[0] in ORDER_TYPES and region is not None:
            timestamps = lines[i + 1].split(";")
            quantities = lines[i + 2].split(";")
            for timestamp, quantity in zip(timestamps, quantities, strict=True):
                records.append(
                    {
                        "timestamp_utc": timestamp,
                        "region": region,
                        "participant": participant,
                        "unit": unit,
                        "order_type": ORDER_TYPES[cells[0]],
                        "order_name": cells[1],
                        "quantity_mw": to_number(quantity),
                    }
                )

    df = pd.DataFrame(records, columns=ORDER_COLUMNS)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["quantity_mw"] = df["quantity_mw"].astype(float)

    # If SEMO ever lists the same order twice, saving would keep only one copy.
    # Say so rather than lose data quietly.
    repeated = df.duplicated(subset=ORDER_KEY, keep=False)
    if repeated.any():
        print(f"warning: {repeated.sum()} order rows share a key; only one of each will be kept")

    return df.dropna().reset_index(drop=True)


def list_files() -> dict[date, str]:
    """Ask SEMO which day-ahead results files exist: {delivery day: file name}.

    The list comes back a page at a time (about 4 pages for a year of files).
    Each item looks like:
    {"ResourceName": "MarketResult_SEM-DA_PWR-MRC-D+1_20261101110000_...csv",
     "Date": "2026-11-02T11:00:00", ...}   <- Date is the delivery day
    """
    files = {}
    page = 1
    while True:
        params = {
            "ResourceName": DAY_AHEAD_FILES,
            "sort_by": "PublishTime",  # oldest first, so if a day ever has two files
            "order_by": "ASC",  # (a re-run), the newer one overwrites the older below
            "page_size": 100,
            "page": page,
        }
        response = requests.get(LIST_URL, params=params, timeout=60)
        response.raise_for_status()
        answer = response.json()
        for item in answer["items"]:
            delivery_day = date.fromisoformat(item["Date"][:10])
            files[delivery_day] = item["ResourceName"]
        if page >= answer["pagination"]["totalPages"]:
            return files
        page += 1


def download(name: str) -> str:
    """Download one results file and return its text."""
    response = requests.get(FILE_URL + name, timeout=120)
    response.raise_for_status()
    return response.text


def fetch(start: date, end: date, parse=parse_market_result) -> pd.DataFrame:
    """Download every delivery day's results file from start to end and read it.

    `parse` decides what to read from each file: parse_market_result for prices
    and volumes (the default), or parse_orders for the company orders.
    """
    if start > end:
        raise ValueError(f"start must be on or before end (got start={start}, end={end})")

    files = list_files()
    frames = []
    day = start
    while day <= end:
        if day in files:
            frames.append(parse(download(files[day])))
            time.sleep(1)  # be polite: each file is about 1 MB
        else:
            print(f"warning: SEMO has no day-ahead results file for {day}")
        day += timedelta(days=1)

    if not frames:
        return parse("")  # an empty table with the right columns
    return pd.concat(frames, ignore_index=True)
