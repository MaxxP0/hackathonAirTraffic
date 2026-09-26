"""Seeded runway disruption, visibility, and clearance behavior."""

import json
import math
import unittest

from atc_bench import AirTrafficEnv
from atc_bench.agents import ReferenceAgent
from atc_bench.scenarios import SCENARIOS, runway_closure


def advance_to(env, target):
    while env.time_s < target:
        env.step([], seconds=min(60, int(target - env.time_s)))
    return env.observe()


def isolated_environment(seed=1, duration=1800):
    env = AirTrafficEnv(seed=seed, scenario="runway_closure", duration_s=duration)
    arrival = next(a for a in env.aircraft.values() if a.kind == "arrival")
    departure = next(a for a in env.aircraft.values() if a.kind == "departure")
    arrival.type = departure.type = "A320"
    arrival.status = "holding"
    arrival.x_nm, arrival.y_nm = 12, 10
    arrival.altitude_ft = arrival.target_altitude_ft = 9000
    arrival.fuel_s = 10000
    arrival.emergency_at_s = None
    env.aircraft = {arrival.callsign: arrival, departure.callsign: departure}
    env._schedule = []
    return env, arrival, departure


class RunwayClosureTests(unittest.TestCase):
    def test_schedule_and_simulation_are_seeded_and_cover_all_arrival_strips(self):
        first = AirTrafficEnv(seed=7, scenario="runway_closure", duration_s=600)
        second = AirTrafficEnv(seed=7, scenario="runway_closure", duration_s=600)
        self.assertEqual(first.observe(), second.observe())
        for _ in range(10):
            self.assertEqual(first.step([], seconds=60), second.step([], seconds=60))
        schedules = [runway_closure(seed, "runway_closure", 1800) for seed in range(12)]
        self.assertEqual({item["physical_id"] for item in schedules}, {"NW", "CENTER", "SOUTH"})
        self.assertGreater(len({item["start_s"] for item in schedules}), 1)
        for item in schedules:
            self.assertGreaterEqual(item["start_s"], 1800 * .25)
            self.assertLessEqual(item["start_s"], 1800 * .4)
            self.assertGreaterEqual(item["end_s"] - item["start_s"], 240)
            self.assertLessEqual(item["end_s"] - item["start_s"], 480)

    def test_future_strip_and_closure_times_are_not_in_the_observation(self):
        first = AirTrafficEnv(seed=1, scenario="runway_closure")
        second = AirTrafficEnv(seed=2, scenario="runway_closure")
        self.assertNotEqual(first._runway_closure["physical_id"], second._runway_closure["physical_id"])
        self.assertEqual(first.observe()["airport"], second.observe()["airport"])
        for env in (first, second):
            observation = env.observe()
            encoded = json.dumps(observation)
            for hidden in ("start_s", "end_s", "_runway_closure", "reopens_at"):
                self.assertNotIn(hidden, encoded)
            self.assertFalse(any(runway["closed"] for runway in observation["airport"]["runways"]))
            self.assertFalse(any(event["type"].startswith("runway_") for event in observation["events"]))

    def test_closure_boundaries_do_not_change_physics_with_different_step_batches(self):
        coarse = AirTrafficEnv(seed=1, scenario="runway_closure", duration_s=600)
        fine = AirTrafficEnv(seed=1, scenario="runway_closure", duration_s=600)
        for _ in range(10):
            coarse.step([], seconds=60)
        for _ in range(600):
            fine.step([], seconds=1)
        self.assertEqual(coarse.observe(), fine.observe())

    def test_both_reciprocals_close_then_reopen_with_one_event_each(self):
        env, _, _ = isolated_environment()
        schedule = env._runway_closure
        before = advance_to(env, math.floor(schedule["start_s"]))
        self.assertFalse(any(runway["closed"] for runway in before["airport"]["runways"]))
        closed = advance_to(env, math.ceil(schedule["start_s"]))
        self.assertEqual({runway["id"] for runway in closed["airport"]["runways"] if runway["closed"]}, {"25C", "07C"})
        self.assertEqual(closed["metrics"]["runway_closures"], 1)
        self.assertEqual(closed["metrics"]["safety_violations"], 0)
        self.assertEqual(len([event for event in closed["events"] if event["type"] == "runway_closed"]), 1)
        reopened = advance_to(env, math.ceil(schedule["end_s"]))
        self.assertFalse(any(runway["closed"] for runway in reopened["airport"]["runways"]))
        env.step([], seconds=30)
        disruptions = [event["type"] for event in env.observe()["events"] if event["type"].startswith("runway_")]
        self.assertEqual(disruptions, ["runway_closed", "runway_reopened"])

    def test_closed_runway_rejects_new_arrival_and_departure_clearances(self):
        env, arrival, departure = isolated_environment()
        advance_to(env, math.ceil(env._runway_closure["start_s"]))
        for runway_id, direction in (("25C", "25"), ("07C", "07")):
            env.weather.update(active_direction=direction, wind_from_deg=250 if direction == "25" else 70)
            observation = env.step([f"APPROACH {arrival.callsign} {runway_id}",
                                    f"TAKEOFF {departure.callsign} {runway_id}"], seconds=0)
            self.assertTrue(all(not result["accepted"] for result in observation["command_results"]))
            self.assertTrue(all("closed" in result["message"] for result in observation["command_results"]))
        self.assertEqual(arrival.status, "holding")
        self.assertEqual(departure.status, "ground")
        advance_to(env, math.ceil(env._runway_closure["end_s"]))
        observation = env.step([f"APPROACH {arrival.callsign} 25C"], seconds=0)
        self.assertTrue(observation["command_results"][0]["accepted"])

    def test_closure_go_around_clears_old_route_and_controller_can_reroute(self):
        env, arrival, _ = isolated_environment()
        advance_to(env, math.floor(env._runway_closure["start_s"]))
        env.aircraft = {arrival.callsign: arrival}
        runway = env.runways["25C"]
        arrival.x_nm, arrival.y_nm = runway.approach_fix
        arrival.altitude_ft = arrival.target_altitude_ft = 3200
        arrival.heading_deg = arrival.target_heading_deg = runway.heading_deg
        arrival.speed_kt = arrival.target_speed_kt = 190
        accepted = env.step([f"APPROACH {arrival.callsign} 25C"], seconds=0)
        self.assertTrue(accepted["command_results"][0]["accepted"])
        closed = advance_to(env, math.ceil(env._runway_closure["start_s"]))
        self.assertEqual(arrival.status, "inbound")
        self.assertIsNone(arrival.runway)
        self.assertIsNone(arrival.approach_stage)
        self.assertIsNone(arrival.waypoint)
        self.assertGreaterEqual(arrival.target_altitude_ft, 4000)
        self.assertEqual(closed["metrics"]["closure_go_arounds"], 1)
        self.assertEqual(closed["metrics"]["runway_incursions"], 0)
        self.assertEqual(closed["metrics"]["wake_violations"], 0)
        self.assertEqual(closed["metrics"]["safety_violations"], 0)
        self.assertTrue(any(event["type"] == "go_around" and "closed" in event["message"] for event in closed["events"]))
        commands = ReferenceAgent().act(closed)
        self.assertTrue(any(command.startswith(f"APPROACH {arrival.callsign} ") for command in commands))
        self.assertFalse(any(command.endswith(" 25C") for command in commands))
        redirected = env.step(commands, seconds=1)
        self.assertEqual(arrival.status, "approach")
        self.assertNotEqual(env.runways[arrival.runway].physical_id, "CENTER")
        self.assertTrue(all(result["accepted"] for result in redirected["command_results"]))
        self.assertEqual(redirected["metrics"]["closure_go_arounds"], 1)

    def test_aircraft_already_on_the_runway_complete_their_roll_safely(self):
        for status, expected in (("takeoff_roll", "outbound"), ("landing_roll", "taxi_in")):
            with self.subTest(status=status):
                env, arrival, departure = isolated_environment()
                advance_to(env, math.floor(env._runway_closure["start_s"]))
                plane = departure if status == "takeoff_roll" else arrival
                env.aircraft = {plane.callsign: plane}
                plane.status, plane.runway, plane.roll_remaining_s = status, "25C", 10
                plane.altitude_ft = 0
                plane.heading_deg = plane.target_heading_deg = env.runways["25C"].heading_deg
                plane.x_nm, plane.y_nm = env.runways["25C"].threshold
                env._runway_states["CENTER"].occupied_by = plane.callsign
                observation = env.step([], seconds=20)
                self.assertEqual(plane.status, expected)
                self.assertEqual(observation["metrics"]["crashed"], 0)
                self.assertEqual(observation["metrics"]["closure_go_arounds"], 0)
                self.assertEqual(observation["metrics"]["safety_violations"], 0)
                self.assertTrue(next(r for r in observation["airport"]["runways"] if r["id"] == "25C")["closed"])

    def test_short_episodes_still_close_and_reopen(self):
        for duration in (1, 2, 30, 60):
            with self.subTest(duration=duration):
                env = AirTrafficEnv(seed=7, scenario="runway_closure", duration_s=duration)
                observation = env.step([], seconds=duration)
                self.assertTrue(observation["done"])
                self.assertEqual(observation["time_s"], duration)
                disruptions = [event["type"] for event in observation["events"] if event["type"].startswith("runway_")]
                self.assertEqual(disruptions, ["runway_closed", "runway_reopened"])
                self.assertFalse(any(runway["closed"] for runway in observation["airport"]["runways"]))

    def test_other_scenarios_do_not_schedule_operational_closures(self):
        for scenario in SCENARIOS:
            if scenario == "runway_closure":
                continue
            with self.subTest(scenario=scenario):
                env = AirTrafficEnv(seed=7, scenario=scenario, duration_s=180)
                self.assertIsNone(env._runway_closure)
                advance_to(env, 180)
                self.assertEqual(env.metrics()["runway_closures"], 0)
                self.assertEqual(env.metrics()["closure_go_arounds"], 0)
                self.assertFalse(any(event["type"].startswith("runway_") for event in env.observe()["events"]))


if __name__ == "__main__":
    unittest.main()
