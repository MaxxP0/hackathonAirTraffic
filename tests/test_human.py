"""Human turns use the model contract without touching the hosted controller."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

from atc_bench.human import HumanSessions, HumanError
from atc_bench.lmstudio import compact_observation, SYSTEM_PROMPT
from tests import test_controller_server as controller_tests


def decision(state, **overrides):
    return {"id": state["id"], "expected_time_s": state["observation"]["time_s"],
            "commands": [], "plan": "Maintain separation and reconsider after the next window.",
            "summary": "Observe existing traffic.", **overrides}


class HumanSessionTests(unittest.TestCase):
    def setUp(self):
        self.store = HumanSessions()
        self.initial = self.store.start({"scenario": "runway_closure", "seed": 7})

    def test_exact_model_input_and_no_future_closure_schedule(self):
        obs = deepcopy(self.initial["observation"])
        obs["decision_interval_s"] = 120
        expected = compact_observation(obs)
        expected["controller_memory"] = {"latest_plan": "", "successful_decisions": 0, "prior_decision_error": None}
        self.assertEqual(self.initial["model_observation"], expected)
        self.assertEqual(self.initial["system_prompt"], SYSTEM_PROMPT)
        self.assertNotIn("closure_schedule", json.dumps(expected))
        self.assertTrue(all(not r["closed"] for r in expected["runway_state"].values()))

    def test_plan_and_four_turn_memory_persist_through_fixed_windows(self):
        state = self.initial
        for i in range(5):
            state = self.store.step(decision(state, plan=f"Plan for turn {i}"))
        self.assertEqual(state["observation"]["time_s"], 600)
        self.assertEqual(state["decision_count"], 5)
        self.assertEqual(state["latest_plan"], "Plan for turn 4")
        self.assertEqual(len(state["conversation"]), 8)
        self.assertEqual(json.loads(state["conversation"][-2]["content"])["controller_memory"]["latest_plan"], "Plan for turn 3")
        self.assertEqual(len(state["replay"]), 5)

    def test_invalid_batches_do_not_advance_or_erase_plan(self):
        for overrides in ({"plan": ""}, {"commands": ["HOLD DLH100"]*2},
                          {"commands": ["X"*81]}, {"commands": [" "]},
                          {"summary": "x"*241}, {"expected_time_s": True}):
            with self.subTest(overrides=overrides), self.assertRaises(HumanError):
                self.store.step(decision(self.initial, **overrides))
        self.assertEqual(self.store.state(self.initial["id"]), self.initial)

    def test_command_rejection_advances_and_appears_in_next_model_input(self):
        state = self.store.step(decision(self.initial, commands=["TAKEOFF UNKNOWN 18"]))
        self.assertEqual(state["observation"]["time_s"], 120)
        self.assertFalse(state["replay"][-1]["command_results"][0]["accepted"])
        self.assertEqual(len(state["model_observation"]["prior_command_errors"]), 1)

    def test_stale_simultaneous_submissions_apply_at_most_once(self):
        def submit():
            try:
                self.store.step(decision(self.initial))
                return 200
            except HumanError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(self.store.state(self.initial["id"])["decision_count"], 1)

    def test_separate_sessions_and_complete_episode(self):
        other = self.store.start({"scenario": "emergency", "seed": 7})
        state = self.initial
        for _ in range(15):
            state = self.store.step(decision(state))
        self.assertTrue(state["done"])
        self.assertEqual(state["observation"]["time_s"], 1800)
        self.assertEqual(self.store.state(other["id"]), other)
        with self.assertRaises(HumanError) as caught:
            self.store.step(decision(state))
        self.assertEqual(caught.exception.status, 409)


class HumanHttpTests(unittest.TestCase):
    setUp = controller_tests.ControllerServerTests.setUp
    tearDown = controller_tests.ControllerServerTests.tearDown
    request = controller_tests.ControllerServerTests.request

    def test_human_http_round_trip_never_calls_or_advances_main_agent(self):
        original = self.server.env.observe()
        with patch.object(self.server.agent, "act", side_effect=AssertionError("Must not call an agent")):
            code, state = self.request("/api/human/start", {"scenario": "emergency", "seed": 7})
            self.assertEqual(code, 200)
            code, advanced = self.request("/api/human/step", decision(state))
            self.assertEqual(code, 200)
            self.assertEqual(advanced["observation"]["time_s"], 120)
            code, loaded = self.request("/api/human/state?id=" + state["id"])
            self.assertEqual(loaded, advanced)
            code, _ = self.request("/api/human/step", decision(state))
            self.assertEqual(code, 409)
            self.assertEqual(self.server.env.observe(), original)
            code, _ = self.request("/api/human/state?id=missing")
            self.assertEqual(code, 404)
