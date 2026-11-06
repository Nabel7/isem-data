"""Tests for the SEMO parser.

`fixtures/semo_da_2026-11-02.csv` is the first 54 lines of a real results file
(the auction held on 1 Nov 2026 for delivery on 2 Nov): the header, both market
sections, and the first company order.

`fixtures/semo_da_orders_2026-11-02.csv` is three excerpts from the company order
part of the same file: the start of the NI orders (three orders), the start of the
ROI orders, and an SSE unit with two orders sharing one name (linear and scalable
complex).
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from isem_data import eirgrid, semo, storage

FIXTURE = Path(__file__).parent / "fixtures" / "semo_da_2026-11-02.csv"
ORDERS_FIXTURE = Path(__file__).parent / "fixtures" / "semo_da_orders_2026-11-02.csv"


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


class FakeResponse:
    """Stands in for a `requests` response that returns the given JSON."""

    def __init__(self, answer):
        self.answer = answer

    def raise_for_status(self):
        pass

    def json(self):
        return self.answer


def listing_page(items, total_pages):
    """One page of SEMO's file list, shaped like the real API's answer."""
    return {
        "items": [{"ResourceName": name, "Date": f"{day}T11:00:00"} for day, name in items],
        "pagination": {"totalPages": total_pages},
    }


def test_list_files_reads_every_page(monkeypatch):
    pages = {
        1: listing_page([("2026-11-01", "file_a.csv"), ("2026-11-02", "file_b.csv")], 2),
        2: listing_page([("2026-11-03", "file_c.csv")], 2),
    }
    monkeypatch.setattr(
        semo.requests, "get", lambda url, params, timeout: FakeResponse(pages[params["page"]])
    )

    assert semo.list_files() == {
        date(2026, 11, 1): "file_a.csv",
        date(2026, 11, 2): "file_b.csv",
        date(2026, 11, 3): "file_c.csv",
    }


def test_list_files_asks_for_the_requested_report(monkeypatch):
    asked_for = []

    def fake_get(url, params, timeout):
        asked_for.append(params["ResourceName"])
        return FakeResponse(listing_page([], 1))

    monkeypatch.setattr(semo.requests, "get", fake_get)

    semo.list_files()
    semo.list_files("PUB_DailyMeterDataD1")
    assert asked_for == ["MarketResult_SEM-DA", "PUB_DailyMeterDataD1"]


def test_list_files_keeps_the_newer_file_when_a_day_has_two(monkeypatch):
    # The list is requested oldest first, so a re-run for the same day comes later.
    page = listing_page([("2026-11-02", "first_run.csv"), ("2026-11-02", "re_run.csv")], 1)
    monkeypatch.setattr(semo.requests, "get", lambda url, params, timeout: FakeResponse(page))

    assert semo.list_files() == {date(2026, 11, 2): "re_run.csv"}


def test_fetch_downloads_each_day_and_warns_about_missing_ones(monkeypatch, capsys):
    downloaded = []

    def fake_download(name):
        downloaded.append(name)
        return FIXTURE.read_text()

    monkeypatch.setattr(semo, "list_files", lambda files: {date(2026, 11, 2): "nov2.csv"})
    monkeypatch.setattr(semo, "download", fake_download)
    monkeypatch.setattr(semo.time, "sleep", lambda seconds: None)

    df = semo.fetch(date(2026, 11, 2), date(2026, 11, 3))

    assert downloaded == ["nov2.csv"]
    assert len(df) == 4 * 48
    assert "no MarketResult_SEM-DA file for 2026-11-03" in capsys.readouterr().out


def test_fetch_with_no_files_gives_an_empty_table(monkeypatch):
    monkeypatch.setattr(semo, "list_files", lambda files: {})
    df = semo.fetch(date(2026, 11, 2), date(2026, 11, 2))
    assert df.empty
    assert list(df.columns) == semo.COLUMNS


def test_fetch_rejects_a_reversed_date_range():
    with pytest.raises(ValueError, match="start must be on or before end"):
        semo.fetch(date(2026, 11, 3), date(2026, 11, 1))


def parse_orders_fixture():
    return semo.parse_orders(ORDERS_FIXTURE.read_text())


def test_orders_give_one_row_per_order_and_half_hour():
    df = parse_orders_fixture()
    assert list(df.columns) == semo.ORDER_COLUMNS
    assert len(df) == 5 * 48  # five orders in the excerpt


def test_orders_take_company_unit_and_region_from_the_lines_above():
    df = parse_orders_fixture()
    first = df.iloc[0]
    assert first["region"] == "NI"
    assert first["participant"] == "AARHUS"
    assert first["unit"] == "AU_500145"
    assert first["order_type"] == "linear"
    assert first["order_name"] == "AARHUSR01API"
    assert first["timestamp_utc"] == pd.Timestamp("2026-11-01 23:00", tz="UTC")
    assert first["quantity_mw"] == -18.8  # written "-18,8": a sale
    assert set(df["region"]) == {"NI", "ROI"}


def test_a_unit_with_two_orders_of_the_same_name_keeps_both():
    df = parse_orders_fixture()
    sse = df[df["unit"] == "GU_400770"]
    assert sorted(sse["order_type"].unique()) == ["linear", "scalable_complex"]
    assert not df.duplicated(subset=semo.ORDER_KEY).any()


def test_both_orders_survive_saving_and_saving_twice_changes_nothing(tmp_path):
    df = parse_orders_fixture()
    storage.save(df, "orders", data_dir=tmp_path, key=semo.ORDER_KEY)
    storage.save(df, "orders", data_dir=tmp_path, key=semo.ORDER_KEY)
    assert len(storage.load("orders", data_dir=tmp_path)) == 5 * 48


def test_each_parser_reads_only_its_own_part_of_the_file():
    # The prices fixture ends with the first NI order; the orders fixture has no prices.
    assert len(semo.parse_orders(FIXTURE.read_text())) == 48
    assert semo.parse_market_result(ORDERS_FIXTURE.read_text()).empty


def test_fetch_can_read_orders_instead_of_prices(monkeypatch):
    monkeypatch.setattr(semo, "list_files", lambda files: {date(2026, 11, 2): "nov2.csv"})
    monkeypatch.setattr(semo, "download", lambda name: ORDERS_FIXTURE.read_text())
    monkeypatch.setattr(semo.time, "sleep", lambda seconds: None)

    df = semo.fetch(date(2026, 11, 2), date(2026, 11, 2), parse=semo.parse_orders)

    assert list(df.columns) == semo.ORDER_COLUMNS
    assert len(df) == 5 * 48
