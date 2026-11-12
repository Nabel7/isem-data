"""Read SEMO's per-unit balancing market reports: what each unit was told to do,
what it could have produced, and what it actually produced.

Together these show dispatch-down: wind or solar output the grid operator
(EirGrid) told a farm not to produce. Roughly,

    dispatch-down (MW) = availability - 2 * metered energy per half-hour

and the dispatch instructions say why: a constraint (the local grid can't carry
the power) or a curtailment (too much wind for the whole island).

| Dataset                 | SEMO report                              | One row per           |
|-------------------------|------------------------------------------|-----------------------|
| `dispatch_instructions` | BM-037 Daily Dispatch Instructions (D+1) | instruction to a unit |
| `dispatch_instructions_d4` | the same, re-issued four days later (D+4) | instruction to a unit |
| `availability`          | BM-101 Average Outturn Availability      | unit and half-hour    |
| `metered`               | BM-086 Daily Meter Data                  | unit and half-hour    |
| `units`                 | BM-091 Unit Data                         | unit                  |

Each report is one XML file per trading day, with every row's data in its
attributes:

    <PUB_DailyDispatchInstrD1 ROW="551" EffTime="2026-11-03T15:29:02"
        ParticipantName="PT_400021" ResourceName="GU_407510" DispatchInstructionMW="11.4"
        InstructionCode="WIND" InstructionCombinationCode="LOCL"
        InstrIssueTime="2026-11-03T15:29:02" .../>

Despite its name, the meter report's MeteredMW is energy per half-hour (MWh), not
MW: a unit told to run at 340 MW meters about 170 per half-hour. So we call the
column metered_mwh, and its average MW over the half-hour is metered_mwh * 2.
Availability is in MW.

Times are UTC: the day the clocks went back (25 Oct 2026) has 50 half-hours
starting at 23:00 on 24 Oct, none repeated.

Wind farm instructions have InstructionCode "WIND". The combination code says why:

    LOCL  constraint on      LCLO  constraint lifted
    CURL  curtailment on     CRLO  curtailment lifted

Units are anonymised codes like GU_407510, not wind farm names. The unit data
report (BM-091) gives each unit's company and fuel type; solar farms appear to be
listed as WIND (unconfirmed).

The instructions come in two versions. D+1 is out the next day; D+4, four days later,
adds instructions the first version missed, and sometimes exists for a day D+1 never
covered (27 Oct 2026), or the other way round (24-25 Oct 2026). latest_instructions
puts the two together.

SEMO only keeps these files online for about 2-3 months (much less than the
day-ahead files), so the daily download is the only way to build up a history.
"""

import xml.etree.ElementTree as ET

import pandas as pd

# Each dataset: the start of SEMO's file names, the XML attributes we keep
# -> our column names, which columns are numbers, and which columns identify a row.
REPORTS = {
    "dispatch_instructions": {
        "files": "PUB_DailyDispatchInstrD1",
        "columns": {
            "EffTime": "timestamp_utc",
            "InstrIssueTime": "issued_utc",
            "ParticipantName": "participant",
            "ResourceName": "unit",
            "InstructionCode": "instruction_code",
            "InstructionCombinationCode": "combination_code",
            "DispatchInstructionMW": "instruction_mw",
        },
        "numbers": ["instruction_mw"],
        # A unit sometimes gets two instructions issued at the same moment that differ
        # only in MW, so the MW is part of what identifies a row.
        "key": [
            "timestamp_utc",
            "unit",
            "instruction_code",
            "combination_code",
            "issued_utc",
            "instruction_mw",
        ],
    },
    "availability": {
        "files": "PUB_AvgOutturnAvail",
        "columns": {
            "StartTime": "timestamp_utc",
            "ParticipantName": "participant",
            "ResourceName": "unit",
            "AvgOutturnAvail": "availability_mw",
        },
        "numbers": ["availability_mw"],
        "key": ["timestamp_utc", "unit"],
    },
    "metered": {
        "files": "PUB_DailyMeterDataD1",
        "columns": {
            "StartTime": "timestamp_utc",
            "ParticipantName": "participant",
            "ResourceName": "unit",
            "ResourceType": "unit_type",
            "Jurisdiction": "region",
            "MeteredMW": "metered_mwh",
        },
        "numbers": ["metered_mwh"],
        "key": ["timestamp_utc", "unit"],
    },
    # Who owns each unit and what it runs on (WIND, GAS, BATTERY_STORAGE, ...).
    # One row per unit: newer files replace older ones, so this is the latest list.
    "units": {
        "files": "PUB_DailyUnitData",
        "columns": {
            "TradeDate": "trade_date",
            "ParticipantName": "participant",
            "ParticipantFullName": "company",
            "ResourceName": "unit",
            "ResourceType": "unit_type",
            "Jurisdiction": "region",
            "FuelType": "fuel_type",
        },
        "numbers": [],
        "key": ["unit"],
    },
}

# The four-day-later version of the instructions: same columns, different files.
REPORTS["dispatch_instructions_d4"] = {
    **REPORTS["dispatch_instructions"],
    "files": "PUB_DailyDispatchInstrD4",
    "lag_days": 4,  # SEMO publishes it four days after the day it covers
}

DATASETS = list(REPORTS)

LOCAL_TIME = "Europe/Dublin"


def latest_instructions(next_day: pd.DataFrame, later: pd.DataFrame | None) -> pd.DataFrame:
    """One table of instructions from the two versions SEMO publishes.

    For each Irish day the later (D+4) version covers, use it: it is the more complete.
    For every other day (the newest four, and any day D+4 never appeared for), use the
    next-day (D+1) version. `later` can be None or empty, if it hasn't been downloaded.
    """
    if later is None or later.empty:
        return next_day

    def local_day(df):
        return df["timestamp_utc"].dt.tz_convert(LOCAL_TIME).dt.strftime("%Y-%m-%d")

    covered = set(local_day(later))
    only_next_day = next_day[~local_day(next_day).isin(covered)]
    both = pd.concat([only_next_day, later], ignore_index=True)
    return both.sort_values(["timestamp_utc", "unit"]).reset_index(drop=True)


TIMES = ["timestamp_utc", "issued_utc"]


def parse(text: str, dataset: str) -> pd.DataFrame:
    """Turn the text of one report file into a table with our column names."""
    report = REPORTS[dataset]
    columns = list(report["columns"].values())
    if not text:
        return pd.DataFrame(columns=columns)

    rows = [element.attrib for element in ET.fromstring(text)]
    # A missing attribute (instructions without a combination code) becomes an empty
    # string rather than NaN, so it can still be part of a row's key.
    df = pd.DataFrame(rows).reindex(columns=list(report["columns"]), fill_value="").fillna("")
    df = df.rename(columns=report["columns"])

    for column in TIMES:
        if column in df:
            df[column] = pd.to_datetime(df[column]).dt.tz_localize("UTC")
    for column in report["numbers"]:
        df[column] = pd.to_numeric(df[column]).astype(float)
    return df.reset_index(drop=True)


def parser(dataset: str):
    """A parse function for one dataset, in the shape semo.fetch expects."""
    return lambda text: parse(text, dataset)
