"""SEM day-ahead prices from ENTSO-E's Transparency Platform: tomorrow's, a day early.

semo.py already has the day-ahead prices, half-hourly, from SEMO's report site. But that
site only puts a day's results up at about 01:00 after the day is over. ENTSO-E has
them from the afternoon before (by 14:00 Irish time), so this is how the archive knows
tomorrow's prices while it is still today.

ENTSO-E's prices are hourly: each hour is exactly the average of SEMO's two half-hours
(checked to the cent on 19 Nov 2026). So SEMO's file stays the half-hourly record, and
this one is for knowing early, and as an independent check on SEMO's (see checks.py,
`entsoe_matches_semo`).

One row per hour: timestamp_utc (the hour's start), region "SEM", series
"dam_price_eur_mwh", value in €/MWh.

The token. ENTSO-E needs a personal access token, read from the ENTSOE_TOKEN
environment variable (GitHub's daily job, from a repository secret), or else from
~/.config/isem-data/entsoe_token. It is sent in a request header, never in the web
address, and is removed from every error message, so it can't reach status.json, the
logs or STATUS.md, all of which are kept or shown elsewhere.
"""

import os
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from . import web

DATASET = "entsoe_prices"
API_URL = "https://web-api.tp.entsoe.eu/api"
SEM = "10Y1001A1001A59C"  # the SEM bidding zone (the whole island)
TOKEN_ENV = "ENTSOE_TOKEN"
TOKEN_FILE = Path("~/.config/isem-data/entsoe_token")
LOCAL_TIME = "Europe/Dublin"

COLUMNS = ["timestamp_utc", "region", "series", "value"]
REGION = "SEM"
SERIES = "dam_price_eur_mwh"

# ENTSO-E answers at most one year per request; half a year keeps well inside that.
DAYS_PER_REQUEST = 180

PRICES_NS = {"m": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}
ACK_NS = {"a": "urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0"}
NO_DATA = "No matching data found"


class EntsoeError(RuntimeError):
    """ENTSO-E refused or failed a request. The message never contains the token."""


def read_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        path = TOKEN_FILE.expanduser()
        token = path.read_text().strip() if path.exists() else ""
    if not token:
        raise EntsoeError(
            f"no ENTSO-E token: set {TOKEN_ENV}, or save it in {TOKEN_FILE} "
            "(see the README's ENTSO-E section)"
        )
    return token


def scrub(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def window(start: date, end: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Trading days `start` to `end` in UTC: from 23:00 Irish time the evening before
    `start` to 23:00 Irish time on `end`."""
    first = pd.Timestamp(start).tz_localize(LOCAL_TIME) - pd.Timedelta(hours=1)
    # Midnight after `end` minus an hour, not midnight plus 23 hours: on the day the clocks
    # go back those differ (the day has 25 hours), and its last hour would be missed.
    last = pd.Timestamp(end + timedelta(days=1)).tz_localize(LOCAL_TIME) - pd.Timedelta(hours=1)
    return first.tz_convert("UTC"), last.tz_convert("UTC")


def parse_prices(text: str) -> pd.DataFrame:
    """ENTSO-E's answer to a day-ahead price request, as tidy rows.

    An answer of "no matching data" (days not published yet) gives no rows; any other
    refusal raises EntsoeError with ENTSO-E's reason. ENTSO-E leaves a point out when
    its price is the same as the one before (curve type A03), so each period's points
    are filled in from its start, end and resolution."""
    root = ET.fromstring(text)
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        reason = root.findtext("a:Reason/a:text", default="", namespaces=ACK_NS)
        if reason.startswith(NO_DATA):
            return pd.DataFrame(columns=COLUMNS)
        raise EntsoeError(f"ENTSO-E refused the request: {reason or 'no reason given'}")
    rows = []
    for period in root.iterfind(".//m:Period", PRICES_NS):
        start = pd.Timestamp(period.findtext("m:timeInterval/m:start", namespaces=PRICES_NS))
        end = pd.Timestamp(period.findtext("m:timeInterval/m:end", namespaces=PRICES_NS))
        step = pd.Timedelta(period.findtext("m:resolution", namespaces=PRICES_NS))
        points = {
            int(point.findtext("m:position", namespaces=PRICES_NS)): float(
                point.findtext("m:price.amount", namespaces=PRICES_NS)
            )
            for point in period.iterfind("m:Point", PRICES_NS)
        }
        n = int((end - start) / step)
        prices = pd.Series(points).reindex(range(1, n + 1)).ffill()
        times = [start + (position - 1) * step for position in prices.index]
        rows += list(zip(times, prices.to_numpy(), strict=True))
    df = pd.DataFrame(rows, columns=["timestamp_utc", "value"])
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["region"], df["series"] = REGION, SERIES
    return df.dropna(subset=["value"])[COLUMNS].sort_values("timestamp_utc").reset_index(drop=True)


def request(first: pd.Timestamp, last: pd.Timestamp, token: str) -> str:
    """One request for the prices from `first` to `last` (UTC); returns ENTSO-E's XML."""
    params = {
        "documentType": "A44",  # prices
        "in_Domain": SEM,
        "out_Domain": SEM,
        "periodStart": first.strftime("%Y%m%d%H%M"),
        "periodEnd": last.strftime("%Y%m%d%H%M"),
    }
    try:
        return web.get(API_URL, params=params, headers={"SECURITY_TOKEN": token}).text
    except requests.HTTPError as error:
        # ENTSO-E explains a refusal (a range too long, a bad token) in the answer itself.
        if error.response is not None and "Acknowledgement" in error.response.text:
            return error.response.text
        raise


def fetch(start: date, end: date, token: str | None = None) -> pd.DataFrame:
    """The hourly prices for trading days `start` to `end`. Days not published yet are
    simply missing. Errors are raised as EntsoeError, with the token taken out."""
    token = token or read_token()
    try:
        parts = []
        day = start
        while day <= end:
            last_day = min(day + timedelta(days=DAYS_PER_REQUEST - 1), end)
            parts.append(parse_prices(request(*window(day, last_day), token)))
            day = last_day + timedelta(days=1)
        parts = [p for p in parts if not p.empty]
        if not parts:
            return pd.DataFrame(columns=COLUMNS)
        return pd.concat(parts, ignore_index=True)
    except Exception as error:
        # `from None` drops the original error, whose message or traceback might
        # include the request; only the cleaned message is kept.
        message = str(error)
        if not isinstance(error, EntsoeError):
            message = f"{type(error).__name__}: {message}"
        raise EntsoeError(scrub(message, token)) from None
