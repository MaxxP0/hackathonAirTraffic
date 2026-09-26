"""Replay verification uses the real environment and needs no media packages."""

from copy import deepcopy
import json
import unittest

from atc_bench.cli import run_episode
from examples.render_replay import reconstruct


class ReplayVerificationTests(unittest.TestCase):
    def setUp(self):
        self.run = json.loads(json.dumps(run_episode(seed=7, scenario="runway_closure", duration=250,
                                                     agent_spec="reference", step_seconds=10)))

    def test_selected_frames_preserve_full_episode_outcome_and_feedback(self):
        samples, metrics = reconstruct(self.run, [0, 50, 120, 249])
        self.assertEqual([frame["time_s"] for frame in samples], [0, 50, 120, 249])
        self.assertEqual(metrics, self.run["metrics"])
        self.assertEqual(samples[0]["command_results"], self.run["replay"][0]["command_results"])
        self.assertEqual(samples[1]["command_results"], self.run["replay"][5]["command_results"])

    def test_changed_recorded_metrics_are_rejected(self):
        changed = deepcopy(self.run)
        changed["metrics"]["score"] += 1
        with self.assertRaisesRegex(ValueError, "metrics do not match"):
            reconstruct(changed, [0])

    def test_missing_command_or_modified_final_position_is_rejected(self):
        changed = deepcopy(self.run)
        changed["replay"][0]["commands"] = []
        with self.assertRaisesRegex(ValueError, "Command results differ"):
            reconstruct(changed, [0])
        changed = deepcopy(self.run)
        changed["final_observation"]["aircraft"][0]["x_nm"] += 1
        with self.assertRaisesRegex(ValueError, "aircraft differs"):
            reconstruct(changed, [0])


if __name__ == "__main__":
    unittest.main()
