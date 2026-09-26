"""Resume a saved interrupted hosted run without repeating completed decisions.

Replays physics and verifies the checkpoint exactly, then restores the recorded
dialogue and operational plan. The original interrupted result is never edited.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import time

from atc_bench import AirTrafficEnv, __version__
from atc_bench.cli import advance_simulation, rank_key
from atc_bench.landing_metrics import landing_metrics
from atc_bench.lmstudio import LMStudioError, SYSTEM_PROMPT
from atc_bench.openrouter import OpenRouterAgent


def restore(run, agent):
    if run.get("benchmark_version") != __version__ or run.get("status") != "aborted":
        raise ValueError("Resume requires an aborted run from the current benchmark version")
    config, controller = run["configuration"], run["controller"]
    if config["agent"] != "openrouter" or controller["system_prompt"] != SYSTEM_PROMPT:
        raise ValueError("Resume requires the same hosted controller and exact system prompt")
    if agent.model != controller["model"] or agent.max_tokens != controller["max_tokens"]:
        raise ValueError("Resuming with different model settings is not allowed")
    env = AirTrafficEnv(seed=config["seed"], scenario=config["scenario"], duration_s=config["duration_s"])
    if json.loads(json.dumps(env.observe())) != run["initial_observation"]:
        raise ValueError("Initial scenario state differs from the saved run")
    successes = []
    for entry in run["replay"]:
        if env.time_s != entry["time_s"]:
            raise ValueError("Saved replay has a discontinuous timeline")
        decision = entry.get("decision", {})
        if decision.get("status") == "error":
            if entry["seconds"] != 0 or entry["commands"]:
                raise ValueError("A failed decision must not advance simulation time")
            continue
        observation, _ = advance_simulation(env, entry["commands"], int(entry["seconds"]))
        if observation["command_results"] != entry["command_results"]:
            raise ValueError("Command acceptance differs from the saved replay")
        successes.append(decision)
    if env.metrics() != run["metrics"] or json.loads(json.dumps(env.observe())) != run["final_observation"]:
        raise ValueError("Checkpoint does not reproduce exactly; continuation blocked")
    agent._conversation = []
    for decision in successes[-agent.max_memory_turns:]:
        agent._conversation.extend([
            {"role": "user", "content": json.dumps(decision["observation"], separators=(",", ":"), allow_nan=False)},
            {"role": "assistant", "content": json.dumps({k: decision[k] for k in ("commands", "summary", "plan")}, separators=(",", ":"))},
        ])
    agent.latest_plan = successes[-1]["plan"] if successes else ""
    agent._successful_decisions = len(successes)
    agent._calls = controller["calls"]
    agent._errors = controller["errors"]
    agent._usage = deepcopy(controller["usage"])
    agent._latencies = [entry["decision"]["latency_s"] for entry in run["replay"] if "decision" in entry]
    agent._last_decision_error = run.get("error")
    agent._episode_identity = (config["seed"], config["scenario"], config["duration_s"])
    agent._last_observation_time_s = env.time_s
    return env


def resume(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Preserve the original interrupted result; use another output file")
    run = json.loads(source.read_text())
    controller = run["controller"]
    agent = OpenRouterAgent(model=controller["model"], timeout_s=controller["timeout_s"],
                            max_tokens=controller["max_tokens"], temperature=controller["temperature"],
                            max_memory_turns=controller["max_memory_turns"])
    env = restore(run, agent)
    started, resume_time = time.monotonic(), env.time_s
    run.pop("error", None)
    run["status"] = "running"
    while not env.done:
        observation = env.observe()
        observation["decision_interval_s"] = run["configuration"]["step_seconds"]
        try:
            commands = agent.act(observation)
        except LMStudioError as error:
            run["replay"].append({"time_s": env.time_s, "seconds": 0, "commands": [],
                                  "command_results": [], "events": [], "decision": deepcopy(agent.last_decision)})
            run["error"], run["status"] = str(error), "aborted"
            break
        before = env.time_s
        observation, events = advance_simulation(env, commands, run["configuration"]["step_seconds"])
        run["replay"].append({"time_s": before, "seconds": env.time_s - before, "commands": commands,
                              "command_results": observation["command_results"], "events": events,
                              "decision": deepcopy(agent.last_decision)})
    elapsed = time.monotonic() - started
    prior_wall = run.get("wall_duration_s", controller["total_latency_s"])
    run.update(final_observation=env.observe(), metrics=env.metrics(), rank_key=rank_key(env.metrics()),
               landing_metrics=landing_metrics(env.observe()),
               controller=agent.metadata(), result_file=destination.name,
               wall_duration_s=round(prior_wall + elapsed, 3))
    if run.get("resume_info"):
        run.setdefault("previous_resumes", []).append(run["resume_info"])
    run["resume_info"] = {"source_file": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                          "resumed_at_sim_s": resume_time,
                          "prior_wall_duration_s": prior_wall, "continuation_wall_duration_s": round(elapsed, 3),
                          "checkpoint_verified_exactly": True,
                          "note": "Active execution time excludes the manual pause between processes; original failure remains in replay."}
    if env.done:
        run["status"] = "completed"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(run, indent=2, allow_nan=False) + "\n")
    print(f"{run['status']}: {destination}, t={env.time_s}, score={env.metrics()['score']:.2f}", flush=True)
    return run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raise SystemExit(0 if resume(args.source, args.output)["status"] == "completed" else 2)
