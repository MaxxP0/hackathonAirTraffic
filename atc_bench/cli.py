"""Command-line runs, reproducible evaluations, and a JSON-lines agent bridge."""

from __future__ import annotations

import argparse
from copy import deepcopy
import importlib
import json
import math
from pathlib import Path
import statistics
import sys

from .agents import NoOpAgent, ReferenceAgent
from .environment import AirTrafficEnv
from .landing_metrics import landing_metrics
from .lmstudio import LMStudioAgent, LMStudioError
from . import __version__


SCENARIOS = ("mixed", "rush_hour", "low_visibility", "storm", "emergency", "wind_shift", "runway_closure")
BENCHMARK_VERSION = __version__
RANK_FIELDS = (
    "collisions", "crashed", "emergencies_failed", "runway_incursions", "wake_violations",
    "separation_loss_seconds", "weather_exposure_seconds",
    "negative_completed", "ground_delay_seconds", "airborne_seconds",
)


def load_agent(spec: str, **lmstudio_options):
    """Load a builtin or an explicitly supplied, zero-argument Python class."""
    if spec == "reference":
        return ReferenceAgent()
    if spec == "noop":
        return NoOpAgent()
    if spec == "lmstudio":
        return LMStudioAgent(**lmstudio_options)
    if spec == "openrouter":
        from .openrouter import OpenRouterAgent
        return OpenRouterAgent(**lmstudio_options)
    if ":" not in spec:
        raise ValueError("agent must be reference, noop, lmstudio, openrouter, or module:Class")
    module_name, class_name = spec.split(":", 1)
    if not module_name or not class_name:
        raise ValueError("custom agent must use module:Class")
    cls = getattr(importlib.import_module(module_name), class_name)
    agent = cls()
    if not callable(getattr(agent, "act", None)):
        raise ValueError("custom agent must expose act(observation)")
    return agent


def rank_key(metrics: dict) -> list[float]:
    """Lower is better. All recorded safety outcomes precede efficiency."""
    values = dict(metrics)
    values["negative_completed"] = -(metrics.get("landed", 0) + metrics.get("departed", 0))
    return [values.get(field, 0) for field in RANK_FIELDS]


def advance_simulation(env: AirTrafficEnv, commands: list, seconds: int) -> tuple[dict, list]:
    """Apply one batch, then fast-forward up to 600 seconds without decisions.

    The low-level environment retains its 60-second limit. A zero-time command
    phase captures its events before integration can rotate the event log.
    Subsequent chunks contain no commands, and the batch's feedback remains
    available in both the returned observation and the next env.observe().
    """
    commands, seconds, _ = validate_step({"commands": commands, "seconds": seconds}, max_seconds=600)
    previous_events = env.observe()["events"]
    events = []

    def collect(observation):
        nonlocal previous_events
        current = observation["events"]
        overlap = min(len(previous_events), len(current))
        while overlap and previous_events[-overlap:] != current[:overlap]:
            overlap -= 1
        events.extend(deepcopy(current[overlap:]))
        previous_events = deepcopy(current)

    observation = env.step(commands, seconds=0)
    collect(observation)
    command_results = deepcopy(observation["command_results"])
    remaining = seconds
    while remaining > 0 and not env.done:
        chunk = min(60, remaining)
        previous_time = env.time_s
        observation = env.step([], seconds=chunk)
        collect(observation)
        remaining -= int(env.time_s - previous_time)
    env.command_results = deepcopy(command_results)
    observation["command_results"] = command_results
    return observation, events


def run_episode(*, seed: int, scenario: str, duration: int, agent_spec: str,
                step_seconds: int = 120, agent_options: dict | None = None) -> dict:
    if isinstance(step_seconds, bool) or not isinstance(step_seconds, int) or not 1 <= step_seconds <= 600:
        raise ValueError("step_seconds must be an integer between 1 and 600")
    env = AirTrafficEnv(seed=seed, scenario=scenario, duration_s=duration)
    agent = load_agent(agent_spec, **(agent_options or {}))
    observation = env.observe()
    initial = json.loads(json.dumps(observation))
    replay = []
    error_message = None
    while not env.done:
        # A plugin receives a detached JSON value, never live environment data.
        try:
            agent_observation = json.loads(json.dumps(observation))
            agent_observation["decision_interval_s"] = step_seconds
            commands = agent.act(agent_observation)
        except LMStudioError as error:
            # Preserve the last valid simulation state and the failed request.
            # Never replace the LLM with a heuristic or advance on an API error.
            error_message = str(error)
            replay.append({"time_s": observation["time_s"], "seconds": 0,
                           "commands": [], "command_results": [], "events": [],
                           "decision": json.loads(json.dumps(agent.last_decision))})
            break
        if not isinstance(commands, list):
            raise ValueError("agent.act() must return a list of command strings or objects")
        commands = json.loads(json.dumps(commands, allow_nan=False))
        previous_time = observation["time_s"]
        observation, events = advance_simulation(env, commands, step_seconds)
        replay.append({
            "time_s": previous_time,
            "seconds": observation["time_s"] - previous_time,
            "commands": commands,
            "command_results": observation["command_results"],
            "events": events,
        })
        if isinstance(agent, LMStudioAgent):
            replay[-1]["decision"] = json.loads(json.dumps(agent.last_decision))
    metrics = env.metrics()
    result = {
        "benchmark_version": BENCHMARK_VERSION,
        "configuration": {"seed": seed, "scenario": scenario, "duration_s": duration,
                          "agent": agent_spec, "step_seconds": step_seconds},
        "metrics": metrics,
        "landing_metrics": landing_metrics(observation),
        "rank_key": rank_key(metrics),
        "rank_fields": RANK_FIELDS,
        "initial_observation": initial,
        "final_observation": observation,
        "replay": replay,
    }
    if isinstance(agent, LMStudioAgent):
        result["controller"] = agent.metadata()
        result["status"] = "aborted" if error_message else "completed"
        if error_message:
            result["error"] = error_message
    return result


def write_result(result: dict, output: str | None) -> None:
    payload = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        print(f"Wrote {path}", file=sys.stderr)
    else:
        sys.stdout.write(payload)


def validate_reset(payload: object, current: AirTrafficEnv) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("reset must be an object")
    unknown = set(payload) - {"seed", "scenario", "duration_s"}
    if unknown:
        raise ValueError("unknown reset fields: " + ", ".join(sorted(unknown)))
    seed = payload.get("seed", current.seed)
    duration = payload.get("duration_s", current.duration_s)
    scenario = payload.get("scenario", current.scenario)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 86400:
        raise ValueError("duration_s must be an integer between 1 and 86400")
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario")
    return {"seed": seed, "scenario": scenario, "duration_s": duration}


def validate_step(payload: object, *, allow_autopilot: bool = False,
                  max_seconds: int = 60) -> tuple[list, int, bool]:
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    allowed = {"commands", "seconds"} | ({"autopilot"} if allow_autopilot else set())
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("unknown step fields: " + ", ".join(sorted(unknown)))
    commands = payload.get("commands", [])
    seconds = payload.get("seconds", 10)
    autopilot = payload.get("autopilot", False)
    if not isinstance(commands, list) or len(commands) > 100:
        raise ValueError("commands must be a list with at most 100 entries")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= max_seconds:
        raise ValueError(f"seconds must be an integer between 0 and {max_seconds}")
    if not isinstance(autopilot, bool):
        raise ValueError("autopilot must be a boolean")
    return commands, seconds, autopilot


def stdio(args: argparse.Namespace) -> None:
    env = AirTrafficEnv(seed=args.seed, scenario=args.scenario, duration_s=args.duration)

    def emit(value: dict):
        sys.stdout.write(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    emit(env.observe())
    for line in sys.stdin:
        try:
            if len(line) > 1_000_000:
                raise ValueError("request exceeds 1 MB")
            def reject_constant(value):
                raise ValueError(f"non-finite JSON number: {value}")

            request = json.loads(line, parse_constant=reject_constant)
            if isinstance(request, dict) and "reset" in request:
                if set(request) != {"reset"}:
                    raise ValueError("reset cannot be combined with other fields")
                env = AirTrafficEnv(**validate_reset(request["reset"], env))
                emit(env.observe())
            else:
                commands, seconds, _ = validate_step(request)
                emit(env.step(commands, seconds=seconds))
        except (ValueError, TypeError, KeyError) as error:
            emit({"error": str(error)})


def positive_duration(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 86400:
        raise argparse.ArgumentTypeError("duration must be between 1 and 86400 seconds")
    return result


def add_agent_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", default="reference", help="reference, noop, lmstudio, openrouter, or module:Class")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234", help="LM Studio server URL")
    parser.add_argument("--model", help="model ID; LM Studio auto-detects, OpenRouter defaults to GLM 5.3 Flash")
    parser.add_argument("--llm-timeout", type=float, help="seconds allowed per request (LM Studio: 900, OpenRouter: 180)")
    parser.add_argument("--max-tokens", type=int, help="output token limit (LM Studio: 8192, OpenRouter: 8192)")


def agent_options(args: argparse.Namespace) -> dict:
    options = {"model": args.model,
               "timeout_s": args.llm_timeout if args.llm_timeout is not None else (180 if args.agent == "openrouter" else 900),
               "max_tokens": args.max_tokens if args.max_tokens is not None else 8192}
    if args.agent != "openrouter":
        options["base_url"] = args.base_url
    return options


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frankfurt-inspired command-driven air traffic benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "stdio", "serve"):
        child = subparsers.add_parser(name)
        child.add_argument("--seed", type=int, default=7)
        child.add_argument("--scenario", choices=SCENARIOS, default="mixed")
        child.add_argument("--duration", type=positive_duration, default=1800)
        if name in {"run", "serve"}:
            add_agent_arguments(child)
        if name == "run":
            child.add_argument("--output")
            child.add_argument("--step-seconds", type=int, choices=range(1, 601), default=120, metavar="1..600")
        if name == "serve":
            child.add_argument("--port", type=int, default=8000)
    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    benchmark.add_argument("--scenarios", choices=SCENARIOS, nargs="+", default=list(SCENARIOS))
    benchmark.add_argument("--duration", type=positive_duration, default=1800)
    add_agent_arguments(benchmark)
    benchmark.add_argument("--output")
    benchmark.add_argument("--step-seconds", type=int, choices=range(1, 601), default=120, metavar="1..600")
    args = parser.parse_args(argv)
    try:
        if args.command == "stdio":
            stdio(args)
        elif args.command == "serve":
            from .server import serve
            options = agent_options(args)
            serve(port=args.port, seed=args.seed, scenario=args.scenario, duration=args.duration,
                  agent_spec=args.agent, base_url=args.base_url, model=args.model,
                  llm_timeout=options["timeout_s"], max_tokens=options["max_tokens"])
        elif args.command == "run":
            result = run_episode(seed=args.seed, scenario=args.scenario, duration=args.duration,
                                 agent_spec=args.agent, step_seconds=args.step_seconds,
                                 agent_options=agent_options(args))
            write_result(result, args.output)
            if result.get("status") == "aborted":
                print(f"error: {result['error']}", file=sys.stderr)
                return 2
        else:
            runs = []
            for scenario in args.scenarios:
                for seed in args.seeds:
                    result = run_episode(seed=seed, scenario=scenario, duration=args.duration,
                                         agent_spec=args.agent, step_seconds=args.step_seconds,
                                         agent_options=agent_options(args))
                    runs.append(result)
                    print(f"{scenario} seed={seed}: score={result['metrics']['score']:.2f}", file=sys.stderr)
                    if result.get("status") == "aborted":
                        break
                if runs[-1].get("status") == "aborted":
                    break
            completed = [run for run in runs if run.get("status", "completed") == "completed"]
            samples = {}
            for run in completed:
                for field, value in run["metrics"].items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                        samples.setdefault(field, []).append(value)
            means = {field: statistics.fmean(values) for field, values in samples.items()}
            aborted = any(run.get("status") == "aborted" for run in runs)
            write_result({"benchmark_version": BENCHMARK_VERSION, "agent": args.agent,
                          "status": "aborted" if aborted else "completed",
                          "run_count": len(runs), "mean_metrics": means,
                          "completed_run_count": len(completed), "aborted_run_count": len(runs) - len(completed),
                          "metric_sample_counts": {field: len(values) for field, values in samples.items()},
                          "rank_fields": RANK_FIELDS, "mean_rank_key": rank_key(means) if completed else None,
                          "runs": runs}, args.output)
            if aborted:
                print(f"error: {runs[-1]['error']}", file=sys.stderr)
                return 2
    except (ValueError, TypeError, ImportError, AttributeError, OSError, LMStudioError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
