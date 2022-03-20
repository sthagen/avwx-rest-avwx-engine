"""
Methods to resolve flight paths in coordinates
"""

# stdlib
from typing import Optional, Union

# library
from geopy.distance import great_circle

# module
from avwx import Station
from avwx.exceptions import BadStation
from avwx.load_utils import LazyLoad
from avwx.structs import Coord

NAVAIDS = LazyLoad("navaids")
QCoord = Union[Coord, list[Coord]]


def _distance(near: Coord, far: Coord) -> float:
    return great_circle(near.pair, far.pair).nm


def _closest(coord: QCoord, coords: list[Coord]) -> Coord:
    if isinstance(coord, Coord):
        distances = [(_distance(coord, c), c) for c in coords]
    else:
        distances = [(_distance(c, _closest(c, coords)), c) for c in coord]
    distances.sort(key=lambda x: x[0])
    return distances[0][1]


def _best_coord(
    previous: Optional[QCoord],
    current: QCoord,
    up_next: Optional[Coord],
) -> Coord:
    """Determine the best coordinate based on surroundings
    At least one of these should be a list
    """
    if previous is None and up_next is None:
        if isinstance(current, list):
            raise Exception("Unable to determine best coordinate")
        return current
    # NOTE: add handling to determine best midpoint
    if up_next is None:
        up_next = previous
    if isinstance(up_next, list):
        return _closest(current, up_next)
    return _closest(up_next, current)


def to_coordinates(
    values: list[Union[Coord, str]], last_value: Optional[list[Coord]] = None
) -> list[Coord]:
    """Convert any known idents found in a flight path into coordinates

    Prefers Coord > ICAO > Navaid > IATA
    """
    if not values:
        return values
    coord = values[0]
    if isinstance(coord, str):
        try:
            coord = Station.from_icao(coord).coord
        except BadStation:
            try:
                coords = [Coord(lat=c[0], lon=c[1]) for c in NAVAIDS[coord]]
            except KeyError:
                coord = Station.from_iata(coord).coord
            else:
                if len(coords) == 1:
                    coord = coords[0]
                else:
                    new_coords = to_coordinates(values[1:], coords)
                    new_coord = new_coords[0] if new_coords else None
                    coord = _best_coord(last_value, coords, new_coord)
                    return [coord] + new_coords
    return [coord] + to_coordinates(values[1:], coord)