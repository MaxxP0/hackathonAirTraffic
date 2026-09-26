"""Deterministic continuous-motion simulator with a small command interface.

Navigation follows simplified point-mass dynamics. Clearances delegate route
following to the aircraft; separation, sequencing and runway selection belong
to the agent. All safety numbers are benchmark rules, not operational guidance.
"""
from dataclasses import asdict
import math
import shlex

from .airport import FIXES, make_runways
from .models import AIRBORNE, FINISHED, SURFACE, RunwayState
from .scenarios import SCENARIOS, traffic, weather_at


def bearing(dx, dy):
    return math.degrees(math.atan2(dx, dy)) % 360


def angle_delta(target, current):
    return (target - current + 180) % 360 - 180


def clamp(value, low, high):
    return max(low, min(high, value))


def _numeric(value, low, high, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}]")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}]") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}]")
    return number


def _swept_overlap(p0, p1, q0, q1, horizontal, vertical):
    """Do horizontal and vertical separation violations overlap in this step?"""
    rx, ry, rz = (p0[i] - q0[i] for i in range(3))
    vx, vy, vz = ((p1[i] - p0[i]) - (q1[i] - q0[i]) for i in range(3))
    aa, bb, cc = vx * vx + vy * vy, 2 * (rx * vx + ry * vy), rx * rx + ry * ry - horizontal ** 2
    lo, hi = 0.0, 1.0
    if aa < 1e-15:
        if cc >= 0:
            return False
    else:
        discriminant = bb * bb - 4 * aa * cc
        if discriminant <= 0:
            return False
        root = math.sqrt(discriminant)
        lo, hi = max(lo, (-bb - root) / (2 * aa)), min(hi, (-bb + root) / (2 * aa))
    if abs(vz) < 1e-12:
        if abs(rz) >= vertical:
            return False
    else:
        a, b = (-vertical - rz) / vz, (vertical - rz) / vz
        lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
    return lo < hi


class AirTrafficEnv:
    def __init__(self, seed=7, scenario="mixed", duration_s=1800):
        if isinstance(duration_s, bool) or not isinstance(duration_s, int) or not 1 <= duration_s <= 86400:
            raise ValueError("duration_s must be an integer in [1, 86400]")
        self.duration_s = duration_s
        self.seed = seed
        self.scenario = scenario
        self.reset(seed, scenario)

    def reset(self, seed=None, scenario=None):
        new_seed = self.seed if seed is None else seed
        new_scenario = self.scenario if scenario is None else scenario
        if isinstance(new_seed, bool) or not isinstance(new_seed, int) or not 0 <= new_seed <= 2**32 - 1:
            raise ValueError("seed must be an integer in [0, 4294967295]")
        if new_scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")
        self.seed, self.scenario = new_seed, new_scenario
        self.time_s = 0.0
        self.done = False
        self.runways = make_runways()
        self._runway_states = {r.physical_id: RunwayState() for r in self.runways.values()}
        self.aircraft = {}
        self._schedule = traffic(self.seed, self.scenario, self.duration_s)
        self._next_spawn = 0
        self.weather = weather_at(self.scenario, 0, self.duration_s)
        self.events = []
        self.command_results = []
        self.conflicts = []
        self._active_conflicts = set()
        self._collided_pairs = set()
        self._failed_emergencies = set()
        self._landed_emergencies = set()
        self._counters = dict(collisions=0, separation_losses=0, separation_loss_seconds=0,
                              runway_incursions=0, wake_violations=0, weather_exposure_seconds=0,
                              emergency_landings=0, emergencies_failed=0, invalid_commands=0)
        self._spawn_due()
        self._event("episode", f"EDDF {self.scenario} · seed {self.seed}")
        return self.observe()

    def _event(self, kind, message, callsigns=()):
        self.events.append({"time_s": round(self.time_s, 2), "type": kind, "message": message,
                            "callsigns": list(callsigns)})
        if len(self.events) > 100:
            del self.events[:-100]

    def _spawn_due(self):
        while self._next_spawn < len(self._schedule) and self._schedule[self._next_spawn].spawn_time_s <= self.time_s:
            a = self._schedule[self._next_spawn]
            self.aircraft[a.callsign] = a
            a.history.append([a.x_nm, a.y_nm])
            self._event("spawn", f"{a.callsign} {a.type} {'inbound' if a.kind == 'arrival' else 'ready for departure'}", [a.callsign])
            self._next_spawn += 1

    def _wake_seconds(self, state, follower=None):
        base = {"medium": 75, "heavy": 120, "super": 180}[state.last_wake]
        if follower in {"heavy", "super"} and state.last_wake != "super":
            base = max(60, base - 20)
        return base + (45 if self.weather["visibility_m"] < 1000 else 0)

    def _available_in(self, runway, follower=None):
        state = self._runway_states[runway.physical_id]
        remaining = max(0, state.last_release_s + self._wake_seconds(state, follower) - self.time_s)
        if state.occupied_by:
            occupant = self.aircraft.get(state.occupied_by)
            remaining = max(remaining, (occupant.roll_remaining_s if occupant else 60) + self._wake_seconds(state, follower))
        return remaining

    def _closed(self, runway):
        return any(math.dist(runway.threshold, (c["x_nm"], c["y_nm"])) < c["radius_nm"]
                   for c in self.weather["cells"])

    def _runway_problem(self, a, runway, departure=False):
        if departure and not runway.departure:
            return "runway is arrivals only"
        if not departure and not runway.arrival:
            return "runway is departures only"
        if runway.id != "18" and not runway.id.startswith(self.weather["active_direction"]):
            return "runway is opposite the active wind direction"
        if self._closed(runway):
            return "runway closed by a storm cell"
        angle = math.radians(self.weather["wind_from_deg"] - runway.heading_deg)
        crosswind = abs(math.sin(angle)) * self.weather["gust_kt"]
        tailwind = -math.cos(angle) * self.weather["wind_speed_kt"]
        if crosswind > a.spec.crosswind_limit_kt:
            return "crosswind exceeds aircraft limit"
        if tailwind > 5:
            return "tailwind exceeds 5 kt"
        if self.weather["visibility_m"] < (300 if departure else 550):
            return "visibility below benchmark minimum"
        if not departure and self.weather["ceiling_ft"] < 200:
            return "ceiling below benchmark minimum"
        distance = a.spec.takeoff_distance_m if departure else a.spec.landing_distance_m
        distance *= 1.15 if self.weather["precipitation"] != "none" else 1
        if runway.length_m < distance:
            return f"runway too short for {a.type} in current conditions"
        return None

    def _parse(self, command):
        if isinstance(command, str):
            bits = shlex.split(command)
            if len(bits) < 2:
                raise ValueError("expected ACTION CALLSIGN [ARGUMENTS]")
            action, callsign, *args = bits
            action = action.lower()
            names = {"heading": ["heading"], "altitude": ["altitude"], "speed": ["speed"],
                     "approach": ["runway"], "takeoff": ["runway"], "hold": [],
                     "go_around": [], "divert": []}
            if action == "direct":
                names[action] = ["fix"] if len(args) == 1 else ["x", "y"]
            if action not in names:
                raise ValueError(f"unknown action: {action}")
            if len(args) != len(names[action]):
                raise ValueError(f"{action.upper()} expects {len(names[action])} argument(s) after callsign")
            return {"action": action, "callsign": callsign.upper(), **dict(zip(names[action], args))}
        if not isinstance(command, dict):
            raise ValueError("command must be a string or object")
        if not isinstance(command.get("action"), str) or not isinstance(command.get("callsign"), str):
            raise ValueError("action and callsign must be strings")
        parsed = dict(command)
        parsed["action"], parsed["callsign"] = parsed["action"].lower(), parsed["callsign"].upper()
        return parsed

    def _apply_command(self, raw):
        cmd = self._parse(raw)
        action, callsign = cmd["action"], cmd["callsign"]
        if self.done:
            raise ValueError("episode is finished; reset to start a new episode")
        if callsign not in self.aircraft:
            raise ValueError(f"unknown aircraft: {callsign}")
        a = self.aircraft[callsign]
        if a.status in FINISHED:
            raise ValueError("aircraft is no longer controllable")
        if action == "takeoff":
            if a.status != "ground":
                raise ValueError("TAKEOFF requires an aircraft waiting on the ground")
            runway = self.runways.get(str(cmd.get("runway", "")).upper())
            if runway is None:
                raise ValueError("unknown runway")
            problem = self._runway_problem(a, runway, departure=True)
            state = self._runway_states[runway.physical_id]
            if problem:
                raise ValueError(problem)
            if state.occupied_by or self._available_in(runway, a.wake) > 0:
                raise ValueError("runway occupied or wake separation has not elapsed")
            for other in self.aircraft.values():
                if other.status == "approach" and other.runway and self.runways[other.runway].physical_id == runway.physical_id:
                    eta = math.dist((other.x_nm, other.y_nm), runway.threshold) / max(other.speed_kt, 100) * 3600
                    if eta < 120:
                        raise ValueError("arrival on short final; departure clearance unavailable")
            a.status, a.runway = "takeoff_roll", runway.id
            a.x_nm, a.y_nm = runway.threshold
            a.altitude_ft = 0
            a.heading_deg = a.target_heading_deg = runway.heading_deg
            a.speed_kt = 0
            a.roll_remaining_s = 45 if a.wake == "medium" else 65
            state.occupied_by = callsign
            return f"{callsign} cleared for takeoff runway {runway.id}"
        if a.status not in AIRBORNE:
            raise ValueError("command requires an airborne aircraft")
        if action == "heading":
            value = _numeric(cmd.get("heading"), 0, 359.999999, "heading")
            self._cancel_guidance(a)
            a.target_heading_deg = value
        elif action == "altitude":
            value = _numeric(cmd.get("altitude"), 1000, 18000, "altitude")
            if a.status == "approach":
                raise ValueError("use GO_AROUND before changing approach altitude")
            a.target_altitude_ft = value
        elif action == "speed":
            value = _numeric(cmd.get("speed"), a.spec.min_speed_kt, a.spec.max_speed_kt, "speed")
            if a.status == "approach":
                raise ValueError("approach speed is managed; use GO_AROUND before changing speed")
            a.target_speed_kt = value
        elif action == "direct":
            if "fix" in cmd:
                fix = str(cmd["fix"]).upper()
                if fix not in FIXES:
                    raise ValueError(f"unknown fix; choose {', '.join(FIXES)}")
                waypoint = FIXES[fix]
            else:
                waypoint = (_numeric(cmd.get("x"), -38, 38, "x"), _numeric(cmd.get("y"), -38, 38, "y"))
            self._cancel_guidance(a)
            a.waypoint = waypoint
        elif action == "hold":
            if a.kind != "arrival":
                raise ValueError("HOLD is for arrivals")
            if a.status == "approach":
                self._go_around(a, "controller assigned holding")
            a.status, a.approach_stage, a.runway, a.waypoint = "holding", None, None, None
            a.target_speed_kt = max(a.spec.min_speed_kt, 180)
        elif action == "approach":
            if a.kind != "arrival" or a.status == "diverting":
                raise ValueError("APPROACH requires a non-diverting arrival")
            runway = self.runways.get(str(cmd.get("runway", "")).upper())
            if runway is None:
                raise ValueError("unknown runway")
            problem = self._runway_problem(a, runway)
            if problem:
                raise ValueError(problem)
            if a.status == "approach":
                raise ValueError("already on approach; use GO_AROUND to change runway")
            a.status, a.runway, a.approach_stage = "approach", runway.id, "intercept"
            a.waypoint = runway.approach_fix
            a.target_altitude_ft = 3200
            a.target_speed_kt = max(180, a.spec.approach_speed_kt)
        elif action == "go_around":
            if a.status != "approach":
                raise ValueError("GO_AROUND requires an approach")
            self._go_around(a, "controller instruction")
        elif action == "divert":
            if a.kind != "arrival" or a.status == "diverting":
                raise ValueError("DIVERT requires an arrival not already diverting")
            a.status, a.approach_stage, a.runway, a.waypoint = "diverting", None, None, None
            a.target_heading_deg = bearing(a.x_nm, a.y_nm)
            a.target_altitude_ft = max(6000, a.altitude_ft)
            a.target_speed_kt = min(280, a.spec.max_speed_kt)
        else:
            raise ValueError(f"unknown action: {action}")
        return f"{callsign} {action.upper()} accepted"

    def _cancel_guidance(self, a):
        if a.status == "approach":
            self._go_around(a, "controller assigned a new route")
        if a.status in {"holding", "approach"}:
            a.status = "inbound"
        a.approach_stage, a.runway, a.waypoint = None, None, None

    def step(self, commands=None, seconds=10):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= 60:
            raise ValueError("seconds must be an integer in [0, 60]")
        if commands is None:
            commands = []
        if not isinstance(commands, list) or len(commands) > 100:
            raise ValueError("commands must be a list of at most 100 commands")
        self.command_results = []
        for raw in commands:
            # A rejected Python command must still be safe to return over JSON.
            import json
            try:
                logged = json.loads(json.dumps(raw, allow_nan=False))
            except (TypeError, ValueError, OverflowError):
                logged = repr(raw)
            try:
                message = self._apply_command(raw)
                self.command_results.append({"command": logged, "accepted": True, "message": message})
                self._event("command", message)
            except (ValueError, TypeError, KeyError) as error:
                self._counters["invalid_commands"] += 1
                self.command_results.append({"command": logged, "accepted": False, "message": str(error)})
        remaining = min(seconds, self.duration_s - self.time_s)
        while remaining > 0 and not self.done:
            dt = min(1.0, remaining)
            self._tick(dt)
            remaining -= dt
        self.done = self.time_s >= self.duration_s
        return self.observe()

    def _go_around(self, a, reason):
        runway = self.runways.get(a.runway)
        a.status, a.approach_stage, a.runway, a.waypoint = "inbound", None, None, None
        a.target_altitude_ft = max(4000, a.altitude_ft)
        a.target_speed_kt = 210
        if runway:
            a.target_heading_deg = runway.heading_deg
        self._event("go_around", f"{a.callsign} going around: {reason}", [a.callsign])

    def _fail_emergency(self, a, reason):
        if a.emergency and a.callsign not in self._failed_emergencies and a.callsign not in self._landed_emergencies:
            self._failed_emergencies.add(a.callsign)
            self._counters["emergencies_failed"] += 1
            self._event("emergency_failed", f"{a.callsign}: {reason}", [a.callsign])

    def _release_runway(self, a):
        if a.runway:
            state = self._runway_states[self.runways[a.runway].physical_id]
            if state.occupied_by == a.callsign:
                state.occupied_by = None
                state.last_release_s, state.last_wake = self.time_s, a.wake

    def _crash(self, a, reason):
        if a.status == "crashed":
            return
        self._release_runway(a)
        a.status = "crashed"
        a.speed_kt = 0
        self._fail_emergency(a, reason)
        self._event("crash", f"{a.callsign}: {reason}", [a.callsign])

    def _tick(self, dt):
        previous = {a.callsign: (a.x_nm, a.y_nm, a.altitude_ft) for a in self.aircraft.values()
                    if a.status in AIRBORNE | {"takeoff_roll", "landing_roll"}}
        previous_airborne = {a.callsign for a in self.aircraft.values() if a.status in AIRBORNE}
        self.time_s = round(self.time_s + dt, 6)
        old_direction = self.weather["active_direction"]
        self.weather = weather_at(self.scenario, self.time_s, self.duration_s)
        if old_direction != self.weather["active_direction"]:
            self._event("weather", f"Wind changed: active runway direction {self.weather['active_direction']}")
        # Aircraft already present move for dt. New traffic spawns at this step's end.
        for a in list(self.aircraft.values()):
            if a.status in FINISHED:
                continue
            # An externally supplied emergency is already known at this tick's
            # start. Scheduled emergencies become known only when announced.
            if a.emergency and a.emergency_declared_time_s is None:
                a.emergency_declared_time_s = self.time_s - dt
            if a.emergency_at_s is not None and self.time_s >= a.emergency_at_s and a.emergency is None:
                a.emergency = {"kind": a.emergency_kind, "deadline_s": self.time_s + a.emergency_budget_s}
                a.emergency_declared_time_s = self.time_s
                if a.emergency_kind == "low_fuel":
                    a.fuel_s = min(a.fuel_s, a.emergency_budget_s)
                self._event("emergency", f"MAYDAY {a.callsign}: {a.emergency_kind}", [a.callsign])
            if a.emergency and a.emergency_touchdown_time_s is None:
                a.emergency_wait_s += max(0, self.time_s - max(self.time_s - dt, a.emergency_declared_time_s))
            if a.emergency and self.time_s > a.emergency["deadline_s"] and a.status not in {"landing_roll", "taxi_in"}:
                self._fail_emergency(a, "emergency landing deadline missed")
            if a.status in SURFACE:
                a.ground_time_s += dt
                if a.kind == "departure" and a.status == "ground":
                    a.ground_wait_s += dt
                self._move_surface(a, dt)
            else:
                a.airborne_time_s += dt
                a.fuel_s = max(0, a.fuel_s - dt * (1.5 if a.emergency and a.emergency["kind"] == "engine_failure" else 1))
                if a.fuel_s <= 0:
                    self._crash(a, "fuel exhausted")
                    continue
                self._move_airborne(a, dt)
                if a.status in AIRBORNE and any(math.dist((a.x_nm, a.y_nm), (c["x_nm"], c["y_nm"])) < c["radius_nm"] for c in self.weather["cells"]):
                    self._counters["weather_exposure_seconds"] += dt
            if int(self.time_s) % 10 == 0:
                a.history.append([round(a.x_nm, 3), round(a.y_nm, 3)])
                a.history[:] = a.history[-30:]
        self._check_separation(previous, previous_airborne, dt)
        self._spawn_due()

    def _move_surface(self, a, dt):
        if a.status == "ground":
            return
        if a.status == "taxi_in":
            a.taxi_remaining_s -= dt
            if a.taxi_remaining_s <= 0:
                a.status = "landed"
                self._event("landed", f"{a.callsign} arrived at stand (abstract taxi)", [a.callsign])
            return
        runway = self.runways[a.runway]
        initial = 45 if a.wake == "medium" else 65
        if a.status == "landing_roll":
            initial = 40 if a.wake == "medium" else 60
            if self.weather["precipitation"] != "none":
                initial *= 1.2
        a.roll_remaining_s -= dt
        fraction = clamp(1 - a.roll_remaining_s / initial, 0, 1)
        distance_fraction = fraction ** 2 if a.status == "takeoff_roll" else 1 - (1 - fraction) ** 2
        a.x_nm = runway.threshold[0] + (runway.end[0] - runway.threshold[0]) * distance_fraction * 0.85
        a.y_nm = runway.threshold[1] + (runway.end[1] - runway.threshold[1]) * distance_fraction * 0.85
        a.speed_kt = a.spec.approach_speed_kt * (fraction if a.status == "takeoff_roll" else 1 - fraction)
        if a.roll_remaining_s <= 0:
            self._release_runway(a)
            if a.status == "takeoff_roll":
                a.status = "outbound"
                a.altitude_ft = 50
                a.target_altitude_ft = 12000
                a.target_speed_kt = min(280, a.spec.max_speed_kt)
                self._event("takeoff", f"{a.callsign} airborne runway {runway.id}", [a.callsign])
            else:
                a.status, a.speed_kt, a.taxi_remaining_s = "taxi_in", 0, 120

    def _move_airborne(self, a, dt):
        if a.status == "holding":
            a.target_heading_deg = (a.heading_deg + a.spec.turn_deg_s * dt) % 360
        elif a.status == "approach":
            runway = self.runways[a.runway]
            problem = self._runway_problem(a, runway)
            if problem:
                self._go_around(a, problem)
            else:
                if a.approach_stage == "intercept":
                    a.waypoint = runway.approach_fix
                    a.target_altitude_ft, a.target_speed_kt = 3200, 190
                    distance = math.dist((a.x_nm, a.y_nm), a.waypoint)
                    if distance < 0.4 and a.altitude_ft < 3700:
                        a.approach_stage = "final"
                        a.waypoint = runway.threshold
                if a.approach_stage == "final":
                    distance = math.dist((a.x_nm, a.y_nm), runway.threshold)
                    a.waypoint = runway.threshold
                    a.target_speed_kt = a.spec.approach_speed_kt if distance < 7 else 180
                    a.target_altitude_ft = max(0, distance * 318)
                    if distance < 0.9:
                        state = self._runway_states[runway.physical_id]
                        if state.occupied_by or self._available_in(runway, a.wake) > distance / max(a.speed_kt, 1) * 3600:
                            # A pilot goes around an obstructed threshold; track the unsafe sequencing.
                            if state.occupied_by:
                                self._counters["runway_incursions"] += 1
                            else:
                                self._counters["wake_violations"] += 1
                            self._go_around(a, "occupied runway or inadequate wake spacing")
        if a.waypoint is not None:
            dx, dy = a.waypoint[0] - a.x_nm, a.waypoint[1] - a.y_nm
            # Wind correction tracks a ground route; heading commands retain drift.
            wind_angle = math.radians(self.weather["wind_from_deg"])
            desired = bearing(dx, dy)
            cross = self.weather["wind_speed_kt"] * math.sin(wind_angle - math.radians(desired))
            correction = math.degrees(math.asin(clamp(cross / max(a.speed_kt, 100), -0.8, 0.8)))
            a.target_heading_deg = (desired + correction) % 360
            if a.status != "approach" and math.hypot(dx, dy) < 0.25:
                a.waypoint = None
        turn_rate = a.spec.turn_deg_s * (0.6 if a.emergency and a.emergency["kind"] == "engine_failure" else 1)
        a.heading_deg = (a.heading_deg + clamp(angle_delta(a.target_heading_deg, a.heading_deg), -turn_rate * dt, turn_rate * dt)) % 360
        a.speed_kt += clamp(a.target_speed_kt - a.speed_kt, -1.5 * dt, 1.2 * dt)
        climb = a.spec.climb_fpm * (0.45 if a.emergency and a.emergency["kind"] == "engine_failure" else 1)
        a.altitude_ft += clamp(a.target_altitude_ft - a.altitude_ft, -a.spec.descent_fpm / 60 * dt, climb / 60 * dt)
        h, wind = math.radians(a.heading_deg), math.radians(self.weather["wind_from_deg"])
        a.x_nm += (math.sin(h) * a.speed_kt - math.sin(wind) * self.weather["wind_speed_kt"]) / 3600 * dt
        a.y_nm += (math.cos(h) * a.speed_kt - math.cos(wind) * self.weather["wind_speed_kt"]) / 3600 * dt
        if a.status == "approach" and a.approach_stage == "final":
            runway = self.runways[a.runway]
            distance = math.dist((a.x_nm, a.y_nm), runway.threshold)
            if distance < 0.12:
                if a.altitude_ft < 180 and abs(angle_delta(a.heading_deg, runway.heading_deg)) < 18:
                    state = self._runway_states[runway.physical_id]
                    if state.occupied_by:
                        self._go_around(a, "threshold occupied")
                        self._counters["runway_incursions"] += 1
                    else:
                        state.occupied_by = a.callsign
                        a.status, a.approach_stage = "landing_roll", None
                        a.altitude_ft = 0
                        a.x_nm, a.y_nm = runway.threshold
                        a.heading_deg = runway.heading_deg
                        a.roll_remaining_s = (40 if a.wake == "medium" else 60) * (1.2 if self.weather["precipitation"] != "none" else 1)
                        if a.emergency:
                            a.emergency_touchdown_time_s = self.time_s
                        if a.emergency and a.callsign not in self._failed_emergencies:
                            self._landed_emergencies.add(a.callsign)
                            self._counters["emergency_landings"] += 1
                        self._event("touchdown", f"{a.callsign} touchdown runway {runway.id}", [a.callsign])
                else:
                    self._go_around(a, "unstable approach")
        if a.status in AIRBORNE and math.hypot(a.x_nm, a.y_nm) > 40:
            if a.kind == "departure":
                a.status = "departed"
                self._event("departed", f"{a.callsign} handed off at sector boundary", [a.callsign])
            elif a.status == "diverting":
                a.status = "diverted"
                self._fail_emergency(a, "emergency flight left the sector without landing")
                self._event("diverted", f"{a.callsign} diverted out of sector", [a.callsign])
            else:
                a.status = "diverted"
                self._fail_emergency(a, "emergency flight lost out of sector")
                self._event("lost_contact", f"{a.callsign} left sector without landing clearance", [a.callsign])

    def _check_separation(self, previous, previous_airborne, dt):
        active = [self.aircraft[c] for c in previous if self.aircraft[c].status != "crashed"]
        current_conflicts, conflicts = set(), []
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                pair = tuple(sorted((a.callsign, b.callsign)))
                ap, bp = (a.x_nm, a.y_nm, a.altitude_ft), (b.x_nm, b.y_nm, b.altitude_ft)
                if _swept_overlap(previous[a.callsign], ap, previous[b.callsign], bp, 0.06, 100):
                    if pair not in self._collided_pairs:
                        self._collided_pairs.add(pair)
                        self._counters["collisions"] += 1
                        self._crash(a, f"collision with {b.callsign}")
                        self._crash(b, f"collision with {a.callsign}")
                if a.callsign in previous_airborne and b.callsign in previous_airborne and _swept_overlap(previous[a.callsign], ap, previous[b.callsign], bp, 3, 1000):
                    current_conflicts.add(pair)
                    self._counters["separation_loss_seconds"] += dt
                    if pair not in self._active_conflicts:
                        self._counters["separation_losses"] += 1
                        self._event("separation", f"Loss of separation: {a.callsign} / {b.callsign}", pair)
                    conflicts.append({"callsigns": list(pair), "distance_nm": round(math.dist(ap[:2], bp[:2]), 3),
                                      "vertical_ft": round(abs(ap[2] - bp[2]), 1)})
        self._active_conflicts, self.conflicts = current_conflicts, conflicts

    def metrics(self):
        values = list(self.aircraft.values())
        landed = sum(a.status == "landed" for a in values)
        departed = sum(a.status == "departed" for a in values)
        diverted = sum(a.status == "diverted" for a in values)
        crashed = sum(a.status == "crashed" for a in values)
        unfinished = sum(a.status not in FINISHED for a in values)
        ground = sum(a.ground_time_s for a in values)
        airborne = sum(a.airborne_time_s for a in values)
        departures = [a for a in values if a.kind == "departure"]
        emergencies = [a for a in values if a.emergency is not None]
        ground_wait = sum(a.ground_wait_s for a in departures)
        emergency_wait = sum(a.emergency_wait_s for a in emergencies)
        ground_wait_mean = ground_wait / len(departures) if departures else None
        emergency_wait_mean = emergency_wait / len(emergencies) if emergencies else None
        ground_wait_component = -ground_wait / 60
        emergency_wait_component = -5 * emergency_wait / 60
        emergency_outcomes = {
            "resolved": [a for a in emergencies if a.callsign in self._landed_emergencies],
            "failed": [a for a in emergencies if a.callsign in self._failed_emergencies],
            "pending": [a for a in emergencies if a.callsign not in self._landed_emergencies
                        and a.callsign not in self._failed_emergencies],
        }
        c = self._counters
        rate = (landed + departed) / max(1, len(values))
        efficiency = (rate * 1000 - diverted * 50 - unfinished * 10 - crashed * 100
                      - ground / 60 * 0.2 - airborne / 60 * 0.05
                      + ground_wait_component + emergency_wait_component
                      - c["invalid_commands"] * 0.5 - c["separation_loss_seconds"] * 0.5
                      - c["weather_exposure_seconds"] * 0.2 - c["runway_incursions"] * 100 - c["wake_violations"] * 50)
        score = clamp(efficiency, -9999, 9999) - c["collisions"] * 1_000_000 - c["emergencies_failed"] * 100_000 - crashed * 100_000
        metrics = {**c, "score": round(score, 3), "landed": landed, "departed": departed,
                   "diverted": diverted, "crashed": crashed, "unfinished": unfinished, "spawned": len(values),
                   "emergencies_unresolved": sum(a.emergency is not None and a.callsign not in self._failed_emergencies
                                                 and a.callsign not in self._landed_emergencies for a in values),
                   "ground_delay_seconds": round(ground, 3), "airborne_seconds": round(airborne, 3),
                   "ground_wait_seconds": round(ground_wait, 3), "ground_wait_count": len(departures),
                   "ground_wait_mean_seconds": round(ground_wait_mean, 3) if departures else None,
                   "ground_wait_max_seconds": round(max(a.ground_wait_s for a in departures), 3) if departures else None,
                   "ground_wait_score": round(100 / (1 + ground_wait_mean / 300), 3) if departures else None,
                   "ground_wait_score_component": round(ground_wait_component, 3),
                   "emergency_wait_seconds": round(emergency_wait, 3), "emergency_wait_count": len(emergencies),
                   "emergency_wait_mean_seconds": round(emergency_wait_mean, 3) if emergencies else None,
                   "emergency_wait_max_seconds": round(max(a.emergency_wait_s for a in emergencies), 3) if emergencies else None,
                   "emergency_wait_score": round(100 / (1 + emergency_wait_mean / 180), 3) if emergencies else None,
                   "emergency_wait_score_component": round(emergency_wait_component, 3),
                   "emergency_wait_by_outcome": {
                       outcome: {"count": len(flights), "seconds": round(sum(a.emergency_wait_s for a in flights), 3),
                                 "mean_seconds": round(sum(a.emergency_wait_s for a in flights) / len(flights), 3) if flights else None,
                                 "max_seconds": round(max(a.emergency_wait_s for a in flights), 3) if flights else None}
                       for outcome, flights in emergency_outcomes.items()},
                   "completion_rate": round(rate, 6), "safety_violations": crashed + c["collisions"] + c["emergencies_failed"] + c["separation_losses"] + c["runway_incursions"] + c["wake_violations"]}
        metrics["rank_key"] = [c["collisions"], crashed, c["emergencies_failed"], c["runway_incursions"],
                               c["wake_violations"], round(c["separation_loss_seconds"], 3),
                               round(c["weather_exposure_seconds"], 3), -(landed + departed), round(ground, 3), round(airborne, 3)]
        return metrics

    def observe(self):
        runways = []
        for r in self.runways.values():
            state = self._runway_states[r.physical_id]
            runways.append({**asdict(r), "occupied_by": state.occupied_by,
                            "available_in_s": round(self._available_in(r), 1), "closed": self._closed(r),
                            "approach_fix": list(r.approach_fix)})
        aircraft = []
        for a in self.aircraft.values():
            aircraft.append({key: value for key, value in asdict(a).items()
                             if key not in {"emergency_at_s", "emergency_kind", "emergency_budget_s", "waypoint", "roll_remaining_s", "taxi_remaining_s"}})
            aircraft[-1].update(wake=a.wake, min_speed_kt=a.spec.min_speed_kt,
                                 max_speed_kt=a.spec.max_speed_kt, landing_distance_m=a.spec.landing_distance_m,
                                 takeoff_distance_m=a.spec.takeoff_distance_m, crosswind_limit_kt=a.spec.crosswind_limit_kt)
        # asdict copies mutable aircraft data; weather/events are also copied so
        # an observation-only agent cannot modify simulator state by aliasing.
        from copy import deepcopy
        return {"time_s": self.time_s, "duration_s": self.duration_s, "seed": self.seed,
                "scenario": self.scenario, "done": self.done,
                "airport": {"icao": "EDDF", "name": "Frankfurt am Main · benchmark mock", "elevation_ft": 328,
                            "radius_nm": 40, "fixes": {k: list(v) for k, v in FIXES.items()}, "runways": runways},
                "weather": deepcopy(self.weather), "aircraft": aircraft, "metrics": self.metrics(),
                "events": deepcopy(self.events), "conflicts": deepcopy(self.conflicts),
                "command_results": deepcopy(self.command_results)}
