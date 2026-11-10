"""Read a shared dependency branch without importing runtime code."""

import argparse
import re
import tomllib
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("package", choices=["rpt-protobuf", "rpt-common"])
args = parser.parse_args()
project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
repository = {"rpt-protobuf": "rpt_protobuf", "rpt-common": "pyCommon"}[args.package]
owner = "Hamlink-Canada"
pattern = (
    rf"{args.package}(?:\[[^]]+\])? @ git\+https://github\.com/{owner}/{repository}\.git@(main)"
)
entries = [d for d in project["project"]["dependencies"] if d.startswith(args.package)]
if len(entries) != 1 or (match := re.fullmatch(pattern, entries[0])) is None:
    raise SystemExit("Expected one shared dependency main reference")
print(match[1])
