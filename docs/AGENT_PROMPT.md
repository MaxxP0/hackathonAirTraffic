# Language-model controller protocol

The benchmark includes a real LM Studio controller in [atc_bench/lmstudio.py](../atc_bench/lmstudio.py). Its `SYSTEM_PROMPT` and `RESPONSE_FORMAT` are the authoritative built-in prompt and output schema; the saved run includes prompt text, version and SHA-256 hash. `--agent lmstudio` calls the model at each automatic decision. The Python reference controller remains a separate heuristic selection, and is never substituted for failed model calls.

## What the model receives

Each request contains the system prompt, up to four recent successful user/assistant exchanges, and one fresh `compact-v2` JSON observation as the latest user message. The latest public operational plan is also carried explicitly in `controller_memory`; the model updates it across decisions even as older exchanges leave the bounded history. The model receives text, not a radar image or browser UI. The observation contains:

- Simulation time/horizon, current airport/runway geometry and availability, weather and conflicts.
- Unfinished aircraft rows with positions, targets, type identifiers, fuel, declared emergencies and waiting timers, plus a shared type/performance catalog.
- Selected scalar scores and outcome counts, including unfinished, failed and unresolved traffic.
- `prior_command_errors` from the previous command batch.

Radar trails, finished aircraft rows, repeated event logs and nested metric breakdowns are omitted to reduce input size. Recent dialogue contains observations and public decisions/plans; private model reasoning text is not copied into the saved conversation. Future arrivals, weather and scheduled emergency declarations stay private. Aircraft `emergency_declared_time_s` and `emergency_touchdown_time_s` become non-null only once the corresponding public event occurs.

### Compact-v2 wire format

`compact_observation()` in [atc_bench/lmstudio.py](../atc_bench/lmstudio.py) transforms a full simulator observation into the model's input. It preserves all active-aircraft values except radar `history`, storing repeated field names and type limits separately. Coordinates and other numeric values are copied without additional rounding. It does not change `env.observe()`, the full radar/HTTP/JSON-lines observation, or saved initial/final observations.

| Field | Representation |
| --- | --- |
| `airport` | Airport metadata, fixes and `runways`. Each runway keeps its `id`, `physical_id`, geometry, length, heading and arrival/departure permissions. |
| `aircraft_types` | Object keyed by observed aircraft type. Each entry has `wake`, `min_speed_kt`, `max_speed_kt`, `landing_distance_m`, `takeoff_distance_m` and `crosswind_limit_kt`. Limits appear once per type. |
| `aircraft_columns` | Ordered column names shared by every aircraft row. Read this list from each observation rather than hard-coding column positions. |
| `aircraft` | Arrays whose values correspond to `aircraft_columns`. Includes `ground`, roll and taxi states as well as airborne aircraft; excludes `landed`, `departed`, `diverted` and `crashed`. |
| `runway_state` | Object keyed by directional runway ID, containing `occupied_by`, `available_in_s` and `closed`. Join to `airport.runways` by `id`. Reciprocal ends still share the underlying physical resource. |
| `time_s`, `duration_s`, `decision_interval_s`, `scenario`, `done`, `weather`, `conflicts` | Current public values; the harness adds `decision_interval_s` so the model can plan for the next control window. |
| `metrics` | Selected scalar decision metrics, including safety outcomes, completed/unfinished counts, ground/airborne time, waiting totals and waiting diagnostics. This is a subset of the full result metrics. |
| `prior_command_errors` | Rejected entries from the immediately preceding batch's `command_results`, including their rejection reasons. Accepted results are omitted. |
| `controller_memory` | Added by the LM adapter: `latest_plan`, total `successful_decisions`, and `prior_decision_error`. This field belongs to the controller and does not overwrite simulator state. |

The static context is serialized first: `airport`, `aircraft_types`, then `aircraft_columns`. That context remains in every observation so the latest snapshot can be read independently, while recent dialogue and the explicit plan retain decision history. The catalog includes types from already finished observed aircraft, so a type does not disappear when its last active aircraft finishes. A new type is added only after it becomes visible in the simulation.

To reconstruct active aircraft and current runway records from a compact payload:

```python
aircraft = []
for row in payload["aircraft"]:
    plane = dict(zip(payload["aircraft_columns"], row))
    plane.update(payload["aircraft_types"][plane["type"]])
    aircraft.append(plane)

runways = [
    {**runway, **payload["runway_state"][runway["id"]]}
    for runway in payload["airport"]["runways"]
]
```

The compact metric whitelist includes `ground_wait_seconds`, `ground_wait_score`, `emergency_wait_seconds` and `emergency_wait_score`; it omits their population counts, means, maxima, signed score components and `emergency_wait_by_outcome`. Those remain available in full observations and saved result metrics for evaluation. Other omitted fields include the seed, event log, finished aircraft records and rank key. The recorded metadata identifies `observation_format: "compact-v2"` and `prompt_version: "frankfurt-controller-v3"`.

### Model response, memory and timing

The adapter leaves the loaded model's reasoning setting unchanged. It omits a
`reasoning_effort` override and records `reasoning_mode: "model_default"` plus the
inventory's `advertised_reasoning_default`. The currently observed Qwen model
advertises `on`. Actual reasoning-token usage is recorded when returned by LM
Studio; an advertised default alone is not a measurement of generated tokens.
Defaults allow 8,192 output tokens and a 900-second wall-clock request timeout.

The response must be exactly this JSON shape:

```json
{"commands":["APPROACH DLH101 25R"],"summary":"Prioritizing the emergency arrival.","plan":"Reserve 25R for DLH101; keep nearby arrivals separated until it clears, then release the oldest suitable departure."}
```

Use current identifiers. There are at most 32 command strings, each 1–80
characters; `summary` is at most 240 characters and `plan` is 1–1,200 characters.
The plan states operational intentions, priorities and reconsideration
conditions; it is not a private reasoning transcript. Empty `commands` is valid
while existing clearances execute. The model does not return `seconds`.

Successful responses enter the persistent dialogue. When its four-exchange
window fills, the oldest exchanges are removed and the latest explicit plan
remains available. Errors and truncated responses do not erase that plan or
become fictitious successful decisions. A new controller instance starts empty;
episode reset/controller replacement also starts a fresh conversation.

The simulator **pauses during inference**. It first validates and applies the
model's commands, then advances `decision_interval_s` simulated seconds quickly.
The next request receives the resulting observation and rejection feedback.
The default window is 120 seconds; high-level CLI/HTTP windows support 1–600
seconds. Longer windows reduce call count but increase the simulated time before
the model can react. A final window is limited by the remaining episode horizon.

Model `latency_s` measures real wall-clock time. A ten-minute model response does
not consume ten simulated minutes of fuel, emergency wait or ground wait. Those
timers accrue as the simulator advances its configured window. Total wall time
still depends on model latency, so measure it rather than claiming that every
model run is faster than real time.

Only one model request runs at a time. There is no artificial real-time delay
between automatic turns. Invalid JSON, truncation, unavailable models and
transport errors leave the simulator at its previous valid state, preserve the
last plan and surface an error. There is no heuristic fallback. Retrying can
reuse the plan and successful dialogue; a new episode or controller clears it.

## Control and scoring guidance

The built-in prompt explains the following constraints:

- Safety and emergency outcomes take priority over throughput and waiting. An approach can take several simulated minutes.
- `APPROACH` assigns automatic intercept/final guidance. Do not repeatedly reissue it to an aircraft already approaching. Reassigning `HEADING`, `DIRECT` or `HOLD` cancels an approach; use a go-around intentionally when needed.
- Heading, altitude and speed are targets with gradual aircraft response. Airborne separation requires at least 3 NM horizontally or 1,000 ft vertically in this benchmark.
- Reciprocal ends share runway occupancy/wake spacing. Runway length, aircraft limits, weather, direction and arrival/departure restrictions all matter; availability alone does not establish suitability.
- In `runway_closure`, an unexpected physical-runway closure appears in `runway_state.closed` for both directions. Existing approaches can automatically go around. Revise the operational plan from the resulting aircraft/runway state; future closure/reopening times are not supplied. The compact input omits event logs and the two closure counters, which remain available in full evaluation results.
- Emergencies have absolute `deadline_s` and elapsed `emergency_wait_s`. Departures have `ground_wait_s` while queued before takeoff clearance. Approach flight time counts toward emergency response time; landing roll and taxi do not.

The score formula introduced in version 0.2 adds scalar waiting penalties of 1 point per departure queue minute and 5 points per emergency response minute, in addition to the existing ground/airborne time costs. `ground_wait_score` and `emergency_wait_score` are separate 0–100 delay diagnostics; they do not establish safety or service quality. Failed and unfinished eligible aircraft stay in the waiting denominators with observed time only. When analyzing full observations or saved results, inspect emergency outcomes and unfinished counts alongside the means; compact model inputs contain only the selected waiting totals and diagnostics described above. See [SCORING.md](SCORING.md) for formulas and censoring semantics. Version 0.3 adds persistent planning, model-default reasoning and configurable larger control windows. Saved version 0.2 stateless-model experiments are legacy evidence, not validation of this controller. Match window sizes and controller settings when comparing scores; the formula is unchanged from version 0.2. Version 0.1 used different scalar weights.

## External harnesses

The JSON-lines bridge and manual HTTP stepping remain available for other model providers. An external harness should validate its model's decision, extract commands, and submit:

```json
{"commands":["APPROACH DLH101 25R"],"seconds":10}
```

For HTTP `POST /api/step`, include `"autopilot":false` to prevent an additional call to the server's selected controller. The Python plugin interface expects only the command list from `act(observation)`, since the runner sets the interval. The built-in LM Studio response's `summary` and `plan` are controller metadata, not simulator command fields. The bare JSON-lines bridge accepts low-level steps of at most 60 seconds. For a longer control window, apply commands once and advance the remaining chunks with empty command lists; collect rejection feedback from the command-bearing chunk for the next model observation.

Use equal scenario, seed, simulated horizon and control window for comparisons.
Record elapsed `wall_duration_s`, model/hardware, reasoning and decoding settings,
prompt/memory configuration and per-episode safety outcomes. Wall-clock model
latency does not alter the command application schedule, but model outputs may
vary. Replay commands at their recorded simulation timestamps to reproduce a
particular trajectory. Keep held-out seeds separate from controller development.
An aborted run remains partial evidence, not a completed benchmark result.
