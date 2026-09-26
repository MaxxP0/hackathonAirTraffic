"""EDDF runway roles and lengths; schematic local geometry, not navigation data."""
import math
from .models import Runway

FIXES = {"NORTH": (0, 22), "EAST": (24, 0), "SOUTH": (0, -22), "WEST": (-24, 0)}


def make_runways():
    result = {}
    for physical, center, length, east, west, arrival, departure in [
        ("NW", (-1.5, 1.5), 2800, "07L", "25R", True, False),
        ("CENTER", (0, 0.2), 4000, "07C", "25C", True, True),
        ("SOUTH", (0.2, -0.6), 4000, "07R", "25L", True, True),
    ]:
        h = math.radians(70)
        dx, dy = math.sin(h) * length / 1852 / 2, math.cos(h) * length / 1852 / 2
        p, q = (center[0] - dx, center[1] - dy), (center[0] + dx, center[1] + dy)
        result[east] = Runway(east, physical, 70, length, p, q, arrival, departure)
        result[west] = Runway(west, physical, 250, length, q, p, arrival, departure)
    result["18"] = Runway("18", "WEST", 180, 4000, (-2.8, -0.4), (-2.8, -0.4 - 4000 / 1852), False, True)
    return result
