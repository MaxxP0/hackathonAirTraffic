"""Waiting metrics measure observed service time without exposing future events."""

from dataclasses import replace
import math
import unittest

from atc_bench import AirTrafficEnv


def isolated_plane(kind="arrival", duration_s=120):
    env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=duration_s)
    prototype = next(a for a in env.aircraft.values() if a.kind == kind)
    plane = replace(prototype, callsign="WAIT101", type="A320", history=[],
                    emergency=None, emergency_at_s=None, emergency_kind=None)
    env.aircraft = {plane.callsign: plane}
    env._schedule = []
    env._next_spawn = 0
    return env, plane


def announce_at(plane, time_s=3, budget_s=90):
    plane.emergency_at_s = time_s
    plane.emergency_kind = "medical"
    plane.emergency_budget_s = budget_s


def position_for_touchdown(env, plane):
    runway = env.runways["25R"]
    direction = math.radians(runway.heading_deg)
    plane.x_nm = runway.threshold[0] - 0.15 * math.sin(direction)
    plane.y_nm = runway.threshold[1] - 0.15 * math.cos(direction)
    plane.status, plane.runway, plane.approach_stage = "approach", "25R", "final"
    plane.heading_deg = plane.target_heading_deg = runway.heading_deg
    plane.altitude_ft = plane.target_altitude_ft = 20
    plane.speed_kt = plane.target_speed_kt = plane.spec.approach_speed_kt


class WaitScoreTests(unittest.TestCase):
    def test_ground_queue_stops_at_takeoff_clearance(self):
        env, plane = isolated_plane("departure")
        env.step([], seconds=20)
        result = env.step([f"TAKEOFF {plane.callsign} 25C"], seconds=20)
        self.assertTrue(result["command_results"][0]["accepted"])
        self.assertEqual(plane.ground_wait_s, 20)
        self.assertEqual(plane.ground_time_s, 40)
        metrics = result["metrics"]
        self.assertEqual(metrics["ground_wait_count"], 1)
        self.assertEqual(metrics["ground_wait_seconds"], 20)
        self.assertEqual(metrics["ground_wait_mean_seconds"], 20)
        self.assertEqual(metrics["ground_wait_max_seconds"], 20)

    def test_emergency_clock_starts_at_public_declaration(self):
        env, plane = isolated_plane()
        announce_at(plane)
        before = env.step([], seconds=2)
        self.assertIsNone(before["aircraft"][0]["emergency_declared_time_s"])
        self.assertEqual(before["metrics"]["emergency_wait_count"], 0)
        for private_field in ("emergency_at_s", "emergency_kind", "emergency_budget_s"):
            self.assertNotIn(private_field, before["aircraft"][0])
        declared = env.step([], seconds=1)
        self.assertEqual(declared["aircraft"][0]["emergency_declared_time_s"], 3)
        self.assertEqual(declared["aircraft"][0]["emergency_wait_s"], 0)
        after = env.step([], seconds=5)
        self.assertEqual(after["aircraft"][0]["emergency_wait_s"], 5)
        self.assertEqual(after["metrics"]["emergency_wait_count"], 1)
        self.assertEqual(after["metrics"]["emergency_wait_by_outcome"]["pending"]["seconds"], 5)

    def test_touchdown_freezes_emergency_clock_before_taxi_completes(self):
        env, plane = isolated_plane(duration_s=300)
        announce_at(plane)
        env.step([], seconds=8)
        position_for_touchdown(env, plane)
        env.step([], seconds=1)
        self.assertEqual(plane.status, "landing_roll")
        self.assertEqual(plane.emergency_touchdown_time_s, 9)
        self.assertEqual(plane.emergency_wait_s, 6)
        env.step([], seconds=60)
        self.assertEqual(plane.emergency_wait_s, 6)
        self.assertEqual(env.metrics()["emergency_wait_by_outcome"]["resolved"],
                         {"count": 1, "seconds": 6, "mean_seconds": 6, "max_seconds": 6})

    def test_crash_freezes_observed_wait_and_counts_as_failed(self):
        env, plane = isolated_plane()
        announce_at(plane, time_s=1)
        plane.fuel_s = 4
        env.step([], seconds=10)
        self.assertEqual(plane.status, "crashed")
        self.assertEqual(plane.emergency_wait_s, 3)
        self.assertIsNone(plane.emergency_touchdown_time_s)
        self.assertEqual(env.metrics()["emergency_wait_by_outcome"]["failed"]["seconds"], 3)
        env.step([], seconds=10)
        self.assertEqual(plane.emergency_wait_s, 3)

    def test_diversion_freezes_wait_at_sector_exit(self):
        env, plane = isolated_plane()
        announce_at(plane, time_s=1)
        env.step([], seconds=3)
        plane.x_nm, plane.y_nm = 39.99, 0
        plane.heading_deg = plane.target_heading_deg = 90
        plane.speed_kt = plane.target_speed_kt = 250
        result = env.step([f"DIVERT {plane.callsign}"], seconds=10)
        self.assertTrue(result["command_results"][0]["accepted"])
        self.assertEqual(plane.status, "diverted")
        self.assertEqual(plane.emergency_wait_s, 3)
        self.assertEqual(result["metrics"]["emergency_wait_by_outcome"]["failed"]["count"], 1)

    def test_pending_wait_is_included_at_horizon(self):
        env, plane = isolated_plane(duration_s=12)
        announce_at(plane)
        result = env.step([], seconds=60)
        self.assertTrue(result["done"])
        self.assertEqual(plane.emergency_wait_s, 9)
        self.assertEqual(result["metrics"]["emergency_wait_seconds"], 9)
        self.assertEqual(result["metrics"]["emergency_wait_mean_seconds"], 9)
        self.assertEqual(result["metrics"]["emergency_wait_by_outcome"]["pending"]["count"], 1)
        env.step([], seconds=60)
        self.assertEqual(plane.emergency_wait_s, 9)

    def test_deadline_failure_keeps_counting_until_late_touchdown(self):
        env, plane = isolated_plane()
        announce_at(plane, time_s=1, budget_s=2)
        env.step([], seconds=6)
        self.assertEqual(plane.emergency_wait_s, 5)
        self.assertEqual(env.metrics()["emergencies_failed"], 1)
        position_for_touchdown(env, plane)
        env.step([], seconds=1)
        self.assertEqual(plane.emergency_touchdown_time_s, 7)
        self.assertEqual(plane.emergency_wait_s, 6)
        breakdown = env.metrics()["emergency_wait_by_outcome"]
        self.assertEqual(breakdown["failed"]["count"], 1)
        self.assertEqual(breakdown["resolved"]["count"], 0)
        self.assertEqual(breakdown["pending"]["count"], 0)

    def test_absent_populations_have_null_diagnostics(self):
        env, _ = isolated_plane()
        metrics = env.metrics()
        for prefix in ("ground_wait", "emergency_wait"):
            self.assertEqual(metrics[f"{prefix}_seconds"], 0)
            self.assertEqual(metrics[f"{prefix}_count"], 0)
            self.assertIsNone(metrics[f"{prefix}_mean_seconds"])
            self.assertIsNone(metrics[f"{prefix}_max_seconds"])
            self.assertIsNone(metrics[f"{prefix}_score"])

    def test_scores_decrease_with_waiting_and_components_match_formula(self):
        env, plane = isolated_plane("departure")
        before = env.metrics()
        self.assertEqual(before["ground_wait_score"], 100)
        plane.ground_wait_s = 300
        ground = env.metrics()
        self.assertEqual(ground["ground_wait_score"], 50)
        self.assertEqual(ground["ground_wait_score_component"], -5)
        self.assertEqual(ground["score"], before["score"] - 5)
        plane.emergency = {"kind": "medical", "deadline_s": 500}
        plane.emergency_declared_time_s = 0
        self.assertEqual(env.metrics()["emergency_wait_score"], 100)
        plane.emergency_wait_s = 180
        emergency = env.metrics()
        self.assertEqual(emergency["emergency_wait_score"], 50)
        self.assertEqual(emergency["emergency_wait_score_component"], -15)
        self.assertEqual(emergency["score"], ground["score"] - 15)
        self.assertEqual(emergency["rank_key"], before["rank_key"])

    def test_mean_includes_zero_wait_and_unfinished_departures(self):
        env, first = isolated_plane("departure")
        first.ground_wait_s = 60
        second = replace(first, callsign="WAIT102", status="outbound", ground_wait_s=0, history=[])
        env.aircraft[second.callsign] = second
        metrics = env.metrics()
        self.assertEqual(metrics["ground_wait_count"], 2)
        self.assertEqual(metrics["ground_wait_seconds"], 60)
        self.assertEqual(metrics["ground_wait_mean_seconds"], 30)
        self.assertEqual(metrics["ground_wait_max_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
