"""Slow/failed model calls must be observable without damaging an episode."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from atc_bench.server import OPENROUTER_DEFAULT_MODEL, SimulationServer


class ControllerServerTests(unittest.TestCase):
    def setUp(self):
        self.server = SimulationServer(("127.0.0.1", 0), duration=120)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(3)
        self.server.server_close()

    def request(self, path, body=None):
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_state_remains_readable_and_mutations_rejected_during_inference(self):
        entered, release = threading.Event(), threading.Event()

        class SlowAgent:
            model = "test-model"
            last_decision = None

            def act(self, observation):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test did not release inference")
                self.last_decision = {"summary": "Maintain clearances", "commands": [], "latency_s": .1}
                return []

        self.server.agent = SlowAgent()
        self.server.controller["kind"] = "lmstudio"
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(self.request, "/api/step", {"seconds": 30, "autopilot": True})
            try:
                self.assertTrue(entered.wait(1))
                code, state = self.request("/api/state")
                self.assertEqual(code, 200)
                self.assertEqual(state["time_s"], 0)
                self.assertEqual(state["controller"]["status"], "thinking")
                code, _ = self.request("/api/reset", {"seed": 8})
                self.assertEqual(code, 409)
            finally:
                release.set()
            code, state = request.result(timeout=2)
        self.assertEqual(code, 200)
        self.assertEqual(state["time_s"], 30)
        self.assertEqual(state["controller"]["decision_count"], 1)
        self.assertEqual(state["controller"]["last_decision"]["summary"], "Maintain clearances")

    def test_model_failure_pauses_time_and_reports_error_without_fallback(self):
        class FailedAgent:
            model = "offline-model"
            last_decision = {"error": "connection refused", "status": "error"}

            def act(self, observation):
                raise RuntimeError("connection refused")

        self.server.agent = FailedAgent()
        self.server.controller["kind"] = "lmstudio"
        initial = self.server.env.observe()
        code, body = self.request("/api/step", {"seconds": 30, "autopilot": True})
        self.assertEqual(code, 502)
        self.assertIn("connection refused", body["error"])
        self.assertEqual(body["controller"]["status"], "error")
        self.assertEqual(self.server.env.observe(), initial)
        self.assertEqual(body["controller"]["decision_count"], 0)

    def test_reset_retains_model_configuration(self):
        class FakeAgent:
            model = "local-model"
            def act(self, observation):
                return []

        with patch("atc_bench.server.load_agent", return_value=FakeAgent()) as loader:
            code, state = self.request("/api/controller", {"kind": "lmstudio", "model": "local-model"})
            self.assertEqual(code, 200)
            self.assertEqual(state["controller"]["kind"], "lmstudio")
            self.request("/api/step", {"autopilot": True, "seconds": 30})
            code, state = self.request("/api/reset", {"seed": 9})
            self.assertEqual(code, 200)
            self.assertEqual(state["time_s"], 0)
            self.assertEqual(state["controller"]["kind"], "lmstudio")
            self.assertEqual(state["controller"]["model"], "local-model")
            self.assertEqual(state["controller"]["decision_count"], 0)
            self.assertEqual(loader.call_args.kwargs["model"], "local-model")

    def test_large_step_calls_model_once_and_preserves_rejected_command_feedback(self):
        class RecordingAgent:
            model = "local-model"
            last_decision = None

            def __init__(self):
                self.observations = []

            def act(self, observation):
                self.observations.append(observation)
                return ["HEADING UNKNOWN 250"]

        self.request("/api/reset", {"duration_s": 700})
        agent = RecordingAgent()
        self.server.agent = agent
        self.server.controller["kind"] = "lmstudio"
        code, state = self.request("/api/step", {"seconds": 300, "autopilot": True})
        self.assertEqual(code, 200)
        self.assertEqual(state["time_s"], 300)
        self.assertEqual(len(agent.observations), 1)
        self.assertEqual(agent.observations[0]["decision_interval_s"], 300)
        self.assertEqual(state["metrics"]["invalid_commands"], 1)
        self.assertFalse(state["command_results"][0]["accepted"])
        self.assertEqual(self.request("/api/state")[1]["command_results"], state["command_results"])
        code, state = self.request("/api/step", {"seconds": 600, "autopilot": True})
        self.assertEqual(code, 200)
        self.assertEqual(state["time_s"], 700)
        self.assertTrue(state["done"])
        self.assertEqual(len(agent.observations), 2)
        self.assertEqual(agent.observations[1]["time_s"], 300)
        self.assertEqual(agent.observations[1]["decision_interval_s"], 600)
        self.assertFalse(agent.observations[1]["command_results"][0]["accepted"])
        self.assertEqual(state["metrics"]["invalid_commands"], 2)

    def test_interval_above_600_is_rejected_without_a_model_call(self):
        class UnexpectedAgent:
            def act(self, observation):
                raise AssertionError("invalid intervals must not call the model")

        self.server.agent = UnexpectedAgent()
        initial = self.server.env.observe()
        code, body = self.request("/api/step", {"seconds": 601, "autopilot": True})
        self.assertEqual(code, 400)
        self.assertIn("600", body["error"])
        self.assertEqual(self.server.env.observe(), initial)

    def test_server_uses_extended_reasoning_defaults(self):
        self.assertEqual(self.server.agent_options["timeout_s"], 900)
        self.assertEqual(self.server.agent_options["max_tokens"], 8192)

    def test_openrouter_configuration_filters_endpoint_and_exposes_only_budget(self):
        class HostedAgent:
            model = OPENROUTER_DEFAULT_MODEL

            def act(self, observation):
                return []

            def metadata(self):
                return {"provider": "openrouter", "api_key": "must-not-reach-browser",
                        "budget": {"limit_usd": 10, "spent_usd": .02, "reserved_usd": .01,
                                   "remaining_usd": 9.97, "private": "must-not-reach-browser"}}

        with patch("atc_bench.server.load_agent", return_value=HostedAgent()) as loader:
            code, state = self.request("/api/controller", {"kind": "openrouter"})
            self.assertEqual(code, 200)
            self.assertEqual(loader.call_args.args, ("openrouter",))
            self.assertEqual(set(loader.call_args.kwargs), {"model", "timeout_s", "max_tokens"})
            self.assertEqual(loader.call_args.kwargs["model"], "z-ai/glm-5.3-flash")
            self.assertEqual(state["controller"]["model"], OPENROUTER_DEFAULT_MODEL)
            self.assertNotIn("base_url", state["controller"])
            self.assertEqual(state["controller"]["budget"], {"limit_usd": 10, "spent_usd": .02,
                                                           "reserved_usd": .01, "remaining_usd": 9.97})
            self.assertNotIn("must-not-reach-browser", json.dumps(state))
            code, reset = self.request("/api/reset", {"seed": 9})
            self.assertEqual(code, 200)
            self.assertEqual(reset["controller"]["kind"], "openrouter")
            self.assertNotIn("base_url", loader.call_args.kwargs)
            self.assertEqual(reset["controller"]["budget"], state["controller"]["budget"])

    def test_hosted_controller_does_not_accept_browser_credentials_or_endpoint(self):
        for fields in ({"base_url": "https://example.invalid"}, {"api_key": "not-a-key"}):
            with self.subTest(fields=fields), patch("atc_bench.server.load_agent") as loader:
                code, body = self.request("/api/controller", {"kind": "openrouter", **fields})
                self.assertEqual(code, 400)
                self.assertIn("error", body)
                loader.assert_not_called()
        self.assertEqual(self.request("/api/state")[1]["controller"]["kind"], "reference")

    def test_unreadable_or_invalid_hosted_budget_does_not_break_state_polling(self):
        class HostedAgent:
            model = OPENROUTER_DEFAULT_MODEL

            def act(self, observation):
                raise AssertionError("state polling must not call a model")

            def metadata(self):
                raise ValueError("private-ledger-content-must-not-be-exposed")

        agent = HostedAgent()
        with patch("atc_bench.server.load_agent", return_value=agent):
            code, state = self.request("/api/controller", {"kind": "openrouter"})
        self.assertEqual(code, 200)
        self.assertIn("unavailable", state["controller"]["budget_error"])
        self.assertNotIn("private-ledger", json.dumps(state))
        for budget in ({"spent_usd": float("nan")}, {"remaining_usd": "secret-value"}, "invalid"):
            with self.subTest(budget=budget), patch.object(agent, "metadata", return_value={"budget": budget}):
                code, state = self.request("/api/state")
                self.assertEqual(code, 200)
                self.assertIn("budget_error", state["controller"])
                self.assertNotIn("secret-value", json.dumps(state))
                self.assertEqual(state["time_s"], 0)

    def test_openrouter_pauses_time_and_reuses_agent_for_accelerated_decisions(self):
        entered, release = threading.Event(), threading.Event()

        class HostedAgent:
            model = OPENROUTER_DEFAULT_MODEL
            last_decision = None

            def __init__(self):
                self.times = []

            def act(self, observation):
                self.times.append(observation["time_s"])
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test did not release inference")
                self.last_decision = {"commands": [], "summary": "Keep the current plan",
                                      "plan": "Sequence emergency traffic first", "memory_turns": len(self.times),
                                      "cost_usd": .001, "latency_s": .1}
                return []

        self.request("/api/reset", {"duration_s": 300})
        agent = HostedAgent()
        with patch("atc_bench.server.load_agent", return_value=agent):
            self.request("/api/controller", {"kind": "openrouter"})
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(self.request, "/api/step", {"seconds": 120, "autopilot": True})
            try:
                self.assertTrue(entered.wait(1))
                _, thinking = self.request("/api/state")
                self.assertEqual(thinking["time_s"], 0)
                self.assertEqual(thinking["controller"]["status"], "thinking")
                self.assertIn("OpenRouter", thinking["controller"]["message"])
            finally:
                release.set()
            code, state = request.result(timeout=2)
        self.assertEqual(code, 200)
        self.assertEqual(state["time_s"], 120)
        code, second = self.request("/api/step", {"seconds": 120, "autopilot": True})
        self.assertEqual(code, 200)
        self.assertEqual(second["time_s"], 240)
        self.assertEqual(agent.times, [0, 120])
        self.assertEqual(second["controller"]["last_decision"]["memory_turns"], 2)
        self.assertEqual(second["controller"]["last_decision"]["cost_usd"], .001)

    def test_real_openrouter_missing_key_and_corrupt_budget_return_json_without_network(self):
        from atc_bench.openrouter import OpenRouterAgent

        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "test-budget.json"
            agent = OpenRouterAgent(budget_path=ledger_path, timeout_s=180, max_tokens=8192)
            # Bypass only public catalog discovery. Actual key validation,
            # budget enforcement and HTTP error serialization remain exercised.
            agent._resolved_model = True
            agent._context_length = 196608
            with patch("atc_bench.server.load_agent", return_value=agent):
                self.assertEqual(self.request("/api/controller", {"kind": "openrouter"})[0], 200)
            with patch("atc_bench.openrouter.os.environ", {}), patch("atc_bench.openrouter.build_opener") as network:
                code, body = self.request("/api/step", {"seconds": 120, "autopilot": True})
                self.assertEqual(code, 502)
                self.assertIn("OPENROUTER_API_KEY", body["error"])
                self.assertEqual(body["controller"]["status"], "error")
                network.assert_not_called()
            self.assertEqual(self.server.env.time_s, 0)
            self.assertEqual(agent.ledger.snapshot()["reserved_usd"], 0)
            ledger_path.write_text("{invalid budget", encoding="utf-8")
            code, state = self.request("/api/state")
            self.assertEqual(code, 200)
            self.assertIn("budget_error", state["controller"])
            self.assertNotIn("{invalid budget", json.dumps(state))
            with patch("atc_bench.openrouter.os.environ", {"OPENROUTER_API_KEY": "test-only-dummy"}), patch("atc_bench.openrouter.build_opener") as network:
                code, body = self.request("/api/step", {"seconds": 120, "autopilot": True})
                self.assertEqual(code, 502)
                self.assertIn("budget", body["error"].lower())
                self.assertNotIn("test-only-dummy", json.dumps(body))
                self.assertIn("budget_error", body["controller"])
                network.assert_not_called()
            self.assertEqual(self.server.env.time_s, 0)


if __name__ == "__main__":
    unittest.main()
