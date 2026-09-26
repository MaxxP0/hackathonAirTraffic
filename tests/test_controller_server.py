"""Slow/failed model calls must be observable without damaging an episode."""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from atc_bench.server import SimulationServer


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


if __name__ == "__main__":
    unittest.main()
