"""ENTSO-E's day-ahead wind forecast for the island: a second source, kept as it changes.

The price forecasts use EirGrid's per-farm wind forecast from SEMO (forecasts.py). Twice in
November 2026 SEMO listed no new version for two days or more, and the price forecast had
nothing to go on. ENTSO-E's Transparency Platform publishes a wind forecast too (data item
14.1.D, "day-ahead generation forecast for wind and solar": onshore wind, hourly, MW), for
Ireland's and Northern Ireland's grids separately, often two days ahead. It's the backup.

From 20 Aug to 24 Nov 2026 the two agreed closely (correlation 0.95), ENTSO-E's reading
about 6% lower; against the wind that blew, neither was better throughout.

ENTSO-E keeps only its latest numbers, not when they were published. So every download
here is compared with what's already saved, and only a value that is new or has changed
is added, with the time this download first saw it (`first_seen_utc`). That builds up the
record ENTSO-E doesn't keep: what was known when. A forecast can then use only values
first seen before its cut-off (`known_by`).

One row per hour, region and version:
    first_seen_utc  when a download first saw this value
    timestamp_utc   start of the hour the forecast is for
    region          "IE" (Ireland's grid) or "NI" (Northern Ireland's)
    forecast_mw     forecast wind output in MW

The token is handled as in entsoe.py: in a header, never in the web address, and taken
out of every error message.
"""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from . import entsoe, storage, web

DATASET = "entsoe_wind_forecasts"
AREAS = {"IE": "10YIE-1001A00010", "NI": "10Y1001A1001A016"}  # the two grids' control areas
COLUMNS = ["first_seen_utc", "timestamp_utc", "region", "forecast_mw"]
KEY = ["timestamp_utc", "region", "first_seen_utc"]
DAYS_AHEAD = 3  # ENTSO-E has often published two days ahead; ask for three


def parse(text: str, region: str) -> pd.DataFrame:
    """ENTSO-E's answer to a wind forecast request: one row per hour (timestamp_utc,
    region, forecast_mw). No data yet gives no rows; any other refusal raises
    EntsoeError. As with prices, a point ENTSO-E leaves out repeats the one before."""
    root = ET.fromstring(text)
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        reason = root.findtext("a:Reason/a:text", default="", namespaces=entsoe.ACK_NS)
        if reason.startswith(entsoe.NO_DATA):
            return pd.DataFrame(columns=["timestamp_utc", "region", "forecast_mw"])
        raise entsoe.EntsoeError(f"ENTSO-E refused the request: {reason or 'no reason given'}")
    ns = {"m": root.tag.split("}")[0][1:]}
    rows = []
    for series in root.iterfind("m:TimeSeries", ns):
        if series.findtext("m:MktPSRType/m:psrType", namespaces=ns) != "B19":  # onshore wind
            continue
        for period in series.iterfind("m:Period", ns):
            start = pd.Timestamp(period.findtext("m:timeInterval/m:start", namespaces=ns))
            end = pd.Timestamp(period.findtext("m:timeInterval/m:end", namespaces=ns))
            step = pd.Timedelta(period.findtext("m:resolution", namespaces=ns))
            points = {
                int(p.findtext("m:position", namespaces=ns)): float(
                    p.findtext("m:quantity", namespaces=ns)
                )
                for p in period.iterfind("m:Point", ns)
            }
            values = pd.Series(points).reindex(range(1, int((end - start) / step) + 1)).ffill()
            rows += [(start + (i - 1) * step, v) for i, v in values.items()]
    df = pd.DataFrame(rows, columns=["timestamp_utc", "forecast_mw"]).dropna()
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    # Hourly is the norm; anything finer is averaged to the hour, as the price models use.
    df = df.groupby(df["timestamp_utc"].dt.floor("h"))["forecast_mw"].mean().reset_index()
    df["region"] = region
    return df[["timestamp_utc", "region", "forecast_mw"]]


def fetch(start: date, end: date, token: str | None = None) -> pd.DataFrame:
    """Both grids' forecasts for trading days `start` to `end`, as ENTSO-E has them now."""
    token = token or entsoe.read_token()
    try:
        first, last = entsoe.window(start, end)
        parts = []
        for region, area in AREAS.items():
            params = {
                "documentType": "A69",  # wind and solar forecast
                "processType": "A01",  # day ahead
                "in_Domain": area,
                "periodStart": first.strftime("%Y%m%d%H%M"),
                "periodEnd": last.strftime("%Y%m%d%H%M"),
            }
            try:
                text = web.get(
                    entsoe.API_URL, params=params, headers={"SECURITY_TOKEN": token}
                ).text
            except requests.HTTPError as error:
                if error.response is not None and "Acknowledgement" in error.response.text:
                    text = error.response.text
                else:
                    raise
            parts.append(parse(text, region))
        parts = [p for p in parts if not p.empty]
        if not parts:
            return pd.DataFrame(columns=["timestamp_utc", "region", "forecast_mw"])
        return pd.concat(parts, ignore_index=True)
    except Exception as error:
        message = str(error)
        if not isinstance(error, entsoe.EntsoeError):
            message = f"{type(error).__name__}: {message}"
        raise entsoe.EntsoeError(entsoe.scrub(message, token)) from None


def latest(saved: pd.DataFrame) -> pd.DataFrame:
    """The newest saved value for each hour and region."""
    if saved.empty:
        return saved
    newest = saved.sort_values("first_seen_utc").groupby(["timestamp_utc", "region"]).tail(1)
    return newest.reset_index(drop=True)


def fetch_and_save(
    start: date,
    end: date,
    data_dir: Path = storage.DATA_DIR,
    now: datetime | None = None,
    token=None,
) -> int:
    """Download, and save the values that are new or have changed since the last download.
    Returns how many were saved."""
    now = pd.Timestamp(now or datetime.now(timezone.utc)).floor("s")
    fresh = fetch(start, end, token)
    path = data_dir / f"{DATASET}.parquet"
    saved = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
    before = latest(saved).set_index(["timestamp_utc", "region"])["forecast_mw"]
    fresh = fresh.set_index(["timestamp_utc", "region"])["forecast_mw"]
    old = before.reindex(fresh.index)
    changed = fresh[old.isna() | (old - fresh).abs().gt(0.05)]
    if changed.empty:
        return 0
    new = changed.rename("forecast_mw").reset_index().assign(first_seen_utc=now)[COLUMNS]
    storage.save(new, DATASET, data_dir=data_dir, key=KEY)
    return len(new)


def known_by(when: pd.Timestamp, data_dir: Path = storage.DATA_DIR) -> pd.DataFrame:
    """Each hour's forecast as it stood at `when`: the newest value first seen by then,
    one column per region (IE, NI), indexed by UTC hour. Empty if there's nothing."""
    path = data_dir / f"{DATASET}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=list(AREAS))
    saved = pd.read_parquet(path)
    saved = saved[saved["first_seen_utc"] <= when]
    table = latest(saved).pivot(index="timestamp_utc", columns="region", values="forecast_mw")
    return table.reindex(columns=list(AREAS)).sort_index()


def default_end(end: date, today: date | None = None) -> date:
    """How far ahead to ask: DAYS_AHEAD past `end`, but never more than that past today."""
    today = today or date.today()
    return min(end + timedelta(days=DAYS_AHEAD), today + timedelta(days=DAYS_AHEAD))
