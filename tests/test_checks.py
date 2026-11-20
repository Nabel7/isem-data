"""Tests for the daily data checks, on small made-up tables."""

import json
from datetime import date

import pandas as pd

from isem_data import checks

TODAY = date(2026, 11, 18)


def up_to_date():
    """Each dataset's newest day, exactly as recent as usual."""
    return {
        dataset: (pd.Timestamp(TODAY) - pd.Timedelta(days=lag)).strftime("%Y-%m-%d")
        for dataset, lag in checks.EXPECTED_LAG_DAYS.items()
    }


# --- freshness ---


def test_datasets_as_recent_as_usual_pass():
    assert checks.freshness(up_to_date(), TODAY).status == "pass"


def test_one_day_late_is_allowed():
    newest = up_to_date()
    newest["demand"] = "2026-11-16"  # usually yesterday (17th); one day late
    assert checks.freshness(newest, TODAY).status == "pass"


def test_two_days_late_fails_and_names_the_dataset():
    newest = up_to_date()
    newest["demand"] = "2026-11-15"
    result = checks.freshness(newest, TODAY)
    assert result.status == "fail"
    assert "demand" in result.summary and "wind" not in result.summary
    assert result.details["days_behind"]["demand"] == 3


def test_a_dataset_with_no_data_fails():
    newest = up_to_date()
    newest["prices"] = None
    result = checks.freshness(newest, TODAY)
    assert result.status == "fail"
    assert "prices (no data)" in result.summary


def test_newest_day_is_the_irish_day(tmp_path):
    # 23:30 UTC on 30 Jun is 00:30 on 1 Jul in Ireland (summer time).
    df = pd.DataFrame({"timestamp_utc": [pd.Timestamp("2026-06-30 23:30", tz="UTC")]})
    df.to_parquet(tmp_path / "wind.parquet")
    assert checks.newest_day("wind", tmp_path) == "2026-07-01"
    assert checks.newest_day("demand", tmp_path) is None


def test_newest_forecast_day_comes_from_the_file_names(tmp_path):
    (tmp_path / "wind_unit_forecasts").mkdir()
    for day in ["2026-11-15", "2026-11-16"]:
        (tmp_path / "wind_unit_forecasts" / f"{day}.parquet").touch()
    assert checks.newest_day("wind_unit_forecasts", tmp_path) == "2026-11-16"


# --- missing days ---

NO_KNOWN_GAPS = pd.DataFrame(columns=["dataset", "day", "note"])


def test_days_without_data():
    assert checks.days_without_data({"2026-10-26", "2026-10-28"}) == ["2026-10-27"]
    assert checks.days_without_data({"2026-10-26"}) == []
    assert checks.days_without_data(set()) == []


def test_a_missing_day_fails():
    result = checks.missing_days({"metered": {"2026-10-26", "2026-10-28"}}, NO_KNOWN_GAPS)
    assert result.status == "fail"
    assert result.details["missing"] == {"metered": ["2026-10-27"]}


def test_a_known_gap_passes():
    known = pd.DataFrame([{"dataset": "metered", "day": "2026-10-27", "note": "never published"}])
    result = checks.missing_days({"metered": {"2026-10-26", "2026-10-28"}}, known)
    assert result.status == "pass"
    assert result.details["known_gaps"] == {"metered": ["2026-10-27"]}


def test_a_known_gap_for_one_dataset_doesnt_excuse_another():
    known = pd.DataFrame([{"dataset": "availability", "day": "2026-10-27", "note": ""}])
    result = checks.missing_days({"metered": {"2026-10-26", "2026-10-28"}}, known)
    assert result.status == "fail"


def test_a_day_missing_only_from_the_four_day_later_instructions_warns():
    days = {"dispatch_instructions_d4": {"2026-10-23", "2026-10-26"}}
    result = checks.missing_days(days, NO_KNOWN_GAPS)
    assert result.status == "warn"
    assert result.details["missing_d4_only"] == {
        "dispatch_instructions_d4": ["2026-10-24", "2026-10-25"]
    }


def test_the_known_gaps_file_reads_and_names_real_datasets_and_days():
    known = checks.read_known_gaps()
    assert list(known.columns) == ["dataset", "day", "note"]
    assert set(known["dataset"]) <= set(checks.EXPECTED_LAG_DAYS)
    pd.to_datetime(known["day"], format="%Y-%m-%d")  # raises if a day is malformed
    assert (known["note"] != "").all()


# --- stuck switches ---

UNITS = [f"GU_{i}" for i in range(10)]
DAY = pd.date_range("2026-11-10 00:00", periods=48, freq="30min", tz="UTC")  # winter: UTC


def make_farms(curtailed_units):
    """Ten farms over one day; the units in `curtailed_units` are curtailed all day."""
    rows = [
        {"timestamp_utc": t, "unit": u, "constrained": False, "curtailed": u in curtailed_units}
        for t in DAY
        for u in UNITS
    ]
    return pd.DataFrame(rows)


def make_instructions(n_curtailment):
    return pd.DataFrame(
        {
            "timestamp_utc": [DAY[0] + pd.Timedelta(minutes=i) for i in range(n_curtailment)],
            "instruction_code": "WIND",
            "combination_code": "CURL",
        }
    )


def test_most_farms_curtailed_with_almost_no_instructions_fails():
    # 28 Oct 2026 before PR #16: 82% of units "curtailed", 2 instructions all day.
    result = checks.stuck_switches(make_farms(UNITS[:8]), make_instructions(2))
    assert result.status == "fail"
    assert result.details["stuck"] == [
        {"day": "2026-11-10", "switch": "curtailed", "share": 0.8, "instructions": 2}
    ]


def test_most_farms_curtailed_with_many_instructions_passes():
    # A real island-wide curtailment comes with thousands of instructions.
    result = checks.stuck_switches(make_farms(UNITS[:8]), make_instructions(500))
    assert result.status == "pass"


def test_a_few_farms_held_down_with_few_instructions_passes():
    result = checks.stuck_switches(make_farms(UNITS[:2]), make_instructions(2))
    assert result.status == "pass"


def test_instructions_of_other_kinds_dont_count():
    instructions = make_instructions(500).assign(instruction_code="GAS")
    result = checks.stuck_switches(make_farms(UNITS[:8]), instructions)
    assert result.status == "fail"


# --- running and writing ---


def test_a_check_that_cannot_run_fails_and_the_others_still_run(tmp_path):
    results = checks.run(tmp_path, TODAY)  # an empty archive
    assert [r.name for r in results] == [
        "freshness",
        "missing_days",
        "stuck_switches",
        "entsoe_matches_semo",
    ]
    assert all(r.status == "fail" for r in results)
    assert "Could not run" in results[1].summary


def test_results_are_written_with_the_worst_status(tmp_path):
    results = [
        checks.Result("a", "pass", "fine"),
        checks.Result("b", "warn", "hmm", {"n": 1}),
    ]
    path = checks.write(results, tmp_path / "new-folder")
    report = json.loads(path.read_text())
    assert report["result"] == "warn"
    assert report["checks"][1] == {
        "name": "b",
        "status": "warn",
        "summary": "hmm",
        "details": {"n": 1},
    }


# --- ENTSO-E against SEMO ---


def semo_prices(values, start="2026-11-19 00:00"):
    times = pd.date_range(start, periods=len(values), freq="30min", tz="UTC")
    rows = [
        (t, region, "dam_price_eur_mwh", v)
        for t, v in zip(times, values, strict=True)
        for region in ["ROI", "NI"]
    ]
    return pd.DataFrame(rows, columns=["timestamp_utc", "region", "series", "value"])


def entsoe_prices(values, start="2026-11-19 00:00"):
    times = pd.date_range(start, periods=len(values), freq="h", tz="UTC")
    return pd.DataFrame(
        {"timestamp_utc": times, "region": "SEM", "series": "dam_price_eur_mwh", "value": values}
    )


def test_entsoe_hours_that_average_semos_half_hours_pass():
    result = checks.entsoe_matches_semo(
        entsoe_prices([105.0, 92.005]), semo_prices([100.0, 110.0, 92.0, 92.01])
    )
    assert result.status == "pass"
    assert result.details["hours_compared"] == 2


def test_an_entsoe_hour_that_differs_from_semo_fails():
    result = checks.entsoe_matches_semo(
        entsoe_prices([105.0, 95.0]), semo_prices([100.0, 110.0, 92.0, 92.01])
    )
    assert result.status == "fail"
    assert result.details["mismatches"][0]["hour_utc"] == "2026-11-19T01:00Z"


def test_hours_semo_doesnt_have_yet_arent_compared():
    # Tomorrow from ENTSO-E, and an hour with only one SEMO half-hour: neither counts.
    result = checks.entsoe_matches_semo(
        entsoe_prices([105.0, 999.0, 999.0]), semo_prices([100.0, 110.0, 50.0])
    )
    assert result.status == "pass"
    assert result.details["hours_compared"] == 1


def test_nothing_to_compare_warns():
    result = checks.entsoe_matches_semo(
        entsoe_prices([105.0], start="2026-11-21 00:00"), semo_prices([100.0, 110.0])
    )
    assert result.status == "warn"


def test_a_missing_trading_day_is_found_though_every_clock_day_has_some_hours():
    # Trading days 18 and 20 Nov, hourly, but not 19 Nov. The 23:00 hour that starts
    # 20 Nov falls on clock day 19 Nov, so by clock day nothing looks missing.
    hours = pd.date_range("2026-11-17 23:00", "2026-11-20 22:00", freq="h", tz="UTC")
    times = pd.Series(hours[(hours < "2026-11-18 23:00") | (hours >= "2026-11-19 23:00")])
    assert checks.days_without_data(checks.irish_days(times)) == []
    assert checks.days_without_data(checks.trading_days(times)) == ["2026-11-19"]
