"""Observation-only controllers and the public custom-agent interface."""

from __future__ import annotations

import math


class NoOpAgent:
    """A deliberately inactive baseline."""

    def act(self, observation: dict) -> list[str]:
        return []


class ReferenceAgent:
    """A conservative heuristic baseline, not an optimal traffic controller.

    Emergencies go first, then arrivals with least remaining fuel. Approach
    traffic reserves its physical runway; departures use the other runways.
    Waiting arrivals receive separate holding altitudes. The policy sees the
    same observation provided to a custom agent and has no forecast access.
    """

    def __init__(self):
        self._holding_levels: dict[str, int] = {}
        self._last_time_s = -1

    def act(self, observation: dict) -> list[str]:
        if observation.get("done"):
            return []
        if observation["time_s"] < self._last_time_s:
            self._holding_levels.clear()
        self._last_time_s = observation["time_s"]
        aircraft = observation["aircraft"]
        runways = observation["airport"]["runways"]
        weather = observation["weather"]
        commands: list[str] = []
        reserved = {
            runway["physical_id"]
            for runway in runways
            if any(
                plane.get("runway") == runway["id"]
                and plane["status"] in {"approach", "landing_roll", "takeoff_roll"}
                for plane in aircraft
            )
        }
        arrivals = sorted(
            (plane for plane in aircraft if plane["status"] in {"inbound", "holding"}),
            key=lambda plane: (
                plane.get("emergency") is None,
                (plane.get("emergency") or {}).get("deadline_s", float("inf")),
                plane.get("fuel_s", float("inf")),
                plane["spawn_time_s"],
                plane["callsign"],
            ),
        )
        waiting = []
        for plane in arrivals:
            choices = [
                runway for runway in runways
                if runway["physical_id"] not in reserved
                and self._suitable(runway, plane, weather, arrival=True)
                and not runway.get("occupied_by")
                and runway.get("available_in_s", 0) <= 0
            ]
            if choices:
                # Prefer the arrival-only northwest strip when available,
                # then the nearest intercept, preserving departure capacity.
                chosen = min(choices, key=lambda runway: (
                    bool(runway["departure"]),
                    math.hypot(plane["x_nm"] - runway["approach_fix"][0],
                               plane["y_nm"] - runway["approach_fix"][1]),
                ))
                commands.append(f"APPROACH {plane['callsign']} {chosen['id']}")
                reserved.add(chosen["physical_id"])
            else:
                waiting.append(plane)

        # Retain each waiting flight's level while queues change. Re-sorting
        # flights into levels each tick would cause repeated crossing climbs.
        waiting_callsigns = {plane["callsign"] for plane in waiting}
        self._holding_levels = {callsign: altitude for callsign, altitude in self._holding_levels.items()
                                if callsign in waiting_callsigns}
        for plane in waiting:
            if plane["callsign"] not in self._holding_levels:
                levels = list(range(8000, 19000, 2000))
                # Entry traffic uses odd-thousand-foot levels. Keep holds
                # between those streams, and reuse a level only where the
                # existing holder is horizontally distant.
                neighbors = [other for other in waiting if other["callsign"] in self._holding_levels]
                self._holding_levels[plane["callsign"]] = min(
                    levels,
                    key=lambda level: (
                        sum(math.hypot(plane["x_nm"] - other["x_nm"], plane["y_nm"] - other["y_nm"]) < 10
                            for other in neighbors if self._holding_levels[other["callsign"]] == level),
                        sum(self._holding_levels[other["callsign"]] == level for other in neighbors),
                        abs(level - plane["altitude_ft"]),
                    ),
                )
            altitude = self._holding_levels[plane["callsign"]]
            if plane["status"] != "holding":
                commands.append(f"HOLD {plane['callsign']}")
            if abs(plane.get("target_altitude_ft", plane["altitude_ft"]) - altitude) > 50:
                commands.append(f"ALTITUDE {plane['callsign']} {altitude}")

        departures = sorted(
            (plane for plane in aircraft if plane["kind"] == "departure" and plane["status"] == "ground"),
            key=lambda plane: (plane["spawn_time_s"], plane["callsign"]),
        )
        for plane in departures:
            choices = [
                runway for runway in runways
                if runway["physical_id"] not in reserved
                and self._suitable(runway, plane, weather, arrival=False)
                and not runway.get("occupied_by")
                and runway.get("available_in_s", 0) <= 0
            ]
            if choices:
                chosen = min(choices, key=lambda runway: (bool(runway["arrival"]), runway["id"]))
                commands.append(f"TAKEOFF {plane['callsign']} {chosen['id']}")
                reserved.add(chosen["physical_id"])
        return commands[:100]

    @staticmethod
    def _suitable(runway: dict, plane: dict, weather: dict, *, arrival: bool) -> bool:
        if runway.get("closed") or not runway["arrival" if arrival else "departure"]:
            return False
        if runway["id"] != "18" and not runway["id"].startswith(weather["active_direction"]):
            return False
        distance = plane["landing_distance_m" if arrival else "takeoff_distance_m"]
        distance *= 1.15 if weather.get("precipitation", "none") != "none" else 1
        if runway["length_m"] < distance:
            return False
        # Frankfurt's northwest strip does not accept the largest aircraft.
        if runway["id"] in {"25R", "07L"} and plane["type"] in {"A388", "B748", "B744", "MD11"}:
            return False
        angle = math.radians(weather["wind_from_deg"] - runway["heading_deg"])
        crosswind = abs(math.sin(angle)) * max(weather["wind_speed_kt"], weather.get("gust_kt", 0))
        tailwind = -math.cos(angle) * weather["wind_speed_kt"]
        if crosswind > plane["crosswind_limit_kt"] or tailwind > 5:
            return False
        if weather["visibility_m"] < (550 if arrival else 300):
            return False
        if arrival and weather["ceiling_ft"] < 200:
            return False
        return True
