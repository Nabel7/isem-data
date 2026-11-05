# Changelog

## Unreleased

### Changed
- **Breaking:** the value column is now called `value` (was `value_mw`), and series names carry their unit: `wind_actual_mw`, `wind_forecast_mw`, `demand_actual_mw`. This lets prices and other units share the same table shape. Delete your `data/` folder and download again; saving into an old-style file now stops with a message saying so.

### Fixed
- A start date after the end date now stops with a clear error instead of a pandas traceback.
- Data is saved to the project's `data/` folder wherever the command is run from. Use `--data-dir` to choose another folder.

### Added
- `semo.parse_market_result()` reads a SEMO day-ahead results file into the tidy table: half-hourly prices (`dam_price_eur_mwh`) and volumes (`dam_volume_mw`) for ROI and NI. Company order results in the same file are skipped for now. Downloading the files comes next.
- Downloads warn about missing readings (`warning: wind_actual_mw is missing 4 reading(s) from ...`) instead of silently leaving gaps.

## 0.1.0 (2026-11-03)
- First version: EirGrid wind and demand into Parquet.
