"""Tests for the EirGrid module. They use made-up rows, so they never touch the internet."""

from datetime import date

import pandas as pd

from isem_data import eirgrid


def row(time, field="WIND_ACTUAL", value=100):
    """One row shaped like the API's output."""
    return {"EffectiveTime": time, "FieldName": field, "Region": "ALL", "Value": value}


def test_to_dataframe_has_tidy_columns():
    df = eirgrid.to_dataframe([row("01-Nov-2026 00:00:00"), row("01-Nov-2026 00:15:00")])
    assert list(df.columns) == eirgrid.COLUMNS
    assert len(df) == 2
    assert df["series"].tolist() == ["wind_actual", "wind_actual"]


def test_local_time_is_converted_to_utc():
    winter = eirgrid.to_dataframe([row("01-Nov-2026 00:00:00")])  # Irish winter time = UTC
    summer = eirgrid.to_dataframe([row("01-Jul-2026 12:00:00")])  # Irish summer time = UTC+1
    assert winter["timestamp_utc"][0] == pd.Timestamp("2026-11-01 00:00", tz="UTC")
    assert summer["timestamp_utc"][0] == pd.Timestamp("2026-07-01 11:00", tz="UTC")


def test_repeated_hour_when_clocks_go_back_is_dropped():
    # On 25 Oct 2026 the clocks went back, so 01:00-01:59 local time happened twice.
    rows = [
        row("25-Oct-2026 00:45:00"),
        row("25-Oct-2026 01:00:00"),
        row("25-Oct-2026 01:00:00"),
        row("25-Oct-2026 02:00:00"),
    ]
    df = eirgrid.to_dataframe(rows)
    assert len(df) == 2


def test_unknown_fields_and_missing_values_are_dropped():
    rows = [
        row("01-Nov-2026 00:00:00", field="SOMETHING_NEW"),
        row("01-Nov-2026 00:15:00", value=None),
    ]
    assert eirgrid.to_dataframe(rows).empty


def test_empty_response_gives_empty_table():
    df = eirgrid.to_dataframe([])
    assert df.empty
    assert list(df.columns) == eirgrid.COLUMNS


def test_fetch_splits_long_ranges_into_weeks(monkeypatch):
    calls = []

    def fake_fetch_rows(dataset, start, end, region):
        calls.append((start, end))
        return [row(start.strftime("%d-%b-%Y 00:00:00"))]

    # Replace the real download (and the pause) with fakes for this test.
    monkeypatch.setattr(eirgrid, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(eirgrid.time, "sleep", lambda seconds: None)

    df = eirgrid.fetch("wind", date(2026, 10, 1), date(2026, 10, 20))

    assert calls == [
        (date(2026, 10, 1), date(2026, 10, 7)),
        (date(2026, 10, 8), date(2026, 10, 14)),
        (date(2026, 10, 15), date(2026, 10, 20)),
    ]
    assert len(df) == 3


class FakeResponse:
    """Stands in for a `requests` response that returns the given rows."""

    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return {"Rows": self.rows}


def test_empty_answers_are_retried(monkeypatch):
    answers = [FakeResponse([]), FakeResponse([row("01-Nov-2026 00:00:00")])]
    monkeypatch.setattr(eirgrid.requests, "get", lambda url, params, timeout: answers.pop(0))
    monkeypatch.setattr(eirgrid.time, "sleep", lambda seconds: None)

    rows = eirgrid.fetch_rows("wind", date(2026, 11, 1), date(2026, 11, 1))

    assert len(rows) == 1


def test_gives_up_after_three_empty_answers(monkeypatch):
    calls = []

    def always_empty(url, params, timeout):
        calls.append(1)
        return FakeResponse([])

    monkeypatch.setattr(eirgrid.requests, "get", always_empty)
    monkeypatch.setattr(eirgrid.time, "sleep", lambda seconds: None)

    assert eirgrid.fetch_rows("wind", date(2026, 11, 1), date(2026, 11, 1)) == []
    assert len(calls) == 3
