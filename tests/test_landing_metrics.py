"""Landing waits include every arrival and stop before ground operations."""
from copy import deepcopy
import unittest

from atc_bench.landing_metrics import landing_metrics
from tests.test_wait_scores import isolated_plane, position_for_touchdown


def snapshot(*rows):
    return {"aircraft": [{"kind": "arrival", "status": status, "airborne_time_s": seconds}
                         for status, seconds in rows]}


class LandingMetricsTests(unittest.TestCase):
    def test_disjoint_outcomes_and_zero_wait_arrivals_are_all_included(self):
        observation = snapshot(("landing_roll", 100), ("taxi_in", 200), ("landed", 300),
                               ("inbound", 0), ("holding", 400), ("approach", 500),
                               ("diverting", 600), ("diverted", 700), ("crashed", 800))
        observation["aircraft"].append({"kind": "departure", "status": "outbound", "airborne_time_s": 9999})
        result = landing_metrics(observation)
        self.assertEqual(result["landing_wait_count"], 9)
        self.assertEqual(result["landing_wait_seconds"], 3600)
        self.assertEqual(result["landing_wait_mean_seconds"], 400)
        self.assertEqual(result["landing_wait_max_seconds"], 800)
        self.assertEqual(result["landing_wait_score"], 60)
        groups = result["landing_wait_by_outcome"]
        self.assertEqual([groups[k]["count"] for k in ("landed", "pending", "failed")], [3, 3, 3])
        self.assertEqual([groups[k]["seconds"] for k in ("landed", "pending", "failed")], [600, 900, 2100])

    def test_empty_population_has_no_score_or_mean(self):
        result = landing_metrics({"aircraft": [{"kind": "departure"}]})
        self.assertEqual(result["landing_wait_count"], 0)
        self.assertEqual(result["landing_wait_seconds"], 0)
        for key in ("mean_seconds", "max_seconds", "score"):
            self.assertIsNone(result[f"landing_wait_{key}"])
        self.assertTrue(all(row["count"] == 0 and row["mean_seconds"] is None
                            for row in result["landing_wait_by_outcome"].values()))

    def test_touchdown_stops_clock_before_runway_roll_and_taxi(self):
        env, plane = isolated_plane(duration_s=300)
        env.step([], seconds=10)
        position_for_touchdown(env, plane)
        touchdown = env.step([], seconds=1)
        self.assertEqual(plane.status, "landing_roll")
        before = landing_metrics(touchdown)
        self.assertEqual(before["landing_wait_seconds"], 11)
        self.assertEqual(before["landing_wait_by_outcome"]["landed"]["count"], 1)
        self.assertEqual(landing_metrics(env.step([], seconds=60)), before)

    def test_pending_arrival_wait_is_censored_at_horizon(self):
        env, _ = isolated_plane(duration_s=12)
        observation = env.step([], seconds=60)
        result = landing_metrics(observation)
        self.assertTrue(observation["done"])
        self.assertEqual(result["landing_wait_seconds"], 12)
        self.assertEqual(result["landing_wait_by_outcome"]["pending"]["count"], 1)

    def test_crashed_and_diverted_arrivals_are_retained(self):
        env, plane = isolated_plane()
        plane.fuel_s = 4
        crashed = landing_metrics(env.step([], seconds=10))
        self.assertEqual(crashed["landing_wait_seconds"], 4)
        self.assertEqual(crashed["landing_wait_by_outcome"]["failed"]["count"], 1)
        self.assertEqual(landing_metrics(env.step([], seconds=10)), crashed)
        env, plane = isolated_plane()
        env.step([], seconds=3)
        plane.x_nm, plane.y_nm = 39.99, 0
        plane.heading_deg = plane.target_heading_deg = 90
        diverted = env.step([f"DIVERT {plane.callsign}"], seconds=10)
        self.assertEqual(plane.status, "diverted")
        self.assertEqual(landing_metrics(diverted)["landing_wait_by_outcome"]["failed"]["seconds"], 4)

    def test_diagnostic_is_pure_and_score_decreases_with_elapsed_time(self):
        env, _ = isolated_plane()
        observation = env.step([], seconds=5)
        before = deepcopy(observation)
        landing_metrics(observation)
        self.assertEqual(observation, before)
        self.assertNotIn("landing_wait_score", env.metrics())
        self.assertEqual(landing_metrics(snapshot(("inbound", 0)))["landing_wait_score"], 100)
        self.assertEqual(landing_metrics(snapshot(("holding", 600)))["landing_wait_score"], 50)
        self.assertAlmostEqual(landing_metrics(snapshot(("approach", 1200)))["landing_wait_score"], 33.333)

    def test_bad_arrival_data_is_not_silently_scored_as_zero(self):
        for bad in (None, -1, float("nan"), float("inf"), True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                landing_metrics(snapshot(("inbound", bad)))
        with self.assertRaises(ValueError):
            landing_metrics(snapshot(("unknown", 0)))


if __name__ == "__main__":
    unittest.main()
