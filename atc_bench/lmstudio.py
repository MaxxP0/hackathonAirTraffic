"""Observation-only local LLM controller through LM Studio's JSON API.

Uses an already loaded language model; it never downloads, loads, or unloads one.
No reference-controller fallback is used when an inference or validation fails.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


PROMPT_VERSION = "frankfurt-controller-v3"
MAX_COMMANDS = 32
MAX_COMMAND_CHARS = 80
MAX_SUMMARY_CHARS = 240
MAX_PLAN_CHARS = 1200
SYSTEM_PROMPT = """Control this simplified Frankfurt air traffic simulation using only the observation.
Return {"commands":[strings],"summary":"brief operational decision","plan":"operational plan"}.
Summary <=240 characters; plan 1..1200 characters; command strings <=80 characters.
Plan should retain future sequencing, runway reservations, priorities and when
to reconsider them. This is a public operational plan, not a reasoning transcript.
Keep useful intentions across turns, updating them against fresh observations.
Recent dialogue and controller_memory.latest_plan preserve earlier decisions.
Empty commands means let existing clearances execute.

The simulation pauses while you reason. After your commands are validated and
applied, it advances decision_interval_s simulated seconds quickly before your
next observation. Plan for this entire upcoming control window; longer windows
mean fewer chances to react. Model wall-clock latency does not burn aircraft
fuel or increase simulated waiting. Use the latest observation over older
dialogue, review command rejections, and revise the operational plan. Do not
assume a previously planned command was accepted. Future events stay hidden.

Read aircraft rows by aircraft_columns; join each row's type to aircraft_types
for performance limits. Merge airport.runways geometry with runway_state by id.
Coordinates NM east/north, headings degrees clockwise from north, altitude feet
above airport, speed knots, time seconds. Responses are gradual, not instant.
Prioritize safety, emergency touchdown, completed traffic, then ground/airborne
delay. Diversions, failures and unfinished traffic do not count as success.
Emergency deadline_s is absolute; approaches take minutes. Separate aircraft
by >=3 NM OR >=1000 ft; inspect targets, fuel, conflicts and storm cells.

Commands (C=current callsign, R=runway):
HEADING C degrees [0,360)
ALTITUDE C feet [1000,18000]
SPEED C knots [type min_speed_kt,max_speed_kt]
DIRECT C FIX or DIRECT C x_nm y_nm [-38,38]
HOLD C
APPROACH C R
TAKEOFF C R
GO_AROUND C
DIVERT C
At most 32 commands, no duplicates, applied in order. Review prior_command_errors; rejected
commands do not cancel others. Use existing clearances rather than reissuing.

APPROACH automatically intercepts approach_fix at 3200 ft, then descends/tracks
final through touchdown, managing altitude/speed. Do not reissue APPROACH or
change its targets. HEADING/DIRECT cancel approach or hold; HOLD on approach
causes go-around. Only interrupt approach for safety. GO_AROUND requires
approach. HOLD circles an arrival at its position; ALTITUDE preserves HOLD.
Separate nearby waiting arrivals by altitude. TAKEOFF only for ground status.
No airborne commands during ground, takeoff_roll, landing_roll or taxi_in.
Outbound flights normally climb and leave the 40 NM sector automatically.

Reciprocal runway IDs share physical_id occupancy/wake. Schedule one approach
per physical runway; preserve it until that arrival clears. Require unoccupied,
open runway, available_in_s=0 and suitable weather/type. Match active_direction
except departure-only 18 if wind permits. Arrival-only 25R/07L forbid
A388/B748/B744/MD11. Length must meet type distance (+15% in precipitation).
Crosswind=abs(sin(wind_from_deg-heading_deg))*max(wind_speed_kt,gust_kt) <=type
crosswind_limit_kt; tailwind=-cos(wind_from_deg-heading_deg)*wind_speed_kt <=5.
Use degrees in trigonometry. Landing requires visibility>=550m, ceiling>=200ft;
takeoff requires visibility>=300m. Weather changes can cause go-arounds.
"""

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "atc_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "commands": {"type": "array", "items": {"type": "string", "minLength": 1,
                                                           "maxLength": MAX_COMMAND_CHARS},
                             "maxItems": MAX_COMMANDS},
                "summary": {"type": "string", "maxLength": MAX_SUMMARY_CHARS},
                "plan": {"type": "string", "minLength": 1, "maxLength": MAX_PLAN_CHARS},
            },
            "required": ["commands", "summary", "plan"],
            "additionalProperties": False,
        },
    },
}
FINISHED_STATUSES = {"landed", "departed", "diverted", "crashed"}


class LMStudioError(RuntimeError):
    """A visible model discovery, inference, or response validation failure."""


AIRCRAFT_TYPE_FIELDS = ("wake", "min_speed_kt", "max_speed_kt", "landing_distance_m",
                        "takeoff_distance_m", "crosswind_limit_kt")
RUNWAY_STATE_FIELDS = ("occupied_by", "available_in_s", "closed")
DECISION_METRICS = ("score", "collisions", "crashed", "emergencies_failed", "emergencies_unresolved",
                    "runway_incursions", "wake_violations", "separation_loss_seconds",
                    "weather_exposure_seconds", "landed", "departed", "diverted", "unfinished",
                    "ground_delay_seconds", "ground_wait_seconds", "ground_wait_score",
                    "emergency_wait_seconds", "emergency_wait_score", "airborne_seconds", "invalid_commands")


def compact_observation(observation: dict) -> dict:
    """Losslessly pack active aircraft and runway state with static context first.

    All active aircraft fields except radar trails survive, at original numeric
    precision. Type limits are stored once and rows share their column names.
    Only cumulative scalar metrics relevant to controller decisions are sent;
    the simulator/result still exposes all metrics and every finished aircraft.
    """
    airport = deepcopy(observation.get("airport", {}))
    runway_state = {}
    for runway in airport.get("runways", []):
        runway_state[runway["id"]] = {key: runway.pop(key) for key in RUNWAY_STATE_FIELDS if key in runway}
    # Include known types from finished aircraft too so their disappearance does
    # not unnecessarily change the static prompt prefix. No future data is used.
    aircraft_types = {}
    for plane in observation.get("aircraft", []):
        type_id = plane["type"]
        spec = {key: deepcopy(plane[key]) for key in AIRCRAFT_TYPE_FIELDS if key in plane}
        if type_id in aircraft_types and aircraft_types[type_id] != spec:
            raise LMStudioError(f"Inconsistent observed performance limits for aircraft type {type_id}")
        aircraft_types[type_id] = spec
    aircraft = [plane for plane in observation.get("aircraft", [])
                if plane.get("status") not in FINISHED_STATUSES]
    columns = list(dict.fromkeys(key for plane in aircraft for key in plane
                                if key != "history" and key not in AIRCRAFT_TYPE_FIELDS))
    compact = {"airport": airport, "aircraft_types": dict(sorted(aircraft_types.items())),
               "aircraft_columns": columns}
    compact.update({key: deepcopy(observation[key]) for key in (
        "time_s", "duration_s", "decision_interval_s", "scenario", "done", "weather", "conflicts"
    ) if key in observation})
    compact["runway_state"] = runway_state
    compact["aircraft"] = [[deepcopy(plane.get(key)) for key in columns] for plane in aircraft]
    metrics = observation.get("metrics", {})
    compact["metrics"] = {key: metrics[key] for key in DECISION_METRICS if key in metrics
                          and (metrics[key] is None or isinstance(metrics[key], (str, bool, int, float)))}
    compact["prior_command_errors"] = deepcopy([
        result for result in observation.get("command_results", []) if not result.get("accepted")
    ])
    return compact


class LMStudioAgent:
    def __init__(self, base_url: str = "http://127.0.0.1:1234", model: str | None = None,
                 timeout_s: float = 900, max_tokens: int = 8192, temperature: float = 0,
                 max_memory_turns: int = 4):
        base_url = base_url.rstrip("/")
        # Accept the standard OpenAI-client base URL as well as the server root.
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("LM Studio base_url must be an HTTP(S) server URL")
        if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("LM Studio timeout_s must be positive and finite")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("LM Studio max_tokens must be a positive integer")
        if isinstance(temperature, bool) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("LM Studio temperature must be between 0 and 2")
        if isinstance(max_memory_turns, bool) or not isinstance(max_memory_turns, int) or not 1 <= max_memory_turns <= 16:
            raise ValueError("LM Studio max_memory_turns must be an integer between 1 and 16")
        self.base_url, self.model = base_url, model
        self.timeout_s, self.max_tokens, self.temperature = timeout_s, max_tokens, temperature
        self.requested_model = model
        self.model_info: dict = {}
        self.reasoning_effort: str | None = None
        self.advertised_reasoning_default: str | None = None
        self.reasoning_mode = "model_default"
        self.last_decision: dict | None = None
        self._resolved_model = False
        self._calls = self._errors = 0
        self._latencies: list[float] = []
        self._usage: dict[str, int] = {}
        self.max_memory_turns = max_memory_turns
        self.latest_plan = ""
        self._conversation: list[dict] = []
        self._successful_decisions = 0
        self._last_decision_error: str | None = None
        self._episode_identity: tuple | None = None
        self._last_observation_time_s: float | None = None

    def _json_request(self, path: str, payload: dict | None = None) -> dict:
        request = Request(self.base_url + path,
                          data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                data = json.loads(response.read())
        except HTTPError as error:
            detail = error.read(2000).decode("utf-8", errors="replace")
            raise LMStudioError(f"LM Studio {path} returned HTTP {error.code}: {detail}") from error
        except (URLError, OSError, ValueError) as error:
            raise LMStudioError(f"LM Studio {path} failed: {error}") from error
        if not isinstance(data, dict):
            raise LMStudioError(f"LM Studio {path} returned a non-object JSON response")
        if "error" in data:
            raise LMStudioError(f"LM Studio {path}: {str(data['error'])[:2000]}")
        return data

    def _discover_model(self) -> None:
        candidates = []
        try:
            inventory = self._json_request("/api/v1/models")
            models = inventory.get("models")
            if not isinstance(models, list):
                raise LMStudioError("LM Studio model inventory has no models list")
            for model in models:
                if not isinstance(model, dict) or model.get("type") != "llm":
                    continue
                for instance in model.get("loaded_instances", []):
                    if isinstance(instance, dict) and isinstance(instance.get("id"), str):
                        candidates.append((instance["id"], model.get("key"), model))
        except LMStudioError as primary_error:
            # Older LM Studio versions expose loaded state only through v0.
            try:
                inventory = self._json_request("/api/v0/models")
                models = inventory.get("data")
                if not isinstance(models, list):
                    raise LMStudioError("LM Studio legacy model inventory has no data list")
                candidates = [(model["id"], model["id"], model) for model in models
                              if isinstance(model, dict) and isinstance(model.get("id"), str)
                              and model.get("state") == "loaded"
                              and model.get("type") in {"llm", "vlm"}]
            except LMStudioError as legacy_error:
                raise LMStudioError(f"Cannot inspect loaded LM Studio models: {primary_error}; {legacy_error}") from legacy_error
        if self.requested_model:
            candidates = [item for item in candidates if self.requested_model in item[:2]]
        if not candidates:
            requested = f" {self.requested_model!r}" if self.requested_model else ""
            raise LMStudioError(f"No already loaded language model{requested} found in LM Studio; load a model there first")
        instance_id, _, info = sorted(candidates, key=lambda item: item[0])[0]
        self.model, self.model_info = instance_id, deepcopy(info)
        # Keep the loaded model's reasoning settings and report the actual
        # wall-clock latency separately from simulated aircraft waiting.
        capabilities = info.get("capabilities", {})
        reasoning = capabilities.get("reasoning", {}) if isinstance(capabilities, dict) else {}
        if isinstance(reasoning, dict):
            self.advertised_reasoning_default = reasoning.get("default")
        self._resolved_model = True

    def describe(self) -> dict:
        return {"agent": "lmstudio", "base_url": self.base_url, "model": self.model,
                "requested_model": self.requested_model, "model_info": deepcopy(self.model_info),
                "prompt_version": PROMPT_VERSION, "system_prompt": SYSTEM_PROMPT,
                "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                "temperature": self.temperature, "max_tokens": self.max_tokens,
                "reasoning_effort": self.reasoning_effort,
                "reasoning_mode": self.reasoning_mode,
                "advertised_reasoning_default": self.advertised_reasoning_default,
                "timeout_s": self.timeout_s, "response_format": deepcopy(RESPONSE_FORMAT),
                "observation_format": "compact-v2", "history": "bounded persistent dialogue",
                "max_memory_turns": self.max_memory_turns,
                "memory_turns": len(self._conversation) // 2,
                "successful_decisions": self._successful_decisions, "latest_plan": self.latest_plan}

    def metadata(self) -> dict:
        return {**self.describe(), "calls": self._calls, "errors": self._errors,
                "total_latency_s": round(sum(self._latencies), 3),
                "mean_latency_s": round(sum(self._latencies) / len(self._latencies), 3) if self._latencies else 0,
                "usage": dict(self._usage)}

    def act(self, observation: dict) -> list[str]:
        identity = tuple(observation.get(key) for key in ("seed", "scenario", "duration_s"))
        observed_time = observation.get("time_s")
        if self._episode_identity is not None and (
            identity != self._episode_identity or
            (observed_time is not None and self._last_observation_time_s is not None
             and observed_time < self._last_observation_time_s)
        ):
            self._conversation.clear()
            self.latest_plan = ""
            self._successful_decisions = 0
            self._last_decision_error = None
        self._episode_identity = identity
        self._last_observation_time_s = observed_time
        if observation.get("done"):
            self.last_decision = {"status": "done", "model": self.model, "time_s": observation.get("time_s"),
                                  "commands": [], "summary": "Episode complete.", "latency_s": 0, "usage": {},
                                  "plan": self.latest_plan, "memory_turns": len(self._conversation) // 2}
            return []
        started = time.perf_counter()
        compact = compact_observation(observation)
        compact["controller_memory"] = {"latest_plan": self.latest_plan,
                                        "successful_decisions": self._successful_decisions,
                                        "prior_decision_error": self._last_decision_error}
        user_message = {"role": "user", "content": json.dumps(compact, separators=(",", ":"), allow_nan=False)}
        memory_turns = len(self._conversation) // 2
        self.last_decision = {"status": "requesting", "model": self.model, "time_s": observation.get("time_s"),
                              "commands": [], "summary": "", "usage": {}, "observation": compact,
                              "plan": self.latest_plan, "memory_turns": memory_turns,
                              "input_memory_turns": memory_turns}
        try:
            if not self._resolved_model:
                self._discover_model()
            self.last_decision["model"] = self.model
            self._calls += 1
            print(f"LM Studio call {self._calls}: t={observation.get('time_s')}s model={self.model}", file=sys.stderr, flush=True)
            payload = {
                "model": self.model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             *deepcopy(self._conversation), user_message],
                "temperature": self.temperature, "max_tokens": self.max_tokens,
                "response_format": RESPONSE_FORMAT, "stream": False,
            }
            self.last_decision["reasoning_effort"] = self.reasoning_effort
            self.last_decision["reasoning_mode"] = self.reasoning_mode
            self.last_decision["advertised_reasoning_default"] = self.advertised_reasoning_default
            response = self._json_request("/v1/chat/completions", payload)
            usage = response.get("usage", {})
            if isinstance(usage, dict):
                details = usage.get("completion_tokens_details", {})
                usage = {key: value for key, value in usage.items()
                         if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                if isinstance(details, dict):
                    reasoning_tokens = details.get("reasoning_tokens")
                    if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool) and reasoning_tokens >= 0:
                        usage["reasoning_tokens"] = reasoning_tokens
                self.last_decision["usage"] = usage
                for key, value in usage.items():
                    self._usage[key] = self._usage.get(key, 0) + value
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise LMStudioError("LM Studio returned no completion choice")
            choice = choices[0]
            self.last_decision["finish_reason"] = choice.get("finish_reason")
            if choice.get("finish_reason") not in {"stop", None}:
                raise LMStudioError(f"LM Studio completion ended with {choice.get('finish_reason')!r}; no commands applied")
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str):
                raise LMStudioError("LM Studio returned no JSON content")
            try:
                decision = json.loads(content)
            except ValueError as error:
                raise LMStudioError("LM Studio returned invalid decision JSON; no commands applied") from error
            if not isinstance(decision, dict) or set(decision) != {"commands", "summary", "plan"}:
                raise LMStudioError("LM Studio decision must contain only commands, summary and plan")
            commands, summary, plan = decision["commands"], decision["summary"], decision["plan"]
            if not isinstance(commands, list) or len(commands) > MAX_COMMANDS or any(
                not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS for command in commands
            ):
                raise LMStudioError(f"LM Studio commands must be at most {MAX_COMMANDS} nonempty strings of at most {MAX_COMMAND_CHARS} characters")
            if not isinstance(summary, str) or len(summary) > MAX_SUMMARY_CHARS:
                raise LMStudioError(f"LM Studio summary must be a string of at most {MAX_SUMMARY_CHARS} characters")
            if not isinstance(plan, str) or not plan.strip() or len(plan) > MAX_PLAN_CHARS:
                raise LMStudioError(f"LM Studio plan must be a nonempty string of at most {MAX_PLAN_CHARS} characters")
            self.latest_plan = plan
            self._successful_decisions += 1
            self._last_decision_error = None
            self._conversation.extend([user_message, {"role": "assistant", "content": json.dumps(decision, separators=(",", ":"))}])
            self._conversation = self._conversation[-2 * self.max_memory_turns:]
            self.last_decision.update(status="ok", commands=commands, summary=summary, plan=plan,
                                      memory_turns=len(self._conversation) // 2)
            return commands
        except LMStudioError as error:
            self._errors += 1
            self._last_decision_error = str(error)
            self.last_decision.update(status="error", error=str(error))
            raise
        finally:
            latency = time.perf_counter() - started
            self._latencies.append(latency)
            self.last_decision["latency_s"] = round(latency, 3)
            print(f"LM Studio t={observation.get('time_s')}s: {self.last_decision['status']} "
                  f"{latency:.2f}s, {len(self.last_decision['commands'])} commands", file=sys.stderr, flush=True)
