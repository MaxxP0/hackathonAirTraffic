"""Hosted-controller protocol and spend guards, with no paid API requests."""

from contextlib import redirect_stderr
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from atc_bench import AirTrafficEnv
from atc_bench.lmstudio import LMStudioError
from atc_bench.openrouter import (
    BASE_URL, DEFAULT_BUDGET_PATH, DEFAULT_MODEL, NANODOLLARS, PRICE_CAPS,
    BudgetError, OpenRouterAgent, OpenRouterError, SpendLedger, _NoRedirect,
)


FAKE_KEY = "sk-or-test-only-never-a-real-credential"
MISSING = object()


def inventory():
    return {"data": [{"id": DEFAULT_MODEL, "name": "GLM test catalog entry",
                      "context_length": 1310720,
                      "pricing": {"prompt": "0.00000004", "completion": "0.0000005"},
                      "supported_parameters": ["response_format", "structured_outputs", "max_tokens", "reasoning_effort"]}]}


def completion(cost=.003, plan="Sequence the emergency arrival before releasing departures.", commands=None):
    usage = {"prompt_tokens": 1000, "completion_tokens": 80, "total_tokens": 1080,
             "completion_tokens_details": {"reasoning_tokens": 40}}
    if cost is not MISSING:
        usage["cost"] = cost
    return {"model": DEFAULT_MODEL, "provider": "Test Provider", "usage": usage,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "commands": [] if commands is None else commands,
                "summary": "Keep the current emergency sequence.", "plan": plan,
            })}}]}


class FakeTransport:
    def __init__(self):
        self.inventory = inventory()
        self.responses = []
        self.requests = []
        self.before_post = None

    def open(self, request, timeout):
        body = json.loads(request.data) if request.data is not None else None
        self.requests.append((request, body, timeout))
        if body is None:
            response = self.inventory
        else:
            if self.before_post:
                self.before_post()
            response = self.responses.pop(0) if self.responses else completion()
        if isinstance(response, BaseException):
            raise response
        return io.BytesIO(json.dumps(response).encode())


def reserve_in_process(path, queue):
    try:
        SpendLedger(path).reserve(3 * NANODOLLARS, DEFAULT_MODEL)
        queue.put(True)
    except BudgetError:
        queue.put(False)


class SpendLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "budget.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_persistent_reservations_and_confirmed_costs_share_one_cap(self):
        first = SpendLedger(self.path)
        reservation = first.reserve(6 * NANODOLLARS, DEFAULT_MODEL)
        second = SpendLedger(self.path)
        with self.assertRaisesRegex(BudgetError, "limit reached"):
            second.reserve(5 * NANODOLLARS, DEFAULT_MODEL)
        first.finish(reservation, .0123)
        second.reserve(9 * NANODOLLARS, DEFAULT_MODEL)
        state = SpendLedger(self.path).snapshot()
        self.assertEqual(state["spent_usd"], .0123)
        self.assertEqual(state["reserved_usd"], 9)
        self.assertAlmostEqual(state["remaining_usd"], .9877)
        self.assertEqual(state["request_count"], 2)

    def test_concurrent_processes_cannot_overspend(self):
        # Eight competing $3 reservations must admit exactly three under $10.
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [context.Process(target=reserve_in_process, args=(str(self.path), queue)) for _ in range(8)]
        for process in processes:
            process.start()
        outcomes = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)
        queue.close()
        queue.join_thread()
        self.assertEqual(sum(outcomes), 3)
        self.assertEqual(SpendLedger(self.path).snapshot()["reserved_usd"], 9)

    def test_budget_cannot_exceed_ten_or_raise_an_existing_lower_limit(self):
        for invalid in (10.01, 0, -1, float("nan"), True):
            with self.subTest(invalid=invalid), self.assertRaises(BudgetError):
                SpendLedger(self.path, invalid)
        SpendLedger(self.path, 4)
        self.assertEqual(SpendLedger(self.path, 10).snapshot()["limit_usd"], 4)
        self.assertTrue(DEFAULT_BUDGET_PATH.is_absolute())

    def test_invalid_costs_retain_reservations_and_overrun_blocks_forever(self):
        ledger = SpendLedger(self.path)
        for invalid in (None, -1, float("nan"), float("inf"), "bad", True):
            identity = ledger.reserve(NANODOLLARS, DEFAULT_MODEL)
            self.assertIsNone(ledger.finish(identity, invalid))
        self.assertEqual(ledger.snapshot()["reserved_usd"], 6)
        identity = ledger.reserve(NANODOLLARS, DEFAULT_MODEL)
        with self.assertRaisesRegex(BudgetError, "above its reserved bound"):
            ledger.finish(identity, 1.01)
        self.assertTrue(SpendLedger(self.path).snapshot()["blocked"])
        with self.assertRaises(BudgetError):
            ledger.reserve(1, DEFAULT_MODEL)

    def test_missing_or_corrupted_ledger_fails_closed(self):
        ledger = SpendLedger(self.path)
        original = self.path.read_text()
        self.path.unlink()
        with self.assertRaisesRegex(BudgetError, "missing after initialization"):
            SpendLedger(self.path)
        for malformed in ("not json", "{}", original.replace('"blocked": false', '"blocked": "false"')):
            self.path.write_text(malformed)
            with self.subTest(malformed=malformed), self.assertRaises(BudgetError):
                ledger.snapshot()

    def test_ledger_files_are_private_and_symlinks_rejected(self):
        ledger = SpendLedger(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(ledger.lock_path.stat().st_mode & 0o777, 0o600)
        linked = self.path.with_name("linked.json")
        linked.symlink_to(self.path)
        with self.assertRaisesRegex(BudgetError, "symbolic links"):
            SpendLedger(linked)


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "budget.json"
        self.transport = FakeTransport()
        self.transport_patch = patch("atc_bench.openrouter.build_opener", return_value=self.transport)
        self.transport_patch.start()
        self.environment_patch = patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_KEY})
        self.environment_patch.start()
        self.stderr_output = io.StringIO()
        self.stderr = redirect_stderr(self.stderr_output)
        self.stderr.__enter__()
        self.env = AirTrafficEnv(seed=7, scenario="emergency", duration_s=1800)

    def tearDown(self):
        self.stderr.__exit__(None, None, None)
        self.environment_patch.stop()
        self.transport_patch.stop()
        self.directory.cleanup()

    def agent(self, **kwargs):
        return OpenRouterAgent(budget_path=self.path, **kwargs)

    def posts(self):
        return [entry for entry in self.transport.requests if entry[1] is not None]

    def test_rate_limit_retries_pause_without_losing_memory_or_escaping_budget(self):
        agent = self.agent()
        self.transport.responses = [completion(plan="Reserve the center runway."),
                                   HTTPError(BASE_URL, 429, "rate limit", {"Retry-After": "35"}, io.BytesIO()),
                                   completion(plan="Keep the center runway reserved.")]
        agent.act(self.env.observe())
        before = self.env.observe()
        with patch("atc_bench.openrouter.time.sleep") as sleep:
            agent.act(before)
        sleep.assert_called_once_with(35)
        self.assertEqual(self.env.observe(), before)
        self.assertEqual(self.posts()[1][1], self.posts()[2][1])
        self.assertEqual(agent.last_decision["input_memory_turns"], 1)
        self.assertEqual(agent.last_decision["memory_turns"], 2)
        self.assertEqual(agent.last_decision["rate_limit_attempts"][0]["http_status"], 429)
        self.assertEqual(agent.metadata()["budget"]["reserved_usd"], 1.327104)
        self.assertEqual(agent.metadata()["budget"]["spent_usd"], .006)

    def test_retry_after_above_bound_stops_without_retrying_early(self):
        self.transport.responses = [HTTPError(BASE_URL, 429, "rate limit", {"Retry-After": "120"}, io.BytesIO())]
        agent = self.agent()
        with patch("atc_bench.openrouter.time.sleep") as sleep, self.assertRaises(OpenRouterError):
            agent.act(self.env.observe())
        sleep.assert_not_called()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(agent.metadata()["budget"]["reserved_usd"], 1.327104)

    def test_reasoning_model_without_temperature_omits_unsupported_sampling(self):
        agent = self.agent()
        agent.act(self.env.observe())
        self.assertNotIn("temperature", self.posts()[0][1])
        self.assertFalse(agent.metadata()["temperature_sent"])

    def test_keepalive_bytes_do_not_reset_total_response_deadline(self):
        class Keepalive:
            def read(self, amount):
                return b" "
        with patch("atc_bench.openrouter.time.monotonic", side_effect=[1, 2, 4]), self.assertRaisesRegex(OpenRouterError, "total wall-clock deadline"):
            OpenRouterAgent._read_body(Keepalive(), 3)

    def test_fixed_endpoint_full_context_reservation_and_actual_cost(self):
        agent = self.agent()
        agent.base_url = "https://untrusted.invalid"
        reservations = []
        self.transport.before_post = lambda: reservations.append(SpendLedger(self.path).snapshot()["reserved_usd"])
        self.transport.responses = [completion(commands=["APPROACH DLH100 25R"])]
        self.assertEqual(agent.act(self.env.observe()), ["APPROACH DLH100 25R"])
        request, payload, timeout = self.posts()[0]
        self.assertEqual(request.full_url, BASE_URL + "/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertIsNone(self.transport.requests[0][0].get_header("Authorization"))
        self.assertEqual(timeout, 180)
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["provider"], {"max_price": PRICE_CAPS, "require_parameters": True,
                                              "allow_fallbacks": False, "sort": "throughput"})
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["plugins"], [])
        self.assertNotIn("models", payload)
        self.assertNotIn("tools", payload)
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(reservations, [1.327104])
        metadata = agent.metadata()
        self.assertEqual(metadata["budget"]["spent_usd"], .003)
        self.assertEqual(metadata["budget"]["reserved_usd"], 0)
        self.assertEqual(metadata["usage"]["reasoning_tokens"], 40)
        self.assertEqual(metadata["reasoning_mode"], "low")
        self.assertNotIn("advertised_reasoning_default", metadata)
        self.assertEqual(agent.last_decision["cost_usd"], .003)
        self.assertEqual(agent.last_decision["accounting_status"], "settled")
        self.assertNotIn(FAKE_KEY, json.dumps(metadata) + json.dumps(agent.last_decision) + self.stderr_output.getvalue())

    def test_persistent_plan_history_and_fresh_state_use_same_contract(self):
        agent = self.agent(max_memory_turns=1)
        self.transport.responses = [completion(plan="First explicit plan."), completion(plan="Updated explicit plan."), completion()]
        agent.act(self.env.observe())
        self.env.step([], 60)
        observation = self.env.observe()
        observation["decision_interval_s"] = 120
        agent.act(observation)
        messages = self.posts()[1][1]["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "user"])
        state = json.loads(messages[-1]["content"])
        self.assertEqual(state["time_s"], 60)
        self.assertEqual(state["decision_interval_s"], 120)
        self.assertEqual(state["controller_memory"]["latest_plan"], "First explicit plan.")
        self.env.step([], 60)
        agent.act(self.env.observe())
        self.assertEqual(len(self.posts()[2][1]["messages"]), 4)
        self.assertEqual(agent.metadata()["memory_turns"], 1)

    def test_new_agent_and_new_episode_clear_memory_but_keep_spend(self):
        agent = self.agent()
        agent.act(self.env.observe())
        fresh = self.agent()
        self.assertEqual(fresh.metadata()["budget"]["spent_usd"], .003)
        self.assertEqual(fresh.metadata()["memory_turns"], 0)
        other = AirTrafficEnv(seed=8, scenario="emergency", duration_s=1800)
        agent.act(other.observe())
        self.assertEqual(len(self.posts()[-1][1]["messages"]), 2)
        self.assertEqual(agent.metadata()["budget"]["spent_usd"], .006)

    def test_known_cost_is_settled_even_when_output_is_rejected(self):
        agent = self.agent()
        first = completion(plan="Retain this verified plan.")
        truncated = completion(cost=.017)
        truncated["choices"][0]["finish_reason"] = "length"
        self.transport.responses = [first, truncated]
        agent.act(self.env.observe())
        self.env.step([], 60)
        with self.assertRaisesRegex(LMStudioError, "length"):
            agent.act(self.env.observe())
        self.assertEqual(agent.latest_plan, "Retain this verified plan.")
        self.assertEqual(agent.metadata()["memory_turns"], 1)
        self.assertEqual(agent.last_decision["accounting_status"], "settled")
        self.assertEqual(agent.last_decision["cost_usd"], .017)
        self.assertEqual(agent.metadata()["budget"]["spent_usd"], .02)
        self.assertEqual(agent.metadata()["budget"]["reserved_usd"], 0)

    def test_unknown_cost_and_transport_failure_hold_full_reservation(self):
        agent = self.agent()
        self.transport.responses = [completion(cost=MISSING), URLError("provider timeout " + FAKE_KEY)]
        agent.act(self.env.observe())
        self.assertIsNone(agent.last_decision["cost_usd"])
        self.assertEqual(agent.last_decision["accounting_status"], "uncertain")
        with self.assertRaises(OpenRouterError) as caught:
            agent.act(self.env.observe())
        self.assertNotIn(FAKE_KEY, str(caught.exception) + self.stderr_output.getvalue())
        self.assertEqual(agent.metadata()["budget"]["reserved_usd"], 2.654208)
        self.assertEqual(agent.metadata()["budget"]["spent_usd"], 0)

    def test_budget_exhaustion_never_makes_authenticated_request(self):
        agent = self.agent(budget_usd=1)
        with self.assertRaisesRegex(BudgetError, "limit reached"):
            agent.act(self.env.observe())
        self.assertEqual(self.posts(), [])
        self.assertEqual(agent.metadata()["budget"]["request_count"], 0)

    def test_missing_key_unknown_model_and_unsupported_controls_are_unpaid(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with self.assertRaisesRegex(OpenRouterError, "OPENROUTER_API_KEY"):
                self.agent().act(self.env.observe())
        with self.assertRaisesRegex(OpenRouterError, "not present"):
            self.agent(model="unknown/model").act(self.env.observe())
        self.transport.inventory["data"][0]["supported_parameters"] = ["max_tokens"]
        with self.assertRaisesRegex(OpenRouterError, "lacks required"):
            self.agent().act(self.env.observe())
        self.assertEqual(self.posts(), [])
        self.assertEqual(SpendLedger(self.path).snapshot()["request_count"], 0)

    def test_http_error_body_and_success_echo_cannot_expose_credential(self):
        error = HTTPError(BASE_URL + "/v1/chat/completions", 401, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode()))
        self.transport.responses = [error, completion(plan="Returned credential: " + FAKE_KEY)]
        agent = self.agent()
        with self.assertRaisesRegex(OpenRouterError, "HTTP 401") as caught:
            agent.act(self.env.observe())
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        agent.act(self.env.observe())
        self.assertIn("[REDACTED]", agent.latest_plan)
        public = json.dumps(agent.metadata()) + json.dumps(agent.last_decision) + self.stderr_output.getvalue()
        self.assertNotIn(FAKE_KEY, public)

    def test_redirects_and_alternate_endpoint_constructor_are_rejected(self):
        request = Request(BASE_URL + "/v1/chat/completions", headers={"Authorization": "Bearer " + FAKE_KEY})
        with self.assertRaisesRegex(OpenRouterError, "redirects are refused"):
            _NoRedirect().redirect_request(request, None, 307, "redirect", {}, "https://untrusted.invalid")
        with self.assertRaises(TypeError):
            OpenRouterAgent(base_url="https://untrusted.invalid", budget_path=self.path)
        for variant in ("openrouter/auto", "x/y:free", "x/y@variant"):
            with self.subTest(variant=variant):
                # Automatic routers and routing variants are rejected even
                # before discovery; a concrete catalog model is required.
                with self.assertRaises(OpenRouterError):
                    self.agent(model=variant).act(self.env.observe())
        self.assertEqual(self.posts(), [])

    def test_cost_overrun_blocks_commands_and_does_not_restore_old_episode(self):
        agent = self.agent()
        self.transport.responses = [completion(plan="Old episode plan."), completion(cost=2, plan="Unaccepted new plan.")]
        agent.act(self.env.observe())
        other = AirTrafficEnv(seed=8, scenario="emergency", duration_s=1800)
        with self.assertRaisesRegex(BudgetError, "above its reserved bound"):
            agent.act(other.observe())
        self.assertEqual(agent.latest_plan, "")
        self.assertEqual(agent.last_decision["commands"], [])
        self.assertEqual(agent.last_decision["status"], "error")
        self.assertEqual(agent.metadata()["memory_turns"], 0)
        self.assertTrue(agent.metadata()["budget"]["blocked"])
        self.assertEqual(agent.metadata()["budget"]["spent_usd"], 2.003)

    def test_corrupt_ledger_metadata_is_safe_and_further_requests_stop(self):
        agent = self.agent()
        self.path.write_text("broken")
        budget = agent.metadata()["budget"]
        self.assertTrue(budget["blocked"])
        self.assertEqual(budget["remaining_usd"], 0)
        with self.assertRaises(BudgetError):
            agent.act(self.env.observe())
        self.assertEqual(self.posts(), [])


if __name__ == "__main__":
    unittest.main()
