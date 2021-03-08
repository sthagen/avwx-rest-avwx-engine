"""
Station text-based search
"""

# stdlib
from typing import List, Tuple

# library
from fuzzywuzzy import fuzz, process

# module
from avwx.load_utils import LazyCalc
from avwx.station.meta import STATIONS
from avwx.station.station import Station


TYPE_ORDER = [
    "large_airport",
    "medium_airport",
    "small_airport",
    "seaplane_base",
    "heliport",
    "balloonport",
    "weather_station",
]


def _format_search(airport: dict, keys: list[str]) -> str:
    values = [airport.get(k) for k in keys]
    return " - ".join(k for k in values if k)


def _build_corpus() -> List[str]:
    keys = ("icao", "iata", "city", "state", "name")
    return [_format_search(s, keys) for s in STATIONS.values()]


_CORPUS = LazyCalc(_build_corpus)


def _sort_key(result: Tuple[dict, int]) -> Tuple[int]:
    station, score = result
    try:
        type_order = TYPE_ORDER.index(station.type)
    except ValueError:
        type_order = 10
    return (score, 10 - type_order)


def search(text: str, limit: int = 10) -> List[Station]:
    """Text search for stations against codes, name, city, and state"""
    results = process.extract(
        text, _CORPUS.value, limit=limit * 10, scorer=fuzz.token_set_ratio
    )
    results = [(Station.from_icao(k[:4]), s) for k, s in results]
    results.sort(key=_sort_key, reverse=True)
    return [s[0] for s in results][:limit]