"""Public-interface regression tests for the benchmark and agent protocol."""

import json
import unittest

from atc_bench import AirTrafficEnv


SCENARIOS = ("mixed", "rush_hour", "low_visibility", "storm", "emergency", "wind_shift")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=600)

    def arrival(self):
        return next(a for a in self.env.observe()["aircraft"] if a["kind"] == "arrival")

    def test_same_seed_and_command_stream_reproduce_complete_observation(self):
        other = AirTrafficEnv(seed=7, scenario="mixed", duration_s=600)
        self.assertEqual(self.env.observe(), other.observe())
        callsign = self.arrival()["callsign"]
        for commands in ([f"HEADING {callsign} 230"], [], [f"ALTITUDE {callsign} 7000"], []):
            self.assertEqual(self.env.step(commands, seconds=30), other.step(commands, seconds=30))

    def test_reset_reproduces_initial_state_and_clears_metrics(self):
        initial = self.env.observe()
        self.env.step(["UNKNOWN INVALID"], seconds=60)
        self.env.reset()
        self.assertEqual(initial, self.env.observe())

    def test_observation_is_json_serializable_and_detached(self):
        observation = self.env.observe()
        json.dumps(observation, allow_nan=False)
        observation["aircraft"][0]["x_nm"] = 99999
        observation["airport"]["runways"][0]["length_m"] = -1
        current = self.env.observe()
        self.assertNotEqual(current["aircraft"][0]["x_nm"], 99999)
        self.assertGreater(current["airport"]["runways"][0]["length_m"], 0)

    def test_string_and_dictionary_commands_are_equivalent(self):
        other = AirTrafficEnv(seed=7, scenario="mixed", duration_s=600)
        callsign = self.arrival()["callsign"]
        self.env.step([f"HEADING {callsign} 123"], seconds=10)
        other.step([{"action": "heading", "callsign": callsign, "heading": 123}], seconds=10)
        self.assertEqual(self.env.observe()["aircraft"], other.observe()["aircraft"])
        self.assertEqual(self.env.metrics(), other.metrics())

    def test_malformed_command_does_not_cancel_other_commands(self):
        callsign = self.arrival()["callsign"]
        malformed = ["", 12, None, {}, {"action": "heading", "callsign": callsign}, f"HEADING {callsign} NaN"]
        commands = malformed + [f"HEADING {callsign} 123"]
        observation = self.env.step(commands, seconds=5)
        self.assertEqual(observation["time_s"], 5)
        self.assertEqual(len(observation["command_results"]), len(commands))
        self.assertTrue(all(not r["accepted"] for r in observation["command_results"][:-1]))
        self.assertTrue(observation["command_results"][-1]["accepted"])
        current = next(a for a in observation["aircraft"] if a["callsign"] == callsign)
        self.assertEqual(current["target_heading_deg"], 123)
        self.assertEqual(observation["metrics"]["invalid_commands"], len(malformed))

    def test_invalid_step_durations_fail_without_mutating_state(self):
        initial = self.env.observe()
        for seconds in (-1, 61, 1.5, True, "10", None):
            with self.subTest(seconds=seconds):
                with self.assertRaises((TypeError, ValueError)):
                    self.env.step([], seconds=seconds)
                self.assertEqual(self.env.observe(), initial)

    def test_nonfinite_and_oversized_numbers_cannot_poison_a_batch(self):
        callsign = self.arrival()["callsign"]
        malformed = [{"action": "heading", "callsign": callsign, "heading": value}
                     for value in (float("nan"), float("inf"), 10 ** 400)]
        result = self.env.step(malformed + [f"HEADING {callsign} 123"], seconds=0)
        self.assertEqual([r["accepted"] for r in result["command_results"]], [False, False, False, True])
        json.dumps(result, allow_nan=False)

    def test_zero_second_step_applies_commands_without_motion(self):
        callsign = self.arrival()["callsign"]
        initial = self.arrival()
        observation = self.env.step([f"HEADING {callsign} 123"], seconds=0)
        current = next(a for a in observation["aircraft"] if a["callsign"] == callsign)
        self.assertEqual(observation["time_s"], 0)
        self.assertEqual(current["x_nm"], initial["x_nm"])
        self.assertEqual(current["y_nm"], initial["y_nm"])
        self.assertEqual(current["target_heading_deg"], 123)

    def test_batch_limit_rejects_before_applying_commands(self):
        initial = self.env.observe()
        with self.assertRaises((ValueError, TypeError)):
            self.env.step(["HOLD NO_SUCH_FLIGHT"] * 101, seconds=10)
        self.assertEqual(self.env.observe(), initial)

    def test_unknown_callsign_and_runway_return_reasons(self):
        callsign = self.arrival()["callsign"]
        observation = self.env.step(["HOLD NO_SUCH_FLIGHT", f"APPROACH {callsign} 99X"], seconds=0)
        for result in observation["command_results"]:
            self.assertFalse(result["accepted"])
            self.assertTrue(result["message"])

    def test_aircraft_speed_limits_are_enforced(self):
        aircraft = self.arrival()
        observation = self.env.step([f"SPEED {aircraft['callsign']} {aircraft['max_speed_kt'] + 100}"], seconds=0)
        self.assertFalse(observation["command_results"][0]["accepted"])

    def test_horizon_clamps_time_and_unfinished_waiting_still_counts(self):
        env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=25)
        initial = env.observe()
        departure_count = sum(a["kind"] == "departure" for a in initial["aircraft"])
        self.assertGreater(departure_count, 0)
        final = env.step([], seconds=60)
        self.assertTrue(env.done)
        self.assertEqual(final["time_s"], 25)
        self.assertGreaterEqual(final["metrics"]["ground_delay_seconds"], 25 * departure_count)
        self.assertGreater(final["metrics"]["airborne_seconds"], 0)
        self.assertGreater(final["metrics"]["unfinished"], 0)
        self.assertEqual(final["metrics"]["departed"], 0)

    def test_all_scenarios_are_deterministic_and_resettable(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                env = AirTrafficEnv(seed=17, scenario=scenario, duration_s=120)
                other = AirTrafficEnv(seed=17, scenario=scenario, duration_s=120)
                self.assertEqual(env.step([], seconds=60), other.step([], seconds=60))
                self.env.reset(seed=17, scenario=scenario)
                self.assertEqual(self.env.observe()["scenario"], scenario)
                self.assertEqual(self.env.observe()["seed"], 17)
                self.assertEqual(self.env.observe()["time_s"], 0)

    def test_four_physical_runways_and_directional_restrictions(self):
        runways = {r["id"]: r for r in self.env.observe()["airport"]["runways"]}
        self.assertEqual(set(runways), {"25R", "07L", "25C", "07C", "25L", "07R", "18"})
        self.assertEqual(len({r["physical_id"] for r in runways.values()}), 4)
        self.assertEqual(runways["25R"]["physical_id"], runways["07L"]["physical_id"])
        self.assertEqual(runways["25R"]["length_m"], 2800)
        self.assertTrue(runways["25R"]["arrival"])
        self.assertFalse(runways["25R"]["departure"])
        self.assertTrue(runways["18"]["departure"])
        self.assertFalse(runways["18"]["arrival"])
        for direction in ("25C", "07C", "25L", "07R", "18"):
            self.assertEqual(runways[direction]["length_m"], 4000)


if __name__ == "__main__":
    unittest.main()
