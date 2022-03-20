"""
AIRMET / SIGMET report parsing
"""

# stdlib
import asyncio as aio
import re
from contextlib import suppress
from datetime import date
from itertools import chain
from typing import List, Optional, Tuple

# library
from geopy.distance import distance as geo_distance

# module
from avwx.base import AVWXBase
from avwx.flight_path import to_coordinates
from avwx.load_utils import LazyLoad
from avwx.parsing import core
from avwx.service.bulk import NOAA_Bulk, NOAA_Intl, Service
from avwx.static.core import CARDINAL_DEGREES, IN_UNITS
from avwx.static.airsigmet import BULLETIN_TYPES, INTENSITY, WEATHER_TYPES
from avwx.structs import (
    AirSigmetData,
    Bulletin,
    Code,
    Coord,
    Movement,
    Number,
    AirSigObservation,
    Timestamp,
    Units,
)

# N1429 W09053 - N1427 W09052 - N1411 W09139 - N1417 W09141
_COORD_PATTERN = re.compile(r"\b[NS]\d{4} [EW]\d{5}\b( -)?")

# FROM 60NW ISN-INL-TVC-GIJ-UIN-FSD-BIL-60NW ISN
# FROM 70SSW ISN TO 20NNW FAR TO 70E DLH TO 40SE EAU TO 80SE RAP TO 40NNW BFF TO 70SSW
_NAVAID_PATTERN = re.compile(
    r"\b(\d{1,3}[NESW]{1,3} [A-z]{3}\b)|((-|(TO )|(FROM ))[A-z]{3}\b)"
)

# N OF N2050 AND S OF N2900
_LATTERAL_PATTERN = re.compile(r"\b([NS] OF [NS]\d{4})|([EW] OF [EW]\d{5})( AND)?\b")

NAVAIDS = LazyLoad("navaids")

# Used to assist parsing after sanitized. Removed after parse
_FLAGS = {
    "...": " <elip> ",
    "..": " <elip> ",
    ". ": " <break> ",
    "/VIS ": " <vis> VIS ",
}


def _parse_prep(report: str) -> list[str]:
    """Prepares sanitized string by replacing elements with flags"""
    report = report.rstrip(".")
    for key, val in _FLAGS.items():
        report = report.replace(key, val)
    return report.split()


def _clean_flags(data: list[str]) -> list[str]:
    return [i for i in data if i[0] != "<"]


def _bulletin(value: str) -> Optional[Bulletin]:
    # if len(value) != 6:
    #     return None
    type_key = value[:2]
    return Bulletin(
        repr=value,
        type=Code(repr=type_key, value=BULLETIN_TYPES[type_key]),
        country=value[2:4],
        number=int(value[4:]),
    )


def _header(data: List[str]) -> Tuple[List[str], Bulletin, str, str, Optional[str]]:
    bulletin = _bulletin(data[0])
    correction, end = None, 3
    if len(data[3]) == 3:
        correction, end = data[3], 4
    return data[end:], bulletin, data[1], data[2], correction


def _spacetime(
    data: List[str],
) -> Tuple[List[str], str, str, Optional[str], str, Optional[str]]:
    area = data.pop(0)
    # Skip airmet type + time repeat
    if data[0] == "WA" and data[1].isdigit():
        data = data[2:]
        area = area[:-1]  # Remove type label from 3-letter ident
    valid_index = data.index("VALID")
    report_type = " ".join(data[:valid_index])
    data = data[valid_index + 1 :]
    if data[0] == "UNTIL":
        start_time = None
        end_time = data[1]
        data = data[2:]
    else:
        target = "-" if "-" in data[0] else "/"
        start_time, end_time = data.pop(0).split(target)
    if data[0][-1] == "-":
        station = data.pop(0)
    else:
        station = None
    return data, area, report_type, start_time, end_time, station


def _first_index(data: List[str], *targets: str) -> int:
    for target in targets:
        with suppress(ValueError):
            return data.index(target)
    return -1


def _region(data: List[str]) -> Tuple[List[str], str]:
    # FIR/CTA region name
    name_end = _first_index(data, "FIR", "CTA", "FROM") + 1
    # State list
    if not name_end:
        for item in data:
            if len(item) == 2:
                name_end += 1
            else:
                break
    name = " ".join(data[:name_end])
    return data[name_end:], name


def _time(
    data: List[str],
) -> Tuple[List[str], Optional[Timestamp], Optional[Timestamp]]:
    """Extracts the start and/or end time based on a couple starting elements"""
    index = _first_index(data, "AT", "FCST", "UNTIL", "VALID", "OUTLOOK")
    if index == -1:
        return data, None, None
    start_item = data.pop(index)
    start, end, observed = None, None, None
    if "-" in data[index]:
        start_item, end_item = data.pop(index).split("-")
        start, end = core.make_timestamp(start_item), core.make_timestamp(end_item)
    elif len(data[index]) >= 4 and data[index][:4].isdigit():
        observed = core.make_timestamp(data.pop(index), time_only=True)
        if index > 0 and data[index - 1] == "OBS":
            data.pop(index - 1)
    for remv in ("FCST", "OUTLOOK", "VALID"):
        with suppress(ValueError):
            data.remove(remv)
    if observed:
        if start_item in ("UNTIL", "VALID"):
            end = observed
        else:
            start = observed
    return data, start, end


def _coord_value(value: str) -> float:
    if value[0] in ("N", "S"):
        index, strip, replace = 3, "N", "S"
    else:
        index, strip, replace = 4, "E", "W"
    num = f"{value[:index]}.{value[index:]}".lstrip(strip).replace(replace, "-")
    return float(num)


def _position(data: List[str]) -> Tuple[List[str], Optional[Coord]]:
    try:
        index = data.index("PSN")
    except ValueError:
        return data, None
    data.pop(index)
    raw = f"{data[index]} {data[index + 1]}"
    lat = _coord_value(data.pop(index))
    lon = _coord_value(data.pop(index))
    return data, Coord(lat=lat, lon=lon, repr=raw)


def _movement(
    data: List[str], units: Units
) -> Tuple[List[str], Units, Optional[Movement]]:
    with suppress(ValueError):
        data.remove("STNR")
        speed = core.make_number("STNR")
        return data, units, Movement(repr="STNR", direction=None, speed=speed)
    try:
        index = data.index("MOV")
    except ValueError:
        return data, units, None
    raw = data.pop(index)
    direction = data.pop(index)
    raw += " " + direction
    speed = None
    kt_unit, kmh_unit = data[index].endswith("KT"), data[index].endswith("KMH")
    if kt_unit or kmh_unit:
        units.wind_speed = "kmh" if kmh_unit else "kt"
        speed_str = data.pop(index)
        raw += " " + speed_str
        speed = core.make_number(speed_str[: -3 if kmh_unit else -2])
    return data, units, Movement(repr=raw, direction=direction, speed=speed)


def _info_from_match(match: re.Match, start: int) -> Tuple[str, int]:
    """Returns the matching text and starting location if none yet available"""
    if start == -1:
        start = match.start()
    return match.group(), start


def _pre_break(report: str) -> str:
    if break_index := report.find(" <break> "):
        return report[:break_index]
    return report


def _bounds_from_latterals(report: str, start: int) -> Tuple[str, List[str], int]:
    """Extract coordinate latterals from report Ex: N OF N2050"""
    bounds = []
    for match in _LATTERAL_PATTERN.finditer(_pre_break(report)):
        group, start = _info_from_match(match, start)
        bounds.append(group.removesuffix(" AND"))
        report = report.replace(group, " ")
    return report, bounds, start


def _coords_from_text(report: str, start: int) -> Tuple[str, List[Coord], int]:
    """Extract raw coordinate values from report Ex: N4409 E01506"""
    coords = []
    for match in _COORD_PATTERN.finditer(_pre_break(report)):
        group, start = _info_from_match(match, start)
        lat, lon = group.strip(" -").split()
        coord = Coord(lat=_coord_value(lat), lon=_coord_value(lon), repr=group)
        coords.append(coord)
        report = report.replace(group, " ")
    return report, coords, start


def _coords_from_navaids(report: str, start: int) -> Tuple[str, List[Coord], int]:
    """Extract navaid referenced coordinates from report Ex: 30SSW BNA"""
    # pylint: disable=too-many-locals
    coords, navs = [], []
    for match in _NAVAID_PATTERN.finditer(_pre_break(report)):
        group, start = _info_from_match(match, start)
        report = report.replace(group, " ")
        group = group.strip("-").removeprefix("FROM ").removeprefix("TO ")
        navs.append((group, *group.split()))
    locs = to_coordinates([n[2 if len(n) == 3 else 1] for n in navs])
    for i, nav in enumerate(navs):
        value = nav[0]
        if len(nav) == 3:
            vector, num_index = nav[1], 0
            while vector[num_index].isdigit():
                num_index += 1
            distance, bearing = (
                int(vector[:num_index]),
                CARDINAL_DEGREES[vector[num_index:]],
            )
            loc = geo_distance(nautical=distance).destination(
                locs[i].pair, bearing=bearing
            )
            coord = Coord(lat=loc.latitude, lon=loc.longitude, repr=value)
        else:
            coord = locs[i]
            coord.repr = value
        coords.append(coord)
    return report, coords, start


def _bounds(data: List[str]) -> Tuple[List[str], List[Coord], List[str]]:
    """Extract coordinate bounds by coord, navaid, and latterals"""
    report, start = " ".join(data), -1
    report, bounds, start = _bounds_from_latterals(report, start)
    report, coords, start = _coords_from_text(report, start)
    report, navs, start = _coords_from_navaids(report, start)
    coords += navs
    from_index = report.find("FROM ")
    if from_index != -1 and from_index < start:
        start = from_index
    report = report[:start] + report[report.rfind("  ") :]
    data = [s for s in report.split() if s]
    return data, coords, bounds


def _is_altitude(value: str) -> bool:
    if len(value) < 5:
        return False
    if value[:4] == "SFC/":
        return True
    if value[:2] == "FL" and value[2:5].isdigit():
        return True
    first, *_ = value.split("/")
    if first[-2:] == "FT" and first[-5:-2].isdigit():
        return True
    return False


def _make_altitude(value: str, force_fl: bool = False) -> Optional[Number]:
    raw = value
    if force_fl:
        value = "FL" + value
    return core.make_number(value.removesuffix("FT"), repr=raw)


def _altitudes(
    data: List[str], units: Units
) -> Tuple[List[str], Units, Optional[Number], Optional[Number]]:
    floor, ceiling = None, None
    for i, item in enumerate(data):
        if item == "BTN" and len(data) < i + 2 and data[i + 2] == "AND":
            floor = _make_altitude(data[i + 1])
            ceiling = _make_altitude(data[i + 3])
            data = data[:i] + data[i + 4 :]
            break
        if item in ("TOP", "TOPS", "BLW"):
            if data[i + 1] == "ABV":
                ceiling = core.make_number("ABV " + data[i + 2])
                data = data[:i] + data[i + 3 :]
                break
            if data[i + 1] == "TO":
                data.pop(i)
            ceiling = _make_altitude(data[i + 1])
            data = data[:i] + data[i + 2 :]
            break
        if _is_altitude(item):
            if "/" in item:
                floor_val, ceiling_val = item.split("/")
                floor = _make_altitude(floor_val)
                if (floor_val == "SFC" or floor_val[:2] == "FL") and ceiling_val[
                    :2
                ] != "FL":
                    ceiling = _make_altitude(ceiling_val, True)
                else:
                    ceiling = _make_altitude(ceiling_val)
            else:
                ceiling = _make_altitude(item)
            data.pop(i)
            break
    return data, units, floor, ceiling


def _weather_type(data: List[str]) -> Tuple[List[str], Optional[Code]]:
    weather = None
    report = " ".join(data)
    for key, val in WEATHER_TYPES.items():
        if key in report:
            weather = Code(repr=key, value=val)
            data = [i for i in report.replace(key, "").split() if i]
            break
    return data, weather


def _intensity(data: List[str]) -> Tuple[List[str], Optional[Code]]:
    if not data:
        return data, None
    try:
        value = INTENSITY[data[-1]]
        code = data.pop()
        return data, Code(repr=code, value=value)
    except KeyError:
        return data, None


def _sigmet_observation(
    data: List[str], units: Units
) -> Tuple[AirSigObservation, Units]:
    data, start_time, end_time = _time(data)
    data, position = _position(data)
    data, coords, bounds = _bounds(data)
    data, units, movement = _movement(data, units)
    data, intensity = _intensity(data)
    data, units, floor, ceiling = _altitudes(data, units)
    data, weather = _weather_type(data)
    struct = AirSigObservation(
        type=weather,
        start_time=start_time,
        end_time=end_time,
        position=position,
        floor=floor,
        ceiling=ceiling,
        coords=coords,
        bounds=bounds,
        movement=movement,
        intensity=intensity,
        other=_clean_flags(data),
    )
    return struct, units


def _observations(
    data: List[str], units: Units
) -> Tuple[Units, Optional[AirSigObservation], Optional[AirSigObservation]]:
    observation, forecast, forecast_index = None, None, -1
    forecast_index = _first_index(data, "FCST", "OUTLOOK")
    if forecast_index == -1:
        observation, units = _sigmet_observation(data, units)
    elif forecast_index < 6:
        forecast, units = _sigmet_observation(data, units)
    else:
        observation, units = _sigmet_observation(data[:forecast_index], units)
        forecast, units = _sigmet_observation(data[forecast_index:], units)
    return units, observation, forecast


def sanitize(report: str) -> str:
    """Sanitized AIRMET / SIGMET report string"""
    return " ".join(report.strip(" =").split())


def parse(report: str, issued: date = None) -> Tuple[AirSigmetData, Units]:
    """Parse AIRMET / SIGMET report string"""
    # pylint: disable=too-many-locals
    units = Units(**IN_UNITS)
    sanitized = sanitize(report)
    data, bulletin, issuer, time, correction = _header(_parse_prep(sanitized))
    data, area, report_type, start_time, end_time, station = _spacetime(data)
    body = sanitized[: sanitized.find(" ".join(data[:2]))]
    # Trim AIRMET type
    if data[0] == "AIRMET":
        data = data[: data.index("<elip>")]
    data, region = _region(data)
    units, observation, forecast = _observations(data, units)
    struct = AirSigmetData(
        raw=report,
        sanitized=sanitized,
        station=station,
        time=core.make_timestamp(time, target_date=issued),
        remarks=None,
        bulletin=bulletin,
        issuer=issuer,
        correction=correction,
        area=area,
        type=report_type,
        start_time=core.make_timestamp(start_time, target_date=issued),
        end_time=core.make_timestamp(end_time, target_date=issued),
        body=body,
        region=region,
        observation=observation,
        forecast=forecast,
    )
    return struct, units


class AirSigmet(AVWXBase):
    """Class representing an AIRMET or SIGMET report"""

    def _post_parse(self) -> None:
        self.data, self.units = parse(self.raw)

    @staticmethod
    def sanitize(report: str) -> str:
        """Sanitizes the report string"""
        return sanitize(report)


class AirSigManager:
    """Class to fetch and manage AIRMET and SIGMET reports"""

    _services: List[Service]
    reports: Optional[List[AirSigmet]] = None

    def __init__(self):
        self._services = [NOAA_Bulk("airsigmet"), NOAA_Intl("airsigmet")]

    async def __update(self, index: int) -> List[AirSigmet]:
        source = self._services[index].root
        reports = await self._services[index].async_fetch()
        data = []
        for report in reports:
            obj = AirSigmet.from_report(report)
            obj.source = source
            data.append(obj)
        return data

    def update(self) -> bool:
        """Updates fetched reports and returns whether they've changed"""
        return aio.run(self.async_update())

    async def async_update(self) -> bool:
        """Updates fetched reports and returns whether they've changed"""
        coros = [self.__update(i) for i in range(len(self._services))]
        data = await aio.gather(*coros)
        reports = list(chain.from_iterable(data))
        changed = reports != self.reports
        if changed:
            self.reports = reports
        return changed