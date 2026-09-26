# Language-model controller protocol

The benchmark includes a real LM Studio controller in [atc_bench/lmstudio.py](../atc_bench/lmstudio.py). Its `SYSTEM_PROMPT` and `RESPONSE_FORMAT` are the authoritative built-in prompt and output schema; the saved run includes prompt text, version and SHA-256 hash. `--agent lmstudio` calls the model at each automatic decision. The Python reference controller remains a separate heuristic selection, and is never substituted for failed model calls.

## What the model receives

Each request contains the system prompt and one fresh `compact-v2` JSON observation as a user message. The model receives text, not a radar image or browser UI. The observation contains:

- Simulation time/horizon, current airport/runway geometry and availability, weather and conflicts.
- Unfinished aircraft rows with positions, targets, type identifiers, fuel, declared emergencies and waiting timers, plus a shared type/performance catalog.
- Selected scalar scores and outcome counts, including unfinished, failed and unresolved traffic.
- `prior_command_errors` from the previous command batch.

Radar trails, finished aircraft rows, repeated event logs, nested metric breakdowns and the previous chat transcript are omitted to reduce input size. Future arrivals, weather and scheduled emergency declarations stay private. Aircraft `emergency_declared_time_s` and `emergency_touchdown_time_s` become non-null only once the corresponding public event occurs.

### Compact-v2 wire format

`compact_observation()` in [atc_bench/lmstudio.py](../atc_bench/lmstudio.py) transforms a full simulator observation into the model's input. It preserves all active-aircraft values except radar `history`, storing repeated field names and type limits separately. Coordinates and other numeric values are copied without additional rounding. It does not change `env.observe()`, the full radar/HTTP/JSON-lines observation, or saved initial/final observations.

| Field | Representation |
| --- | --- |
| `airport` | Airport metadata, fixes and `runways`. Each runway keeps its `id`, `physical_id`, geometry, length, heading and arrival/departure permissions. |
| `aircraft_types` | Object keyed by observed aircraft type. Each entry has `wake`, `min_speed_kt`, `max_speed_kt`, `landing_distance_m`, `takeoff_distance_m` and `crosswind_limit_kt`. Limits appear once per type. |
| `aircraft_columns` | Ordered column names shared by every aircraft row. Read this list from each observation rather than hard-coding column positions. |
| `aircraft` | Arrays whose values correspond to `aircraft_columns`. Includes `ground`, roll and taxi states as well as airborne aircraft; excludes `landed`, `departed`, `diverted` and `crashed`. |
| `runway_state` | Object keyed by directional runway ID, containing `occupied_by`, `available_in_s` and `closed`. Join to `airport.runways` by `id`. Reciprocal ends still share the underlying physical resource. |
| `time_s`, `duration_s`, `scenario`, `done`, `weather`, `conflicts` | Current public values copied from the full observation. |
| `metrics` | Selected scalar decision metrics, including safety outcomes, completed/unfinished counts, ground/airborne time, waiting totals and waiting diagnostics. This is a subset of the full result metrics. |
| `prior_command_errors` | Rejected entries from the immediately preceding batch's `command_results`, including their rejection reasons. Accepted results are omitted. |

The static context is serialized first: `airport`, `aircraft_types`, then `aircraft_columns`. That context is still included with every fresh request; the model is not expected to remember a previous request. The catalog includes types from already finished observed aircraft, so a type does not disappear when its last active aircraft finishes. A new type is added only after it becomes visible in the simulation.

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

The compact metric whitelist includes `ground_wait_seconds`, `ground_wait_score`, `emergency_wait_seconds` and `emergency_wait_score`; it omits their population counts, means, maxima, signed score components and `emergency_wait_by_outcome`. Those remain available in full observations and saved result metrics for evaluation. Other omitted fields include the seed, event log, finished aircraft records and rank key. The recorded metadata identifies `observation_format: "compact-v2"` and `prompt_version: "frankfurt-controller-v2"`.

### Model response and timing

When the loaded model advertises a reasoning-off capability, the adapter requests `reasoning_effort: "none"` for that request and records it in metadata; it does not change saved LM Studio settings.

The LM Studio response must be exactly this JSON shape:

```json
{"commands":["APPROACH DLH101 25R"],"summary":"Prioritizing the emergency arrival on runway 25R."}
```

The callsign/runway here are examples; use identifiers from the current observation. The schema allows up to 100 command strings and requires a short public operational summary. No reasoning transcript is requested. `{"commands":[],"summary":"Waiting for the current approach to complete."}` is valid. The harness sets the decision interval; the model does not return `seconds`.

Simulation time pauses while the model responds. Request `latency_s` is wall-clock time, while `ground_wait_s`, `emergency_wait_s` and all aircraft deadlines are simulated seconds. After a valid decision, the simulator validates commands, executes accepted commands and advances the requested interval. Command rejection reasons reach the next model turn. Invalid JSON, a truncated response, missing loaded model, connection failure or timeout produces a visible error and no simulation advance; there is no heuristic fallback.

## Control and scoring guidance

The built-in prompt explains the following constraints:

- Safety and emergency outcomes take priority over throughput and waiting. An approach can take several simulated minutes.
- `APPROACH` assigns automatic intercept/final guidance. Do not repeatedly reissue it to an aircraft already approaching. Reassigning `HEADING`, `DIRECT` or `HOLD` cancels an approach; use a go-around intentionally when needed.
- Heading, altitude and speed are targets with gradual aircraft response. Airborne separation requires at least 3 NM horizontally or 1,000 ft vertically in this benchmark.
- Reciprocal ends share runway occupancy/wake spacing. Runway length, aircraft limits, weather, direction and arrival/departure restrictions all matter; availability alone does not establish suitability.
- Emergencies have absolute `deadline_s` and elapsed `emergency_wait_s`. Departures have `ground_wait_s` while queued before takeoff clearance. Approach flight time counts toward emergency response time; landing roll and taxi do not.

Version 0.2.0 adds scalar waiting penalties of 1 point per departure queue minute and 5 points per emergency response minute, in addition to the existing ground/airborne time costs. `ground_wait_score` and `emergency_wait_score` are separate 0–100 delay diagnostics; they do not establish safety or service quality. Failed and unfinished eligible aircraft stay in the waiting denominators with observed time only. When analyzing full observations or saved results, inspect emergency outcomes and unfinished counts alongside the means; compact model inputs contain only the selected waiting totals and diagnostics described above. See [SCORING.md](SCORING.md) for formulas and censoring semantics. Version 0.2 scalar scores should not be compared directly with saved version 0.1 scores.

## External harnesses

The JSON-lines bridge and manual HTTP stepping remain available for other model providers. An external harness should validate its model's decision, extract commands, and submit:

```json
{"commands":["APPROACH DLH101 25R"],"seconds":10}
```

For HTTP `POST /api/step`, include `"autopilot":false` to prevent an additional call to the server's selected controller. The Python plugin interface expects only the command list from `act(observation)`, since the runner sets the interval. The built-in LM Studio response's `summary` is logging/display metadata and is not a simulator command field.

Use the same scenario, seed, horizon and decision interval when comparing controllers. Keep held-out seeds separate from controller development, retain model/prompt/decoding metadata, and report per-episode safety outcomes alongside scores. An aborted LLM run is a partial observation, not a completed benchmark result.
