#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# ///
"""Splice an upper-constraints file into pyproject.toml."""

import argparse
import json
import sys
from pathlib import Path

BEGIN = "# BEGIN upper-constraints"
END = "# END upper-constraints"


def read_constraints(path):
    """Return the requirement lines of a constraints file."""
    lines = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def splice(pyproject, constraints):
    """Replace the sentinel block in pyproject text with constraints."""
    out = []
    in_block = False
    begin_seen = False
    end_seen = False
    for line in pyproject.splitlines():
        stripped = line.strip()
        if stripped.startswith(BEGIN):
            begin_seen = True
            in_block = True
            out.append(line)
            indent = line[: len(line) - len(line.lstrip())]
            out.extend(f"{indent}{json.dumps(c)}," for c in constraints)
            continue
        if stripped.startswith(END):
            end_seen = True
            in_block = False
        if not in_block:
            out.append(line)
    if not (begin_seen and end_seen):
        raise SystemExit("pyproject.toml is missing the upper-constraints sentinels")
    return "\n".join(out) + "\n"


def main():
    """Rewrite the pyproject constraint block from a constraints file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("constraints", help="path to upper-constraints.txt")
    parser.add_argument("pyproject", help="path to pyproject.toml to rewrite")
    args = parser.parse_args()

    constraints = read_constraints(args.constraints)
    path = Path(args.pyproject)
    path.write_text(splice(path.read_text(), constraints))
    print(f"spliced {len(constraints)} constraints into {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
