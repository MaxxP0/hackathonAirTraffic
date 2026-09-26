# Implementation contract

Phase 1: deterministic command-driven airborne and runway control at a simplified EDDF airport. Python standard library only. The optional real LLM controller uses an already loaded model on LM Studio's local server. Detailed taxi routes/gates deferred. Coordinates are local nautical miles, x east/y north; headings clockwise from north; altitude feet AGL; speed knots; time simulated seconds unless a field explicitly measures model latency.

## Python API

`from atc_bench import AirTrafficEnv`

`AirTrafficEnv(seed=7, scenario="mixed", duration_s=1800)`

`env.observe()` returns a JSON-serializable observation. `env.step(commands=[], seconds=10)` executes commands sequentially and advances the simulation in <=1-second substeps, returning an observation including `command_results`. `env.reset(seed=None, scenario=None)` resets the episode. `env.metrics()` returns cumulative metrics. `env.done` is a boolean. Supported scenarios: mixed, rush_hour, low_visibility, storm, emergency, wind_shift. `env.time_s` and `env.duration_s` are accessible. `env.aircraft` contains mutable aircraft dataclasses; `env.runways` contains runway dataclasses.

Command strings: `HEADING DLH101 250`, `ALTITUDE DLH101 5000`, `SPEED DLH101 210`, `DIRECT DLH101 NORTH` (or `DIRECT DLH101 10 15`), `HOLD DLH101`, `APPROACH DLH101 25R`, `TAKEOFF DLH102 25C`, `GO_AROUND DLH101`, `DIVERT DLH101`. Dictionary equivalent: `{"action":"heading","callsign":"DLH101","heading":250}`; altitude/altitude, speed/speed, direct/fix or x/y, approach/runway, takeoff/runway. No-op: empty list. Invalid commands produce rejected results without aborting a batch or advancing extra time. `seconds` is an integer 0–60; booleans are rejected. Maximum 100 commands/step. Hidden future traffic, weather and emergency schedules are not exposed.

Observation fields:

- `time_s`, `duration_s`, `seed`, `scenario`, `done`.
- `airport`: `{icao:"EDDF",name,elevation_ft:328,radius_nm:40,fixes:{NORTH:[0,22],EAST:[24,0],SOUTH:[0,-22],WEST:[-24,0]},runways:[{id,physical_id,heading_deg,length_m,threshold:[x,y],end:[x,y],arrival,departure,occupied_by,available_in_s,closed,approach_fix:[x,y]}]}`. Direction IDs: 25R/07L (NW), 25C/07C (CENTER), 25L/07R (SOUTH), 18 (WEST). Reciprocal ends share occupancy/wake. Coordinates are approximate. Availability includes wake time but does not guarantee aircraft/weather suitability.
- `weather`: `{wind_from_deg,wind_speed_kt,gust_kt,visibility_m,ceiling_ft,precipitation,active_direction:"25"|"07",cells:[{id,x_nm,y_nm,radius_nm,severity}],description}`.
- `aircraft`: active and finished aircraft `{callsign,type,wake,kind:"arrival"|"departure",status,x_nm,y_nm,altitude_ft,heading_deg,speed_kt,target_altitude_ft,target_heading_deg,target_speed_kt,runway:null|id,fuel_s,emergency:null|{kind,deadline_s},spawn_time_s,ground_time_s,ground_wait_s,airborne_time_s,emergency_wait_s,emergency_declared_time_s,emergency_touchdown_time_s,approach_stage:null|"intercept"|"final",min_speed_kt,max_speed_kt,landing_distance_m,takeoff_distance_m,crosswind_limit_kt,history:[[x,y],...]}`. Statuses: inbound, holding, approach, landing_roll, taxi_in, ground, takeoff_roll, outbound, diverting, landed, departed, diverted, crashed. Emergency `deadline_s` is absolute simulation time. Declaration/touchdown timestamps are `null` until the corresponding public event.
- `metrics`: `{score,landed,departed,diverted,crashed,collisions,separation_losses,separation_loss_seconds,runway_incursions,wake_violations,weather_exposure_seconds,emergency_landings,emergencies_failed,emergencies_unresolved,ground_delay_seconds,airborne_seconds,invalid_commands,unfinished,spawned,completion_rate,safety_violations,rank_key}`, plus waiting fields below. The capped scalar efficiency term cannot offset a collision, crash or failed emergency. The separate lexicographic rank retains its original ten fields.
- `events`: most recent <=100 `{time_s,type,message,callsigns:[]}`.
- `conflicts`: current `{callsigns:[a,b],distance_nm,vertical_ft}` entries.
- `command_results`: last batch `{command,accepted,message}` entries.

Waiting metrics use two prefixes, `ground_wait` and `emergency_wait`. Each has `*_seconds`, `*_count`, `*_mean_seconds`, `*_max_seconds`, `*_score`, and `*_score_component`. Ground wait counts all spawned departures, including zero-wait and unfinished departures, and accumulates only in status `ground`. Emergency wait counts all announced emergencies and accumulates from declaration to touchdown, crash, diversion or current horizon; a missed deadline records failure while elapsed response time continues until an endpoint. Empty means/maxima/diagnostic scores are `null`, totals/counts are zero. `emergency_wait_by_outcome` maps each of `resolved`, `pending`, `failed` to `{count,seconds,mean_seconds,max_seconds}`. These disjoint outcome groups include failed emergencies that subsequently touch down.

`ground_wait_score = 100 / (1 + ground_wait_mean_seconds / 300)`; `emergency_wait_score = 100 / (1 + emergency_wait_mean_seconds / 180)`. These are delay diagnostics, not substitutes for safety/outcome ranking. Signed scalar contributions are `ground_wait_score_component = -ground_wait_seconds / 60` and `emergency_wait_score_component = -5 * emergency_wait_seconds / 60`. Version 0.2.0 adds these contributions, so scalar scores cannot be directly compared with version 0.1. The rank schema is unchanged. See [SCORING.md](SCORING.md) for censoring and exact time-accounting semantics.

## Agents and LM Studio

`atc_bench.agents.ReferenceAgent.act(observation) -> list[str]` and
`NoOpAgent.act(observation)` receive observations only, never environment
internals. The reference controller is a heuristic baseline, not an LLM or an
optimal controller.

`LMStudioAgent(base_url="http://127.0.0.1:1234", model=None, timeout_s=900,
max_tokens=8192, temperature=0, max_memory_turns=4).act(observation) -> list[str]`
is defined in [atc_bench/lmstudio.py](../atc_bench/lmstudio.py). A base URL ending
in `/v1` is also accepted. Discovery uses `/api/v1/models`, falling back to
`/api/v0/models` for compatibility, and selects an already loaded language model.
An explicit model must match a loaded instance ID or model key; otherwise the
first loaded instance by sorted ID is selected. No model is downloaded, loaded
or unloaded by the adapter.

Requests to `/v1/chat/completions` contain the system prompt, up to four recent
successful user/assistant exchanges, and a fresh compact observation as the
latest user message. The observation also includes `controller_memory` with
`latest_plan`, `successful_decisions` and `prior_decision_error`. Successful
responses update the public operational plan and bounded dialogue. Errors retain
the prior plan/history. A new instance or episode resets the conversation.

The strict response shape is `{commands:[string,...],summary:string,plan:string}`:
at most 32 commands of 1–80 characters, a summary of at most 240 characters and a
nonempty plan of at most 1,200 characters. The plan describes intended sequencing,
priorities and reconsideration conditions. Private reasoning text is not copied
into the dialogue or decision record. The model does not choose the time step;
the harness supplies `decision_interval_s` in the observation.

The adapter does not override `reasoning_effort`. Metadata records
`reasoning_mode:"model_default"` and `advertised_reasoning_default`; the locally
observed Qwen model advertises `on`. Actual `reasoning_tokens` are retained when
the response exposes them. Defaults are 8,192 output tokens and 900 wall-clock
seconds per request. The model supplies every LLM command; no reference fallback
is used.

`last_decision` contains status, model, simulation time, commands, summary, plan,
compact observation, usage, wall-clock latency, `memory_turns` and
`input_memory_turns`; it also includes finish reason, reasoning metadata or error
when available. `metadata()` includes model inventory, prompt text/version/hash,
response schema, decoding settings, history policy, latest plan, calls/errors,
latency totals/mean and token usage. Current prompt version is
`frankfurt-controller-v3`.

The unchanged `compact-v2` packing uses `aircraft_columns` and dense aircraft
rows, with type performance limits in `aircraft_types`. Static runway geometry
stays in `airport.runways`; `runway_state` contains current occupancy,
availability and closure keyed by runway ID. All active-aircraft values except
radar trails are preserved at original precision. Input omits finished aircraft,
event logs and nested metric reports; selected scalar outcomes and rejection
feedback remain. See [the wire-format reference](AGENT_PROMPT.md#compact-v2-wire-format).
Full environment, radar, HTTP and JSON-lines observations retain aircraft objects.

## Decision windows and timing

Simulation time is **paused during model inference**. After a valid decision,
the harness applies its commands once, then quickly advances the requested
control window. Default high-level window: 120 simulated seconds; accepted CLI
windows: 1–600; HTTP also permits zero for commands without time advance. The
low-level `env.step` contract remains 0–60 seconds, so longer windows are split
into supported chunks with empty commands after the first chunk. Movement and
safety are still evaluated in substeps of at most one second. A final window is
clipped to the remaining horizon. Command rejection feedback from the
command-bearing chunk must remain visible in the returned observation.

Longer windows require fewer model calls, but also reduce the frequency of
control opportunities. The model receives `decision_interval_s` and plans for
that upcoming window. Automatic turns have no artificial real-time delay between
responses. Measured wall time still depends on model latency; simulation batching
does not guarantee faster-than-real-time total execution.

Ground wait, emergency wait, fuel and weather evolve only during simulated time
advance. Model `latency_s` is measured in wall-clock seconds and contributes to
neither waiting score. Invalid JSON, truncation, unavailable models, HTTP errors
or timeouts raise `LMStudioError` before time advances. Valid decisions pass
through normal per-command validation; rejected commands do not invalidate the
rest of the batch. The last valid state and prior operational plan survive a
model failure.

## HTTP

`python3 -m atc_bench serve --port 8000 --agent lmstudio` starts the localhost
standard-library HTTP server. Without `--agent`, the default is `reference`.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/state` | Full current observation plus controller status; remains readable during inference. |
| `GET /api/scenarios` | `{scenarios:[...]}`. |
| `POST /api/controller` | `{kind:"reference" or "lmstudio",base_url?:string,model?:string or null}` selects the controller. Omitted options retain settings; null/empty model enables discovery. The replacement starts fresh dialogue. |
| `POST /api/step` | `{commands:[],seconds:120,autopilot:false}` applies commands and advances 0–600 simulated seconds. `autopilot:true` first requests a decision from the selected controller. Manual commands override automatic commands for the same callsign. |
| `POST /api/reset` | `{seed:7,scenario:"mixed",duration_s:1800}` replaces the episode and clears controller history while retaining kind/settings. |

Controller state includes its kind/model/base URL, status, message, decision
count, last decision and error. Status distinguishes idle, thinking, ready and
error. The last decision includes the model's public plan. Discovery occurs on
the first model call. All POST bodies use `Content-Type: application/json`.
Invalid input returns HTTP 400; model failures return a visible JSON error
without advancing time. Concurrent conflicting mutations during inference are
rejected rather than interleaved. Static radar assets are served at `/`.

The radar starts paused, defaults to a 120-second control window, and offers
30/60/120/300/600-second choices. Run/pause controls automatic decision cycles;
single step requests one cycle. It shows weather, aircraft, scores, events,
command results and controller decisions/plans. This remains a mock airport
with abstract ground queues.

## CLI, results and reproducibility

```sh
python3 -m atc_bench run --agent lmstudio --scenario mixed --seed 7 --duration 1800 --step-seconds 120 --output results/run.json
python3 -m atc_bench benchmark --agent lmstudio --seeds 1 2 3 --scenarios mixed emergency --duration 1800 --step-seconds 120 --output results/benchmark.json
```

`run`/`benchmark` support `--agent reference|noop|lmstudio|module:Class`; custom
classes are zero-argument local Python code exposing `act(observation)`. High-level
`--step-seconds` accepts 1–600 and defaults to 120. `run`, `benchmark` and `serve`
accept `--base-url` (localhost:1234), `--model` (automatic loaded-model discovery),
`--llm-timeout` (900 seconds) and `--max-tokens` (8192). The simulation advances
as fast as its computation allows between model calls.

Results retain version/configuration, metrics, rank, initial/final observations
and command/event replay. LLM results additionally retain model/prompt/reasoning
metadata, compact observations, summaries/plans, simulation timestamps, latency
and usage. Model errors produce a partial/aborted run, its failed decision and a
visible error; they are not completed episodes. Benchmark means use completed
runs only and skip null diagnostics. `metric_sample_counts` records each mean's
population, and completed/aborted counts distinguish the two sets. With no
completed runs the means are empty and mean rank is null.

Use equal scenario, seed, horizon and control window when comparing agents, and
report both simulated horizon and measured `wall_duration_s`. The simulator is
deterministic for a given command timeline; model outputs need not be identical
on repeated requests. Replay saved commands at their simulation timestamps to
reproduce a trajectory. Version 0.3 adds persistent planning, model-default
reasoning and larger configurable windows. Version 0.2 stateless-controller
results remain legacy evidence, not validation of this controller; match actual
settings before comparisons. The scoring formula is unchanged from version 0.2.

`stdio --seed 7 --scenario mixed` emits an initial observation and then one JSON
response per JSON-line request `{commands:[],seconds:10}` or
`{reset:{seed:8,scenario:"storm"}}`. This low-level bridge accepts 0–60 seconds
and does not call a model. External harnesses implementing longer control windows
must apply commands once, advance subsequent chunks with empty commands, and
retain command rejection feedback. Standard output is reserved for the protocol.
