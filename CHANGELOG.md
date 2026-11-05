# Changelog

## Unreleased

### Fixed
- A start date after the end date now stops with a clear error instead of a pandas traceback.
- Data is saved to the project's `data/` folder wherever the command is run from. Use `--data-dir` to choose another folder.

### Added
- Downloads warn about missing readings (`warning: wind_actual is missing 4 reading(s) from ...`) instead of silently leaving gaps.

## 0.1.0 (2026-11-03)
- First version: EirGrid wind and demand into Parquet.
