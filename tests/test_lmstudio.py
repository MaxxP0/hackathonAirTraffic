"""LM Studio protocol checks against a local mock, with no real inference."""

import contextlib
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
from unittest.mock import patch

from atc_bench import AirTrafficEnv
from atc_bench.cli import main, run_episode
from atc_bench.lmstudio import AIRCRAFT_TYPE_FIELDS, LMStudioAgent, LMStudioError, compact_observation


def completion(commands=None, summary="Maintain existing clearances.", **changes):
    response = {"choices": [{"message": {"content": json.dumps({
        "commands": commands if commands is not None else [], "summary": summary,
    })}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 300, "completion_tokens": 20, "total_tokens": 320}}
    response.update(changes)
    return response


class MockModelServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.requests = []
        self.responses = []
        self.inventory = {"models": [
            {"key": "unloaded", "type": "llm", "loaded_instances": []},
            {"key": "model-key", "type": "llm", "loaded_instances": [{"id": "loaded-model"}]},
        ]}
        self.legacy_inventory = None


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.requests.append((self.path, None))
        if self.path == "/api/v1/models" and self.server.inventory is not None:
            self.send_json(200, self.server.inventory)
        elif self.path == "/api/v0/models" and self.server.legacy_inventory is not None:
            self.send_json(200, self.server.legacy_inventory)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload))
        response = self.server.responses.pop(0) if self.server.responses else completion()
        if isinstance(response, tuple):
            self.send_json(*response)
        else:
            self.send_json(200, response)


class LMStudioTests(unittest.TestCase):
    def setUp(self):
        self.server = MockModelServer()
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.worker.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.env = AirTrafficEnv(seed=7, scenario="emergency", duration_s=120)
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr.__enter__()

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(timeout=5)
        self.server.server_close()
        self.stderr.__exit__(None, None, None)

    def agent(self, **kwargs):
        return LMStudioAgent(base_url=self.base_url, timeout_s=2, **kwargs)

    def test_loaded_model_is_discovered_and_structured_output_requested(self):
        self.server.responses = [completion(["APPROACH DLH100 25R"], "Prioritize the arriving aircraft.")]
        agent = self.agent()
        self.assertEqual(agent.act(self.env.observe()), ["APPROACH DLH100 25R"])
        self.assertEqual(agent.model, "loaded-model")
        self.assertEqual([path for path, _ in self.server.requests], ["/api/v1/models", "/v1/chat/completions"])
        payload = self.server.requests[-1][1]
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(agent.last_decision["status"], "ok")
        self.assertEqual(agent.metadata()["usage"]["total_tokens"], 320)
        self.assertIn("system_prompt", agent.describe())

    def test_unloaded_model_is_never_requested(self):
        agent = self.agent(model="unloaded")
        with self.assertRaisesRegex(LMStudioError, "already loaded"):
            agent.act(self.env.observe())
        self.assertFalse(any(path == "/v1/chat/completions" for path, _ in self.server.requests))
        self.assertEqual(agent.last_decision["status"], "error")
        self.assertEqual(agent.metadata()["errors"], 1)

    def test_model_key_resolves_to_loaded_instance(self):
        agent = self.agent(model="model-key")
        agent.act(self.env.observe())
        self.assertEqual(agent.model, "loaded-model")

    def test_reasoning_is_disabled_per_request_only_when_model_supports_off(self):
        self.server.inventory["models"][1]["capabilities"] = {
            "reasoning": {"allowed_options": ["off", "on"], "default": "on"}}
        agent = self.agent()
        agent.act(self.env.observe())
        self.assertEqual(self.server.requests[-1][1]["reasoning_effort"], "none")
        self.assertEqual(agent.describe()["reasoning_effort"], "none")
        self.assertEqual(agent.last_decision["reasoning_effort"], "none")
        self.assertFalse(any("/load" in path or "/unload" in path for path, _ in self.server.requests))

    def test_legacy_discovery_accepts_only_loaded_language_models(self):
        self.server.inventory = None
        self.server.legacy_inventory = {"data": [
            {"id": "embedding", "type": "embeddings", "state": "loaded"},
            {"id": "unloaded", "type": "llm", "state": "not-loaded"},
            {"id": "legacy-model", "type": "llm", "state": "loaded"},
        ]}
        agent = self.agent()
        agent.act(self.env.observe())
        self.assertEqual(agent.model, "legacy-model")

    def test_compaction_removes_trails_and_finished_planes_without_mutating_input(self):
        observation = self.env.observe()
        finished = deepcopy(observation["aircraft"][0])
        finished["status"] = "landed"
        finished["callsign"] = "FINISHED"
        observation["aircraft"].append(finished)
        observation["command_results"] = [{"accepted": True, "message": "okay"},
                                           {"accepted": False, "message": "already on approach"}]
        before = deepcopy(observation)
        compact = compact_observation(observation)
        self.assertEqual(observation, before)
        self.assertNotIn("events", compact)
        self.assertNotIn("history", compact["aircraft_columns"])
        aircraft = [dict(zip(compact["aircraft_columns"], row)) for row in compact["aircraft"]]
        self.assertFalse(any(plane["callsign"] == "FINISHED" for plane in aircraft))
        self.assertEqual(compact["prior_command_errors"], observation["command_results"][1:])
        for field in ("emergency", "target_altitude_ft"):
            self.assertIn(field, aircraft[0])
        for field in ("min_speed_kt", "crosswind_limit_kt"):
            self.assertIn(field, compact["aircraft_types"][aircraft[0]["type"]])
        self.assertEqual(compact["weather"], observation["weather"])
        reconstructed_runways = [{**runway, **compact["runway_state"][runway["id"]]}
                                 for runway in compact["airport"]["runways"]]
        self.assertEqual(reconstructed_runways, observation["airport"]["runways"])

    def test_dense_rows_preserve_all_active_dynamic_values_and_type_limits_exactly(self):
        observation = self.env.step([], seconds=60)
        compact = compact_observation(observation)
        reconstructed = []
        for row in compact["aircraft"]:
            self.assertEqual(len(row), len(compact["aircraft_columns"]))
            aircraft = dict(zip(compact["aircraft_columns"], row))
            self.assertFalse(set(aircraft) & set(AIRCRAFT_TYPE_FIELDS))
            reconstructed.append({**aircraft, **compact["aircraft_types"][aircraft["type"]]})
        expected = [{key: value for key, value in plane.items() if key != "history"}
                    for plane in observation["aircraft"]]
        self.assertEqual(reconstructed, expected)
        self.assertEqual(list(compact)[:3], ["airport", "aircraft_types", "aircraft_columns"])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(observation)) * .8)
        self.assertEqual(compact["metrics"]["emergency_wait_seconds"], observation["metrics"]["emergency_wait_seconds"])

    def test_reasoning_token_usage_is_retained_without_the_reasoning_text(self):
        self.server.responses = [completion(usage={"prompt_tokens": 20, "completion_tokens": 10,
                                                   "completion_tokens_details": {"reasoning_tokens": 3}})]
        agent = self.agent()
        agent.act(self.env.observe())
        self.assertEqual(agent.last_decision["usage"]["reasoning_tokens"], 3)
        self.assertEqual(agent.metadata()["usage"]["reasoning_tokens"], 3)
        self.assertEqual(agent.describe()["observation_format"], "compact-v2")

    def test_every_step_receives_fresh_observation_and_previous_rejections(self):
        self.server.responses = [completion(["HOLD MISSING"]), completion([])]
        agent = self.agent()
        observation = self.env.observe()
        commands = agent.act(observation)
        observation = self.env.step(commands, seconds=10)
        agent.act(observation)
        posts = [payload for path, payload in self.server.requests if path == "/v1/chat/completions"]
        self.assertEqual(len(posts), 2)
        first = json.loads(posts[0]["messages"][1]["content"])
        second = json.loads(posts[1]["messages"][1]["content"])
        self.assertEqual(first["time_s"], 0)
        self.assertEqual(second["time_s"], 10)
        self.assertEqual(second["prior_command_errors"][0]["command"], "HOLD MISSING")
        self.assertEqual(len(posts[1]["messages"]), 2)
        self.assertEqual(agent.metadata()["calls"], 2)
        self.assertEqual(agent.metadata()["usage"]["completion_tokens"], 40)

    def test_bad_json_and_schema_are_visible_failures_without_commands(self):
        contents = ["not json", '{"commands":[3],"summary":"bad"}',
                    '{"commands":[],"summary":1}', '{"commands":[]}',
                    '{"commands":[],"summary":"ok","extra":true}']
        agent = self.agent()
        for content in contents:
            with self.subTest(content=content):
                self.server.responses = [completion(choices=[{"message": {"content": content}, "finish_reason": "stop"}])]
                with self.assertRaises(LMStudioError):
                    agent.act(self.env.observe())
                self.assertEqual(agent.last_decision["status"], "error")
                self.assertEqual(agent.last_decision["commands"], [])

    def test_truncated_output_and_server_failure_are_not_silent_noops(self):
        self.server.responses = [completion(choices=[{"message": {"content": '{}'}, "finish_reason": "length"}]),
                                 (500, {"error": "model failed"})]
        agent = self.agent()
        for expected in ("length", "HTTP 500"):
            with self.assertRaisesRegex(LMStudioError, expected):
                agent.act(self.env.observe())
        self.assertEqual(agent.metadata()["calls"], 2)
        self.assertEqual(agent.metadata()["errors"], 2)

    def test_episode_records_model_decisions_and_partial_failure(self):
        self.server.responses = [completion([]), (500, {"error": "model failed"})]
        result = run_episode(seed=7, scenario="mixed", duration=30, agent_spec="lmstudio",
                             agent_options={"base_url": self.base_url}, step_seconds=10)
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["final_observation"]["time_s"], 10)
        self.assertFalse(result["final_observation"]["done"])
        self.assertEqual(result["controller"]["calls"], 2)
        self.assertEqual([step["seconds"] for step in result["replay"]], [10, 0])
        self.assertEqual(result["replay"][-1]["decision"]["status"], "error")
        self.assertEqual(result["replay"][0]["decision"]["observation"]["time_s"], 0)

    def test_cli_emits_partial_json_and_nonzero_status_on_failure(self):
        self.server.responses = [(503, {"error": "unavailable"})]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(["run", "--agent", "lmstudio", "--base-url", self.base_url, "--duration", "10"])
        self.assertEqual(status, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["final_observation"]["time_s"], 0)

    def test_benchmark_averages_only_completed_runs_and_observed_numeric_values(self):
        results = [
            {"status": "completed", "metrics": {"score": 10, "emergency_wait": None}},
            {"status": "completed", "metrics": {"score": 20, "emergency_wait": 50}},
            {"status": "aborted", "error": "model failed", "metrics": {"score": -999, "emergency_wait": 500}},
        ]
        stdout = io.StringIO()
        with patch("atc_bench.cli.run_episode", side_effect=results), contextlib.redirect_stdout(stdout):
            status = main(["benchmark", "--agent", "lmstudio", "--seeds", "1", "2", "3", "--scenarios", "mixed"])
        self.assertEqual(status, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["mean_metrics"], {"score": 15, "emergency_wait": 50})
        self.assertEqual(result["metric_sample_counts"], {"score": 2, "emergency_wait": 1})
        self.assertEqual(result["completed_run_count"], 2)
        self.assertEqual(result["aborted_run_count"], 1)

    def test_benchmark_with_no_completed_episodes_has_no_aggregate_score(self):
        self.server.responses = [(503, {"error": "unavailable"})]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(["benchmark", "--agent", "lmstudio", "--base-url", self.base_url,
                           "--duration", "10", "--seeds", "7", "--scenarios", "mixed"])
        self.assertEqual(status, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["completed_run_count"], 0)
        self.assertEqual(result["mean_metrics"], {})
        self.assertIsNone(result["mean_rank_key"])

    def test_done_observation_does_not_call_model(self):
        agent = self.agent()
        self.assertEqual(agent.act({"done": True, "time_s": 120}), [])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(agent.last_decision["status"], "done")


if __name__ == "__main__":
    unittest.main()
