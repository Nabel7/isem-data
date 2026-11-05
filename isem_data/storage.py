"""Save and load datasets as Parquet files in the project's data folder.

Parquet is a compressed column format: much smaller and faster than CSV, and it
keeps the column types (like timezone-aware timestamps) when you read it back.
"""

from pathlib import Path

import pandas as pd

# The data/ folder at the top of the project, wherever the command is run from.
# (A plain Path("data") would mean "data/ inside the current folder", so running the
# command from somewhere else would quietly start a second, separate data folder.)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# A row is identified by these columns. Two rows with the same key are the same data point.
KEY = ["timestamp_utc", "region", "series"]


def save(df: pd.DataFrame, dataset: str, data_dir: Path = DATA_DIR) -> Path:
    """Add rows to the dataset's file. Re-downloaded rows replace the old copies.

    Running the same download twice leaves the file unchanged, so it is always
    safe to re-run. Newer rows win, which matters because forecasts get revised.
    """
    path = data_dir / f"{dataset}.parquet"
    if path.exists():
        existing = pd.read_parquet(path)
        # Stacking tables with different columns would quietly fill the gaps with NaN,
        # e.g. after a column is renamed. Stop and say what to do instead.
        if list(existing.columns) != list(df.columns):
            raise ValueError(
                f"{path} has columns {list(existing.columns)} but the new data has "
                f"{list(df.columns)}. Delete {path} and download again."
            )
        df = pd.concat([existing, df], ignore_index=True)

    df = (
        df.drop_duplicates(subset=KEY, keep="last")
        .sort_values(["series", "timestamp_utc"])
        .reset_index(drop=True)
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def load(dataset: str, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """Read a dataset back as a DataFrame."""
    return pd.read_parquet(data_dir / f"{dataset}.parquet")
