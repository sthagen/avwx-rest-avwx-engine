"""
Update ICAO lists from 3rd-party source
"""

# stdlib
import gzip
import json
import asyncio as aio
from contextlib import suppress
from pathlib import Path
from os import environ

# library
import httpx
from dotenv import load_dotenv

# module
from find_bad_stations import PROJECT_ROOT

load_dotenv()

DATA_PATH = PROJECT_ROOT / "data"
TIMEOUT = 60


async def fetch_data(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    decode: bool = False,
) -> dict:
    """Fetch source data with optional gzip decompress"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            url,
            params=params,
            headers=headers,
        )
    if decode:
        data_bytes = gzip.decompress(resp.content)
        return json.loads(data_bytes.decode("utf8"))
    return resp.json()


def save(data: list[str], path: Path) -> None:
    """Export JSON data"""
    data.sort()
    json.dump(data, path.open("w"), indent=2)


async def update_icaos() -> None:
    """Update ICAO list from Avio source"""
    icaos = []
    for station in await fetch_data(
        "https://api.aviowiki.com/export/airports",
        headers={
            "Authorization": "Bearer " + environ["AVIOWIKI_API_KEY"],
            "Accept": "application/gzip",
        },
        decode=True,
    ):
        if icao := station.get("icao"):
            icaos.append(icao)
    save(icaos, DATA_PATH / "icaos.json")


async def update_awos() -> None:
    """Update AWOS code list"""
    codes = []
    for report in await fetch_data(
        environ["AWOS_URL"],
        params={"authKey": environ["AWOS_KEY"]},
    ):
        codes.append(report.strip().split()[0])
    save(codes, DATA_PATH / "awos.json")


async def main() -> None:
    """Update static repo data codes"""
    await aio.gather(update_icaos(), update_awos())


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        aio.run(main())