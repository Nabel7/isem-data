"""Work out dispatch-down per wind farm and half-hour from SEMO's per-unit reports.

Dispatch-down is output a wind or solar farm could have produced but was told
not to by the grid operator (EirGrid): a *constraint* when the local grid can't
carry the power, a *curtailment* when there is too much wind for the whole island.

For each farm and half-hour:

    output (MW)    = 2 * metered energy (metered is MWh per half-hour)
    available (MW) = the farm's declared availability, checked against what it
                     really makes (see below)
    gap (MW)       = available - output
    lost (MWh)     = gap * 0.5 h, counted only while an instruction is in force
                     and never below zero

Checking availability. Some farms keep declaring their full availability while
they make far less, even with no instruction in force: most likely turbines out of
service whose availability wasn't lowered. Counting availability minus output would
then treat the missing turbines as wind turned down. So each farm's output is
compared with its declared availability over the past 7 days, in the half-hours
with no instruction in force. If it made under 80% of what it declared, its
availability is scaled down by that ratio until it recovers. A farm needs at least
48 such half-hours (a day) in the window to be judged; otherwise its declared
availability stands.

In October 2026 this took the island's figure from 18.1% to 16.3%, against
EirGrid's 15.8% on the same days; September went from 22.8% to 21.6% (EirGrid
21.8%). Scaling every farm by its ratio, not only the ones under 80%, overcorrects
(19.7% and 14.7%): a farm making 90% of its availability is mostly just the wind
varying within the half-hour.

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
within about half a percentage point of EirGrid's official figures for the same days
in September and October 2026. The split between constraint and curtailment is less
sure.

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
    "available_mw",
    "output_mw",
    "constrained",
    "curtailed",
    "reason",
    "produced_mwh",
    "lost_mwh",
]

HALF_HOUR = pd.Timedelta(minutes=30)
LOCAL_TIME = "Europe/Dublin"

# Checking declared availability against output (see the top).
CHECK_WINDOW = "7D"
CHECK_BELOW = 0.8
CHECK_MIN_FREE = 48


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


def availability_factor(farms: pd.DataFrame) -> pd.Series:
    """For each row of `farms`, what to multiply declared availability by: 1, or the
    farm's ratio of output to declared availability over the past 7 days when it was
    under 80%. Only half-hours with no instruction in force count (`reason` "none"), and
    a farm needs at least 48 of them in the window. Looks back only, so a day's figures
    don't change when later days arrive.

    `farms` needs timestamp_utc, unit, availability_mw, output_mw and reason."""
    free = farms["reason"] == "none"
    parts = pd.DataFrame(
        {
            "timestamp_utc": farms["timestamp_utc"],
            "unit": farms["unit"],
            "output": farms["output_mw"].where(free, 0.0).clip(lower=0),
            "declared": farms["availability_mw"].where(free, 0.0).clip(lower=0),
            "free": free.astype(float),
        }
    ).sort_values(["unit", "timestamp_utc"])
    # The rolling sums come back in the same order as `parts` (by unit, then time),
    # indexed by unit and time, so they're put back on parts' rows by position.
    sums = (
        parts.groupby("unit", sort=True)
        .rolling(CHECK_WINDOW, on="timestamp_utc")[["output", "declared", "free"]]
        .sum()
    )
    sums.index = parts.index
    ratio = (sums["output"] / sums["declared"].where(sums["declared"] > 0)).clip(upper=1)
    judged = (sums["free"] >= CHECK_MIN_FREE) & (ratio < CHECK_BELOW)
    factor = ratio.where(judged, 1.0).fillna(1.0)
    return factor.reindex(farms.index)


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
    farms["available_mw"] = farms["availability_mw"] * availability_factor(farms)
    gap_mw = (farms["available_mw"] - farms["output_mw"]).clip(lower=0)
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
