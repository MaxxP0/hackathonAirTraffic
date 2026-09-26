"""Real subprocess and localhost HTTP checks of the supported agent adapters."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from atc_bench.cli import rank_key
from atc_bench.server import SimulationServer


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def command(self, *args, input=None):
        return subprocess.run([sys.executable, "-m", "atc_bench", *args], cwd=ROOT,
                              input=input, capture_output=True, text=True, timeout=30, check=True)

    def test_run_writes_reproducible_result_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "episode.json"
            first = self.command("run", "--agent", "reference", "--duration", "30", "--output", str(output))
            self.assertEqual(first.stdout, "")
            result = json.loads(output.read_text())
            again = json.loads(self.command("run", "--agent", "reference", "--duration", "30").stdout)
        self.assertEqual(result, again)
        self.assertEqual(result["configuration"]["agent"], "reference")
        self.assertEqual(result["final_observation"]["time_s"], 30)
        self.assertTrue(result["final_observation"]["done"])
        self.assertEqual(result["rank_key"], rank_key(result["metrics"]))
        self.assertEqual(result["rank_key"], result["metrics"]["rank_key"])
        self.assertEqual(sum(step["seconds"] for step in result["replay"]), 30)
        self.assertTrue(result["benchmark_version"])

    def test_benchmark_covers_scenario_seed_product(self):
        result = json.loads(self.command("benchmark", "--seeds", "1", "2", "--scenarios", "mixed",
                                         "emergency", "--duration", "20", "--agent", "noop").stdout)
        self.assertEqual(result["run_count"], 4)
        configurations = {(run["configuration"]["scenario"], run["configuration"]["seed"])
                          for run in result["runs"]}
        self.assertEqual(configurations, {("mixed", 1), ("mixed", 2), ("emergency", 1), ("emergency", 2)})
        expected = sum(run["metrics"]["ground_delay_seconds"] for run in result["runs"]) / 4
        self.assertEqual(result["mean_metrics"]["ground_delay_seconds"], expected)

    def test_example_custom_agent_is_loadable(self):
        result = json.loads(self.command("run", "--agent", "examples.custom_agent:MyAgent", "--duration", "10").stdout)
        self.assertEqual(result["configuration"]["agent"], "examples.custom_agent:MyAgent")
        self.assertEqual(result["final_observation"]["time_s"], 10)

    def test_stdio_protocol_recovers_after_bad_request_and_resets(self):
        requests = ["{bad json}", json.dumps({"commands": [], "seconds": 10}),
                    json.dumps({"seconds": True}),
                    json.dumps({"reset": {"seed": 8, "scenario": "storm", "duration_s": 60}}),
                    json.dumps({"commands": [], "seconds": 5})]
        response = self.command("stdio", "--duration", "30", input="\n".join(requests) + "\n")
        lines = [json.loads(line) for line in response.stdout.splitlines()]
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0]["time_s"], 0)
        self.assertIn("error", lines[1])
        self.assertEqual(lines[2]["time_s"], 10)
        self.assertIn("error", lines[3])
        self.assertEqual((lines[4]["time_s"], lines[4]["seed"], lines[4]["scenario"]), (0, 8, "storm"))
        self.assertEqual(lines[5]["time_s"], 5)

    def test_safety_rank_precedes_completed_traffic(self):
        safe = {"collisions": 0, "landed": 0, "departed": 0, "ground_delay_seconds": 100000}
        unsafe = {"collisions": 1, "landed": 10000, "departed": 10000, "ground_delay_seconds": 0}
        self.assertLess(rank_key(safe), rank_key(unsafe))
        emergency_failure = {"collisions": 0, "emergencies_failed": 1, "landed": 10000}
        self.assertLess(rank_key(safe), rank_key(emergency_failure))

    def test_fuel_exhaustion_crash_ranks_below_safe_unfinished_flight(self):
        from atc_bench import AirTrafficEnv

        safe = AirTrafficEnv(seed=7, scenario="mixed", duration_s=30)
        crashed = AirTrafficEnv(seed=7, scenario="mixed", duration_s=30)
        next(a for a in crashed.aircraft.values() if a.kind == "arrival").fuel_s = 0.5
        safe.step([], seconds=5)
        crashed.step([], seconds=5)
        self.assertEqual(crashed.metrics()["crashed"], 1)
        self.assertEqual(crashed.metrics()["collisions"], 0)
        self.assertLess(rank_key(safe.metrics()), rank_key(crashed.metrics()))
        self.assertGreater(safe.metrics()["score"], crashed.metrics()["score"])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SimulationServer(("127.0.0.1", 0), seed=7, scenario="mixed", duration=120)
        cls.worker = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.worker.join(timeout=5)
        cls.server.server_close()

    def request(self, path, payload=None, raw=None, content_type="application/json"):
        body = json.dumps(payload).encode() if payload is not None else raw
        request = Request(self.base + path, data=body, headers={"Content-Type": content_type} if body is not None else {})
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, response.headers, response.read()
            except HTTPError as error:
                return error.code, error.headers, error.read()

    def setUp(self):
        status, _, _ = self.request("/api/reset", {"seed": 7, "scenario": "mixed", "duration_s": 120})
        self.assertEqual(status, 200)

    def test_state_step_and_reset_are_json_and_change_simulation(self):
        status, headers, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        initial = json.loads(body)
        status, _, body = self.request("/api/step", {"commands": [], "seconds": 10, "autopilot": False})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["time_s"], 10)
        status, _, body = self.request("/api/reset", {"seed": 7, "scenario": "mixed", "duration_s": 120})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), initial)

    def test_autopilot_submits_commands_and_preserves_manual_override(self):
        initial = json.loads(self.request("/api/state")[2])
        callsign = next(a["callsign"] for a in initial["aircraft"] if a["kind"] == "arrival")
        status, _, body = self.request("/api/step", {"commands": [f"HEADING {callsign} 123"], "seconds": 0, "autopilot": True})
        self.assertEqual(status, 200)
        observation = json.loads(body)
        aircraft = next(a for a in observation["aircraft"] if a["callsign"] == callsign)
        self.assertEqual(aircraft["target_heading_deg"], 123)
        self.assertGreater(len(observation["command_results"]), 1)

    def test_bad_requests_do_not_advance_state(self):
        for payload in ({"seconds": True}, {"seconds": -1}, {"commands": "HOLD DLH100"}):
            with self.subTest(payload=payload):
                status, _, body = self.request("/api/step", payload)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        status, _, body = self.request("/api/step", raw=b'{"seconds":NaN}')
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        self.assertEqual(json.loads(self.request("/api/state")[2])["time_s"], 0)

    def test_scenario_inventory_and_unknown_endpoint(self):
        status, _, body = self.request("/api/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["scenarios"]), 6)
        status, _, body = self.request("/api/no-such-endpoint")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_static_radar_is_served_and_path_traversal_is_rejected(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<html", body.lower())
        status, _, _ = self.request("/%2e%2e/environment.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
