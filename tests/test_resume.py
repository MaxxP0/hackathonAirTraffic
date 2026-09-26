"""Checkpoint continuation preserves physics and actual recorded model memory."""
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from atc_bench.cli import run_episode
from atc_bench.openrouter import OpenRouterAgent
from examples.resume_openrouter import restore
from tests.test_openrouter import FakeTransport, FAKE_KEY, completion


class ResumeTests(unittest.TestCase):
    def test_checkpoint_restores_exact_history_and_continues_at_same_time(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = FakeTransport()
            transport.responses = [completion(plan="First plan"), completion(plan="Second plan"), URLError("interrupted")]
            with patch("atc_bench.openrouter.build_opener", return_value=transport), \
                    patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_KEY}), redirect_stderr(io.StringIO()):
                run = run_episode(seed=7, scenario="emergency", duration=360, agent_spec="openrouter",
                                  step_seconds=120, agent_options={"budget_path": Path(folder)/"budget.json"})
                run = json.loads(json.dumps(run))
                self.assertEqual(run["status"], "aborted")
                agent = OpenRouterAgent(budget_path=Path(folder)/"budget.json")
                env = restore(run, agent)
                self.assertEqual(env.time_s, 240)
                self.assertEqual(agent.latest_plan, "Second plan")
                self.assertEqual(agent._successful_decisions, 2)
                self.assertEqual(len(agent._conversation), 4)
                prior = deepcopy(agent._conversation)
                agent.act(env.observe())
                sent = transport.requests[-1][1]["messages"]
                self.assertEqual(sent[1:-1], prior)
                self.assertEqual(agent.last_decision["input_memory_turns"], 2)
                self.assertEqual(env.time_s, 240)
                corrupt = deepcopy(run)
                corrupt["metrics"]["score"] += 1
                with self.assertRaisesRegex(ValueError, "Checkpoint does not reproduce"):
                    restore(corrupt, agent)
