"""Checks on the downloaded data itself, run after each daily download.

python -m isem_data.checks                    # check data/, write data/checks.json
python -m isem_data.checks --today 2026-11-18 # as if run on another day

The tests in tests/ show the code is right on small made-up tables. These checks look
at the real archive and ask whether it makes sense. Each big data bug so far was
found by hand, after it had reached the figures. These checks catch that kind of
problem within a day or two, not weeks later.

- `freshness` fails when a dataset's newest day is older than usual. It catches a
  source that has stopped updating, whatever the reason (empty answers, as from EirGrid
  on 13 Nov 2026, a renamed file, or the job not running), from the second day.
- `missing_days` fails when a day has no data and isn't in known_gaps.csv. It would
  have caught 27 Oct 2026, when SEMO never published its next-day reports (PR #16).
- `stuck_switches` fails when most farms count as held down on a day with almost no
  instructions. It would have caught 28 Oct 2026, when 82% of wind units were left
  "curtailed" (PR #16).
- `partial_days` fails when a day has data for far fewer wind units than usual (a source
  published only part of it) and isn't in known_gaps.csv. It would have caught 18 Nov
  2026, when SEMO's metered file had 178 of about 229 wind units. The dispatch down
  figures leave such days out (tracker.partial_days); this makes sure someone looks.
- `switches_left_on` warns when a unit's constraint or curtailment switch went more than
  four days without a new instruction in the last four weeks, so the figures ended it
  (dispatch_down.SWITCH_LIMIT). It would have caught GU_408180, held down from 17 Oct
  2026 by one "on" with no "off".
- `entsoe_matches_semo` fails when ENTSO-E's hourly day-ahead price isn't the average
  of SEMO's two half-hours. The two are independent copies of the same auction result,
  so a mismatch means one of them was misread or later corrected.

Each check passes, warns or fails. Any fail makes the command exit 1, so the daily
script's notification fires and the GitHub job fails. Everything is written to
data/checks.json, with the numbers behind each result.

When you fix a data bug, add a check here that would have caught it, with a test.

known_gaps.csv, next to this file, lists the days a source never published. A new gap
fails until it's either downloaded or added there with a note saying why it's missing.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import balancing, dispatch_down, storage

LOCAL_TIME = "Europe/Dublin"
CHECKS_FILE = "checks.json"
KNOWN_GAPS = Path(__file__).with_name("known_gaps.csv")

# How many days behind today each dataset's newest day normally is when the daily job
# runs. EirGrid's readings and the day-ahead results are complete for yesterday; SEMO's
# per-unit reports come a day later, and the four-day-later instructions four days
# after that. Measured on 18 Nov 2026. ENTSO-E has the next day's prices from the
# afternoon before: today's at least, at whatever time the job runs.
EXPECTED_LAG_DAYS = {
    "wind": 1,
    "demand": 1,
    "wind_regions": 1,
    "prices": 1,
    "orders": 1,
    "units": 1,
    "dispatch_instructions": 2,
    "availability": 2,
    "metered": 2,
    "wind_unit_forecasts": 2,
    "dispatch_instructions_d4": 5,
    "entsoe_prices": 0,
}
# One day late is allowed: a source publishing later than usual, or the job running
# early. Two days late is a failure.
SLACK_DAYS = 1

# Datasets with data for every Irish day. The day-ahead orders come from the same SEMO
# files as the prices, so the prices stand for both (and are much quicker to read).
DAILY_DATASETS = [
    "wind",
    "demand",
    "wind_regions",
    "prices",
    "availability",
    "metered",
    "entsoe_prices",
]

# The day-ahead prices come by trading day, which starts at 23:00 Irish time the
# evening before. Counted by clock day, a whole missing trading day still looks covered
# (its neighbours' hours fall on the same clock days), so these count by trading day.
# That is how ENTSO-E's missing 29 Oct 2026 was first missed.
TRADING_DAY_DATASETS = {"prices", "entsoe_prices"}

# A day counts as "stuck" for a switch (constraint or curtailment) when, on average over
# the day, more than STUCK_SHARE of wind units are held down by it, yet fewer than
# STUCK_FEW instructions for that switch were issued all day. Real island-wide
# curtailment comes with thousands of instructions a day (median 1,600 in Sep-Nov 2026).
# On 28 Oct 2026, before PR #16, 82% of units were "curtailed" with 2 instructions; the
# highest share on any day with under 20 instructions, after the fix, is 2%.
STUCK_SHARE = 0.3
STUCK_FEW = 20

# ENTSO-E gives prices to 3 decimal places and SEMO to 2, so the average of two SEMO
# half-hours matches ENTSO-E's hour exactly; this only allows for rounding.
PRICE_TOLERANCE = 0.01


@dataclass
class Result:
    name: str
    status: str  # "pass", "warn" or "fail"
    summary: str
    details: dict = field(default_factory=dict)


def worst(statuses) -> str:
    order = ["pass", "warn", "fail"]
    return max(statuses, key=order.index, default="pass")


def irish_days(timestamps: pd.Series) -> set[str]:
    """The Irish clock days (YYYY-MM-DD) that UTC timestamps fall on."""
    return set(timestamps.dt.tz_convert(LOCAL_TIME).dt.strftime("%Y-%m-%d"))


def trading_days(timestamps: pd.Series) -> set[str]:
    """The trading days (YYYY-MM-DD) of UTC timestamps: each runs 23:00 to 23:00 Irish time."""
    return irish_days(timestamps + pd.Timedelta(hours=1))


def days_without_data(days: set[str]) -> list[str]:
    """The days between the first and last of `days` that aren't in it."""
    if not days:
        return []
    every_day = pd.date_range(min(days), max(days), freq="D").strftime("%Y-%m-%d")
    return [day for day in every_day if day not in days]


# --- freshness ---------------------------------------------------------------------


def freshness(newest: dict[str, str | None], today: date) -> Result:
    """Is each dataset's newest day as recent as it should be?

    `newest` maps each dataset to its newest Irish day (YYYY-MM-DD), or None when
    there's no data for it at all."""
    behind, late = {}, []
    for dataset, lag in EXPECTED_LAG_DAYS.items():
        day = newest.get(dataset)
        if day is None:
            behind[dataset] = None
            late.append(f"{dataset} (no data)")
            continue
        days = (today - date.fromisoformat(day)).days
        behind[dataset] = days
        if days > lag + SLACK_DAYS:
            late.append(f"{dataset} (newest {day}, {days} days old; usually {lag})")
    details = {"today": today.isoformat(), "days_behind": behind}
    if late:
        return Result("freshness", "fail", "Out of date: " + "; ".join(late), details)
    n = len(EXPECTED_LAG_DAYS)
    return Result("freshness", "pass", f"All {n} datasets are up to date", details)


def newest_day(dataset: str, data_dir: Path) -> str | None:
    """A dataset's newest Irish day, read from the archive (None if there's no data)."""
    if dataset == "wind_unit_forecasts":
        # One file per day, named by the day: data/wind_unit_forecasts/2026-11-16.parquet
        names = [path.stem for path in (data_dir / dataset).glob("*.parquet")]
        return max(names, default=None)
    path = data_dir / f"{dataset}.parquet"
    if not path.exists():
        return None
    if dataset == "units":  # one row per unit, with the date of the list it came from
        dates = pd.read_parquet(path, columns=["trade_date"])["trade_date"]
        return str(dates.max()) if len(dates) else None
    times = pd.read_parquet(path, columns=["timestamp_utc"])["timestamp_utc"]
    if times.empty:
        return None
    return times.max().tz_convert(LOCAL_TIME).strftime("%Y-%m-%d")


# --- missing days ------------------------------------------------------------------


def read_known_gaps(path: Path = KNOWN_GAPS) -> pd.DataFrame:
    return pd.read_csv(path, comment="#", dtype=str).fillna("")


def missing_days(day_sets: dict[str, set[str]], known: pd.DataFrame) -> Result:
    """Does every dataset have data for every day between its first and last?

    `day_sets` maps each dataset to the Irish days it has data for. A missing day listed
    in `known` (known_gaps.csv) is accepted. A missing day in the four-day-later
    instructions only warns, since the next-day version still covers it."""
    accepted = set(zip(known["dataset"], known["day"], strict=True))
    new, warnings, known_seen = {}, {}, {}
    for dataset, days in day_sets.items():
        for day in days_without_data(days):
            if (dataset, day) in accepted:
                known_seen.setdefault(dataset, []).append(day)
            elif dataset == "dispatch_instructions_d4":
                warnings.setdefault(dataset, []).append(day)
            else:
                new.setdefault(dataset, []).append(day)
    details = {
        "missing": new,
        "missing_d4_only": warnings,
        "known_gaps": known_seen,
        "covers": {name: [min(d), max(d)] if d else None for name, d in day_sets.items()},
    }

    def listing(gaps):
        return "; ".join(f"{name}: {', '.join(days)}" for name, days in gaps.items())

    if new:
        return Result(
            "missing_days",
            "fail",
            f"Days with no data: {listing(new)}. Download them again, or if the source "
            "never published them, add them to known_gaps.csv with a note",
            details,
        )
    if warnings:
        return Result(
            "missing_days",
            "warn",
            f"No four-day-later instructions for {listing(warnings)} (next-day version used)",
            details,
        )
    n_known = sum(len(days) for days in known_seen.values())
    note = f" ({n_known} known gap(s) in known_gaps.csv)" if n_known else ""
    return Result("missing_days", "pass", f"No missing days{note}", details)


# --- stuck switches ----------------------------------------------------------------


def stuck_switches(farms: pd.DataFrame, instructions: pd.DataFrame) -> Result:
    """Is any day held down far more than its instructions can explain?

    `farms` is dispatch_down.farm_half_hours's output; `instructions` is what it was
    built from. For each Irish day and switch, compare the share of wind units held down
    (averaged over the day's half-hours) with how many instructions for that switch
    were issued that day. Most units held down with almost no instructions means the
    switches were left on by a gap in the data, not by the grid operator."""
    day = farms["timestamp_utc"].dt.tz_convert(LOCAL_TIME).dt.strftime("%Y-%m-%d")
    wind = instructions[instructions["instruction_code"] == "WIND"]
    wind_day = wind["timestamp_utc"].dt.tz_convert(LOCAL_TIME).dt.strftime("%Y-%m-%d")
    stuck = []
    for switch, codes in dispatch_down.SWITCHES.items():
        share = farms.groupby([day, farms["timestamp_utc"]])[switch].mean().groupby(level=0).mean()
        issued = wind[wind["combination_code"].isin(codes)].groupby(wind_day).size()
        issued = issued.reindex(share.index, fill_value=0)
        bad = share[(share > STUCK_SHARE) & (issued < STUCK_FEW)]
        stuck += [
            {
                "day": d,
                "switch": switch,
                "share": round(float(s), 3),
                "instructions": int(issued[d]),
            }
            for d, s in bad.items()
        ]
    details = {"stuck": stuck, "days_checked": int(day.nunique())}
    if stuck:
        days = ", ".join(
            f"{s['day']} ({s['share']:.0%} {s['switch']}, {s['instructions']} instructions)"
            for s in stuck
        )
        return Result(
            "stuck_switches",
            "fail",
            f"Farms held down with almost no instructions: {days}. Look for a missing day "
            "of instructions just before",
            details,
        )
    return Result("stuck_switches", "pass", f"No stuck switches in {day.nunique()} days", details)


# --- ENTSO-E against SEMO ---------------------------------------------------------


def entsoe_matches_semo(entsoe: pd.DataFrame, prices: pd.DataFrame) -> Result:
    """Is each of ENTSO-E's hourly prices the average of SEMO's two half-hours?

    `entsoe` is the entsoe_prices dataset, `prices` SEMO's prices dataset. ROI's price
    stands for SEMO's: the day-ahead auction gives both regions the same price. Only
    hours where SEMO has both half-hours are compared."""
    semo = prices[(prices["region"] == "ROI") & (prices["series"] == "dam_price_eur_mwh")]
    semo = semo.dropna(subset=["value"])
    hour = semo["timestamp_utc"].dt.floor("h")
    pairs = semo.groupby(hour)["value"].agg(["mean", "size"])
    pairs = pairs[pairs["size"] == 2]["mean"].rename("semo")
    hourly = entsoe.set_index("timestamp_utc")["value"].rename("entsoe")
    both = hourly.to_frame().join(pairs, how="inner")
    gap = (both["entsoe"] - both["semo"]).abs()
    wrong = both[gap > PRICE_TOLERANCE]
    details = {
        "hours_compared": int(len(both)),
        "largest_gap": round(float(gap.max()), 3) if len(gap) else None,
        "mismatches": [
            {"hour_utc": t.strftime("%Y-%m-%dT%H:%MZ"), "entsoe": e, "semo": round(m, 3)}
            for t, e, m in wrong[["entsoe", "semo"]].head(20).itertuples()
        ],
    }
    if both.empty:
        return Result("entsoe_matches_semo", "warn", "No hours in both to compare yet", details)
    if len(wrong):
        first = details["mismatches"][0]
        return Result(
            "entsoe_matches_semo",
            "fail",
            f"{len(wrong)} of {len(both)} hours differ between ENTSO-E and SEMO, first "
            f"{first['hour_utc']}: {first['entsoe']} against {first['semo']}",
            details,
        )
    return Result(
        "entsoe_matches_semo", "pass", f"All {len(both)} hours match SEMO's prices", details
    )


# --- partial days and switches left on ----------------------------------------------


def irish_days_of(times: pd.Series) -> pd.Series:
    return times.dt.tz_convert(LOCAL_TIME).dt.strftime("%Y-%m-%d")


def partial_days(farms: pd.DataFrame, known: pd.DataFrame) -> Result:
    """`farms` is dispatch_down.farm_half_hours's output with an Irish `day` column;
    `known` is known_gaps.csv. A partial day listed there for metered or availability
    is accepted."""
    from .tracker import partial_days as find

    found = find(farms)
    accepted = set(known.loc[known["dataset"].isin(["metered", "availability"]), "day"])
    new = [d for d in found if d not in accepted]
    details = {"partial_days": found, "known": [d for d in found if d in accepted]}
    if new:
        return Result(
            "partial_days",
            "fail",
            f"Days with data for far fewer wind units than usual: {', '.join(new)}. Try "
            "downloading them again; if the source's file is itself incomplete, add them "
            "to known_gaps.csv with a note",
            details,
        )
    note = f" ({len(found)} known: {', '.join(found)})" if found else ""
    return Result("partial_days", "pass", f"No partial days{note}", details)


def switches_left_on(instructions: pd.DataFrame, today: date) -> Result:
    """Switches the figures ended because no new instruction came for four days, in the
    last four weeks."""
    since = pd.Timestamp(today - timedelta(days=28), tz="UTC")
    left = dispatch_down.switches_left_on(instructions)
    left = left[left["on_utc"] >= since]
    rows = [
        {"unit": r.unit, "switch": r.switch, "on_utc": r.on_utc.strftime("%Y-%m-%dT%H:%MZ")}
        for r in left.itertuples()
    ]
    if rows:
        listed = ", ".join(
            f"{r['unit']} ({r['switch']} since {r['on_utc'][:10]})" for r in rows[:10]
        )
        return Result(
            "switches_left_on",
            "warn",
            f"Switches ended after four days with no new instruction: {listed}",
            {"left_on": rows},
        )
    return Result(
        "switches_left_on", "pass", "No switch left on without instructions", {"left_on": []}
    )


# --- running them ------------------------------------------------------------------


def run(data_dir: Path, today: date) -> list[Result]:
    """Run every check on the archive in `data_dir`. A check that can't run (a dataset
    file missing, say) fails with the reason, and the others still run."""
    results = []

    def attempt(name, check):
        try:
            results.append(check())
        except Exception as error:
            results.append(Result(name, "fail", f"Could not run: {type(error).__name__}: {error}"))

    attempt(
        "freshness",
        lambda: freshness({d: newest_day(d, data_dir) for d in EXPECTED_LAG_DAYS}, today),
    )

    loaded = {}

    def load(name):
        if name not in loaded:
            loaded[name] = storage.load(name, data_dir)
        return loaded[name]

    def instructions():
        later_path = data_dir / "dispatch_instructions_d4.parquet"
        later = load("dispatch_instructions_d4") if later_path.exists() else None
        return balancing.latest_instructions(load("dispatch_instructions"), later)

    def check_missing_days():
        day_sets = {}
        for name in DAILY_DATASETS:
            path = data_dir / f"{name}.parquet"
            times = pd.read_parquet(path, columns=["timestamp_utc"])["timestamp_utc"]
            days = trading_days if name in TRADING_DAY_DATASETS else irish_days
            day_sets[name] = days(times)
        # The instructions the figures use: four-day-later where it exists, else next-day.
        day_sets["dispatch_instructions"] = irish_days(instructions()["timestamp_utc"])
        day_sets["dispatch_instructions_d4"] = irish_days(
            load("dispatch_instructions_d4")["timestamp_utc"]
        )
        forecast_days = {path.stem for path in (data_dir / "wind_unit_forecasts").glob("*.parquet")}
        day_sets["wind_unit_forecasts"] = forecast_days
        return missing_days(day_sets, read_known_gaps())

    attempt("missing_days", check_missing_days)

    def check_stuck_switches():
        used = instructions()
        farms = dispatch_down.farm_half_hours(
            load("availability"), load("metered"), used, load("units")
        )
        return stuck_switches(farms, used)

    attempt("stuck_switches", check_stuck_switches)

    def farms_by_day():
        farms = dispatch_down.farm_half_hours(
            load("availability"), load("metered"), instructions(), load("units")
        )
        return farms.assign(day=irish_days_of(farms["timestamp_utc"]))

    attempt("partial_days", lambda: partial_days(farms_by_day(), read_known_gaps()))
    attempt("switches_left_on", lambda: switches_left_on(instructions(), today))
    attempt(
        "entsoe_matches_semo",
        lambda: entsoe_matches_semo(load("entsoe_prices"), load("prices")),
    )
    return results


def write(results: list[Result], data_dir: Path) -> Path:
    """Save the results as data/checks.json (written beside it, then swapped in)."""
    report = {
        "checked_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "result": worst(r.status for r in results),
        "checks": [asdict(r) for r in results],
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / CHECKS_FILE
    unfinished = data_dir / (CHECKS_FILE + ".tmp")
    unfinished.write_text(json.dumps(report, indent=2) + "\n")
    os.replace(unfinished, path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the downloaded data makes sense.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=storage.DATA_DIR,
        help=f"the archive to check (default: {storage.DATA_DIR})",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        help="the day to judge freshness from (default: today, Irish time)",
    )
    args = parser.parse_args()
    today = args.today or datetime.now(ZoneInfo(LOCAL_TIME)).date()
    data_dir = args.data_dir.expanduser()

    results = run(data_dir, today)
    for r in results:
        print(f"{r.status.upper():4}  {r.name}: {r.summary}")
    path = write(results, data_dir)
    overall = worst(r.status for r in results)
    print(f"Checks: {overall.upper()} -> {path}")
    if overall == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
