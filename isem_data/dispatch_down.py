"""Work out dispatch-down per wind farm and half-hour from SEMO's per-unit reports.

Dispatch-down is output a wind or solar farm could have produced but was told
not to by the grid operator (EirGrid): a *constraint* when the local grid can't
carry the power, a *curtailment* when there is too much wind for the whole island.

For each farm and half-hour:

    output (MW)   = 2 * metered energy (metered is MWh per half-hour)
    gap (MW)      = availability - output
    lost (MWh)    = gap * 0.5 h, counted only while an instruction is in force
                    and never below zero

Constraint and curtailment are two separate switches, because a farm can be
under both at once:

    LOCL  constraint on      LCLO  constraint off
    CURL  curtailment on     CRLO  curtailment off

A switch's position in a half-hour is its last change before the middle of the
half-hour. When both are on, the lost energy counts as a constraint (the local
limit is the one that binds).

A switch only turns off when the data shows an "off" instruction, so a day with no
instructions at all (SEMO published nothing for it) would leave switches stuck on.
Every switch is therefore turned off at the start of such a day: after it, a farm
counts as held down only once a new instruction says so. See missing_days.

For the whole island, with all wind generation as the base (see tracker.py), this is
within about 1 percentage point of EirGrid's official figure (7-30 September 2026:
22.8% against about 22%). The split between constraint and curtailment is less sure.

    from isem_data import dispatch_down, storage

    farms = dispatch_down.farm_half_hours(
        storage.load("availability"),
        storage.load("metered"),
        storage.load("dispatch_instructions"),
        storage.load("units"),
    )
    dispatch_down.totals(farms)
"""

import numpy as np
import pandas as pd

# Each switch: its name, and the combination codes that turn it on and off.
SWITCHES = {
    "constrained": ("LOCL", "LCLO"),
    "curtailed": ("CURL", "CRLO"),
}

COLUMNS = [
    "timestamp_utc",
    "unit",
    "region",
    "availability_mw",
    "output_mw",
    "constrained",
    "curtailed",
    "reason",
    "produced_mwh",
    "lost_mwh",
]

HALF_HOUR = pd.Timedelta(minutes=30)
LOCAL_TIME = "Europe/Dublin"


def wind_units(units: pd.DataFrame) -> set[str]:
    """The units SEMO lists with fuel type WIND (solar farms appear to be listed as WIND too)."""
    return set(units.loc[units["fuel_type"] == "WIND", "unit"])


def missing_days(instructions: pd.DataFrame) -> list[pd.Timestamp]:
    """The Irish days, between the first and last instruction, that have no instructions
    of any kind for any unit: days SEMO published no file for. Returned as the UTC time
    each day starts. (A real day always has hundreds of instructions across all units.)"""
    local = instructions["timestamp_utc"].dt.tz_convert(LOCAL_TIME).dt.normalize()
    days = pd.DatetimeIndex(local.unique()).sort_values()
    if len(days) < 2:
        return []
    every_day = pd.date_range(days[0].tz_localize(None), days[-1].tz_localize(None), freq="D")
    every_day = every_day.tz_localize(LOCAL_TIME)
    return [day.tz_convert("UTC") for day in every_day.difference(days)]


def farm_half_hours(
    availability: pd.DataFrame,
    metered: pd.DataFrame,
    instructions: pd.DataFrame,
    units: pd.DataFrame,
    start: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """One row per wind farm and half-hour: what it could make, what it made, and what it lost.

    The inputs are the isem-data datasets of the same names. Half-hours before
    `start` are left out; by default `start` is the first whole day of
    instructions, because before that we can't tell which switches were on.
    Half-hours missing from either availability or metered output are left out.
    """
    if start is None:
        start = instructions["timestamp_utc"].min().ceil("D")

    farms = availability[availability["unit"].isin(wind_units(units))].merge(
        metered[["timestamp_utc", "unit", "metered_mwh", "region"]],
        on=["timestamp_utc", "unit"],
    )
    farms = farms[farms["timestamp_utc"] >= start]
    farms = farms.assign(output_mw=farms["metered_mwh"] * 2)

    # Judge each half-hour by the switches' positions at its middle.
    farms["middle"] = farms["timestamp_utc"] + HALF_HOUR / 2
    farms = farms.sort_values("middle")
    wind_instructions = instructions[instructions["instruction_code"] == "WIND"]
    # On a day with no instructions at all, every switch is turned off (see the top).
    gaps = missing_days(instructions)
    units_seen = farms["unit"].unique()
    for switch, (on, off) in SWITCHES.items():
        changes = wind_instructions[wind_instructions["combination_code"].isin([on, off])]
        changes = changes.assign(**{switch: changes["combination_code"] == on})
        changes = changes.rename(columns={"timestamp_utc": "changed_utc"})
        changes = changes[["changed_utc", "unit", switch]]
        if gaps:
            resets = pd.DataFrame(
                {
                    "changed_utc": np.repeat(pd.DatetimeIndex(gaps), len(units_seen)),
                    "unit": np.tile(units_seen, len(gaps)),
                    switch: False,
                }
            ).astype(changes.dtypes.to_dict())
            changes = pd.concat([changes, resets], ignore_index=True)
        farms = pd.merge_asof(
            farms,
            changes[["changed_utc", "unit", switch]].sort_values("changed_utc"),
            left_on="middle",
            right_on="changed_utc",
            by="unit",
        ).drop(columns="changed_utc")
        # No change yet for this farm means the switch is off.
        farms[switch] = farms[switch].astype("boolean").fillna(False).astype(bool)

    farms["reason"] = np.select(
        [farms["constrained"], farms["curtailed"]], ["constraint", "curtailment"], "none"
    )
    gap_mw = (farms["availability_mw"] - farms["output_mw"]).clip(lower=0)
    farms["produced_mwh"] = farms["metered_mwh"]
    farms["lost_mwh"] = gap_mw.where(farms["reason"] != "none", 0.0) * 0.5

    farms = farms.sort_values(["timestamp_utc", "unit"]).reset_index(drop=True)
    return farms[COLUMNS]


def add_market_value(farms: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Add what the lost energy would have sold for at the day-ahead price.

    lost_value_eur = lost_mwh * the day-ahead price for the farm's zone (ROI or
    NI) in that half-hour. This is the market value of the energy, not what the
    owner lost: some dispatch-down is compensated, and how much is disputed.
    It can be negative when the price is. Half-hours without a price get NaN.

    `prices` is the isem-data prices dataset.
    """
    price = prices[prices["series"] == "dam_price_eur_mwh"]
    price = price[["timestamp_utc", "region", "value"]].rename(columns={"value": "price_eur_mwh"})
    farms = farms.drop(columns=["price_eur_mwh", "lost_value_eur"], errors="ignore")
    farms = farms.merge(price, on=["timestamp_utc", "region"], how="left")
    farms["lost_value_eur"] = farms["lost_mwh"] * farms["price_eur_mwh"]
    return farms


def totals(farms: pd.DataFrame) -> pd.Series:
    """Add up farm half-hours: GWh produced and lost (by reason), and the dispatch-down %.

    Dispatch-down % is lost / (produced + lost): the share of the energy the
    wind allowed that was not produced. Use it with groupby to split by month,
    region or farm, e.g. farms.groupby("region").apply(totals).

    If add_market_value has been applied, the total value of the lost energy is
    included too (half-hours without a price count as zero).
    """
    lost = farms.groupby("reason")["lost_mwh"].sum()
    produced = farms["produced_mwh"].sum()
    available = produced + lost.sum()
    result = {
        "produced_gwh": produced / 1000,
        "constraint_gwh": lost.get("constraint", 0.0) / 1000,
        "curtailment_gwh": lost.get("curtailment", 0.0) / 1000,
        "dispatch_down_pct": 100 * lost.sum() / available if available else float("nan"),
    }
    if "lost_value_eur" in farms:
        result["lost_value_eur"] = farms["lost_value_eur"].sum()
    return pd.Series(result)
