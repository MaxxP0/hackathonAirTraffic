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

`atc_bench.agents.ReferenceAgent.act(observation) -> list[str]`, `NoOpAgent.act(observation)`. Agents receive observations only, never environment internals. ReferenceAgent prioritizes emergencies, arrivals by fuel, and the departure queue using suitable runways; it is a heuristic performance baseline, not an LLM or optimal controller.

`atc_bench.lmstudio.LMStudioAgent(base_url="http://127.0.0.1:1234", model=None, timeout_s=120, max_tokens=1024, temperature=0).act(observation) -> list[str]`. Base URLs ending in `/v1` are also accepted. The adapter discovers already loaded language models from `/api/v1/models`, with `/api/v0/models` as a compatibility fallback. If `model` is supplied it must match a loaded instance ID or model key; otherwise discovery selects the first loaded instance by sorted ID. It never downloads, loads or unloads models.

Every decision sends a fresh system prompt and compact JSON observation as text to `/v1/chat/completions`, with temperature 0 by default, non-streaming output and a strict JSON response schema `{commands:[string,...],summary:string}`. The runner, not the model, sets the simulation interval. Compact input keeps current airport/weather/conflicts, unfinished aircraft with performance and waiting fields, scalar metrics and `prior_command_errors`. It excludes radar trails, finished aircraft, repeated event logs and nested metrics; aggregate outcomes remain in scalar metrics. There is no image/UI input or prior chat transcript. Prompt text/version/hash and observation format are recorded in run metadata. If the loaded model advertises a reasoning-off capability, requests include `reasoning_effort:"none"`; this request-local option is recorded and does not alter saved LM Studio settings.

The model supplies every LLM command; the reference controller is never a fallback. Invalid response shape, missing/truncated JSON, unavailable model, HTTP failure or timeout raises `LMStudioError` before the simulator steps. Valid commands still pass through normal simulator validation and can be rejected individually. `last_decision` records `{status,model,time_s,commands,summary,usage,observation,latency_s}` and, when available, `finish_reason`, `reasoning_effort` or `error`. `metadata()` records model information, prompt/configuration, calls/errors, total/mean latency and token usage. Latency uses wall-clock seconds and does not count toward simulated aircraft waiting.

The current `compact-v2` wire format stores aircraft rows under shared `aircraft_columns` and performance limits in `aircraft_types`. Static runway geometry stays in `airport.runways`; current occupancy, availability and closure move to `runway_state`, keyed by runway ID. Packing adds no numeric rounding. See [the wire-format reference](AGENT_PROMPT.md#compact-v2-wire-format) for decoding and the exact metric subset. Full simulator and HTTP observations retain their original object format.

## HTTP

`python3 -m atc_bench serve --port 8000 --agent lmstudio`: localhost standard-library HTTP server. The default controller without `--agent` is `reference`; the server accepts `reference` or `lmstudio`.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/state` | Full current observation plus `controller`. Remains readable during inference. |
| `GET /api/scenarios` | `{scenarios:[...]}`. |
| `POST /api/controller` | `{kind:"reference" or "lmstudio",base_url?:string,model?:string or null}`. Sets the controller; omitting options retains existing settings. Null/empty model enables discovery. Returns observation. |
| `POST /api/step` | `{commands:[],seconds:10,autopilot:false}`. With `autopilot:true`, call the selected controller, combine commands, then step. Manual commands override automatic commands for the same callsign. |
| `POST /api/reset` | `{seed:7,scenario:"mixed",duration_s:1800}`. Replaces the episode and resets controller decision history while retaining controller kind/settings. |

`controller` contains `{kind,model,base_url,status,message,decision_count,last_decision,error}`. Status is `idle`, `thinking`, `ready` or `error`; the UI can distinguish waiting for inference from an empty but successful decision. Model discovery happens on the first automatic step. Episode completion sets idle status and a completion message. All POST bodies must use `Content-Type: application/json`.

Model failures return HTTP 502 with `{error,controller}` and leave simulation time unchanged. Invalid input returns HTTP 400. Concurrent mutation while a decision is in progress returns HTTP 409; GET state is available throughout. Static radar assets are served at `/`.

## CLI and results

`python3 -m atc_bench run --scenario mixed --seed 7 --duration 1800 --agent lmstudio --base-url http://127.0.0.1:1234 --model qwen3.8-27b-splash --step-seconds 30 --output results/run.json` runs one episode. `benchmark --seeds 1 2 3 --scenarios mixed emergency --duration 1800 --agent lmstudio --output results/benchmark.json` runs several episodes. `run`/`benchmark` support `--agent reference|noop|lmstudio|module:Class`; custom classes are zero-argument local Python code with `act(obs)`. `--step-seconds` is an integer 1–60, default 10.

`run`, `benchmark` and `serve` accept `--base-url` (default localhost:1234), `--model` (default auto-detect), `--llm-timeout` (default 120 seconds), and `--max-tokens` (default 1024). CLI output files retain configuration, metrics, rank fields/key, initial/final observations and command/event replay. LLM runs additionally retain model/prompt metadata and each decision's compact observation, commands, summary, latency and available usage. A model error saves a partial run with `status:"aborted"`, its error and failed decision; no failed step advances the clock. `run`/`benchmark` exit with code 2 on such an error, and a benchmark stops at its first failed run. Completed LLM runs have `status:"completed"`. Benchmark `mean_metrics` uses completed runs only and skips null diagnostics; `metric_sample_counts` reports each metric's contributing count. `completed_run_count` and `aborted_run_count` distinguish the two populations. With no completed runs, the means are empty and `mean_rank_key` is null.

`stdio --seed 7 --scenario mixed` emits an initial observation followed by one JSON response for each JSON-line request `{commands:[],seconds:10}` or `{reset:{seed:8,scenario:"storm"}}`. This bridge does not call a model; an external harness supplies its own decisions. Standard output is reserved for the protocol.

Frontend: radar with map runways, vectors/trails, weather cells, aircraft selection/flight board, command console, play/pause, single step/speed, scenario/seed/reset, controller selection, live metrics/events and command accepted/rejected feedback. The server owns controller selection and reports model/status/last decision. Default simulation state is paused. The interface identifies the mock airport and abstracted ground queues.
