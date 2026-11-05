"""Tests for saving and loading. `tmp_path` is a fresh empty folder pytest gives each test."""

import pandas as pd

from isem_data import storage


def table(values, series="wind_actual"):
    """A small tidy table with one row per value, 15 minutes apart."""
    return pd.DataFrame(
        {
            "timestamp_utc": pd.date_range(
                "2026-11-01", periods=len(values), freq="15min", tz="UTC"
            ),
            "region": "ALL",
            "series": series,
            "value_mw": values,
        }
    )


def test_save_then_load_gives_the_same_table(tmp_path):
    df = table([1.0, 2.0, 3.0])
    storage.save(df, "wind", data_dir=tmp_path)
    pd.testing.assert_frame_equal(storage.load("wind", data_dir=tmp_path), df)


def test_saving_twice_does_not_duplicate_rows(tmp_path):
    storage.save(table([1.0, 2.0]), "wind", data_dir=tmp_path)
    storage.save(table([1.0, 2.0]), "wind", data_dir=tmp_path)
    assert len(storage.load("wind", data_dir=tmp_path)) == 2


def test_newer_values_replace_older_ones(tmp_path):
    storage.save(table([1.0, 2.0]), "wind", data_dir=tmp_path)
    storage.save(table([9.0]), "wind", data_dir=tmp_path)  # a revised first value
    assert storage.load("wind", data_dir=tmp_path)["value_mw"].tolist() == [9.0, 2.0]


def test_different_series_are_kept_apart(tmp_path):
    storage.save(table([1.0]), "wind", data_dir=tmp_path)
    storage.save(table([5.0], series="wind_forecast"), "wind", data_dir=tmp_path)
    assert len(storage.load("wind", data_dir=tmp_path)) == 2


def test_default_data_dir_does_not_depend_on_the_current_folder():
    assert storage.DATA_DIR.is_absolute()
    assert (storage.DATA_DIR.parent / "pyproject.toml").exists()
