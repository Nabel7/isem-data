"""Tests for the EirGrid module. They use made-up rows, so they never touch the internet."""

from datetime import date

import pandas as pd
import pytest

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


def test_fetch_rejects_a_reversed_date_range():
    with pytest.raises(ValueError, match="start must be on or before end"):
        eirgrid.fetch("wind", date(2026, 10, 20), date(2026, 10, 1))


def full_day(field, day="01-Nov-2026", skip=()):
    """Rows for every 15 minutes of one day, leaving out the times in `skip`."""
    times = [f"{h:02d}:{m:02d}:00" for h in range(24) for m in (0, 15, 30, 45)]
    return [row(f"{day} {t}", field=field) for t in times if t not in skip]


def test_find_gaps_is_empty_when_nothing_is_missing():
    rows = full_day("WIND_ACTUAL") + full_day("WIND_FCAST")
    df = eirgrid.to_dataframe(rows)
    assert eirgrid.find_gaps(df, "wind", date(2026, 11, 1), date(2026, 11, 1)).empty


def test_find_gaps_reports_a_missing_hour():
    missing_hour = ("14:00:00", "14:15:00", "14:30:00", "14:45:00")
    rows = full_day("WIND_ACTUAL", skip=missing_hour) + full_day("WIND_FCAST")
    df = eirgrid.to_dataframe(rows)

    gaps = eirgrid.find_gaps(df, "wind", date(2026, 11, 1), date(2026, 11, 1))

    assert len(gaps) == 1
    gap = gaps.iloc[0]
    assert gap["series"] == "wind_actual"
    assert gap["first_missing"] == pd.Timestamp("2026-11-01 14:00", tz="UTC")
    assert gap["last_missing"] == pd.Timestamp("2026-11-01 14:45", tz="UTC")
    assert gap["readings"] == 4


def test_find_gaps_counts_empty_values_and_missing_series():
    rows = full_day("WIND_ACTUAL")
    rows[0]["Value"] = None  # EirGrid sent the reading, but with no value
    df = eirgrid.to_dataframe(rows)  # and no WIND_FCAST rows at all

    gaps = eirgrid.find_gaps(df, "wind", date(2026, 11, 1), date(2026, 11, 1))

    assert gaps[["series", "readings"]].values.tolist() == [
        ["wind_actual", 1],
        ["wind_forecast", 96],
    ]


def test_find_gaps_handles_clock_change_days():
    # 25 Oct 2026 lasts 25 hours, so it has 100 readings, 8 of them in the repeated hour.
    rows = full_day("SYSTEM_DEMAND", day="25-Oct-2026")
    df = eirgrid.to_dataframe(rows)

    gaps = eirgrid.find_gaps(df, "demand", date(2026, 10, 25), date(2026, 10, 25))

    assert gaps["readings"].sum() == 8
