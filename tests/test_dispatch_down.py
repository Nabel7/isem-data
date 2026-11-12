"""Tests for the dispatch-down calculation, on small made-up tables.

One wind farm, GU_1, over five half-hours from 00:00 UTC on 3 Nov 2026, with
these instructions:

    00:10  LOCL  constraint on
    00:40  CURL  curtailment on   (now both)
    01:10  CRLO  curtailment off  (still constrained)
    01:40  LCLO  constraint off
                                  -> 02:00 is free

Each half-hour is judged at its middle (00:15, 00:45, ...).
"""

import pandas as pd
import pytest

from isem_data import dispatch_down

DAY = "2026-11-03"


def at(time):
    return pd.Timestamp(f"{DAY} {time}", tz="UTC")


HALF_HOURS = [at(t) for t in ["00:00", "00:30", "01:00", "01:30", "02:00"]]


def make_units():
    return pd.DataFrame(
        {
            "unit": ["GU_1", "GU_2", "GU_GAS"],
            "fuel_type": ["WIND", "WIND", "GAS"],
        }
    )


def make_availability(units=("GU_1",), mw=20.0):
    return pd.DataFrame(
        [{"timestamp_utc": t, "unit": u, "availability_mw": mw} for u in units for t in HALF_HOURS]
    )


def make_metered(units=("GU_1",), mwh=5.0):
    # 5 MWh in a half-hour is 10 MW on average: 10 MW below a 20 MW availability.
    return pd.DataFrame(
        [
            {"timestamp_utc": t, "unit": u, "metered_mwh": mwh, "region": "ROI"}
            for u in units
            for t in HALF_HOURS
        ]
    )


def make_instructions(unit="GU_1"):
    changes = [("00:10", "LOCL"), ("00:40", "CURL"), ("01:10", "CRLO"), ("01:40", "LCLO")]
    return pd.DataFrame(
        [
            {
                "timestamp_utc": at(time),
                "unit": unit,
                "instruction_code": "WIND",
                "combination_code": code,
            }
            for time, code in changes
        ]
    )


def run(**overrides):
    tables = {
        "availability": make_availability(),
        "metered": make_metered(),
        "instructions": make_instructions(),
        "units": make_units(),
        "start": at("00:00"),
    }
    tables.update(overrides)
    return dispatch_down.farm_half_hours(**tables)


def test_switches_are_tracked_separately():
    farms = run()
    assert farms["constrained"].tolist() == [True, True, True, False, False]
    assert farms["curtailed"].tolist() == [False, True, False, False, False]


def test_lifting_a_curtailment_leaves_the_constraint_on():
    # The bug this module was written to avoid: CRLO at 01:10 must not free the farm.
    farms = run()
    assert farms.loc[farms["timestamp_utc"] == at("01:00"), "reason"].item() == "constraint"


def test_constraint_wins_when_both_are_on():
    farms = run()
    assert farms["reason"].tolist() == [
        "constraint",
        "constraint",  # both on
        "constraint",
        "none",
        "none",
    ]


def test_metered_energy_is_doubled_to_get_mw():
    farms = run()
    assert (farms["output_mw"] == 10.0).all()
    assert (farms["produced_mwh"] == 5.0).all()


def test_energy_is_lost_only_while_an_instruction_is_in_force():
    farms = run()
    # 20 MW available, 10 MW produced: 10 MW for half an hour = 5 MWh.
    assert farms["lost_mwh"].tolist() == [5.0, 5.0, 5.0, 0.0, 0.0]


def test_output_above_availability_counts_as_nothing_lost():
    farms = run(metered=make_metered(mwh=12.0))  # 24 MW, above the 20 MW available
    assert (farms["lost_mwh"] == 0.0).all()


def test_a_farm_with_no_instructions_loses_nothing():
    farms = run(
        availability=make_availability(units=("GU_1", "GU_2")),
        metered=make_metered(units=("GU_1", "GU_2")),
    )
    other = farms[farms["unit"] == "GU_2"]
    assert len(other) == 5
    assert (other["reason"] == "none").all()
    assert (other["lost_mwh"] == 0.0).all()


def test_one_farms_instructions_dont_apply_to_another():
    farms = run(
        availability=make_availability(units=("GU_1", "GU_2")),
        metered=make_metered(units=("GU_1", "GU_2")),
        instructions=make_instructions(unit="GU_2"),
    )
    assert (farms.loc[farms["unit"] == "GU_1", "reason"] == "none").all()
    assert (farms.loc[farms["unit"] == "GU_2", "reason"] != "none").any()


def test_units_that_are_not_wind_are_left_out():
    farms = run(
        availability=make_availability(units=("GU_1", "GU_GAS")),
        metered=make_metered(units=("GU_1", "GU_GAS")),
    )
    assert set(farms["unit"]) == {"GU_1"}


def test_other_instruction_codes_are_ignored():
    instructions = make_instructions()
    instructions["instruction_code"] = "MWOF"  # thermal-style instructions, not wind
    farms = run(instructions=instructions)
    assert (farms["reason"] == "none").all()


def test_half_hours_before_start_are_left_out():
    farms = run(start=at("01:00"))
    assert farms["timestamp_utc"].min() == at("01:00")
    assert len(farms) == 3


def test_start_defaults_to_the_first_whole_day_of_instructions():
    farms = run(start=None)
    # Instructions start at 00:10 on 3 Nov, so the first whole day is 4 Nov: nothing left.
    assert farms.empty
    assert list(farms.columns) == dispatch_down.COLUMNS


def test_half_hours_missing_from_either_input_are_left_out():
    metered = make_metered().iloc[1:]  # no meter reading for 00:00
    farms = run(metered=metered)
    assert farms["timestamp_utc"].min() == at("00:30")


def test_totals():
    totals = dispatch_down.totals(run())
    assert totals["produced_gwh"] == pytest.approx(25 / 1000)
    assert totals["constraint_gwh"] == pytest.approx(15 / 1000)
    assert totals["curtailment_gwh"] == 0.0
    # 15 MWh lost out of 25 + 15 = 40 MWh the wind allowed.
    assert totals["dispatch_down_pct"] == pytest.approx(100 * 15 / 40)


def test_totals_split_curtailment_when_only_curtailed():
    instructions = make_instructions()
    instructions = instructions[instructions["combination_code"].isin(["CURL", "CRLO"])]
    totals = dispatch_down.totals(run(instructions=instructions))
    assert totals["constraint_gwh"] == 0.0
    assert totals["curtailment_gwh"] == pytest.approx(5 / 1000)  # only 00:30 is curtailed


def test_totals_of_nothing_is_not_a_division_error():
    totals = dispatch_down.totals(run(start=at("03:00")))
    assert totals["produced_gwh"] == 0.0
    assert pd.isna(totals["dispatch_down_pct"])


def instruction(time, unit, combination_code, instruction_code="WIND"):
    return {
        "timestamp_utc": pd.Timestamp(time, tz="UTC"),
        "unit": unit,
        "instruction_code": instruction_code,
        "combination_code": combination_code,
    }


def held_down_on_the_12th(instructions):
    """GU_1 at midday on 12 Nov, given these instructions (it was curtailed on the 10th)."""
    midday = pd.Timestamp("2026-11-12 12:00", tz="UTC")
    farms = dispatch_down.farm_half_hours(
        pd.DataFrame({"timestamp_utc": [midday], "unit": ["GU_1"], "availability_mw": [20.0]}),
        pd.DataFrame(
            {"timestamp_utc": [midday], "unit": ["GU_1"], "metered_mwh": [5.0], "region": ["ROI"]}
        ),
        pd.DataFrame(instructions),
        make_units(),
    )
    return farms.iloc[0]


def test_a_day_with_no_instructions_turns_every_switch_off():
    # Curtailed on the 10th. SEMO published nothing for the 11th, so the release was never
    # seen. On the 12th the farm must not still count as curtailed.
    instructions = [
        instruction("2026-11-10 20:00", "GU_1", "CURL"),
        instruction("2026-11-12 09:00", "GU_GAS", "", "SYNC"),  # the 12th does have a file
    ]
    assert dispatch_down.missing_days(pd.DataFrame(instructions)) == [
        pd.Timestamp("2026-11-11 00:00", tz="UTC")
    ]
    row = held_down_on_the_12th(instructions)
    assert not row["curtailed"]
    assert row["lost_mwh"] == 0.0


def test_a_switch_stays_on_across_days_that_do_have_instructions():
    # The same, but the 11th has a file (an instruction for another unit): no gap, so the
    # curtailment still stands on the 12th.
    instructions = [
        instruction("2026-11-10 20:00", "GU_1", "CURL"),
        instruction("2026-11-11 08:00", "GU_GAS", "", "SYNC"),
        instruction("2026-11-12 09:00", "GU_GAS", "", "SYNC"),
    ]
    assert dispatch_down.missing_days(pd.DataFrame(instructions)) == []
    row = held_down_on_the_12th(instructions)
    assert row["curtailed"]
    assert row["lost_mwh"] == 5.0


def test_an_instruction_after_the_gap_turns_the_switch_back_on():
    instructions = [
        instruction("2026-11-10 20:00", "GU_1", "CURL"),
        instruction("2026-11-12 09:00", "GU_1", "LOCL"),
    ]
    row = held_down_on_the_12th(instructions)
    assert row["constrained"] and not row["curtailed"]
    assert row["reason"] == "constraint"
