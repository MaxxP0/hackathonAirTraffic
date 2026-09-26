"""Small, explicit state objects and deliberately approximate aircraft envelopes."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AircraftType:
    name: str
    wake: str
    min_speed_kt: float
    max_speed_kt: float
    approach_speed_kt: float
    climb_fpm: float
    descent_fpm: float
    turn_deg_s: float
    landing_distance_m: int
    takeoff_distance_m: int
    crosswind_limit_kt: float


# Benchmark abstractions, not certified aircraft performance data.
AIRCRAFT_TYPES = {
    "E190": AircraftType("E190", "medium", 140, 300, 140, 2600, 2200, 3.0, 1600, 1900, 28),
    "A320": AircraftType("A320", "medium", 140, 320, 145, 2500, 2000, 2.8, 1800, 2200, 30),
    "B738": AircraftType("B738", "medium", 145, 320, 150, 2400, 2000, 2.8, 1900, 2400, 30),
    "A359": AircraftType("A359", "heavy", 150, 340, 155, 2000, 1800, 2.2, 2400, 2900, 32),
    "B77W": AircraftType("B77W", "heavy", 155, 340, 160, 1800, 1700, 2.0, 2600, 3100, 30),
    "A388": AircraftType("A388", "super", 155, 330, 160, 1600, 1500, 1.8, 3000, 3400, 30),
}


@dataclass
class Aircraft:
    callsign: str
    type: str
    kind: str
    status: str
    x_nm: float
    y_nm: float
    altitude_ft: float
    heading_deg: float
    speed_kt: float
    target_altitude_ft: float
    target_heading_deg: float
    target_speed_kt: float
    spawn_time_s: float
    fuel_s: float = 3600
    runway: str | None = None
    emergency: dict | None = None
    ground_time_s: float = 0
    ground_wait_s: float = 0
    airborne_time_s: float = 0
    emergency_wait_s: float = 0
    emergency_declared_time_s: float | None = None
    emergency_touchdown_time_s: float | None = None
    approach_stage: str | None = None
    history: list = field(default_factory=list)
    waypoint: tuple | None = None
    roll_remaining_s: float = 0
    taxi_remaining_s: float = 0
    emergency_at_s: float | None = None
    emergency_kind: str | None = None
    emergency_budget_s: float = 720

    @property
    def spec(self):
        return AIRCRAFT_TYPES[self.type]

    @property
    def wake(self):
        return self.spec.wake


@dataclass(frozen=True)
class Runway:
    id: str
    physical_id: str
    heading_deg: float
    length_m: int
    threshold: tuple
    end: tuple
    arrival: bool
    departure: bool

    @property
    def approach_fix(self):
        import math
        h = math.radians(self.heading_deg)
        return (self.threshold[0] - 10 * math.sin(h), self.threshold[1] - 10 * math.cos(h))


@dataclass
class RunwayState:
    occupied_by: str | None = None
    last_release_s: float = -10000
    last_wake: str = "medium"


AIRBORNE = {"inbound", "holding", "approach", "outbound", "diverting"}
FINISHED = {"landed", "departed", "diverted", "crashed"}
SURFACE = {"ground", "takeoff_roll", "landing_roll", "taxi_in"}
