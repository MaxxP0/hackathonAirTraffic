# Frankfurt ATC benchmark

A command-driven airport simulation for evaluating AI air traffic controllers. The LLM keeps an operational plan across decisions; the simulator pauses while it reasons, then advances traffic in fast batches. The simulation is deterministic for a given scenario and command timeline. Direct incoming traffic, select approaches, release departures, manage changing weather and prioritize emergencies. The airport is a simplified mock of Frankfurt (EDDF).

This first phase covers airborne traffic and runway scheduling. Ground time is already measured through departure queues and abstracted runway/taxi phases; taxi routes, gate allocation, pushback and turnaround operations are future extensions.

For team setup, checks and the branch/pull-request workflow, see [CONTRIBUTING.md](CONTRIBUTING.md). Recorded local-model results are in [the Qwen evaluation](docs/QWEN_RESULTS.md). Hosted runs and matched baselines are in [the GLM and GPT-6 Luna report](docs/GLM_RESULTS.md) and [the interactive comparison](http://127.0.0.1:8000/benchmark-results.html).

## Run it

Requires Python 3.10+ and a modern browser. The simulator and LM Studio adapter use only the Python standard library. Run these commands from the repository directory:

```sh
python3 -m atc_bench serve --port 8000
```

Open [localhost:8000](http://localhost:8000). The radar starts paused with the reference controller selected. Select a controller, enter commands manually, choose a scenario and seed, and watch traffic, weather, events and scores. The reference controller is a Python heuristic; select LM Studio or OpenRouter to use a real language model.

### Run GLM 5.3 Flash through OpenRouter

Export `OPENROUTER_API_KEY` in your shell, then start the hosted controller:

```sh
python3 -m atc_bench serve --agent openrouter --model z-ai/glm-5.3-flash --scenario runway_closure --port 8000
python3 -m examples.evaluate_openrouter --model z-ai/glm-5.3-flash --scenarios mixed emergency runway_closure --seeds 7 --step-seconds 120
```

The key stays on the server and is never sent to the radar, stored in results, or
committed to Git. `.env.example` shows the variable name; if you keep a local
`.env.local`, export it yourself before launching (`set -a; source .env.local;
set +a`). The application does not automatically read environment files.

OpenRouter defaults to `z-ai/glm-5.3-flash`, 8,192 output tokens, a 180-second
request timeout, and low reasoning effort. It uses the same compact telemetry,
four recent exchanges, persistent operational plan, command validation and
paused simulation clock as the local controller. It sends no screenshots.

A shared ledger at `results/openrouter-budget.json` limits this checkout to
**$10** across CLI runs, dashboard decisions, resets and restarts. Every request
reserves a conservative maximum cost before it is sent. Confirmed API costs
release unused reserves; an uncertain request keeps its reservation. The adapter
also caps provider prices, prioritizes throughput among eligible providers, disables paid plugins and automatic model fallback,
and exposes spend and remaining budget in the dashboard and saved results.
Keep both the ledger and its lock file; deleting the ledger alone blocks further
requests. A new checkout or deleting both files starts a new local accounting
history. Set a $10
limit on the API key in OpenRouter as an independent account-side cap.

Rate-limit responses receive at most two retries with 30/60-second backoff while
the simulation stays paused. Longer provider retry delays stop the run visibly.
Each attempt is budgeted; uncertain charges remain reserved until reconciled.
Interrupted episodes can be resumed without repeating successful model calls:

```sh
python3 -m examples.resume_openrouter results/interrupted.json --output results/resumed.json
```

The checkpoint must reproduce exactly before continuation. The same model,
prompt, recent dialogue and operational plan are restored, and the original
failure remains in the new replay. Reported active wall time excludes the pause
between processes.

For a matched GPT-6 Luna comparison, use the same scenarios, seed, horizon and
control window with `--model openai/gpt-6-luna`. Models without a temperature
control omit that parameter; saved metadata records whether it was sent.

To resume a checkpoint through a specific provider while preserving the model, prompt and saved plan:

```sh
python3 -m examples.resume_openrouter results/interrupted.json --output results/continued.json --provider sail-research/fp8 --rate-limit-retries 0
```

Provider pins retain the same spending guards and are recorded in result metadata. Resumed trajectories include earlier interruptions and may use different provider quantization, so compare them as recorded episodes rather than controlled provider experiments.

### Play the benchmark yourself

See the [web demo setup README](demo/README.md) for clone-and-run instructions, controls and troubleshooting.

Open [the human controller demo](http://127.0.0.1:8000/human.html). Read the same compact telemetry and instructions as the model, choose aircraft/actions with clickable controls, keep an operational plan, and submit the queued command batch to advance exactly 120 simulated seconds. Each episode lasts 30 simulated minutes. Choose runway closure, emergency arrivals or wind reversal, then inspect your score, waiting times and command feedback. You can export your recorded decisions after playing.

Human episodes use isolated in-memory sessions and make no model/API calls. They preserve the last four observation/decision exchanges plus your latest plan. Reloading the page resumes its browser session while the server remains running. A restart clears sessions, and starting more than 32 sessions expires the oldest.

The [PowerPoint](presentation/ATC-Benchmark-Luna-vs-GLM.pptx) contains the completed Luna-versus-GLM comparison and three embedded Luna scenario videos. Open it in desktop PowerPoint and click the videos during Slide Show.

### Event videos

Open [the event gallery](http://127.0.0.1:8000/replays.html) from the radar to watch
accelerated replays of runway closures, emergency landings and changing winds.
Each clip identifies its controller and scenario. Reference demonstrations are
separate from actual hosted-model results. Videos replay recorded commands
through the simulator; rendering does not make model calls. To render another
saved run, install the optional `videos` dependencies and FFmpeg:

```sh
python3 -m pip install -e '.[videos]'
python3 examples/render_replay.py results/run.json --output atc_bench/web/videos/my-run.mp4 --start 0 --end 900 --speed 30 --gallery
```

The renderer checks both the final metrics and aircraft states. The gallery
links each video to its command replay.

### Run a local LLM with LM Studio

Start LM Studio's server at [localhost:1234](http://127.0.0.1:1234) and load a language model there, then start the radar with:

```sh
python3 -m atc_bench serve --port 8000 --agent lmstudio --base-url http://127.0.0.1:1234
```

The adapter discovers an already loaded model; `--model` selects a particular model or loaded instance. For the model used in this local setup:

```sh
python3 -m atc_bench run --agent lmstudio --model qwen3.8-27b-splash --scenario emergency --seed 7 --duration 1800 --step-seconds 120 --output results/lmstudio-emergency.json
```

`run`, `benchmark` and `serve` accept `--base-url` (default `http://127.0.0.1:1234`), `--model` (default automatic discovery), `--llm-timeout` (default 900 wall-clock seconds per request) and `--max-tokens` (default 8192). The adapter uses an already loaded model and does not download or load one. It sends the model compact JSON observations as text and requests structured JSON commands, a short decision summary and a persistent operational plan. It keeps the latest plan and four recent observation/response exchanges for continuity, and leaves the loaded model’s reasoning settings at their default. The currently observed Qwen model advertises reasoning enabled. It does not send screenshots or use the radar UI as model input.

The LM request uses `compact-v2`: each active aircraft is an array mapped by `aircraft_columns`, with wake/performance limits stored once per type in `aircraft_types`. Runway geometry and permissions remain in `airport.runways`; live occupancy, availability and closure are in `runway_state`, keyed by runway ID. Packing preserves the full observation's numeric precision. The radar, Python environment, HTTP state and JSON-lines bridge still use the full observation with aircraft objects. [The compact wire format](docs/AGENT_PROMPT.md#compact-v2-wire-format) describes decoding and omitted fields.

The default control window is **120 simulated seconds**. The model sees the
window length, current observation, its latest operational plan and up to four
recent exchanges. After its response, the simulator applies valid commands and
advances that window in fast batches. It then asks the model again immediately,
without an artificial real-time delay. Choose a 30/60/120/300/600-second window
in the radar; CLI windows may range from 1 to 600 seconds. Longer windows need
fewer model calls but give the controller fewer opportunities to react.

Simulation time is paused during inference. Ten minutes of thinking costs ten
minutes of wall time, but does not advance aircraft or their waiting timers.
Total run speed depends on actual model latency and the selected window; a fast
simulation does not guarantee a faster-than-real-time end-to-end model run.
The UI exposes thinking/ready/error status, the last decision and its plan.

Model discovery, connection, timeout, truncation and invalid-JSON failures leave
the simulation at its last valid state, with a visible error and no heuristic
fallback. CLI failures retain partial results rather than masquerading as
completed episodes. Errors preserve the prior plan. Resetting an episode or
replacing its controller starts a fresh conversation; controller settings are
retained on reset.

Saved LLM results include model identity, prompt text/version/hash, decoding settings, request latency, available token usage including reasoning tokens, compact input observations, public summaries/plans, simulation timestamps, generated commands and command acceptance/rejections. This makes it possible to inspect the actual decisions that produced a score. The built-in prompt and the external-agent protocol are described in [docs/AGENT_PROMPT.md](docs/AGENT_PROMPT.md).

To run fast reference baselines and checks without the browser:

```sh
python3 -m atc_bench run --scenario mixed --seed 7 --duration 1800 --agent reference --output results/run.json
python3 -m atc_bench run --scenario mixed --seed 7 --duration 1800 --agent noop --output results/noop.json
python3 -m atc_bench benchmark --seeds 1 2 3 --scenarios mixed emergency --duration 1800 --output results/benchmark.json
python3 -m unittest discover -s tests -v
```

`--duration` is the simulated episode horizon and `--step-seconds` is the
control window (default 120, range 1–600). The model reasons once per window;
a 1,800-second horizon at 120-second windows needs up to 15 calls. The low-level
simulator still integrates movement and safety in substeps no longer than one
second. Reference/no-op baselines run the same windows without model requests.

Compare equal scenario, seed, horizon and control window. Retain the model,
prompt, reasoning settings and memory configuration, and report both simulated
horizon and measured `wall_duration_s`. Replay saved commands at their recorded
simulation timestamps to reproduce a trajectory. Run several seeds and report
individual safety outcomes and unfinished traffic alongside averages. Benchmark
means exclude aborted runs and null diagnostics; `metric_sample_counts` records
how many completed runs contributed to each mean.

## Controller interface

The agent receives a JSON-compatible observation and returns a list of commands. The low-level example below advances 10 seconds per call; the CLI and radar can split longer control windows into supported simulator steps. Coordinates are local nautical miles: x points east, y points north. Headings are degrees clockwise from north; speeds are knots; altitudes are feet above the simulated airport; times are seconds.

```python
from atc_bench import AirTrafficEnv
from atc_bench.agents import ReferenceAgent

env = AirTrafficEnv(seed=7, scenario="mixed", duration_s=1800)
agent = ReferenceAgent()
observation = env.observe()
while not env.done:
    observation = env.step(agent.act(observation), seconds=10)
print(env.metrics())
```

The full observation includes visible aircraft and their performance limits, current weather, runway geometry and availability, recent events, current conflicts, metrics and previous command results. Future traffic and future weather are not supplied to the controller. Finished aircraft remain visible with a terminal status. Both LLM adapters pack this value into its compact request format before sending it to the model.

Replace the example callsigns below with aircraft from the observation:

| Command | Effect |
| --- | --- |
| `HEADING DLH101 250` | Set a heading target. |
| `ALTITUDE DLH101 5000` | Set an altitude target. |
| `SPEED DLH101 210` | Set a speed target within aircraft limits. |
| `DIRECT DLH101 NORTH` | Fly toward a named fix (`NORTH`, `EAST`, `SOUTH`, `WEST`). |
| `DIRECT DLH101 10 15` | Fly toward local x/y coordinates. |
| `HOLD DLH101` | Enter an abstract holding pattern. |
| `APPROACH DLH101 25R` | Assign an approach and automated final guidance. |
| `TAKEOFF DLH102 25C` | Release a queued departure onto a suitable runway. |
| `GO_AROUND DLH101` | Cancel the approach and climb away. |
| `DIVERT DLH101` | Send an arrival out of the simulated area. |

Commands also accept dictionaries, for example `{"action":"heading","callsign":"DLH101","heading":250}` or `{"action":"approach","callsign":"DLH101","runway":"25R"}`. Parameters use `altitude`, `speed`, `fix`, or `x`/`y` for the corresponding commands. Assigning a heading, direct route or hold during an approach initiates a go-around climb. Change approach altitude or speed only after issuing `GO_AROUND`.

`step` processes commands in order, then advances time once. A rejected command is returned with a reason and does not cancel valid commands in the same batch. Use `[]` to let time pass. Each step accepts up to 100 commands and an integer duration from 0 to 60 seconds; the simulation evaluates movement and safety in substeps no longer than one second.

### Connect your own AI agent

For a Python agent, implement a zero-argument class with `act(observation) -> list[str | dict]` in an importable module:

```python
# my_agent.py
class Controller:
    def act(self, observation):
        # Feed the observation to your planner or model and return commands.
        return []
```

```sh
python3 -m atc_bench run --agent my_agent:Controller --scenario mixed --seed 7 --output results/custom.json
```

For an external process or language-model harness:

```sh
python3 -m atc_bench stdio --scenario mixed --seed 7
```

The process emits one initial JSON observation. Send one JSON request per line and receive one JSON response per line. Standard output is reserved for the protocol. The [model protocol guide](docs/AGENT_PROMPT.md) describes external AI controllers:

```json
{"commands":[],"seconds":10}
{"commands":["HEADING DLH101 250","ALTITUDE DLH101 5000"],"seconds":10}
{"reset":{"seed":8,"scenario":"storm"}}
```

The browser uses the same observation/command interface over localhost HTTP:

| Endpoint | Request / response |
| --- | --- |
| `GET /api/state` | Current observation, selected controller and decision status. |
| `GET /api/scenarios` | Scenario names. |
| `POST /api/step` | `{"commands":[],"seconds":120,"autopilot":true}` → ask the selected controller, apply commands, then advance up to 600 seconds in supported simulator chunks. Use `autopilot:false` for manual commands. |
| `POST /api/controller` | `{"kind":"lmstudio","base_url":"http://127.0.0.1:1234","model":null}` → select the local LLM. Use `{"kind":"openrouter","model":"z-ai/glm-5.3-flash"}` for the hosted model, or `{"kind":"reference"}` for the heuristic. |
| `POST /api/reset` | `{"seed":7,"scenario":"mixed","duration_s":1800}` → initial observation; controller selection/settings persist and dialogue restarts. |

Malformed HTTP requests return a JSON `error`. The full schema is in [docs/CONTRACT.md](docs/CONTRACT.md).

## Scenarios and evaluation

| Scenario | Challenge |
| --- | --- |
| `mixed` | Mixed arrivals and departures under ordinary conditions. |
| `rush_hour` | More traffic competing for runway capacity. |
| `low_visibility` | Restricted visibility and ceiling. |
| `storm` | Hazardous weather cells and adverse runway conditions. |
| `emergency` | Urgent aircraft that must be handled before their deadlines. |
| `wind_shift` | A change in the operating direction of the parallel runways. |
| `runway_closure` | An unexpected temporary closure forces arrivals to use the remaining runways and revise their sequencing. |

The `runway_closure` scenario selects one arrival-capable physical runway
(Northwest, Center or South) from the seed. The closure begins 25–40% through the
configured horizon and lasts 240–480 seconds, capped at 30% of the horizon for
short episodes. The same seed/horizon reproduces the disruption; its selected
runway and future start/reopening times remain hidden from the agent. When the
event occurs, both reciprocal ends report `closed: true`; the event log announces
closure and later reopening. Aircraft already approaching that runway go around,
while a takeoff/landing roll already underway continues. New clearances to the
closed runway are rejected. `runway_closures` and `closure_go_arounds` count these
disruptions without adding a direct score penalty; extra waiting, airborne time
and any resulting safety outcomes still affect scores.

Aircraft have different speed envelopes, wake categories, runway distance requirements and crosswind limits. Emergencies carry an absolute deadline in simulation time. Runway occupancy and wake clearance are shared between reciprocal runway ends, so `25C` and `07C` are one physical resource.

Metrics expose collisions, separation losses and duration, runway incursions, wake violations, weather exposure, emergency outcomes, landed/departed/diverted aircraft, unfinished traffic, ground time and airborne time. Departure `ground_wait_s` measures only time queued before takeoff clearance. `emergency_wait_s` measures elapsed time from public declaration until touchdown, crash, diversion or the current horizon. Ground and airborne time accrue for unresolved traffic too. Diversions are recorded separately from successful airport throughput. `emergencies_unresolved` reports urgent flights still awaiting an outcome; a horizon that ends before a deadline does not prematurely mark that emergency as failed.

The corresponding `ground_wait_seconds` and `emergency_wait_seconds` totals also have counts, means, maxima and 0–100 delay diagnostics. A five-minute mean ground queue scores 50; a three-minute mean emergency response scores 50. `emergency_wait_by_outcome` separates resolved, pending and failed emergencies. No eligible flights gives `null` for the mean, maximum and diagnostic score. These diagnostics include incomplete observations, so early episode termination can make waiting appear shorter. Compare equal seeds/scenarios/horizons and report safety, unfinished counts and emergency outcomes alongside waiting.

Benchmark version **0.3.0** adds persistent operational plans, recent dialogue,
model-default reasoning and configurable control windows up to 600 seconds. The
clock pauses during inference, and model latency is measured separately. The
score formula retains version 0.2's penalties of 1 point per departure queue
minute and 5 points per emergency response minute, plus total ground/airborne
costs. Saved version 0.2 stateless-controller experiments are legacy evidence;
they are not validation of the current controller. Match control windows and
other settings before comparing scores. Version 0.1 used different scalar
weights. The ten-field lexicographic rank schema is unchanged.

Use the saved lexicographic rank key as well as the scalar score; lower rank keys are better. Safety must be compared before throughput and delay; collision and emergency-failure episodes cannot outscore safe episodes. [Scoring rules and metric definitions](docs/SCORING.md) specify the weights, safety envelopes and time accounting. The reference agent is a transparent heuristic baseline, not an optimal controller or an LLM. Custom agents receive observations only through the public interface; Python plugins execute locally and are not a security sandbox.

## Frankfurt model and limits

The runway inventory follows Fraport's published airport user regulations: Northwest `25R/07L` is 2,800 m and landing-only; Center `25C/07C` and South `25L/07R` are 4,000 m and support arrivals/departures; West `18` is 4,000 m and departure-only. The map uses approximate local geometry. [Fraport airport user regulations, sections 1.1–1.2](https://www.fraport.com/content/dam/fraport-company/documents/geschaeftsfelder/service/richtlinien-und-zahlungsbedingungen/richtlinien/en/C2.1%20Airport%20User%20Regulations%20v1.4.pdf/_jcr_content/renditions/original./C2.1%20Airport%20User%20Regulations%20v1.4.pdf).

The simulation models easterly `07` and westerly `25` operations. Actual direction selection depends on wind and operational constraints; Fraport describes a five-knot tailwind changeover criterion on the parallel system. [Fraport operating direction explanation](https://www.fraport.com/en/business-areas/operations/airside-and-terminal-operations/easterly-and-westerly-operations.html).

This is a benchmark abstraction, not an operational flight simulator. Aircraft performance, separation rules, wake timers, weather, routes, emergencies and runway reservations are simplified and synthetic. It does not implement certified flight dynamics, instrument procedures, real schedules, terrain, airspace sectors, ATC radio phraseology or complete Frankfurt runway dependencies. Ground queues and taxi time are abstracted rather than routed through the real airport surface. Benchmark outcomes describe this model, not real-world controller safety or capacity.

The next phase can add a taxiway graph, gates, pushback clearance, runway crossings, service vehicles and turnaround tasks without changing the observation → command → step interaction pattern.

## BlueSky evaluation

The current prototype keeps its lightweight Python standard-library engine. [TU Delft's BlueSky](https://github.com/TUDelft-CNS-ATM/bluesky) is a candidate for a later flight-dynamics backend: it is MIT-licensed and offers aircraft performance models, navigation/airport data, a command stack and conflict detection. Its headless Python interface could sit underneath this benchmark's controller protocol; runway scheduling, emergency objectives, ground-delay accounting and evaluation would remain benchmark responsibilities. BlueSky is not integrated into the main simulator.

An optional [native BlueSky smoke test](examples/bluesky_smoke.py) was run successfully with BlueSky 1.1.1, OpenAP 2.6.2 and NumPy 2.5.3. To run it in a local virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install bluesky-simulator==1.1.1
.venv/bin/python examples/bluesky_smoke.py
```

The smoke exercises two airborne aircraft near Frankfurt, wind and heading/altitude/speed commands over 120 simulated seconds, then checks a native reset and repeat. It does not test landings, runway scheduling or emergencies. [BlueSky evidence and future-adapter notes](docs/BLUESKY.md) describe the verified scope and important API/unit differences.

### Arrival landing wait

CLI results and HTTP state include a separate `landing_metrics` object. Landing wait measures sector entry to touchdown, including normal approach, holding and go-arounds. It reports total, count, mean, maximum and a diagnostic score `100 / (1 + mean_seconds / 600)`. All spawned arrivals count, with separate landed, pending and failed/diverting outcome groups; pending waits end at the current horizon. Empty means and scores are null. Read outcomes alongside time: early diversion or crash can shorten observed waiting. This supplement preserves the existing main score, rank and model input.
