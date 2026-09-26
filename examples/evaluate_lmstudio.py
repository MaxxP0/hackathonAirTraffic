"""Compare the local LLM with matched baselines and preserve every episode.

Run from the project root: python3 -m examples.evaluate_lmstudio
No live dashboard episode is changed. Model calls are deliberately sequential.
"""
import argparse
import json
from pathlib import Path
import sys
import time

from atc_bench.cli import SCENARIOS, positive_duration, run_episode
from atc_bench import __version__


def write_reports(folder, runs, configuration):
    versions = {run["benchmark_version"] for run in runs}
    if len(versions) > 1:
        raise ValueError("Report one benchmark version at a time")
    version = next(iter(versions), __version__)
    summary = {"benchmark_version": version, "configuration": configuration, "runs": []}
    for run in runs:
        summary["runs"].append({
            "configuration": run["configuration"], "status": run.get("status", "completed"),
            "metrics": run["metrics"], "result_file": run["result_file"],
            "controller": {key: run.get("controller", {}).get(key) for key in
                           ("model", "reasoning_effort", "calls", "errors", "mean_latency_s", "total_latency_s", "usage")},
            "error": run.get("error"),
            "wall_duration_s": run.get("wall_duration_s"),
        })
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    lines = ["# LM Studio evaluation", "",
             f"Benchmark {version}; {configuration['duration_s']} simulated seconds per episode; "
             f"one decision every {configuration['step_seconds']} simulated seconds. "
             "Every controller sees the same scenario/seed for a matched comparison.", "",
             "The simulation clock pauses during model inference. Model latency is wall time and is reported separately. "
             "The LLM receives JSON telemetry and generates its own commands, with no reference fallback. "
             "Controller metadata records whether it uses persistent conversation and plan memory.", "",
             "| Scenario | Seed | Controller | Status | Simulated minutes | Finished / spawned | Collisions | Emergency resolved / pending / failed | Mean queue wait (min) | Mean emergency wait (min) | Score |",
             "|---|---:|---|---|---:|---:|---:|---|---:|---:|---:|"]
    for run in runs:
        c, m = run["configuration"], run["metrics"]
        ground = "—" if m["ground_wait_mean_seconds"] is None else f"{m['ground_wait_mean_seconds']/60:.2f}"
        emergency = "—" if m["emergency_wait_mean_seconds"] is None else f"{m['emergency_wait_mean_seconds']/60:.2f}"
        score = f"{m['score']:.1f}" if run.get("status") != "aborted" else "—"
        elapsed = run["final_observation"]["time_s"] / 60
        lines.append(f"| {c['scenario']} | {c['seed']} | {c['agent']} | {run.get('status', 'completed')} | {elapsed:.1f} | "
                     f"{m['landed']+m['departed']} / {m['spawned']} | {m['collisions']} | "
                     f"{m['emergency_landings']} / {m['emergencies_unresolved']} / {m['emergencies_failed']} | "
                     f"{ground} | {emergency} | {score} |")
    lines += ["", "Ground queue means include unfinished departures. Emergency means include pending and failed flights up to their "
              "observed endpoint; read them with the outcome counts. An early crash/diversion can shorten a raw wait, but incurs a severe "
              "score penalty. A completed episode can still have unfinished aircraft at its fixed horizon. "
              "Aborted rows contain partial diagnostics only: their waits, counts and partial scalar score are not comparable to completed episodes. "
              "These are small-sample synthetic tests, not evidence of operational aviation safety.", "",
              "## LLM inference", ""]
    for run in runs:
        if run["configuration"]["agent"] != "lmstudio":
            continue
        c, model = run["configuration"], run.get("controller", {})
        lines.append(f"- {c['scenario']}, seed {c['seed']}: model `{model.get('model')}`, "
                     f"{model.get('calls', 0)} calls, {model.get('errors', 0)} errors, "
                     f"mean {model.get('mean_latency_s', 0):.2f}s / total {model.get('total_latency_s', 0):.2f}s wall time. "
                     f"[Full result]({run['result_file']}).")
        if run.get("error"):
            lines.append(f"  Aborted: {run['error']}")
    lines += ["", "Full results retain the prompt/version, actual compact observations, commands, command rejections, "
              "model summaries, token counts, safety events, per-aircraft timers and final outcomes. "
              "Scores from benchmark 0.1.0 use different waiting penalties. "
              "Versions 0.2.0 and 0.3.0 use different controller prompts, memory and decoding settings; compare configurations explicitly.", ""]
    (folder / "report.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=["mixed", "emergency", "storm"])
    parser.add_argument("--seeds", nargs="+", type=int, default=[7, 11])
    parser.add_argument("--duration", type=positive_duration, default=1800)
    parser.add_argument("--step-seconds", type=int, choices=range(1, 601), default=120, metavar="1..600")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--llm-timeout", type=float, default=900)
    parser.add_argument("--output", default="results/lmstudio-evaluation")
    args = parser.parse_args()
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    configuration = {"scenarios": args.scenarios, "seeds": args.seeds,
                     "duration_s": args.duration, "step_seconds": args.step_seconds,
                     "requested_model": args.model, "base_url": args.base_url,
                     "time_mode": "paused_during_inference", "max_tokens": args.max_tokens,
                     "llm_timeout_s": args.llm_timeout}
    runs = []
    failures = 0
    # Finish cheap baselines first, then run one model request at a time.
    for agent in ("reference", "noop", "lmstudio"):
        for scenario in args.scenarios:
            for seed in args.seeds:
                print(f"Starting {agent}, {scenario}, seed {seed}", file=sys.stderr, flush=True)
                started = time.monotonic()
                run = run_episode(seed=seed, scenario=scenario, duration=args.duration,
                                  agent_spec=agent, step_seconds=args.step_seconds,
                                  agent_options={"base_url": args.base_url, "model": args.model,
                                                 "max_tokens": args.max_tokens, "timeout_s": args.llm_timeout})
                run["wall_duration_s"] = round(time.monotonic() - started, 3)
                run["result_file"] = f"{agent}-{scenario}-{seed}.json"
                (folder / run["result_file"]).write_text(json.dumps(run, indent=2, allow_nan=False) + "\n")
                runs.append(run)
                write_reports(folder, runs, configuration)
                print(f"Finished {agent}, {scenario}, seed {seed}: {run['metrics']['score']:.1f}, "
                      f"{time.monotonic()-started:.1f}s wall time", file=sys.stderr, flush=True)
                if run.get("status") == "aborted":
                    failures += 1
                    print(f"Model failure; partial evidence saved: {run['error']}. Continuing independent episodes.", file=sys.stderr)
    print(f"Saved {folder / 'report.md'}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
