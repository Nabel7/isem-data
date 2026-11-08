"""Tests for the tracker summaries and market value, on small made-up tables."""

import json

import pandas as pd
import pytest

from isem_data import dispatch_down, tracker


def half_hour(text):
    return pd.Timestamp(text, tz="UTC")


def make_farms(rows):
    """Farm half-hours in dispatch_down.farm_half_hours's shape, from a few fields per row."""
    defaults = {"availability_mw": 0.0, "output_mw": 0.0, "constrained": False, "curtailed": False}
    farms = pd.DataFrame([{**defaults, **row} for row in rows])
    farms["timestamp_utc"] = farms["timestamp_utc"].map(half_hour)
    return farms


# Two farms, one in each region, on 10 and 11 November 2026 (winter: Irish time = UTC).
FARMS = make_farms(
    [
        {"timestamp_utc": "2026-11-10 12:00", "unit": "GU_A", "region": "ROI",
         "reason": "constraint", "produced_mwh": 4.0, "lost_mwh": 6.0},
        {"timestamp_utc": "2026-11-10 12:30", "unit": "GU_A", "region": "ROI",
         "reason": "curtailment", "produced_mwh": 4.0, "lost_mwh": 2.0},
        {"timestamp_utc": "2026-11-10 12:00", "unit": "GU_B", "region": "NI",
         "reason": "none", "produced_mwh": 10.0, "lost_mwh": 0.0},
        {"timestamp_utc": "2026-11-11 12:00", "unit": "GU_B", "region": "NI",
         "reason": "constraint", "produced_mwh": 2.0, "lost_mwh": 8.0},
    ]
)  # fmt: skip


def local(farms=FARMS):
    return tracker.with_local_dates(farms)


def test_sums_match_dispatch_down_totals():
    table = tracker.sums(local(), ["region"]).set_index("region")
    for region, rows in FARMS.groupby("region"):
        expected = dispatch_down.totals(rows)
        got = table.loc[region]
        assert got["produced_mwh"] == pytest.approx(expected["produced_gwh"] * 1000)
        assert got["constraint_mwh"] == pytest.approx(expected["constraint_gwh"] * 1000)
        assert got["curtailment_mwh"] == pytest.approx(expected["curtailment_gwh"] * 1000)
        assert got["dispatch_down_pct"] == pytest.approx(expected["dispatch_down_pct"])


def test_whole_island_is_the_sum_of_the_regions():
    table = tracker.daily(local()).set_index(["day", "region"])
    day = "2026-11-10"
    for column in ["produced_mwh", "lost_mwh", "half_hours", "held_down_half_hours"]:
        assert table.loc[(day, "ALL"), column] == (
            table.loc[(day, "ROI"), column] + table.loc[(day, "NI"), column]
        )


def test_counts_and_percentage():
    row = tracker.sums(local(), ["unit"]).set_index("unit").loc["GU_A"]
    assert row["lost_mwh"] == 8.0  # 6 constraint + 2 curtailment
    assert row["dispatch_down_pct"] == pytest.approx(100 * 8 / 16)
    assert row["half_hours"] == 2
    assert row["held_down_half_hours"] == 2


def test_days_and_months_are_irish_clock_time():
    summer = make_farms(
        [
            # 23:30 UTC on 10 Sep is 00:30 on 11 Sep in Irish summer time.
            {"timestamp_utc": "2026-09-10 23:30", "unit": "GU_A", "region": "ROI",
             "reason": "none", "produced_mwh": 1.0, "lost_mwh": 0.0},
            # 23:30 UTC on 30 Sep is already October in Ireland.
            {"timestamp_utc": "2026-09-30 23:30", "unit": "GU_A", "region": "ROI",
             "reason": "none", "produced_mwh": 1.0, "lost_mwh": 0.0},
        ]
    )  # fmt: skip
    dated = tracker.with_local_dates(summer)
    assert dated["day"].tolist() == ["2026-09-11", "2026-10-01"]
    assert dated["month"].tolist() == ["2026-09", "2026-10"]
    # In winter Irish time is UTC.
    assert local()["day"].iloc[0] == "2026-11-10"


def test_monthly_says_how_much_of_the_month_has_data():
    table = tracker.monthly(local())
    row = table[table["region"] == "ALL"].iloc[0]
    assert row["month"] == "2026-11"
    assert row["days_with_data"] == 2
    assert row["days_in_month"] == 30


def test_farm_list_is_worst_first_with_company():
    units = pd.DataFrame({"unit": ["GU_A", "GU_B"], "company": ["Alpha Wind", "Beta Wind"]})
    table = tracker.farm_list(local(), units)
    assert table["unit"].tolist() == ["GU_A", "GU_B"]  # both lost 8 MWh: ties go by unit code
    assert table.set_index("unit").loc["GU_B", "company"] == "Beta Wind"
    assert table.set_index("unit").loc["GU_B", "first_day"] == "2026-11-10"
    assert table.set_index("unit").loc["GU_B", "last_day"] == "2026-11-11"


def test_missing_days_are_listed():
    gap = make_farms(
        [
            {"timestamp_utc": "2026-11-10 12:00", "unit": "GU_A", "region": "ROI",
             "reason": "none", "produced_mwh": 1.0, "lost_mwh": 0.0},
            {"timestamp_utc": "2026-11-13 12:00", "unit": "GU_A", "region": "ROI",
             "reason": "none", "produced_mwh": 1.0, "lost_mwh": 0.0},
        ]
    )  # fmt: skip
    assert tracker.missing_days(tracker.with_local_dates(gap)) == ["2026-11-11", "2026-11-12"]


def make_prices():
    rows = [
        ("2026-11-10 12:00", "ROI", 100.0),
        ("2026-11-10 12:00", "NI", 90.0),  # different, to check each farm gets its own zone
        ("2026-11-10 12:30", "ROI", -5.0),  # negative prices happen
        # no price at all for 11 Nov
    ]
    return pd.DataFrame(
        [
            {"timestamp_utc": half_hour(t), "region": r, "series": "dam_price_eur_mwh", "value": v}
            for t, r, v in rows
        ]
        + [
            {"timestamp_utc": half_hour("2026-11-10 12:00"), "region": "ROI",
             "series": "dam_volume_mw", "value": 4000.0},
        ]
    )  # fmt: skip


def test_market_value_uses_each_farms_zone_price():
    valued = dispatch_down.add_market_value(FARMS, make_prices())
    assert len(valued) == len(FARMS)  # the volume series doesn't add rows
    value = valued.set_index(["unit", "timestamp_utc"])["lost_value_eur"]
    assert value[("GU_A", half_hour("2026-11-10 12:00"))] == 600.0  # 6 MWh x 100
    assert value[("GU_A", half_hour("2026-11-10 12:30"))] == -10.0  # 2 MWh x -5
    assert value[("GU_B", half_hour("2026-11-10 12:00"))] == 0.0  # nothing lost, at 90
    assert pd.isna(value[("GU_B", half_hour("2026-11-11 12:00"))])  # no price


def test_market_value_can_be_added_twice():
    once = dispatch_down.add_market_value(FARMS, make_prices())
    twice = dispatch_down.add_market_value(once, make_prices())
    assert list(twice.columns) == list(once.columns)


def test_totals_include_value_when_present():
    valued = dispatch_down.add_market_value(FARMS, make_prices())
    assert dispatch_down.totals(valued)["lost_value_eur"] == 590.0  # missing price counts as 0
    assert "lost_value_eur" not in dispatch_down.totals(FARMS)


def test_build_and_write(tmp_path):
    t = half_hour("2026-11-10 12:00")
    files = tracker.build(
        availability=pd.DataFrame(
            {"timestamp_utc": [t], "unit": ["GU_A"], "availability_mw": [20.0]}
        ),
        metered=pd.DataFrame(
            {"timestamp_utc": [t], "unit": ["GU_A"], "metered_mwh": [5.0], "region": ["ROI"]}
        ),
        instructions=pd.DataFrame(
            {
                "timestamp_utc": [half_hour("2026-11-09 23:00")],
                "unit": ["GU_A"],
                "instruction_code": ["WIND"],
                "combination_code": ["CURL"],
            }
        ),
        units=pd.DataFrame({"unit": ["GU_A"], "fuel_type": ["WIND"], "company": ["Alpha Wind"]}),
        prices=make_prices(),
    )
    paths = tracker.write(files, tmp_path)

    assert {p.name for p in paths} == {
        "monthly.csv",
        "daily.csv",
        "farms.csv",
        "farm_monthly.csv",
        "farm_daily.csv",
        "meta.json",
        "gu-a.json",  # the farm's page, named after its unit code as it has no name
    }
    daily = pd.read_csv(tmp_path / "daily.csv")
    row = daily[daily["region"] == "ALL"].iloc[0]
    # 20 MW available, 10 MW produced, curtailed: 5 MWh lost, worth 5 x 100.
    assert row["curtailment_mwh"] == 5.0
    assert row["lost_value_eur"] == 500.0
    assert row["dispatch_down_pct"] == 50.0

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["credit"] == "Supported by EirGrid Group Data"
    assert meta["first_day"] == meta["last_day"] == "2026-11-10"
    assert meta["missing_days"] == []


def test_farm_list_adds_semo_names_and_spots_solar():
    units = pd.DataFrame({"unit": ["GU_A", "GU_B"], "company": ["Alpha", "Beta"]})
    names = pd.DataFrame({"unit": ["GU_A", "GU_B"], "semo_name": ["Alpha Windfarm", "Beta Solar"]})
    table = tracker.farm_list(local(), units, names).set_index("unit")
    assert table.loc["GU_A", "semo_name"] == "Alpha Windfarm"
    assert table.loc["GU_A", "technology"] == "wind"
    assert table.loc["GU_B", "technology"] == "solar"


def test_farm_locations_are_written_with_names_and_osm(tmp_path):
    t = half_hour("2026-11-10 12:00")
    files = tracker.build(
        availability=pd.DataFrame(
            {"timestamp_utc": [t], "unit": ["GU_A"], "availability_mw": [20.0]}
        ),
        metered=pd.DataFrame(
            {"timestamp_utc": [t], "unit": ["GU_A"], "metered_mwh": [5.0], "region": ["ROI"]}
        ),
        instructions=pd.DataFrame(
            {
                "timestamp_utc": [half_hour("2026-11-09 23:00")],
                "unit": ["GU_A"],
                "instruction_code": ["WIND"],
                "combination_code": ["CURL"],
            }
        ),
        units=pd.DataFrame({"unit": ["GU_A"], "fuel_type": ["WIND"], "company": ["Alpha"]}),
        prices=make_prices(),
        names=pd.DataFrame({"unit": ["GU_A"], "semo_name": ["Cloncreen Windfarm"]}),
        osm=pd.DataFrame(
            {
                "osm_id": ["way/2"],
                "osm_name": ["Cloncreen Wind Farm"],
                "lat": [53.28],
                "lon": [-7.55],
                "capacity_mw": [105.0],
            }
        ),
    )
    tracker.write(files, tmp_path)
    placed = pd.read_csv(tmp_path / "farm_locations.csv").iloc[0]
    assert placed["farm_name"] == "Cloncreen Wind Farm"
    assert placed["located_by"] == "name"
    assert (placed["lat"], placed["lon"]) == (53.28, -7.55)
    assert "OpenStreetMap" in files["meta.json"]["map_credit"]


def test_slug_makes_a_web_address_safe_id():
    assert tracker.slug("Oweninny Wind Farm") == "oweninny-wind-farm"
    assert tracker.slug("Mín na Leice (Phase 2)") == "min-na-leice-phase-2"
    assert tracker.slug("GU_405140") == "gu-405140"


def test_phases_matched_to_one_farm_share_a_page():
    table = pd.DataFrame(
        {
            "unit": ["GU_1", "GU_2", "GU_3", "GU_4"],
            "semo_name": ["Oweninney Windfarm 1", "Oweninney Windfarm 2", "Lonely Windfarm", ""],
        }
    )
    places = pd.DataFrame(
        {
            "unit": ["GU_1", "GU_2", "GU_3", "GU_4"],
            "farm_name": ["Oweninny Wind Farm", "Oweninny Wind Farm", "", ""],
            "located_by": ["name", "name", "", ""],
        }
    )
    groups = tracker.farm_groups(table, places).set_index("unit")
    assert groups.loc["GU_1", "farm_id"] == groups.loc["GU_2", "farm_id"] == "oweninny-wind-farm"
    assert groups.loc["GU_3", "farm_name"] == "Lonely Windfarm"  # not placed: SEMO's name
    assert groups.loc["GU_4", "farm_id"] == "gu-4"  # no name at all: the unit code


def test_different_names_with_the_same_id_are_numbered():
    table = pd.DataFrame({"unit": ["GU_1", "GU_2"], "semo_name": ["Bog Hill", "Bog-Hill"]})
    ids = set(tracker.farm_groups(table)["farm_id"])
    assert ids == {"bog-hill", "bog-hill-2"}


def test_farm_page_adds_up_its_units_and_keeps_recent_half_hours():
    farms = make_farms(
        [
            {"timestamp_utc": "2026-11-10 12:00", "unit": "GU_1", "region": "ROI",
             "availability_mw": 10.0, "output_mw": 4.0,
             "reason": "constraint", "produced_mwh": 2.0, "lost_mwh": 3.0},
            {"timestamp_utc": "2026-11-10 12:00", "unit": "GU_2", "region": "ROI",
             "availability_mw": 6.0, "output_mw": 6.0,
             "reason": "none", "produced_mwh": 3.0, "lost_mwh": 0.0},
            {"timestamp_utc": "2026-11-10 13:00", "unit": "GU_1", "region": "ROI",
             "availability_mw": 8.0, "output_mw": 8.0,
             "reason": "curtailment", "produced_mwh": 4.0, "lost_mwh": 0.0},
        ]
    )  # fmt: skip
    farms = tracker.with_local_dates(farms)
    units = pd.DataFrame({"unit": ["GU_1", "GU_2"], "company": ["Mayo Wind", "Mayo Wind"]})
    table = tracker.farm_list(farms, units)
    table = tracker.farm_groups(table).assign(farm_id="mayo", farm_name="Mayo").merge(table)

    page = tracker.farm_pages(farms, table)["farms/mayo.json"]

    assert page["name"] == "Mayo"
    assert [u["unit"] for u in page["units"]] == ["GU_1", "GU_2"]  # most lost first
    assert page["companies"] == ["Mayo Wind"]
    assert page["daily"] == [
        {"day": "2026-11-10", "produced_mwh": 9.0, "constraint_mwh": 3.0,
         "curtailment_mwh": 0.0, "lost_mwh": 3.0, "dispatch_down_pct": 25.0,
         "lost_value_eur": 0.0, "half_hours": 3, "held_down_half_hours": 2}
    ]  # fmt: skip
    assert {row["hour"] for row in page["hours"]} == {12, 13}  # Irish time = UTC in November

    recent = page["recent"]
    assert recent["start"] == "2026-10-28T00:00Z"  # 14 days, ending on the last day
    i = 13 * 48 + 24  # 10 Nov 12:00
    assert recent["available_mw"][i : i + 3] == [16.0, None, 8.0]  # 12:30 has no data
    assert recent["output_mw"][i] == 10.0
    assert recent["reason"][i : i + 3] == "C?U"
    assert set(recent["reason"][:i]) == {"?"}
