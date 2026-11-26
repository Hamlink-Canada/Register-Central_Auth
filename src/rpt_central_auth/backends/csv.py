"""Validated startup snapshot. Restart to reload file changes."""

import asyncio
import csv
import time
from pathlib import Path
from types import MappingProxyType

from rpt_common.models.credentials import valid_password

API_VERSION = 2


async def initialize(*, options, timeout):
    deadline = time.monotonic() + timeout
    records = {}
    maximum = options.get("max_entries", 100000)
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("invalid credential snapshot capacity")

    def read_records():
        path = Path(options["file"])
        if not path.is_file():
            raise ValueError("credential CSV must be a regular file")
        with path.open(newline="", encoding="utf-8") as stream:
            return _read(stream, maximum, deadline)

    records = await asyncio.to_thread(read_records)
    return MappingProxyType(records)


def _read(stream, maximum, deadline):
    records = {}
    try:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != ["node_id", "password"]:
            raise ValueError("credential CSV requires node_id,password header")
        for row in reader:
            if time.monotonic() > deadline:
                raise TimeoutError()
            if set(row) != {"node_id", "password"}:
                raise ValueError("invalid CSV record")
            value = row["node_id"]
            if not value or not value.isascii() or not value.isdecimal():
                raise ValueError("invalid CSV node ID")
            node = int(value)
            if not 0 < node <= 2**63 - 1 or str(node) != value:
                raise ValueError("invalid CSV node ID")
            if node in records or not valid_password(row["password"]):
                raise ValueError("duplicate node or invalid password")
            if len(records) >= maximum:
                raise ValueError("credential snapshot capacity exceeded")
            records[node] = row["password"]
    except csv.Error as error:
        raise ValueError("invalid credential CSV") from error
    return records


async def lookup(node_id, *, state, timeout):
    return state.get(node_id)


async def check(*, state, timeout):
    # Availability of the validated in-memory snapshot does not depend on the source file.
    pass


async def close(*, state, timeout):
    pass
