"""Tests for the SEMO per-unit balancing reports.

The fixtures are excerpts of real files, keeping the header line and the closing tag:

- `semo_dispatch_instructions_2026-11-03.xml` (BM-037): the first two rows (thermal
  units with no combination code) and every instruction to one wind farm, GU_407510,
  on 3 Nov 2026: eight constraint instructions (LOCL) and two releases (LCLO).
- `semo_availability_2026-10-25.xml` (BM-101): one unit's 50 half-hours on the day
  the clocks went back.
- `semo_metered_2026-11-03.xml` (BM-086): two interconnector rows and one half-hour of
  GU_407510.
- `semo_units_2026-11-03.xml` (BM-091): an interconnector, the wind farm GU_407510 and
  a gas unit, GU_400930. Units are repeated within a file, as in the real one.
"""

from datetime import date
from pathlib import Path

import pandas as pd

from isem_data import balancing, semo, storage

FIXTURES = Path(__file__).parent / "fixtures"


def parse_fixture(dataset, name):
    return balancing.parse((FIXTURES / name).read_text(), dataset)


def instructions():
    return parse_fixture("dispatch_instructions", "semo_dispatch_instructions_2026-11-03.xml")


def test_every_report_gets_its_own_columns():
    for dataset, report in balancing.REPORTS.items():
        df = balancing.parse("", dataset)
        assert df.empty
        assert list(df.columns) == list(report["columns"].values())


def test_wind_instructions_say_constraint_or_release():
    df = instructions()
    wind = df[df["unit"] == "GU_407510"]
    assert len(wind) == 10
    assert set(wind["instruction_code"]) == {"WIND"}
    assert wind["combination_code"].value_counts().to_dict() == {"LOCL": 8, "LCLO": 2}

    first = wind.iloc[0]
    assert first["timestamp_utc"] == pd.Timestamp("2026-11-03 15:29:02", tz="UTC")
    assert first["instruction_mw"] == 11.4
    assert first["participant"] == "PT_400021"


def test_instructions_without_a_combination_code_keep_an_empty_one():
    df = instructions()
    thermal = df[df["instruction_code"] == "MWOF"]
    assert len(thermal) == 2
    assert set(thermal["combination_code"]) == {""}


def test_two_instructions_that_differ_only_in_mw_are_both_kept(tmp_path):
    # Seen in the real files: a unit gets 0 MW and 5 MW instructions issued together.
    row = (
        '<PUB_DailyDispatchInstrD1 EffTime="2026-10-01T06:06:00" ParticipantName="PT_400030" '
        'ResourceName="GU_400362" DispatchInstructionMW="{mw}" InstructionCode="MWOF" '
        'InstrIssueTime="2026-10-01T06:04:36"/>'
    )
    text = f"<OutboundData>{row.format(mw=0)}{row.format(mw=5)}</OutboundData>"
    df = balancing.parse(text, "dispatch_instructions")
    key = balancing.REPORTS["dispatch_instructions"]["key"]
    storage.save(df, "dispatch_instructions", data_dir=tmp_path, key=key)
    saved = storage.load("dispatch_instructions", data_dir=tmp_path)
    assert saved["instruction_mw"].tolist() == [0.0, 5.0]


def test_the_day_the_clocks_go_back_has_50_distinct_utc_half_hours():
    df = parse_fixture("availability", "semo_availability_2026-10-25.xml")
    assert len(df) == 50
    assert df["timestamp_utc"].is_unique
    # The trading day of 25 Oct starts at midnight Irish summer time: 23:00 UTC on 24 Oct.
    assert df["timestamp_utc"].iloc[0] == pd.Timestamp("2026-10-24 23:00", tz="UTC")
    assert df["timestamp_utc"].iloc[-1] == pd.Timestamp("2026-10-25 23:30", tz="UTC")
    assert df["availability_mw"].dtype == float


def test_metered_output_is_read_with_unit_type_and_region():
    df = parse_fixture("metered", "semo_metered_2026-11-03.xml")
    wind = df[df["unit"] == "GU_407510"].iloc[0]
    assert wind["metered_mwh"] == 5.075  # energy in the half-hour: about 10 MW on average
    assert wind["unit_type"] == "GEN"
    assert wind["region"] == "ROI"
    assert set(df["unit_type"]) == {"GEN", "IU"}


def test_saving_twice_changes_nothing(tmp_path):
    for dataset, name in [
        ("dispatch_instructions", "semo_dispatch_instructions_2026-11-03.xml"),
        ("availability", "semo_availability_2026-10-25.xml"),
        ("metered", "semo_metered_2026-11-03.xml"),
    ]:
        df = parse_fixture(dataset, name)
        key = balancing.REPORTS[dataset]["key"]
        assert not df.duplicated(subset=key).any()
        storage.save(df, dataset, data_dir=tmp_path, key=key)
        storage.save(df, dataset, data_dir=tmp_path, key=key)
        assert len(storage.load(dataset, data_dir=tmp_path)) == len(df)


def test_fetch_asks_semo_for_the_right_report(monkeypatch):
    asked_for = []

    def fake_list_files(files):
        asked_for.append(files)
        return {date(2026, 11, 3): "PUB_DailyMeterDataD1_202611051336.xml"}

    monkeypatch.setattr(semo, "list_files", fake_list_files)
    monkeypatch.setattr(
        semo, "download", lambda name: (FIXTURES / "semo_metered_2026-11-03.xml").read_text()
    )
    monkeypatch.setattr(semo.time, "sleep", lambda seconds: None)

    df = semo.fetch(
        date(2026, 11, 3),
        date(2026, 11, 3),
        parse=balancing.parser("metered"),
        files=balancing.REPORTS["metered"]["files"],
    )

    assert asked_for == ["PUB_DailyMeterDataD1"]
    assert len(df) == 3


def test_unit_list_keeps_one_row_per_unit_with_company_and_fuel(tmp_path):
    df = parse_fixture("units", "semo_units_2026-11-03.xml")
    assert len(df) == 7  # repeated rows, as SEMO publishes them

    storage.save(df, "units", data_dir=tmp_path, key=balancing.REPORTS["units"]["key"])
    units = storage.load("units", data_dir=tmp_path).set_index("unit")

    assert len(units) == 3
    assert units.loc["GU_407510", "fuel_type"] == "WIND"
    assert units.loc["GU_407510", "company"] == "SSE Airtricity Limited"
    assert units.loc["IEU_ROIEWIC", "fuel_type"] == ""  # interconnectors have no fuel
