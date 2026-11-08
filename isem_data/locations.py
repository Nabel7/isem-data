"""Find where the wind farms are: link SEMO's anonymous unit codes to farm names and map positions.

SEMO's per-unit data names units only by codes like GU_405140. Three steps put them on a map:

1. **Names.** SEMO publishes PDFs that list each unit code's name:
   - the yearly loss adjustment factor (CLAF) publication for Irish units, which also gives
     the substation each unit connects to;
   - lists of registered units (2024 and 2018), which add Northern Irish units.
   The first source that names a unit wins. `pdftotext` (from poppler: brew install
   poppler) turns the PDFs into text.
2. **Positions.** OpenStreetMap maps most wind farms on the island, with their names.
   They are fetched through the Overpass API.
3. **Matching.** Each named wind unit is matched to the OpenStreetMap farm with the most
   similar name ("Oweninney Windfarm 1" -> "Oweninny Wind Farm"). corrections.csv, next to
   this file, fixes the ones the names can't: it can point a unit at another OpenStreetMap
   farm, or mark a match as wrong.

OpenStreetMap data is © OpenStreetMap contributors, under the Open Database Licence
(ODbL): anything published from it needs that credit and the same licence.
"""

import difflib
import json
import re
import subprocess
import time
from pathlib import Path

import pandas as pd
import requests

# SEMO's PDFs that name unit codes, best first. The CLAF publication is re-issued each
# year (and sometimes within one); update the address when a new one comes out.
NAME_SOURCES = [
    (
        "claf",
        "https://www.sem-o.com/sites/semo/files/2026-08/2526%20IE%20CLAF%20Publication%20V13%20.pdf",
    ),
    ("unit_list", "https://www.sem-o.com/sites/semo/files/2024-11/Registered-Participants.pdf"),
    (
        "unit_list",
        "https://www.sem-o.com/documents/legacy-general-publications/List-of-Registered-Units.pdf",
    ),
]

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Every wind farm in Ireland and Northern Ireland. "out center" gives one position per
# farm (the middle of its outline) rather than every turbine.
OSM_QUERY = """
[out:json][timeout:180];
(area["ISO3166-1"="IE"]["admin_level"="2"]; area["ISO3166-2"="GB-NIR"];)->.island;
(
  nwr["power"="plant"]["plant:source"="wind"](area.island);
  nwr["power"="plant"]["generator:source"="wind"](area.island);
);
out center tags;
"""

CORRECTIONS = Path(__file__).with_name("corrections.csv")

# How similar two names must be (0-1) to count as the same farm. Checked by eye on
# 2026-11-08: every match at or above this was right.
THRESHOLD = 0.85

NAME_COLUMNS = ["unit", "semo_name", "station", "source"]
OSM_COLUMNS = ["osm_id", "osm_name", "lat", "lon", "capacity_mw"]
LOCATION_COLUMNS = [
    "unit",
    "semo_name",
    "farm_name",
    "technology",
    "lat",
    "lon",
    "capacity_mw",
    "located_by",
    "score",
]


# ---- 1. Names ---------------------------------------------------------------------------


def pdf_text(content: bytes) -> str:
    """The text of a PDF, keeping its column layout."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"], input=content, capture_output=True, check=True
        )
    except FileNotFoundError as error:
        message = "pdftotext is needed to read SEMO's PDFs: brew install poppler"
        raise RuntimeError(message) from error
    return result.stdout.decode("utf-8", errors="replace")


# A CLAF line: name, TSO id, unit code, substation, kV, then the first loss factor.
#   Cloncreen Windfarm        OE1    GU_405140      KILCUMBER     110        0.971 ...
CLAF_LINE = re.compile(
    r"^(?P<name>\S.*?)\s{2,}\S+\s+(?P<unit>GU_\d+)\s+"
    r"(?P<station>[A-Z][A-Z0-9_ ]*?)\s+\d{2,3}\s+\d\.\d{3}"
)

# A registered-units line: participant, trading site, unit code, unit name, unit type...
#   PT_400021_SSE Airtricity Limited   TS_400022   GU_400030    Cuillagh Generator Unit   ...
UNIT_LIST_LINE = re.compile(r"(?P<unit>GU_\d+)\s+(?P<name>\S.*?)(?:\s{2,}|$)")


def parse_claf(text: str) -> pd.DataFrame:
    """Unit names and substations from the CLAF publication's text."""
    rows = {}
    for line in text.splitlines():
        found = CLAF_LINE.match(line)
        if found and found["unit"] not in rows:  # each unit appears once per table
            rows[found["unit"]] = (found["name"].strip(), found["station"].strip())
    return pd.DataFrame(
        [(unit, name, station) for unit, (name, station) in rows.items()],
        columns=["unit", "semo_name", "station"],
    )


def parse_unit_list(text: str) -> pd.DataFrame:
    """Unit names from a registered-units list's text."""
    rows = {}
    for line in text.splitlines():
        for found in UNIT_LIST_LINE.finditer(line):
            rows.setdefault(found["unit"], found["name"].strip())
    return pd.DataFrame(list(rows.items()), columns=["unit", "semo_name"])


PARSERS = {"claf": parse_claf, "unit_list": parse_unit_list}


def combine_names(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """One name per unit; tables earlier in the list win."""
    names = pd.concat(tables, ignore_index=True).reindex(columns=NAME_COLUMNS)
    names["station"] = names["station"].fillna("")
    return names.drop_duplicates("unit", keep="first").sort_values("unit").reset_index(drop=True)


def fetch_names() -> pd.DataFrame:
    """Download SEMO's PDFs and read every unit code's name."""
    tables = []
    for kind, url in NAME_SOURCES:
        response = requests.get(url, timeout=120, headers={"User-Agent": "isem-data"})
        response.raise_for_status()
        table = PARSERS[kind](pdf_text(response.content))
        tables.append(table.assign(source=url.rsplit("/", 1)[-1]))
    return combine_names(tables)


# ---- 2. Positions -----------------------------------------------------------------------


def to_megawatts(text) -> float:
    """OpenStreetMap capacity like '57.6 MW' or '800 kW' as MW; 'yes' or missing -> NaN."""
    found = re.fullmatch(r"\s*([\d.]+)\s*(MW|kW)\s*", str(text), flags=re.IGNORECASE)
    if not found:
        return float("nan")
    value = float(found[1])
    return value / 1000 if found[2].lower() == "kw" else value


def parse_osm(answer: dict) -> pd.DataFrame:
    """Named wind farms from an Overpass answer: name, position and capacity."""
    rows = []
    for element in answer["elements"]:
        tags = element.get("tags", {})
        if "name" not in tags:
            continue
        position = element.get("center") or element  # nodes carry lat/lon themselves
        rows.append(
            {
                "osm_id": f"{element['type']}/{element['id']}",
                "osm_name": tags["name"],
                "lat": position["lat"],
                "lon": position["lon"],
                "capacity_mw": to_megawatts(tags.get("plant:output:electricity")),
            }
        )
    return pd.DataFrame(rows, columns=OSM_COLUMNS)


def fetch_osm(attempts: int = 5) -> pd.DataFrame:
    """Ask Overpass for every wind farm on the island. The public server is often busy,
    so wait and retry, doubling the wait each time, as it asks."""
    wait = 10
    for attempt in range(1, attempts + 1):
        response = requests.post(
            OVERPASS_URL, data={"data": OSM_QUERY}, timeout=300, headers={"User-Agent": "isem-data"}
        )
        try:
            return parse_osm(response.json())
        except json.JSONDecodeError:
            if attempt == attempts:
                raise RuntimeError(f"Overpass didn't answer after {attempts} tries") from None
            print(f"Overpass is busy (status {response.status_code}); trying again in {wait} s")
            time.sleep(wait)
            wait *= 2


# ---- 3. Matching ------------------------------------------------------------------------

# Words that say nothing about which farm it is.
FILLER = {
    "an", "and", "co", "company", "dac", "east", "energy", "ext", "extension", "farm",
    "farms", "gen", "generator", "generatot", "green", "hill", "limited", "lower", "ltd",
    "mountain", "netting", "north", "park", "phase", "power", "project", "repower",
    "repowering", "site", "south", "sws", "the", "unit", "upper", "west", "wf", "wfps",
    "wind", "windfarm", "windfarms", "winfarm",
}  # fmt: skip


def name_words(name: str) -> list[str]:
    """The words of a name that identify the farm: 'Kilaveenoge 1 WF' -> ['kilaveenoge']."""
    words = re.findall(r"[a-z]+", re.sub(r"\d+", " ", str(name).lower()))
    return [word for word in words if word not in FILLER]


def semo_words(semo_name: str) -> list[str]:
    """A SEMO name's identifying words, ignoring extra detail in brackets unless that's all
    there is: 'Dunneil Windfarm (Kingsmountain Phase 2)' -> ['dunneil'], but
    'SWS Green Energy Gen Unit (Sillahertane)' -> ['sillahertane']."""
    outside = name_words(re.sub(r"\(.*?\)", " ", semo_name))
    return outside or name_words(" ".join(re.findall(r"\((.*?)\)", semo_name)))


def osm_alternatives(osm_name: str) -> list[list[str]]:
    """An OpenStreetMap name and any old name in brackets:
    'Burren Wind Farm (Bawnmore 1)' -> [['burren'], ['bawnmore']]."""
    inside = re.findall(r"\((.*?)\)", osm_name)
    outside = re.sub(r"\(.*?\)", " ", osm_name)
    return [words for words in map(name_words, [outside, *inside]) if words]


def spelling(a: list[str], b: list[str]) -> float:
    """How alike two lists of words are letter by letter, 0 to 1."""
    return difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()


def similarity(a: list[str], b: list[str]) -> float:
    """How alike two names' words are, 0 to 1. If every word of one name is (nearly) in
    the other, it counts as 0.9: 'meenadreen' vs 'meenadreen'+'south' is the same farm."""
    if not a or not b:
        return 0.0
    score = spelling(a, b)

    def inside(x, y):
        return all(
            max(difflib.SequenceMatcher(None, word, other).ratio() for other in y) >= 0.85
            for word in x
        )

    if inside(a, b) or inside(b, a):
        score = max(score, 0.9)
    return score


def read_corrections(path: Path = CORRECTIONS) -> pd.DataFrame:
    """Hand-made fixes: one row per unit, with the OpenStreetMap farm to use (osm_name),
    or an empty osm_name to say the automatic match is wrong."""
    corrections = pd.read_csv(path, comment="#", dtype=str).fillna("")
    return corrections.set_index("unit")


def match(
    names: pd.DataFrame,
    osm: pd.DataFrame,
    units: list[str],
    corrections: pd.DataFrame | None = None,
    threshold: float = THRESHOLD,
) -> pd.DataFrame:
    """One row per unit in `units`: its name, the farm it was matched to and where it is.

    located_by is "name" (automatic), "correction" (corrections.csv), or "" when the unit
    couldn't be placed. Solar units are kept but not placed: OpenStreetMap's wind farm
    list doesn't include them.
    """
    if corrections is None:
        corrections = pd.DataFrame(columns=["osm_name"]).rename_axis("unit")
    semo_names = names.set_index("unit")["semo_name"]
    by_name = osm.drop_duplicates("osm_name").set_index("osm_name")
    candidates = [
        (row.osm_name, words)
        for row in osm.itertuples()
        for words in osm_alternatives(row.osm_name)
    ]

    rows = []
    for unit in units:
        semo_name = semo_names.get(unit, "")
        technology = "solar" if "solar" in semo_name.lower() else "wind"
        row = {
            "unit": unit,
            "semo_name": semo_name,
            "farm_name": "",
            "technology": technology,
            "lat": float("nan"),
            "lon": float("nan"),
            "capacity_mw": float("nan"),
            "located_by": "",
            "score": float("nan"),
        }

        osm_name, located_by = None, ""
        if unit in corrections.index:
            osm_name = corrections.loc[unit, "osm_name"] or None
            located_by = "correction" if osm_name else ""
        elif semo_name and technology == "wind":
            words = semo_words(semo_name)
            # Ties (common at 0.9) go to the closer spelling: "Dunneil Windfarm
            # (Kingsmountain Phase 2)" is Dunneill, not Kingsmountain.
            scored = [
                (similarity(words, other), spelling(words, other), name)
                for name, other in candidates
            ]
            if scored:
                score, _, best = max(scored)
                row["score"] = round(score, 3)
                if score >= threshold:
                    osm_name, located_by = best, "name"

        if osm_name is not None:
            if osm_name not in by_name.index:
                message = f"corrections.csv: no OpenStreetMap farm called {osm_name!r} ({unit})"
                raise ValueError(message)
            farm = by_name.loc[osm_name]
            row.update(
                farm_name=osm_name,
                lat=farm["lat"],
                lon=farm["lon"],
                capacity_mw=farm["capacity_mw"],
                located_by=located_by,
            )
        rows.append(row)
    return pd.DataFrame(rows, columns=LOCATION_COLUMNS)


def wind_unit_codes(units: pd.DataFrame) -> list[str]:
    return sorted(units.loc[units["fuel_type"] == "WIND", "unit"])
