"""Build the public GLM/Luna comparison from saved episodes, without API calls.

Run from any directory with: python3 docs/evaluations/build_glm_report.py
The source files are local evaluation outputs; this script never loads credentials.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from atc_bench.landing_metrics import landing_metrics

PUBLIC = ROOT / "docs" / "evaluations"
WEB = ROOT / "atc_bench" / "web"
MODEL = "z-ai/glm-5.3-flash"
MODELS = {"glm": MODEL, "luna": "openai/gpt-6-luna"}
REPO = "https://github.com/MaxxP0/hackathonAirTraffic"
SCENARIOS = [
    ("runway_closure", "Runway closure", "glm-closure-v3"),
    ("emergency", "Emergency arrivals", "glm-emergency-v3"),
    ("wind_shift", "Wind shift", "glm-wind-v3"),
]
LABELS = {"glm": "GLM 5.3 Flash", "luna": "GPT 6 Luna", "reference": "Reference policy", "noop": "No commands"}
OMIT = {"reservation_id", "budget_path", "ledger_path", "result_file", "api_key", "authorization", "access_token", "settled_zero_cost_reservations"}


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if key.lower() not in OMIT}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?:/(?:Users|home|private|tmp|var/folders)/|[A-Z]:\\)[^\s\"<>]*", "[local path omitted]", value)
    return value


def dump(path, value):
    text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    # No credential should occur in synthetic records. Fail instead of publishing
    # it if a future adapter accidentally retains one.
    if "sk-or-v1-" in text or "Bearer " in text:
        raise ValueError("Unexpected credential-shaped content in public artifact")
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def aircraft(observation):
    columns = observation.get("aircraft_columns")
    rows = observation.get("aircraft", [])
    return [dict(zip(columns, row)) for row in rows] if columns else rows


def result_rows(step):
    return [{"command": row.get("command"), "accepted": bool(row.get("accepted")), "message": row.get("message")}
            for row in step.get("command_results", [])]


def record_filename(run, cohort, scenario, complete=False):
    prefix = {"glm": "glm-5.3-flash", "luna": "gpt-6-luna"}.get(cohort, cohort)
    failures = sum(step.get("decision", {}).get("status") == "error" for step in run.get("replay", []))
    suffix = "" if complete else f"-interrupted-{int(run['final_observation']['time_s'])}s-{failures}failures"
    return f"{prefix}-{scenario}-seed7{suffix}.json"


def evidence(run, reconciled_ids):
    decisions = [(step, step["decision"]) for step in run.get("replay", []) if isinstance(step.get("decision"), dict)]
    successful = [(step, decision) for step, decision in decisions if decision.get("status") == "ok"]
    latencies = [d["latency_s"] for _, d in decisions if isinstance(d.get("latency_s"), (int, float))]
    costs = [d["cost_usd"] for _, d in decisions if isinstance(d.get("cost_usd"), (int, float))]
    unknown_costs = sum(d.get("cost_usd") is None and d.get("reserved_cost_usd") is not None and d.get("reservation_id") not in reconciled_ids for _, d in decisions)
    reconciled_calls = sum(d.get("cost_usd") is None and d.get("reservation_id") in reconciled_ids for _, d in decisions)
    checked, matched = 0, 0
    previous = None
    for _, decision in successful:
        memory = decision.get("observation", {}).get("controller_memory", {})
        if previous is not None:
            checked += 1
            matched += memory.get("latest_plan") == previous
        previous = decision.get("plan")
    last = successful[-1][1] if successful else {}
    return {
        "decision_count": len(decisions), "successful_decisions": len(successful),
        "mean_call_latency_s": statistics.fmean(latencies) if latencies else None,
        "median_call_latency_s": statistics.median(latencies) if latencies else None,
        "total_call_latency_s": sum(latencies),
        "known_api_cost_usd": sum(costs), "api_cost_usd": sum(costs) if not unknown_costs else None,
        "reported_cost_calls": len(costs), "unknown_cost_calls": unknown_costs, "reconciled_zero_cost_calls": reconciled_calls,
        "failed_attempts": [{"time_s": step["time_s"], "error": d.get("error"), "latency_s": d.get("latency_s")} for step, d in decisions if d.get("status") == "error"],
        "providers": sorted({d["provider_name"] for _, d in decisions if d.get("provider_name")}),
        "provider_routing": [{"time_s": d.get("observation", {}).get("time_s", d.get("time_s")),
                              "sort": d.get("provider_sort", "price"), "provider": d.get("provider_name"),
                              "status": d.get("status")} for _, d in decisions],
        "memory": {"final_input_successful_decisions": last.get("observation", {}).get("controller_memory", {}).get("successful_decisions"),
                   "successful_decisions_counter": run.get("controller", {}).get("successful_decisions"),
                   "final_input_memory_turns": last.get("input_memory_turns"), "final_retained_memory_turns": last.get("memory_turns"),
                   "checked_plan_transitions": checked, "matched_plan_transitions": matched,
                   "last_input_plan": last.get("observation", {}).get("controller_memory", {}).get("latest_plan"),
                   "last_output_plan": last.get("plan")},
    }


def qualitative(run):
    scenario = run["configuration"]["scenario"]
    decisions = [(step, step.get("decision", {})) for step in run["replay"]]
    events = [event for step in run["replay"] for event in step.get("events", [])]
    findings = []
    if scenario == "runway_closure":
        closures = [event for event in events if event.get("type") == "runway_closed"]
        if closures:
            event = closures[0]
            candidates = [(step, d) for step, d in decisions if d.get("observation", {}).get("time_s", -1) >= event["time_s"]]
            if candidates:
                step, decision = candidates[0]
                obs = decision["observation"]
                closed = sorted(key for key, value in obs.get("runway_state", {}).items() if value.get("closed"))
                closed_assignments = []
                for later, d in decisions:
                    shut = {key for key, value in d.get("observation", {}).get("runway_state", {}).items() if value.get("closed")}
                    for result in result_rows(later):
                        bits = str(result["command"]).split()
                        if len(bits) >= 3 and bits[0] in {"APPROACH", "TAKEOFF"} and bits[-1] in shut:
                            closed_assignments.append({"time_s": later["time_s"], **result})
                findings.append({"title": "First decision after the closure", "event_time_s": event["time_s"],
                                 "observation_time_s": obs["time_s"], "closed_runways": closed,
                                 "event": event["message"], "commands": result_rows(step),
                                 "input_plan": obs.get("controller_memory", {}).get("latest_plan"), "output_plan": decision.get("plan"),
                                 "closed_runway_assignments": closed_assignments,
                                 "note": "Closure go-arounds can be initiated automatically by the simulator; they are not proof of a model command."})
    elif scenario == "wind_shift":
        changes = [event for event in events if event.get("type") == "weather" and "direction" in event.get("message", "")]
        if changes:
            event = changes[0]
            candidates = [(step, d) for step, d in decisions if d.get("observation", {}).get("time_s", -1) >= event["time_s"]]
            if candidates:
                step, decision = candidates[0]
                obs = decision["observation"]
                findings.append({"title": "First decision after the wind shift", "event_time_s": event["time_s"],
                                 "observation_time_s": obs["time_s"], "active_direction": obs.get("weather", {}).get("active_direction"),
                                 "event": event["message"], "commands": result_rows(step),
                                 "input_plan": obs.get("controller_memory", {}).get("latest_plan"), "output_plan": decision.get("plan"),
                                 "note": "Accepted new-flow clearances show a command response; separation and throughput outcomes still determine its quality."})
    elif scenario == "emergency":
        for event in events:
            if event.get("type") != "emergency":
                continue
            for callsign in event.get("callsigns", []):
                candidates = [(step, d) for step, d in decisions if any(a.get("callsign") == callsign and a.get("emergency")
                             for a in aircraft(d.get("observation", {})))]
                final = next((a for a in run["final_observation"]["aircraft"] if a["callsign"] == callsign), {})
                if candidates:
                    step, decision = candidates[0]
                    findings.append({"title": f"Emergency response: {callsign}", "event_time_s": event["time_s"],
                                     "observation_time_s": decision["observation"]["time_s"], "event": event["message"],
                                     "commands": [r for r in result_rows(step) if callsign in str(r["command"]).split()],
                                     "final_status": final.get("status"), "emergency_wait_s": final.get("emergency_wait_s"),
                                     "input_plan": decision["observation"].get("controller_memory", {}).get("latest_plan"),
                                     "output_plan": decision.get("plan"),
                                     "note": "This is the first model observation containing the declared emergency. Simulated emergency wait is measured from declaration to touchdown or terminal failure, not API latency."})
    return findings


def build():
    scenarios, records = [], []
    reconciliation_source = ROOT / "results" / "openrouter-reconciliation.json"
    reconciliation = json.loads(reconciliation_source.read_text()) if reconciliation_source.exists() else {}
    reconciled_ids = set(reconciliation.get("settled_zero_cost_reservations", []))
    if reconciliation:
        dump(PUBLIC / "billing-reconciliation.json", {
            "time_unix": reconciliation["time_unix"], "provider_usage_usd": reconciliation["provider_usage_usd"],
            "confirmed_cost_usd": reconciliation["confirmed_cost_usd"], "reconciled_zero_cost_requests": len(reconciled_ids),
            "evidence": f"After all requests had finished, provider account-key usage equaled the sum of confirmed charges. {len(reconciled_ids)} interrupted requests were reconciled at zero cost; historical decision records still retain their original null cost.",
            "scope": "Account total at reconciliation, including two dashboard calls; this is not the three-episode subtotal. Reservation identifiers are omitted."})

    def publish(source, filename, status):
        run = json.loads(source.read_text())
        public = clean(deepcopy(run))
        public["landing_metrics"] = landing_metrics(run["final_observation"])
        public["publication_note"] = "Recorded synthetic simulation. Local paths, credential fields and budget reservation identifiers are omitted; commands, plans, observations, outcomes and measured costs are retained."
        public["publication_note"] += " Supplemental landing_metrics is derived from the recorded final arrival airborne_time_s values; the original scalar score and rank are unchanged."
        sha = dump(PUBLIC / filename, public)
        records.append({"file": filename, "sha256": sha, "source": str(source.relative_to(ROOT)), "status": status})
        return run, sha

    for key, title, folder in SCENARIOS:
        rows = []
        for cohort in ("glm", "luna", "reference", "noop"):
            agent = "openrouter" if cohort in MODELS else cohort
            original = ROOT / "results" / folder / f"{agent}-{key}-7.json"
            if cohort == "glm":
                resumed = ROOT / "results" / "glm-completed-v3" / f"openrouter-{key}-7.json"
                checkpoints = list(resumed.parent.glob(f"openrouter-{key}-7-interrupted-*s.json"))
                source = resumed if resumed.exists() else max(
                    [p for p in [original, *checkpoints] if p.exists()],
                    key=lambda p: json.loads(p.read_text())["final_observation"]["time_s"], default=original)
            elif cohort == "luna":
                source = ROOT / "results" / "luna-v3" / f"openrouter-{key}-7.json"
            else:
                source = original
            if not source.exists():
                rows.append({"agent": agent, "cohort": cohort, "label": LABELS[cohort], "status": "pending", "metrics": None})
                continue
            raw = json.loads(source.read_text())
            config = raw["configuration"]
            assert (config["seed"], config["scenario"], config["duration_s"], config["step_seconds"]) == (7, key, 1800, 120)
            complete = raw.get("status", "completed") == "completed" and raw["final_observation"].get("done") and raw["final_observation"]["time_s"] == 1800
            filename = record_filename(raw, cohort, key, complete)
            run, sha = publish(source, filename, "completed" if complete else "partial")
            row = {"agent": agent, "cohort": cohort, "label": LABELS[cohort], "status": "completed" if complete else "partial",
                   "configuration": config, "final_time_s": run["final_observation"]["time_s"],
                   "metrics": run["metrics"] if complete else None,
                   "landing_metrics": landing_metrics(run["final_observation"]) if complete else None,
                   "wall_duration_s": run.get("wall_duration_s"), "error": run.get("error"),
                   "record_file": filename, "record_url": f"{REPO}/blob/main/docs/evaluations/{filename}",
                   "record_sha256": sha, "resume_info": clean(run.get("resume_info")),
                   "previous_resumes": clean(run.get("previous_resumes", [])),
                   "initial_world_sha256": hashlib.sha256(json.dumps(run["initial_observation"], sort_keys=True).encode()).hexdigest()}
            if cohort in MODELS:
                assert run.get("controller", {}).get("model") == MODELS[cohort]
                row.update(evidence(run, reconciled_ids))
                row["findings"] = qualitative(run)
                row["prompt_version"] = run.get("controller", {}).get("prompt_version")
                row["model"] = MODELS[cohort]
                row["settings"] = {k: run.get("controller", {}).get(k) for k in
                                   ("temperature", "max_tokens", "reasoning_effort", "timeout_s", "provider_sort", "prompt_sha256")}
                row["interruption_records"] = []
                if cohort == "glm":
                    for checkpoint in [original, *sorted(checkpoints)]:
                        if checkpoint == source or not checkpoint.exists():
                            continue
                        prior = json.loads(checkpoint.read_text())
                        at = prior["final_observation"]["time_s"]
                        interrupted = record_filename(prior, cohort, key)
                        if interrupted == filename:
                            continue
                        publish(checkpoint, interrupted, "interrupted_before_resume")
                        row["interruption_records"].append({"file": interrupted, "time_s": at,
                            "url": f"{REPO}/blob/main/docs/evaluations/{interrupted}"})
            rows.append(row)
        assert len({r["initial_world_sha256"] for r in rows if r.get("initial_world_sha256")}) == 1, f"Initial world differs in {key}"
        scenarios.append({"id": key, "title": title, "runs": rows})
    aggregates = {}
    for cohort in MODELS:
        runs = [next(r for r in s["runs"] if r["cohort"] == cohort) for s in scenarios]
        completed = [r for r in runs if r["status"] == "completed"]
        aggregates[cohort] = {
            "label": LABELS[cohort], "model": MODELS[cohort], "completed_episodes": len(completed),
            "completed_flights": sum(r["metrics"]["landed"] + r["metrics"]["departed"] for r in completed),
            "spawned": sum(r["metrics"]["spawned"] for r in completed),
            "collisions": sum(r["metrics"]["collisions"] for r in completed),
            "crashes": sum(r["metrics"]["crashed"] for r in completed),
            "failed_emergencies": sum(r["metrics"]["emergencies_failed"] for r in completed),
            "separation_losses": sum(r["metrics"]["separation_losses"] for r in completed),
            "separation_loss_seconds": sum(r["metrics"]["separation_loss_seconds"] for r in completed),
            "known_api_cost_usd": sum(r.get("known_api_cost_usd", 0) for r in runs),
            "unknown_cost_calls": sum(r.get("unknown_cost_calls", 0) for r in runs)}
    observations = []
    for cohort in MODELS:
        pairs = [(next(r for r in scenario["runs"] if r["cohort"] == cohort),
                  next(r for r in scenario["runs"] if r["cohort"] == "reference")) for scenario in scenarios]
        pairs = [(model, reference) for model, reference in pairs if model["status"] == "completed"]
        if not pairs:
            continue
        flights = [sum(r["metrics"]["landed"] + r["metrics"]["departed"] for r in side) for side in zip(*pairs)]
        separation = [sum(r["metrics"]["separation_loss_seconds"] for r in side) for side in zip(*pairs)]
        observations.append(f"Across {len(pairs)} completed scenarios, {LABELS[cohort]} completed {flights[0]} flights versus {flights[1]} for the matched reference policy, "
                            f"with {separation[0]:,.0f} versus {separation[1]:,.0f} separation-loss pair-seconds. Read individual safety and service outcomes below.")
        for model, reference in pairs:
            if model["configuration"]["scenario"] == "emergency":
                m, ref = model["metrics"], reference["metrics"]
                observations.append(f"In the emergency episode, {LABELS[cohort]} resolved {m['emergency_landings']} emergency landings with {m['emergencies_failed']} failures; "
                                    f"mean emergency wait was {m['emergency_wait_mean_seconds']:.1f}s versus reference {ref['emergency_wait_mean_seconds']:.1f}s. "
                                    f"Separation loss was {m['separation_loss_seconds']:,.0f} versus {ref['separation_loss_seconds']:,.0f} pair-seconds, "
                                    f"and mean departure queue wait {m['ground_wait_mean_seconds']:.1f}s versus {ref['ground_wait_mean_seconds']:.1f}s.")
            failed = model["landing_metrics"]["landing_wait_by_outcome"]["failed"]["count"]
            if failed:
                observations.append(f"{LABELS[cohort]}'s {model['configuration']['scenario'].replace('_', ' ')} episode ended with {failed} failed arrival outcome(s), "
                                    "including diversions or crashes. Its landing-wait mean includes those flights and cannot by itself demonstrate better landing service.")
    report = {
        "title": "GLM 5.3 Flash and GPT 6 Luna — matched ATC episodes", "date": "2026-09-26", "models": MODELS,
        "benchmark_version": "0.3.0", "seed": 7, "duration_s": 1800, "decision_interval_s": 120,
        "status": "completed" if all(a["completed_episodes"] == 3 for a in aggregates.values()) else "incomplete",
        "conditions": ["One seed (7), three synthetic scenarios, one trajectory per model/scenario; no uncertainty estimate or claim of general superiority.",
                       "All controllers use the same 30-minute horizon and 120-second control windows.",
                       "The simulation pauses during inference and advances immediately afterward; API latency is separate from simulated aircraft waiting.",
                       "Both models use low reasoning effort, an 8,192-token output limit, four recent dialogue exchanges and a persistent operational plan. GLM uses temperature 0; Luna omits temperature because its provider catalog does not support that parameter.",
                       "The three original GLM processes ran concurrently alongside two dashboard calls, sharing a provider budget and default prompt caching. All stopped after HTTP 429 interruptions at 1,440 simulated seconds. Only closure resumed from saved state and memory, reaching 1,680 seconds before further connection failures; emergency and wind were never resumed. GPT 6 Luna ran sequentially while GLM remained interrupted. Wall times are not an isolated throughput benchmark.",
                       "Active wall time includes initial execution plus continuation/backoff, but excludes the manual checkpoint gap. Summed call latency includes failed controller attempts and internal request retries/backoff where recorded. Resumed runs retain their interruption in the replay.",
                       "Per-run confirmed API cost sums replay decision.cost_usd; other tests/dashboard calls are excluded. Three original GLM HTTP 429 charges were reconciled at zero using provider-account usage; their historical records retain null cost.",
                       "OpenRouter routing changed from price preference to throughput preference only for the GLM closure continuation at 1,680 seconds; emergency and wind retained their original price preference. Luna used throughput preference. The GLM model, prompt and provider price caps were retained. Later timeout costs remain unconfirmed unless independently reconciled. Routing and interruptions confound wall-time comparisons.",
                       "Partial or failed runs receive no completed score or waiting-time comparison; their raw records remain available.",
                       "Landing wait is arrival sector entry to touchdown, including normal approach flight, holding and go-arounds. Pending arrivals retain elapsed time at the horizon; failed diversions count from the diversion command but accrue airborne time until exit. Mean/max and score include all spawned arrivals. Landed outcome means touchdown, before rollout/taxi are complete. The supplemental score is 100/(1 + mean_seconds/600) and does not change the benchmark score or rank; read it with landed/pending/failed counts.",
                       "Doing nothing can produce few separation losses while completing no flights. Read safety, completions, emergency outcomes and waiting together."],
        "scenarios": scenarios, "aggregates": aggregates, "observations": observations,
        "billing_reconciliation_url": f"{REPO}/blob/main/docs/evaluations/billing-reconciliation.json" if reconciliation else None}
    dump(WEB / "benchmark-results.json", report)
    dump(PUBLIC / "manifest.json", {"models": MODELS, "records": records})
    published = {record["file"] for record in records}
    for pattern in ("glm-5.3-flash-*-seed7*.json", "gpt-6-luna-*-seed7*.json"):
        for path in PUBLIC.glob(pattern):
            if path.name not in published:
                path.unlink()
    markdown(report)
    print(f"Published {len(records)} records; completed episodes: " + ", ".join(f"{cohort} {a['completed_episodes']}/3" for cohort, a in aggregates.items()))

def number(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}"


def stamp(seconds):
    return f"{int(seconds)//60:02d}:{int(seconds)%60:02d}"


def markdown(report):
    text = ["# GLM 5.3 Flash and GPT 6 Luna — 26 September 2026", "",
            "Completed episodes: " + "; ".join(f"**{a['label']}: {a['completed_episodes']} / 3**" for a in report["aggregates"].values()) + ". "
            "This is a small matched comparison, not a safety certification or a general model ranking.", "",
            "[Interactive comparison](../atc_bench/web/benchmark-results.html) · [Event videos](../atc_bench/web/replays.html) · [Public record manifest](evaluations/manifest.json)", "", "## Measurement conditions", ""]
    if report["observations"]:
        text[-2:] = ["## Observed tradeoffs", ""]
        text.extend(f"- {observation}" for observation in report["observations"])
        text.extend(["", "## Measurement conditions", ""])
    text.extend(f"- {condition}" for condition in report["conditions"])
    text.extend(["- The reference-policy event demonstration videos use 10-second control windows. Their video statistics are separate from this matched 120-second comparison; they are not substituted for the reference rows here."])
    text.extend(["", "## Completed-episode outcomes", "",
                 "Ground wait means the departure queue only. Emergency wait includes resolved, failed and pending emergencies. All wait and separation durations below are simulated time. An em dash means no applicable event or no completed result.", ""])
    for scenario in report["scenarios"]:
        text.extend([f"### {scenario['title']}", "", "| Controller | Completed / spawned | Score | Queue mean / max, min | Emergency mean / max, min | Resolved / failed / pending emergencies | Collisions / crashes | Separation events / pair-seconds | Rejected commands |",
                     "|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for row in scenario["runs"]:
            m = row.get("metrics")
            if not m:
                text.append(f"| {row['label']} ({row['status']}) | — | — | — | — | — | — | — | — |")
                continue
            q = f"{number(m.get('ground_wait_mean_seconds', 0)/60 if m.get('ground_wait_mean_seconds') is not None else None,2)} / {number(m.get('ground_wait_max_seconds',0)/60 if m.get('ground_wait_max_seconds') is not None else None,2)}"
            e = f"{number(m.get('emergency_wait_mean_seconds',0)/60 if m.get('emergency_wait_mean_seconds') is not None else None,2)} / {number(m.get('emergency_wait_max_seconds',0)/60 if m.get('emergency_wait_max_seconds') is not None else None,2)}"
            outcomes = m.get("emergency_wait_by_outcome", {})
            eo = " / ".join(str(outcomes.get(k, {}).get("count", 0)) for k in ("resolved", "failed", "pending"))
            text.append(f"| {row['label']} | {m['landed']+m['departed']} / {m['spawned']} | {number(m['score'])} | {q} | {e} | {eo} | {m['collisions']} / {m['crashed']} | {m['separation_losses']} / {number(m['separation_loss_seconds'],0)} | {m['invalid_commands']} |")
        text.append("")
        text.extend(["Landing service diagnostic (arrival sector entry to touchdown, including normal approach time):", "",
                     "| Controller | Mean / max landing wait, min | Diagnostic score / 100 | Landed / pending / failed arrivals |",
                     "|---|---:|---:|---:|"])
        for row in scenario["runs"]:
            lm = row.get("landing_metrics")
            if lm is None:
                text.append(f"| {row['label']} ({row['status']}) | — | — | — |")
                continue
            counts = " / ".join(str(lm["landing_wait_by_outcome"][key]["count"]) for key in ("landed", "pending", "failed"))
            text.append(f"| {row['label']} | {number(lm['landing_wait_mean_seconds']/60 if lm['landing_wait_mean_seconds'] is not None else None,2)} / "
                        f"{number(lm['landing_wait_max_seconds']/60 if lm['landing_wait_max_seconds'] is not None else None,2)} | {number(lm['landing_wait_score'])} | {counts} |")
        text.extend(["", "All spawned arrivals contribute, including pending and failed arrivals. A landed outcome starts at touchdown; completed flights above require the later stand arrival. "
                     "Wait score = 100 / (1 + mean seconds / 600); it is supplemental and does not change the benchmark score or rank. "
                     "A short failed flight can reduce mean wait, so read this diagnostic with outcome counts.", ""])
        for model in [r for r in scenario["runs"] if r["cohort"] in MODELS]:
            text.extend([f"#### {model['label']}: execution and response evidence", ""])
            if model["status"] == "pending":
                text.extend(["Run not yet available; no comparison is published.", ""])
                continue
            cost = f"${model['known_api_cost_usd']:.6f} confirmed"
            if model.get("unknown_cost_calls"):
                cost += f"; {model['unknown_cost_calls']} request cost(s) remain unconfirmed"
            text.extend([f"{model['successful_decisions']} successful decisions from {model['decision_count']} recorded attempts; {cost}. "
                         f"Mean / median call latency: {number(model['mean_call_latency_s'],3)} / {number(model['median_call_latency_s'],3)} seconds. "
                         f"Summed API latency: {number(model['total_call_latency_s']/60,2)} minutes. "
                         f"Active episode wall time: {number(model.get('wall_duration_s',0)/60 if model.get('wall_duration_s') is not None else None,2)} minutes; "
                         f"simulation reached {number(model['final_time_s']/60,0)} / 30 minutes.", ""])
            if model.get("resume_info"):
                text.extend([f"Resumed from T+{stamp(model['resume_info']['resumed_at_sim_s'])}; prior observations, commands and plan memory were restored. Manual checkpoint waiting is excluded from active wall time.", ""])
            routing = model.get("provider_routing", [])
            route_changes = [(r["time_s"], r["sort"]) for i, r in enumerate(routing)
                             if i == 0 or r["sort"] != routing[i-1]["sort"]]
            text.extend(["Provider routing: " + "; ".join(f"{sort} preference from T+{stamp(at)}" for at, sort in route_changes) + ". "
                         "Recorded providers: " + (", ".join(model.get("providers", [])) or "not reported") + ".", ""])
            if model.get("failed_attempts"):
                text.extend(["Recorded interruptions: " + "; ".join(f"T+{stamp(f['time_s'])}: {f.get('error')}" for f in model["failed_attempts"]) + ".", ""])
            if model.get("reconciled_zero_cost_calls"):
                text.extend([f"{model['reconciled_zero_cost_calls']} interrupted request(s) reconciled at zero cost; [billing evidence](evaluations/billing-reconciliation.json).", ""])
            for finding in model.get("findings", []):
                text.append(f"**{finding['title']}.** Event at T+{stamp(finding['event_time_s'])}; first model observation at T+{stamp(finding['observation_time_s'])}.")
                if "closed_runways" in finding:
                    text.append(f"The observation marked {', '.join(finding['closed_runways'])} closed. {len(finding['closed_runway_assignments'])} approach/takeoff assignments targeted an observed closed runway during closure observations.")
                if "active_direction" in finding:
                    text.append(f"The observation showed active flow {finding['active_direction']}.")
                if "final_status" in finding:
                    text.append(f"Final status: {finding['final_status']}; emergency wait: {number(finding.get('emergency_wait_s'),0)} simulated seconds.")
                text.append("")
                text.extend(f"- `{' '.join(r['command'].split()) if isinstance(r['command'],str) else json.dumps(r['command'])}` — {'accepted' if r['accepted'] else 'rejected'}: {r.get('message','')}" for r in finding["commands"])
                if not finding["commands"]:
                    text.append("No command for this emergency aircraft was issued in that first observed decision.")
                text.extend(["", finding["note"], ""])
            memory = model["memory"]
            text.extend([f"Memory evidence: {memory['matched_plan_transitions']} / {memory['checked_plan_transitions']} consecutive successful decision inputs contain the exact previous output plan; "
                         f"the final input reports {memory['final_input_successful_decisions']} prior successful decisions and {memory['final_input_memory_turns']} retained dialogue turns. "
                         f"The final controller counter is {memory['successful_decisions_counter']}. This verifies persisted inputs, not the quality of the plan.", "",
                         "<details><summary>Final recorded operational plan</summary>", "", str(memory.get("last_output_plan") or "No successful plan"), "", "</details>", ""])
            for interrupted in model.get("interruption_records", []):
                text.extend([f"[Interruption record at T+{stamp(interrupted['time_s'])}](evaluations/{interrupted['file']})", ""])
        text.append("Raw records: " + " · ".join(f"[{r['label']}](evaluations/{r['record_file']})" for r in scenario["runs"] if r.get("record_file")))
        text.append("")
    text.extend(["## Provenance and limits", "", "Published JSON files retain full synthetic initial/final observations, compact model observations, command results, plans, latencies and per-call costs. Local machine paths and budget reservation IDs are removed. SHA-256 hashes are in the manifest. Regenerate this report from the saved outputs with `python3 docs/evaluations/build_glm_report.py`.", "", "Unexpected closures, wind changes and emergency declarations are hidden until they occur. The airport, dynamics, wake handling and ground queues are simplified. Efficiency scores must never substitute for safety outcomes. More seeds, traffic densities and model replicates are needed before broader conclusions.", ""])
    (ROOT / "docs" / "GLM_RESULTS.md").write_text("\n".join(text), encoding="utf-8")


if __name__ == "__main__":
    build()
