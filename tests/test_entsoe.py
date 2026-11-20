"""Tests for ENTSO-E's day-ahead prices: reading the answers, and keeping the token out
of every message.

The two fixtures are real answers, saved without the token: 19-20 Nov 2026 (two whole
days) and 1 Aug 2026, where ENTSO-E left out three hours whose price repeated."""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import requests

from isem_data import __main__ as cli
from isem_data import entsoe, storage, web

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "12345678-abcd-4ef0-9abc-1234567890ab"  # made up


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def ack(reason: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument
    xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
  <mRID>x</mRID>
  <Reason><code>999</code><text>{reason}</text></Reason>
</Acknowledgement_MarketDocument>"""


# --- reading answers ---


def test_two_days_give_48_hourly_prices():
    df = entsoe.parse_prices(fixture("entsoe_prices_2026-11-19_20.xml"))
    assert list(df.columns) == entsoe.COLUMNS
    assert len(df) == 48
    assert df["timestamp_utc"].iloc[0] == pd.Timestamp("2026-11-18 23:00", tz="UTC")
    assert df["timestamp_utc"].diff().dropna().eq(pd.Timedelta(hours=1)).all()
    assert df["value"].iloc[0] == 238.06
    assert set(df["region"]) == {"SEM"} and set(df["series"]) == {"dam_price_eur_mwh"}


def test_hours_left_out_repeat_the_price_before():
    df = entsoe.parse_prices(fixture("entsoe_prices_2026-08-01.xml")).set_index("timestamp_utc")
    assert len(df) == 24  # 21 points in the answer
    hours = pd.date_range("2026-08-01 09:00", "2026-08-01 12:00", freq="h", tz="UTC")
    assert df.loc[hours, "value"].tolist() == [122.3] * 4  # 10:00 to 12:00 were left out
    assert df.loc[pd.Timestamp("2026-08-01 13:00", tz="UTC"), "value"] == 122.015


def test_no_data_yet_gives_no_rows():
    df = entsoe.parse_prices(ack("No matching data found for Data item ENERGY_PRICES ..."))
    assert df.empty and list(df.columns) == entsoe.COLUMNS


def test_any_other_refusal_says_why():
    with pytest.raises(entsoe.EntsoeError, match="larger than maximum allowed period"):
        entsoe.parse_prices(ack("Provided time interval is larger than maximum allowed period"))


def test_a_trading_day_runs_23_00_to_23_00_irish_time():
    assert entsoe.window(date(2026, 11, 19), date(2026, 11, 19)) == (
        pd.Timestamp("2026-11-18 23:00", tz="UTC"),
        pd.Timestamp("2026-11-19 23:00", tz="UTC"),
    )
    assert entsoe.window(date(2026, 8, 1), date(2026, 8, 2)) == (  # summer time
        pd.Timestamp("2026-07-31 22:00", tz="UTC"),
        pd.Timestamp("2026-08-02 22:00", tz="UTC"),
    )


def test_a_long_range_is_split_into_requests_ENTSO_E_allows(monkeypatch):
    asked = []

    def request(first, last, token):
        asked.append((first, last))
        return ack("No matching data found")

    monkeypatch.setattr(entsoe, "request", request)
    entsoe.fetch(date(2025, 11, 7), date(2026, 11, 21), token=TOKEN)
    assert len(asked) == 3
    # 180 days, give or take the hour a clock change adds or takes away
    longest = pd.Timedelta(days=entsoe.DAYS_PER_REQUEST, hours=1)
    assert all(last - first <= longest for first, last in asked)
    assert asked[0][0] == entsoe.window(date(2025, 11, 7), date(2025, 11, 7))[0]
    assert asked[-1][1] == entsoe.window(date(2026, 11, 21), date(2026, 11, 21))[1]
    assert all(a[1] == b[0] for a, b in zip(asked, asked[1:], strict=False))  # no gaps


# --- the token ---


def test_the_token_goes_in_a_header_not_the_web_address(monkeypatch):
    sent = {}

    class Answer:
        status_code = 200
        text = fixture("entsoe_prices_2026-11-19_20.xml")

        def raise_for_status(self):
            pass

    def get(url, params=None, timeout=None, **kwargs):
        sent.update(url=url, params=params, **kwargs)
        return Answer()

    monkeypatch.setattr(web.requests, "get", get)
    assert len(entsoe.fetch(date(2026, 11, 19), date(2026, 11, 20), token=TOKEN)) == 48
    assert sent["headers"] == {"SECURITY_TOKEN": TOKEN}
    assert TOKEN not in sent["url"] and TOKEN not in str(sent["params"])


def test_errors_never_contain_the_token(monkeypatch):
    def get(*args, **kwargs):
        raise requests.ConnectionError(f"could not connect (token {TOKEN})")

    monkeypatch.setattr(web, "get", get)
    with pytest.raises(entsoe.EntsoeError) as caught:
        entsoe.fetch(date(2026, 11, 19), date(2026, 11, 19), token=TOKEN)
    assert TOKEN not in str(caught.value)
    assert "<token>" in str(caught.value)
    assert caught.value.__suppress_context__  # the original error isn't printed with it


def test_a_failed_download_keeps_the_token_out_of_status_and_the_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(entsoe.TOKEN_ENV, TOKEN)

    def get(*args, **kwargs):
        raise requests.ConnectionError(f"could not connect (token {TOKEN})")

    monkeypatch.setattr(web, "get", get)
    day = date(2026, 11, 19)
    assert cli.run([entsoe.DATASET], day, day, tmp_path) == [entsoe.DATASET]
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert TOKEN not in (tmp_path / storage.STATUS_FILE).read_text()
    assert "EntsoeError" in storage.read_status(tmp_path)[entsoe.DATASET]["last_error"]


def test_the_token_comes_from_the_environment_first_then_the_file(tmp_path, monkeypatch):
    path = tmp_path / "entsoe_token"
    path.write_text(f"  {TOKEN}\n")
    monkeypatch.setattr(entsoe, "TOKEN_FILE", path)
    monkeypatch.delenv(entsoe.TOKEN_ENV, raising=False)
    assert entsoe.read_token() == TOKEN
    monkeypatch.setenv(entsoe.TOKEN_ENV, "from-the-environment")
    assert entsoe.read_token() == "from-the-environment"


def test_no_token_says_where_to_put_one(tmp_path, monkeypatch):
    monkeypatch.setattr(entsoe, "TOKEN_FILE", tmp_path / "missing")
    monkeypatch.delenv(entsoe.TOKEN_ENV, raising=False)
    with pytest.raises(entsoe.EntsoeError, match="ENTSOE_TOKEN"):
        entsoe.read_token()


# --- the daily run ---


def test_the_daily_run_asks_for_tomorrow_but_no_further(tmp_path, monkeypatch):
    asked = []

    def fetch(start, end):
        asked.append((start, end))
        return entsoe.parse_prices(fixture("entsoe_prices_2026-11-19_20.xml"))

    monkeypatch.setattr(entsoe, "fetch", fetch)
    today = date.today()
    yesterday = today - timedelta(days=1)
    cli.download(entsoe.DATASET, yesterday - timedelta(days=6), yesterday, tmp_path)
    cli.download(entsoe.DATASET, date(2025, 11, 7), date(2025, 11, 30), tmp_path)
    assert asked[0][1] == today + timedelta(days=1)  # tomorrow
    assert asked[1][1] == date(2025, 12, 2)  # an old range: two days past its end
    assert len(storage.load(entsoe.DATASET, tmp_path)) == 48
