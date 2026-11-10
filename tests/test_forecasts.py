"""Tests for saving SEMO's per-unit wind forecasts (BM-012) as published.

`fixtures/semo_wind_unit_forecast_2026-11-08.xml` is the header and four rows of a real
file published at 06:15 on 8 Nov 2026: two 15-minute forecasts for each of two units.
"""

from datetime import date
from pathlib import Path

import pandas as pd

from isem_data import forecasts, semo

FIXTURE = Path(__file__).parent / "fixtures" / "semo_wind_unit_forecast_2026-11-08.xml"


def test_parse_keeps_when_each_version_was_published():
    df = forecasts.parse(FIXTURE.read_text())
    assert list(df.columns) == forecasts.COLUMNS
    assert len(df) == 4
    assert (df["published_utc"] == pd.Timestamp("2026-11-08 06:15:01", tz="UTC")).all()
    first = df.iloc[0]
    assert first["timestamp_utc"] == pd.Timestamp("2026-11-07 23:00", tz="UTC")
    assert first["unit"] == "GU_500010"
    assert first["region"] == "NI"
    assert first["forecast_mw"] == 0.544
    assert set(df["unit"]) == {"GU_500010", "GU_400021"}


def listing(*items):
    return pd.DataFrame(
        [
            {"name": name, "day": day, "published_utc": pd.Timestamp(published, tz="UTC")}
            for name, day, published in items
        ]
    )


def test_versions_already_saved_are_not_downloaded_again(monkeypatch, tmp_path):
    monkeypatch.setattr(
        forecasts,
        "list_publications",
        lambda: listing(
            ("a.xml", date(2026, 11, 8), "2026-11-08 06:15:01"),
            ("b.xml", date(2026, 11, 9), "2026-11-09 06:15:01"),  # outside the dates asked for
        ),
    )
    downloads = []

    def fake_download(name):
        downloads.append(name)
        return FIXTURE.read_text()

    monkeypatch.setattr(semo, "download", fake_download)
    monkeypatch.setattr(forecasts.time, "sleep", lambda seconds: None)

    assert forecasts.fetch_and_save(date(2026, 11, 8), date(2026, 11, 8), data_dir=tmp_path) == 1
    assert forecasts.fetch_and_save(date(2026, 11, 8), date(2026, 11, 8), data_dir=tmp_path) == 0
    assert downloads == ["a.xml"]
    assert (tmp_path / "wind_unit_forecasts" / "2026-11-08.parquet").exists()
    assert len(forecasts.load(tmp_path)) == 4


def test_load_with_nothing_saved_gives_an_empty_table(tmp_path):
    assert list(forecasts.load(tmp_path).columns) == forecasts.COLUMNS
