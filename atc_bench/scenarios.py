"""Seeded, exogenous demand and weather. Future events stay private to the engine."""
import math
import random
from .models import Aircraft, AIRCRAFT_TYPES

SCENARIOS = ("mixed", "rush_hour", "low_visibility", "storm", "emergency", "wind_shift", "runway_closure")


def runway_closure(seed, scenario, duration_s):
    """Return a private, seeded disruption schedule, independent of traffic RNG.

    Normal episodes lose one arrival-capable physical runway for 4–8 minutes.
    Short episodes scale the closure so its onset and reopening still happen
    within the episode. Only the environment publishes an event when due.
    """
    if scenario != "runway_closure":
        return None
    rng = random.Random(f"runway_closure:{seed}")
    physical_id = rng.choice(("NW", "CENTER", "SOUTH"))
    start_s = round(duration_s * rng.uniform(0.25, 0.40), 3)
    closed_seconds = min(rng.randint(240, 480), duration_s * 0.30)
    return {"physical_id": physical_id, "start_s": start_s,
            "end_s": round(start_s + closed_seconds, 3)}


def traffic(seed, scenario, duration_s):
    rng = random.Random(seed)
    # Keep the demand intensity constant when changing the episode horizon.
    interval = 34 if scenario == "rush_hour" else 66
    times = [(0, "arrival", i) for i in range(3)] + [(0, "departure", i) for i in range(3)]
    for i, t in enumerate(range(interval, max(interval, int(duration_s * 0.70)), interval)):
        times.append((float(t + rng.randrange(-8, 9)), "arrival" if i % 2 == 0 else "departure", i + 3))
    times.sort(key=lambda x: (x[0], x[1], x[2]))
    flights = []
    for n, (t, kind, i) in enumerate(times):
        code = rng.choices(list(AIRCRAFT_TYPES), weights=[2, 5, 4, 2, 2, 1])[0]
        spec = AIRCRAFT_TYPES[code]
        callsign = ["DLH", "CFG", "UAL", "BAW", "AFR", "KLM"][n % 6] + str(100 + n)
        if kind == "arrival":
            # Three entry streams, with a useful initial east-side approach.
            heading = [250, 205, 295][i % 3]
            radius = (15 if i == 0 else 21 + (i % 3) * 5) + rng.uniform(-1, 1)
            angle = math.radians(90 - heading)
            x, y = -math.cos(angle) * radius, -math.sin(angle) * radius
            altitude = 4800 if i == 0 else 7000 + (i % 3) * 2000
            speed = 220 if i == 0 else 250
            a = Aircraft(callsign, code, kind, "inbound", x, y, altitude, heading, speed,
                         altitude, heading, speed, t, fuel_s=rng.uniform(2200, 3200))
            if (scenario == "emergency" and i in (0, 5, 11)) or (scenario == "mixed" and i == 9):
                a.emergency_at_s = t + (15 if i == 0 else 60)
                a.emergency_kind = ["medical", "engine_failure", "low_fuel"][i % 3]
                a.emergency_budget_s = 720 if i == 0 else 1000
        else:
            a = Aircraft(callsign, code, kind, "ground", -0.8 + (i % 4) * 0.45, 0.8,
                         0, 250, 0, 0, 250, 0, t, fuel_s=4200)
        flights.append(a)
    return flights


def weather_at(scenario, time_s, duration_s):
    w = {"wind_from_deg": 250, "wind_speed_kt": 10, "gust_kt": 14,
         "visibility_m": 10000, "ceiling_ft": 6000, "precipitation": "none",
         "active_direction": "25", "cells": [], "description": "Westerly flow · good visibility"}
    if scenario == "low_visibility":
        w.update(visibility_m=650, ceiling_ft=350, precipitation="rain", wind_speed_kt=8,
                 gust_kt=12, description="Low visibility · wet runways · longer spacing")
    elif scenario == "storm":
        w.update(wind_from_deg=230, wind_speed_kt=17, gust_kt=25, visibility_m=4000,
                 ceiling_ft=1600, precipitation="rain", description="Moving storm cells · gusting southwesterly")
        w["cells"] = [{"id": "CB1", "x_nm": 6 - time_s / 250, "y_nm": 11 - time_s / 900,
                       "radius_nm": 4.0, "severity": "severe"},
                      {"id": "CB2", "x_nm": -16 + time_s / 400, "y_nm": -9,
                       "radius_nm": 3.5, "severity": "severe"}]
    elif scenario == "wind_shift" and time_s >= duration_s * 0.45:
        w.update(wind_from_deg=70, wind_speed_kt=14, gust_kt=19, active_direction="07",
                 description="Wind shift · easterly flow · runway direction 07")
    return w
