"""Tests for the SEMO parser.

`fixtures/semo_da_2026-11-02.csv` is the first 54 lines of a real results file
(the auction held on 1 Nov 2026 for delivery on 2 Nov): the header, both market
sections, and the first company order.
"""

from pathlib import Path

import pandas as pd

from isem_data import eirgrid, semo

FIXTURE = Path(__file__).parent / "fixtures" / "semo_da_2026-11-02.csv"


def parse_fixture():
    return semo.parse_market_result(FIXTURE.read_text())


def test_real_file_gives_48_half_hours_per_region_and_series():
    df = parse_fixture()
    counts = df.groupby(["region", "series"]).size()
    assert counts.to_dict() == {
        ("NI", "dam_price_eur_mwh"): 48,
        ("NI", "dam_volume_mw"): 48,
        ("ROI", "dam_price_eur_mwh"): 48,
        ("ROI", "dam_volume_mw"): 48,
    }


def test_values_and_timestamps_are_read_correctly():
    df = parse_fixture()
    roi = df[df["region"] == "ROI"]
    first_price = roi[roi["series"] == "dam_price_eur_mwh"].iloc[0]
    first_volume = roi[roi["series"] == "dam_volume_mw"].iloc[0]

    # The first half-hour of 2 Nov delivery starts at 23:00 UTC on 1 Nov.
    assert first_price["timestamp_utc"] == pd.Timestamp("2026-11-01 23:00", tz="UTC")
    assert first_price["value"] == 248.78  # written "248,78" in the file
    assert first_volume["value"] == 4068.9  # written "4068,9"


def test_gbp_prices_and_company_orders_are_left_out():
    df = parse_fixture()
    assert set(df["series"]) == {"dam_price_eur_mwh", "dam_volume_mw"}
    assert len(df) == 4 * 48


def test_uses_the_same_tidy_columns_as_eirgrid():
    df = parse_fixture()
    assert list(df.columns) == eirgrid.COLUMNS
    assert str(df["timestamp_utc"].dt.tz) == "UTC"
    assert df["value"].dtype == float


def test_empty_cells_are_dropped():
    text = "\n".join(
        [
            "Market;ROI-DA",
            "Index prices;30;EUR",
            "2026-11-01T23:00:00Z;2026-11-01T23:30:00Z",
            "100,5;",  # the second half-hour has no price
        ]
    )
    df = semo.parse_market_result(text)
    assert df["value"].tolist() == [100.5]


def test_a_day_with_more_half_hours_is_read_in_full():
    # When the clocks go back the delivery day has 50 half-hours. The parser
    # doesn't assume 48: it reads however many timestamps the file has.
    timestamps = pd.date_range("2026-10-24 23:00", periods=50, freq="30min", tz="UTC")
    text = "\n".join(
        [
            "Market;ROI-DA",
            "Index prices;30;EUR",
            ";".join(t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in timestamps),
            ";".join(["100,0"] * 50),
        ]
    )
    assert len(semo.parse_market_result(text)) == 50
