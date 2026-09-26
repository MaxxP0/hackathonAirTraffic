"""Controlled traffic fixtures exercise safety accounting between decision ticks."""

from dataclasses import replace
import unittest

from atc_bench import AirTrafficEnv


def pair_environment(*, separation_ft=0, head_on=False):
    env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=30)
    prototype = next(a for a in env.aircraft.values() if a.kind == "arrival")
    first = replace(prototype, callsign="TEST101", type="A320", status="inbound",
                    x_nm=-1 if head_on else 0, y_nm=12, altitude_ft=6000,
                    heading_deg=90, speed_kt=300, target_altitude_ft=6000,
                    target_heading_deg=90, target_speed_kt=300, emergency=None,
                    emergency_at_s=None, runway=None, history=[])
    second = replace(first, callsign="TEST102", x_nm=1, altitude_ft=6000 + separation_ft,
                     target_altitude_ft=6000 + separation_ft,
                     heading_deg=270 if head_on else 90,
                     target_heading_deg=270 if head_on else 90, history=[])
    env.aircraft = {first.callsign: first, second.callsign: second}
    return env


class SafetyTests(unittest.TestCase):
    def test_collision_between_decision_ticks_is_detected_once(self):
        env = pair_environment(head_on=True)
        result = env.step([], seconds=30)
        self.assertEqual(result["metrics"]["collisions"], 1)
        self.assertEqual(sum(a["status"] == "crashed" for a in result["aircraft"]), 2)

    def test_collision_outcome_does_not_depend_on_decision_interval(self):
        coarse = pair_environment(head_on=True)
        fine = pair_environment(head_on=True)
        coarse.step([], seconds=30)
        for _ in range(30):
            fine.step([], seconds=1)
        self.assertEqual(coarse.metrics(), fine.metrics())

    def test_continuous_separation_loss_is_one_event_with_elapsed_duration(self):
        env = pair_environment()
        first = env.step([], seconds=10)
        self.assertEqual(first["metrics"]["separation_losses"], 1)
        self.assertEqual(first["metrics"]["collisions"], 0)
        second = env.step([], seconds=10)
        self.assertEqual(second["metrics"]["separation_losses"], 1)
        self.assertEqual(second["metrics"]["separation_loss_seconds"], 20)

    def test_vertical_separation_prevents_false_conflict(self):
        env = pair_environment(separation_ft=2000)
        result = env.step([], seconds=10)
        self.assertEqual(result["metrics"]["collisions"], 0)
        self.assertEqual(result["metrics"]["separation_losses"], 0)
        self.assertEqual(result["conflicts"], [])

    def test_same_runway_is_not_cleared_to_two_departures(self):
        env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=180)
        departures = [a for a in env.aircraft.values() if a.kind == "departure"]
        first, second = departures[:2]
        result = env.step([f"TAKEOFF {first.callsign} 25C", f"TAKEOFF {second.callsign} 25C"], seconds=0)
        self.assertTrue(result["command_results"][0]["accepted"])
        self.assertFalse(result["command_results"][1]["accepted"])
        self.assertEqual(second.status, "ground")
        runways = {r["id"]: r for r in result["airport"]["runways"]}
        self.assertEqual(runways["25C"]["occupied_by"], first.callsign)
        self.assertEqual(runways["07C"]["occupied_by"], first.callsign)

    def test_wake_timer_is_shared_by_reciprocal_runway_ends(self):
        env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=180)
        departure = next(a for a in env.aircraft.values() if a.kind == "departure")
        env.step([f"TAKEOFF {departure.callsign} 25C"], seconds=0)
        for _ in range(150):
            result = env.step([], seconds=1)
            runways = {r["id"]: r for r in result["airport"]["runways"]}
            if runways["25C"]["occupied_by"] is None:
                break
        self.assertIsNone(runways["25C"]["occupied_by"])
        self.assertGreater(runways["25C"]["available_in_s"], 0)
        self.assertEqual(runways["25C"]["available_in_s"], runways["07C"]["available_in_s"])

    def test_landing_only_and_departure_only_restrictions_are_enforced(self):
        env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=120)
        arrival = next(a for a in env.aircraft.values() if a.kind == "arrival")
        departure = next(a for a in env.aircraft.values() if a.kind == "departure")
        result = env.step([f"TAKEOFF {departure.callsign} 25R", f"APPROACH {arrival.callsign} 18"], seconds=0)
        self.assertTrue(all(not r["accepted"] for r in result["command_results"]))

    def test_largest_aircraft_cannot_use_short_northwest_runway(self):
        env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=120)
        arrival = next(a for a in env.aircraft.values() if a.kind == "arrival")
        arrival.type = "A388"
        result = env.step([f"APPROACH {arrival.callsign} 25R"], seconds=0)
        self.assertFalse(result["command_results"][0]["accepted"])

    def test_wind_shift_changes_usable_runway_direction(self):
        env = AirTrafficEnv(seed=7, scenario="wind_shift", duration_s=100)
        env.step([], seconds=46)
        self.assertEqual(env.observe()["weather"]["active_direction"], "07")
        departures = [a for a in env.aircraft.values() if a.kind == "departure" and a.status == "ground"]
        result = env.step([f"TAKEOFF {departures[0].callsign} 25C", f"TAKEOFF {departures[1].callsign} 07C"], seconds=0)
        self.assertFalse(result["command_results"][0]["accepted"])
        self.assertTrue(result["command_results"][1]["accepted"])

    def test_emergency_scenario_announces_deadline(self):
        env = AirTrafficEnv(seed=7, scenario="emergency", duration_s=900)
        result = env.step([], seconds=20)
        emergencies = [a for a in result["aircraft"] if a["emergency"] is not None]
        self.assertGreater(len(emergencies), 0)
        for aircraft in emergencies:
            self.assertGreater(aircraft["emergency"]["deadline_s"], result["time_s"])
            self.assertTrue(aircraft["emergency"]["kind"])

    def test_ignored_emergency_fails_once(self):
        env = pair_environment(separation_ft=2000)
        env.aircraft = {"TEST101": env.aircraft["TEST101"]}
        plane = env.aircraft["TEST101"]
        plane.emergency = {"kind": "engine_failure", "deadline_s": 3}
        result = env.step([], seconds=5)
        self.assertEqual(result["metrics"]["emergencies_failed"], 1)
        result = env.step([], seconds=5)
        self.assertEqual(result["metrics"]["emergencies_failed"], 1)
        self.assertEqual(result["metrics"]["emergency_landings"], 0)

    def test_emergency_can_land_before_deadline_and_complete_taxi(self):
        env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=900)
        plane = next(a for a in env.aircraft.values() if a.kind == "arrival")
        runway = env.runways["25R"]
        plane.type = "A320"
        plane.x_nm, plane.y_nm = runway.approach_fix
        plane.heading_deg = plane.target_heading_deg = runway.heading_deg
        plane.altitude_ft = plane.target_altitude_ft = 3200
        plane.speed_kt = plane.target_speed_kt = 190
        plane.emergency_at_s = None
        plane.emergency = {"kind": "medical", "deadline_s": 500}
        env.aircraft = {plane.callsign: plane}
        env._schedule = []  # Isolate one scheduled emergency from unrelated traffic.
        env.step([f"APPROACH {plane.callsign} 25R"], seconds=0)
        while not env.done and plane.status != "landed":
            env.step([], seconds=10)
        self.assertEqual(plane.status, "landed")
        self.assertEqual(env.metrics()["emergency_landings"], 1)
        self.assertEqual(env.metrics()["emergencies_failed"], 0)
        self.assertGreater(plane.ground_time_s, 120)

    def test_adverse_weather_rejects_unsuitable_clearances(self):
        for weather in ({"wind_from_deg": 160, "wind_speed_kt": 35, "gust_kt": 40},
                        {"visibility_m": 200}, {"ceiling_ft": 100}):
            with self.subTest(weather=weather):
                env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=30)
                plane = next(a for a in env.aircraft.values() if a.kind == "arrival")
                env.weather.update(weather)
                result = env.step([f"APPROACH {plane.callsign} 25C"], seconds=0)
                self.assertFalse(result["command_results"][0]["accepted"])

    def test_wet_surface_changes_required_landing_distance(self):
        dry = AirTrafficEnv(seed=7, scenario="mixed", duration_s=30)
        wet = AirTrafficEnv(seed=7, scenario="low_visibility", duration_s=30)
        for env in (dry, wet):
            plane = next(a for a in env.aircraft.values() if a.kind == "arrival")
            plane.type = "B77W"  # 2600 m fits a dry 2800 m strip, not 15% extra wet distance.
            result = env.step([f"APPROACH {plane.callsign} 25R"], seconds=0)
            self.assertEqual(result["command_results"][0]["accepted"], env is dry)

    def test_pending_emergency_is_visible_at_episode_horizon(self):
        env = AirTrafficEnv(seed=7, scenario="emergency", duration_s=30)
        result = env.step([], seconds=30)
        self.assertTrue(result["done"])
        self.assertEqual(result["metrics"]["emergencies_unresolved"], 1)
        self.assertEqual(result["metrics"]["emergencies_failed"], 0)

    def test_rerouting_low_final_starts_a_go_around_climb(self):
        for instruction in ("HEADING {callsign} 123", "DIRECT {callsign} NORTH", "HOLD {callsign}"):
            with self.subTest(instruction=instruction):
                env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=30)
                plane = next(a for a in env.aircraft.values() if a.kind == "arrival")
                plane.status = "approach"
                plane.runway = "25C"
                plane.approach_stage = "final"
                plane.altitude_ft = 120
                plane.target_altitude_ft = 30
                result = env.step([instruction.format(callsign=plane.callsign)], seconds=0)
                self.assertTrue(result["command_results"][0]["accepted"])
                self.assertGreaterEqual(plane.target_altitude_ft, 4000)
                self.assertIsNone(plane.runway)
                self.assertIsNone(plane.approach_stage)
                self.assertTrue(any(event["type"] == "go_around" for event in result["events"]))


if __name__ == "__main__":
    unittest.main()
