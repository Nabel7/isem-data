"""Tests for ENTSO-E's wind forecast: reading the answer, keeping only what changed (with
when it was first seen), and keeping the token out.

The fixture is a real answer (Northern Ireland, 25 Nov 2026), saved without the token."""

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import requests

from isem_data import __main__ as cli
from isem_data import entsoe, entsoe_wind, storage, web

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "12345678-abcd-4ef0-9abc-1234567890ab"  # made up
NOV25 = (FIXTURES / "entsoe_wind_ni_2026-11-25.xml").read_text()


def test_a_day_gives_24_hourly_values_for_the_region():
    df = entsoe_wind.parse(NOV25, "NI")
    assert len(df) == 24
    assert df["timestamp_utc"].iloc[0] == pd.Timestamp("2026-11-24 23:00", tz="UTC")
    assert (df["region"] == "NI").all() and df["forecast_mw"].between(0, 2000).all()


def test_no_data_yet_gives_no_rows():
    no_data = """<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
  <Reason><code>999</code><text>No matching data found for Data item X</text></Reason>
</Acknowledgement_MarketDocument>"""
    assert entsoe_wind.parse(no_data, "IE").empty


def answer_with(values: dict[str, float]):
    """A stand-in for ENTSO-E: both regions, the given hourly values."""

    def fetch(start, end, token=None):
        rows = [(pd.Timestamp(t, tz="UTC"), r, v) for (t, r), v in values.items()]
        return pd.DataFrame(rows, columns=["timestamp_utc", "region", "forecast_mw"])

    return fetch


def test_only_new_or_changed_values_are_kept_with_when_they_were_first_seen(tmp_path, monkeypatch):
    day = date(2026, 11, 26)
    first = {("2026-11-26 00:00", "IE"): 1000.0, ("2026-11-26 00:00", "NI"): 200.0}
    monkeypatch.setattr(entsoe_wind, "fetch", answer_with(first))
    t1 = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
    assert entsoe_wind.fetch_and_save(day, day, tmp_path, now=t1) == 2
    assert entsoe_wind.fetch_and_save(day, day, tmp_path, now=t1.replace(hour=21)) == 0  # unchanged

    revised = {**first, ("2026-11-26 00:00", "IE"): 1100.0, ("2026-11-26 01:00", "IE"): 900.0}
    monkeypatch.setattr(entsoe_wind, "fetch", answer_with(revised))
    t2 = datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc)
    assert entsoe_wind.fetch_and_save(day, day, tmp_path, now=t2) == 2  # one changed, one new

    before = entsoe_wind.known_by(pd.Timestamp("2026-11-26 05:00", tz="UTC"), tmp_path)
    after = entsoe_wind.known_by(pd.Timestamp("2026-11-26 07:00", tz="UTC"), tmp_path)
    midnight = pd.Timestamp("2026-11-26 00:00", tz="UTC")
    assert before.loc[midnight, "IE"] == 1000.0 and len(before) == 1  # the revision came later
    assert after.loc[midnight, "IE"] == 1100.0 and after.loc[midnight, "NI"] == 200.0
    assert len(after) == 2


def test_nothing_saved_yet_knows_nothing(tmp_path):
    assert entsoe_wind.known_by(pd.Timestamp("2026-11-26", tz="UTC"), tmp_path).empty


def test_it_asks_a_few_days_ahead_but_not_beyond(monkeypatch):
    assert entsoe_wind.default_end(date(2026, 11, 25), today=date(2026, 11, 25)) == date(
        2026, 11, 28
    )
    assert entsoe_wind.default_end(date(2026, 11, 1), today=date(2026, 11, 25)) == date(2026, 11, 4)


def test_the_token_goes_in_a_header_and_never_into_errors(monkeypatch):
    sent = []

    class Answer:
        status_code = 200
        text = NOV25

        def raise_for_status(self):
            pass

    def get(url, params=None, timeout=None, **kwargs):
        sent.append((url, params, kwargs))
        return Answer()

    monkeypatch.setattr(web.requests, "get", get)
    entsoe_wind.fetch(date(2026, 11, 25), date(2026, 11, 25), token=TOKEN)
    assert all(k["headers"] == {"SECURITY_TOKEN": TOKEN} for _, _, k in sent)
    assert all(TOKEN not in u and TOKEN not in str(p) for u, p, _ in sent)

    def broken(*args, **kwargs):
        raise requests.ConnectionError(f"could not connect (token {TOKEN})")

    monkeypatch.setattr(web, "get", broken)
    with pytest.raises(entsoe.EntsoeError) as caught:
        entsoe_wind.fetch(date(2026, 11, 25), date(2026, 11, 25), token=TOKEN)
    assert TOKEN not in str(caught.value)


def test_the_daily_run_includes_it_and_a_failure_keeps_the_token_out(tmp_path, monkeypatch, capsys):
    assert entsoe_wind.DATASET in cli.DATASETS
    monkeypatch.setenv(entsoe.TOKEN_ENV, TOKEN)

    def broken(*args, **kwargs):
        raise requests.ConnectionError(f"could not connect (token {TOKEN})")

    monkeypatch.setattr(web, "get", broken)
    day = date(2026, 11, 25)
    assert cli.run([entsoe_wind.DATASET], day, day, tmp_path) == [entsoe_wind.DATASET]
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert TOKEN not in (tmp_path / storage.STATUS_FILE).read_text()
