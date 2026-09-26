"""Isolated human episodes using the LLM's observation and decision contract."""
from copy import deepcopy
import json
import threading
import uuid

from .cli import advance_simulation
from .environment import AirTrafficEnv
from .landing_metrics import landing_metrics
from .lmstudio import compact_observation, SYSTEM_PROMPT, MAX_COMMANDS, MAX_COMMAND_CHARS, MAX_PLAN_CHARS, MAX_SUMMARY_CHARS


class HumanError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class HumanSessions:
    """Small in-memory demo store. No agent or paid provider is instantiated."""
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def start(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"scenario", "seed"}:
            raise HumanError("Start expects scenario and seed")
        scenario, seed = payload.get("scenario", "runway_closure"), payload.get("seed", 7)
        if scenario not in ("runway_closure", "emergency", "wind_shift"):
            raise HumanError("Choose runway_closure, emergency or wind_shift")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise HumanError("Seed must be an integer from 0 to 4294967295")
        with self.lock:
            identity = uuid.uuid4().hex
            session = {"id": identity, "env": AirTrafficEnv(seed=seed, scenario=scenario, duration_s=1800),
                       "latest_plan": "", "decision_count": 0, "conversation": [], "replay": []}
            session["initial_observation"] = deepcopy(session["env"].observe())
            if len(self.sessions) >= 32:
                del self.sessions[next(iter(self.sessions))]
            self.sessions[identity] = session
            return self._state(session)

    def _get(self, identity):
        if not isinstance(identity, str) or identity not in self.sessions:
            raise HumanError("Human episode expired or does not exist. Start a new episode.", 404)
        return self.sessions[identity]

    def state(self, identity):
        with self.lock:
            return self._state(self._get(identity))

    @staticmethod
    def _input(session):
        observation = session["env"].observe()
        observation["decision_interval_s"] = 120
        compact = compact_observation(observation)
        compact["controller_memory"] = {"latest_plan": session["latest_plan"],
                                        "successful_decisions": session["decision_count"],
                                        "prior_decision_error": None}
        return compact

    def _state(self, session):
        observation = session["env"].observe()
        return deepcopy({"id": session["id"], "observation": observation,
                         "model_observation": self._input(session), "system_prompt": SYSTEM_PROMPT,
                         "conversation": session["conversation"], "decision_count": session["decision_count"],
                         "latest_plan": session["latest_plan"], "landing_metrics": landing_metrics(observation),
                         "replay": session["replay"], "initial_observation": session["initial_observation"],
                         "done": observation["done"]})

    def step(self, payload):
        fields = {"id", "expected_time_s", "commands", "plan", "summary"}
        if not isinstance(payload, dict) or set(payload) != fields:
            raise HumanError("Decision expects id, expected_time_s, commands, plan and summary")
        commands, plan, summary = payload["commands"], payload["plan"], payload["summary"]
        if not isinstance(commands, list) or len(commands) > MAX_COMMANDS or any(
                not isinstance(c, str) or not c.strip() or len(c) > MAX_COMMAND_CHARS for c in commands):
            raise HumanError("Send at most 32 nonempty commands of at most 80 characters each")
        if len({" ".join(c.upper().split()) for c in commands}) != len(commands):
            raise HumanError("Duplicate commands are not allowed")
        if not isinstance(plan, str) or not plan.strip() or len(plan) > MAX_PLAN_CHARS:
            raise HumanError("Keep an operational plan of 1 to 1200 characters")
        if not isinstance(summary, str) or len(summary) > MAX_SUMMARY_CHARS:
            raise HumanError("Decision summary must be at most 240 characters")
        expected = payload["expected_time_s"]
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise HumanError("expected_time_s must match the observed simulation time")
        with self.lock:
            session = self._get(payload["id"])
            env = session["env"]
            if env.done:
                raise HumanError("Episode complete. Start a new episode to continue.", 409)
            if expected != env.time_s:
                raise HumanError("This observation is stale. Refresh before submitting another decision.", 409)
            model_input, before = self._input(session), env.time_s
            observation, events = advance_simulation(env, commands, 120)
            decision = {"commands": commands, "summary": summary, "plan": plan}
            session["conversation"].extend([
                {"role": "user", "content": json.dumps(model_input, separators=(",", ":"), allow_nan=False)},
                {"role": "assistant", "content": json.dumps(decision, separators=(",", ":"), allow_nan=False)},
            ])
            session["conversation"] = session["conversation"][-8:]
            session["latest_plan"] = plan
            session["decision_count"] += 1
            session["replay"].append({"time_s": before, "seconds": env.time_s - before, **deepcopy(decision),
                                      "command_results": deepcopy(observation["command_results"]), "events": events})
            return self._state(session)
