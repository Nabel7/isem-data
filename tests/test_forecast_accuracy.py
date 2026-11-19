"""Tests for scoring EirGrid's wind forecast, on made-up farms and versions."""

from datetime import date

import pandas as pd
import pytest

from isem_data import forecast_accuracy as fa

# Winter, so UTC is Irish time: trading day 3 Nov runs 2 Nov 23:00 to 3 Nov 23:00.
DAY_3 = pd.date_range("2026-11-02 23:00", periods=48, freq="30min", tz="UTC")


def truth(times=DAY_3, available=100.0, output=60.0, units=("GU_1", "GU_2")):
    return pd.DataFrame(
        [
            {"timestamp_utc": t, "unit": u, "available_mw": available, "output_mw": output}
            for t in times
            for u in units
        ]
    )


def version(published, mw, units=("GU_1", "GU_2", "GU_SMALL"), times=DAY_3):
    """A forecast version: every 15 minutes of `times`, `mw` for each unit."""
    quarter_hours = pd.date_range(times[0], times[-1] + pd.Timedelta(minutes=15), freq="15min")
    return pd.DataFrame(
        [
            {
                "published_utc": pd.Timestamp(published, tz="UTC"),
                "timestamp_utc": t,
                "unit": u,
                "forecast_mw": mw,
            }
            for t in quarter_hours
            for u in units
        ]
    )


def test_totals_add_up_only_the_farms_with_both_a_forecast_and_availability():
    totals = fa.totals_for(version("2026-11-02 06:15", 90.0), truth())

    first = totals.iloc[0]
    assert first["farms"] == 2  # GU_SMALL has no availability, so it's left out
    assert first["forecast_mw"] == 180.0
    assert first["available_mw"] == 200.0
    assert first["output_mw"] == 120.0
    assert len(totals) == 48


def test_quarter_hours_are_averaged_into_half_hours():
    table = version("2026-11-02 06:15", 0.0, units=("GU_1",))
    table.loc[table.index % 2 == 1, "forecast_mw"] = 10.0  # 0 then 10 in each half-hour
    totals = fa.totals_for(table, truth(units=("GU_1",)))
    assert (totals["forecast_mw"] == 5.0).all()


def test_hours_ahead_counts_from_publication_and_drops_the_past():
    published = "2026-11-03 06:00"  # during the day: half-hours before it are dropped
    totals = fa.totals_for(version(published, 90.0), truth())
    assert totals["hours_ahead"].min() == 0.0
    assert totals["timestamp_utc"].min() == pd.Timestamp("2026-11-03 06:00", tz="UTC")


def test_by_hours_ahead_averages_the_size_and_sign_of_the_miss():
    totals = pd.DataFrame(
        {
            "hours_ahead": [1.0, 1.5, 30.0],
            "forecast_mw": [110.0, 90.0, 150.0],
            "available_mw": [100.0, 100.0, 100.0],
        }
    )
    table = fa.by_hours_ahead(totals).set_index("hours_ahead")
    assert table.loc[1.0, "mae_mw"] == 10.0
    assert table.loc[1.0, "bias_mw"] == 0.0
    assert table.loc[30.0, "bias_mw"] == 50.0


def test_day_ahead_takes_the_last_version_before_the_cutoff():
    both = pd.concat(
        [
            version("2026-11-02 00:15", 70.0),
            version("2026-11-02 06:15", 80.0),  # the one the 3 Nov auction had
            version("2026-11-02 12:15", 99.0),  # after the auction: never used
        ]
    )
    chosen = fa.day_ahead(fa.totals_for(both, truth()))

    assert len(chosen) == 48
    assert (chosen["forecast_mw"] == 160.0).all()
    assert (chosen["day"] == date(2026, 11, 3)).all()


def test_day_ahead_falls_back_to_an_older_version_and_no_further():
    # The 3 Nov auction's cutoff is 2 Nov 09:00; a version may be up to two days older.
    old = fa.totals_for(version("2026-10-31 06:15", 50.0), truth())  # 2.1 days older
    assert fa.day_ahead(old).empty
    recent = fa.totals_for(version("2026-10-31 18:15", 50.0), truth())  # 1.6 days older
    assert (fa.day_ahead(recent)["forecast_mw"] == 100.0).all()


def test_latest_before_is_the_newest_version_for_each_half_hour():
    both = pd.concat([version("2026-11-02 06:15", 80.0), version("2026-11-02 18:15", 95.0)])
    newest = fa.latest_before(fa.totals_for(both, truth()))
    assert len(newest) == 48
    assert (newest["forecast_mw"] == 190.0).all()


def test_version_totals_reads_every_saved_file(tmp_path):
    folder = tmp_path / "wind_unit_forecasts"
    folder.mkdir()
    version("2026-11-01 06:15", 70.0).to_parquet(folder / "2026-11-01.parquet")
    version("2026-11-02 06:15", 80.0).to_parquet(folder / "2026-11-02.parquet")

    totals = fa.version_totals(truth(), data_dir=tmp_path)

    assert totals["published_utc"].nunique() == 2
    assert totals["forecast_mw"].tolist()[:1] == pytest.approx([140.0])
