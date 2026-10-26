"""Generates spaghetti-bait.stl: a model that fails on purpose, for testing print-guard safely.

A 15 x 15 mm pillar, 20 mm tall, carries a 50 x 50 x 2 mm roof. Sliced with supports
OFF, the roof's overhang is printed into thin air at about 20 mm and turns into loose
strands: a small, contained, real "spaghetti" failure the camera can see.

Run:  uv run python test-prints/make_spaghetti_bait.py
"""

from __future__ import annotations

import struct
from pathlib import Path


def box(x0, y0, z0, x1, y1, z1):
    """12 outward-facing triangles of an axis-aligned box."""
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


def normal(t):
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = sum(c * c for c in n) ** 0.5 or 1.0
    return tuple(c / length for c in n)


def main() -> None:
    # Pillar and roof overlap by 0.5 mm so slicers treat them as one solid.
    tris = box(-7.5, -7.5, 0, 7.5, 7.5, 20.5) + box(-25, -25, 20, 25, 25, 22)
    out = Path(__file__).with_name("spaghetti-bait.stl")
    with out.open("wb") as f:
        f.write(b"print-guard spaghetti bait".ljust(80, b"\0"))
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", *normal(t)))
            for vertex in t:
                f.write(struct.pack("<3f", *vertex))
            f.write(b"\0\0")
    print(f"wrote {out} ({len(tris)} triangles)")


if __name__ == "__main__":
    main()
