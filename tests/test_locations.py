"""Tests for finding wind farms' names and positions.

Fixtures:
- `semo_claf_excerpt.txt`: the header and a few rows of SEMO's 2025/26 IE CLAF publication
  (as pdftotext -layout prints it), cut to 200 characters a line, plus one row from a
  later table that repeats a unit without its substation.
- `semo_unit_list_excerpt.txt`: the top of SEMO's 2018 registered units list and a few
  rows, including a repeated unit and a netting unit.
- `osm_wind_farms.json`: a made-up Overpass answer in the real shape.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from isem_data import locations

FIXTURES = Path(__file__).parent / "fixtures"


def claf():
    return locations.parse_claf((FIXTURES / "semo_claf_excerpt.txt").read_text())


def unit_list():
    return locations.parse_unit_list((FIXTURES / "semo_unit_list_excerpt.txt").read_text())


def osm():
    return locations.parse_osm(json.loads((FIXTURES / "osm_wind_farms.json").read_text()))


# ---- names ------------------------------------------------------------------------------


def test_claf_gives_names_and_substations_once_per_unit():
    table = claf().set_index("unit")
    assert table.loc["GU_405140", "semo_name"] == "Cloncreen Windfarm"
    assert table.loc["GU_405140", "station"] == "KILCUMBER"
    assert table.loc["GU_407510", "semo_name"] == "Yellow River Windfarm"
    assert table.index.is_unique  # the later table's repeat is ignored
    assert len(table) == 6


def test_unit_list_gives_names():
    table = unit_list().set_index("unit")["semo_name"]
    assert table["GU_400021"] == "Dunneil Windfarm (Kingsmountain Phase 2)"
    assert table["GU_501500"] == "Tievenameenta Wind Farm"
    assert table.index.is_unique
    assert "DSU_402040" not in table  # only generator units


def test_earlier_sources_win():
    first = pd.DataFrame({"unit": ["GU_1"], "semo_name": ["Right Name"], "station": ["X"]})
    second = pd.DataFrame({"unit": ["GU_1", "GU_2"], "semo_name": ["Old Name", "Other"]})
    names = locations.combine_names([first, second]).set_index("unit")
    assert names.loc["GU_1", "semo_name"] == "Right Name"
    assert names.loc["GU_2", "station"] == ""  # unit lists have no substation


def test_missing_pdftotext_says_how_to_install_it(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("pdftotext")

    monkeypatch.setattr(locations.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="brew install poppler"):
        locations.pdf_text(b"%PDF")


# ---- positions --------------------------------------------------------------------------


def test_osm_farms_need_a_name_and_get_one_position():
    farms = osm().set_index("osm_name")
    assert len(farms) == 6  # the unnamed one is left out
    assert farms.loc["Oweninny Wind Farm", "lat"] == 54.17
    assert farms.loc["Dunneill Wind Farm", "lon"] == -7.93  # a node carries its own position
    assert farms.loc["Oweninny Wind Farm", "osm_id"] == "relation/1"


@pytest.mark.parametrize(
    ("text", "mw"),
    [("172 MW", 172.0), ("57.6 MW", 57.6), ("800 kW", 0.8), ("yes", None), (None, None)],
)
def test_capacity_in_megawatts(text, mw):
    result = locations.to_megawatts(text)
    assert pd.isna(result) if mw is None else result == pytest.approx(mw)


def test_overpass_is_retried_when_busy(monkeypatch):
    answers = iter(["<html>busy</html>", (FIXTURES / "osm_wind_farms.json").read_text()])

    class Response:
        status_code = 504

        def __init__(self, text):
            self.text = text

        def json(self):
            return json.loads(self.text)

    waits = []
    monkeypatch.setattr(locations.requests, "post", lambda *a, **k: Response(next(answers)))
    monkeypatch.setattr(locations.time, "sleep", waits.append)
    assert len(locations.fetch_osm()) == 6
    assert waits == [10]


# ---- matching ---------------------------------------------------------------------------


def names(**units):
    return pd.DataFrame({"unit": list(units), "semo_name": list(units.values())})


def run(corrections=None, **units):
    return locations.match(names(**units), osm(), list(units), corrections).set_index("unit")


def test_exact_and_near_names_match():
    found = run(GU_1="Cloncreen Windfarm", GU_2="Oweninney Windfarm 1")
    assert found.loc["GU_1", "farm_name"] == "Cloncreen Wind Farm"
    assert found.loc["GU_2", "farm_name"] == "Oweninny Wind Farm"  # spelt differently
    assert found.loc["GU_2", "lat"] == 54.17
    assert found.loc["GU_2", "capacity_mw"] == 172.0
    assert set(found["located_by"]) == {"name"}


def test_old_names_in_brackets_on_openstreetmap_count():
    found = run(GU_1="Bawnmore Windfarm")
    assert found.loc["GU_1", "farm_name"] == "Burren Wind Farm (Bawnmore 1)"


def test_semo_bracket_detail_doesnt_override_the_main_name():
    found = run(GU_1="Dunneil Windfarm (Kingsmountain Phase 2)")
    assert found.loc["GU_1", "farm_name"] == "Dunneill Wind Farm"


def test_semo_brackets_are_used_when_the_main_name_says_nothing():
    words = locations.semo_words("SWS Green Energy Gen Unit (Sillahertane)")
    assert words == ["sillahertane"]


def test_dissimilar_names_are_not_matched():
    found = run(GU_1="Mulreavy Windfarm")
    assert found.loc["GU_1", "located_by"] == ""
    assert pd.isna(found.loc["GU_1", "lat"])
    assert found.loc["GU_1", "score"] < locations.THRESHOLD


def test_solar_units_are_kept_but_not_placed():
    found = run(GU_1="Knockglass Solar")
    assert found.loc["GU_1", "technology"] == "solar"
    assert found.loc["GU_1", "located_by"] == ""


def test_units_without_a_name_are_kept_unplaced():
    found = locations.match(names(GU_1="Cloncreen Windfarm"), osm(), ["GU_1", "GU_9"])
    row = found.set_index("unit").loc["GU_9"]
    assert row["semo_name"] == ""
    assert row["located_by"] == ""


def correction(unit, osm_name):
    return pd.DataFrame({"unit": [unit], "osm_name": [osm_name]}).set_index("unit")


def test_a_correction_places_a_unit_the_names_cant():
    found = run(correction("GU_1", "Galway Wind Park"), GU_1="Seecon Windfarm")
    assert found.loc["GU_1", "farm_name"] == "Galway Wind Park"
    assert found.loc["GU_1", "located_by"] == "correction"


def test_an_empty_correction_undoes_a_wrong_match():
    found = run(correction("GU_1", ""), GU_1="Cloncreen Windfarm")
    assert found.loc["GU_1", "located_by"] == ""
    assert pd.isna(found.loc["GU_1", "lat"])


def test_a_correction_naming_an_unknown_farm_is_an_error():
    with pytest.raises(ValueError, match="no OpenStreetMap farm called 'Nowhere'"):
        run(correction("GU_1", "Nowhere"), GU_1="Cloncreen Windfarm")


def test_the_corrections_file_reads():
    corrections = locations.read_corrections()
    assert corrections.index.is_unique
    assert "osm_name" in corrections.columns


def test_wind_unit_codes():
    units = pd.DataFrame({"unit": ["GU_2", "GU_1", "GU_3"], "fuel_type": ["WIND", "WIND", "GAS"]})
    assert locations.wind_unit_codes(units) == ["GU_1", "GU_2"]
