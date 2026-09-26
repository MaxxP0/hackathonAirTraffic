# Frankfurt ATC benchmark

A deterministic, command-driven airport simulation for evaluating AI air traffic controllers. Direct incoming traffic, select approaches, release departures, manage changing weather and prioritize emergencies. The airport is a simplified mock of Frankfurt (EDDF).

This first phase covers airborne traffic and runway scheduling. Ground time is already measured through departure queues and abstracted runway/taxi phases; taxi routes, gate allocation, pushback and turnaround operations are future extensions.

For team setup, checks and the branch/pull-request workflow, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Run it

Requires Python 3.10+ and a modern browser. The simulator and LM Studio adapter use only the Python standard library. Run these commands from the repository directory:

```sh
python3 -m atc_bench serve --port 8000
```

Open [localhost:8000](http://localhost:8000). The radar starts paused with the reference controller selected. Select a controller, enter commands manually, choose a scenario and seed, and watch traffic, weather, events and scores. The reference controller is a Python heuristic; select LM Studio to use a real language model.

### Run a local LLM with LM Studio

Start LM Studio's server at [localhost:1234](http://127.0.0.1:1234) and load a language model there, then start the radar with:

```sh
python3 -m atc_bench serve --port 8000 --agent lmstudio --base-url http://127.0.0.1:1234
```

The adapter discovers an already loaded model; `--model` selects a particular model or loaded instance. For the model used in this local setup:

```sh
python3 -m atc_bench run --agent lmstudio --model qwen3.8-27b-splash --scenario emergency --seed 7 --duration 1800 --step-seconds 30 --output results/lmstudio-emergency.json
```

`run`, `benchmark` and `serve` accept `--base-url` (default `http://127.0.0.1:1234`), `--model` (default automatic discovery), `--llm-timeout` (default 120 wall-clock seconds per request) and `--max-tokens` (default 1024). The adapter uses an already loaded model and does not download or load one. It sends the model compact JSON observations as text and requests structured JSON commands plus a short decision summary. It does not send screenshots or use the radar UI as model input.

The LM request uses `compact-v2`: each active aircraft is an array mapped by `aircraft_columns`, with wake/performance limits stored once per type in `aircraft_types`. Runway geometry and permissions remain in `airport.runways`; live occupancy, availability and closure are in `runway_state`, keyed by runway ID. Packing preserves the full observation's numeric precision. The radar, Python environment, HTTP state and JSON-lines bridge still use the full observation with aircraft objects. [The compact wire format](docs/AGENT_PROMPT.md#compact-v2-wire-format) describes decoding and omitted fields.

Each automatic step calls the selected model again. The UI exposes thinking/ready/error status and the last decision. Simulation time pauses during inference, so a slow model can visibly pause the radar without increasing aircraft waiting times. Model latency is measured separately in wall-clock seconds. An empty command list lets existing clearances continue when the step advances.

Model discovery, connection, timeout, truncated output and invalid decision JSON errors stop that step without advancing simulation time. The error is visible, and there is no heuristic fallback. CLI runs save partial results with `status: "aborted"` and exit with code 2; these are not completed benchmark episodes. Scenario resets retain the selected controller and its settings.

Saved LLM results include model identity, prompt text/version/hash, decoding settings, request latency, token usage when supplied by LM Studio, compact input observations, generated commands and command acceptance/rejections. This makes it possible to inspect the actual decisions that produced a score. The built-in prompt and the external-agent protocol are described in [docs/AGENT_PROMPT.md](docs/AGENT_PROMPT.md).

To run reproducible episodes without the browser:

```sh
python3 -m atc_bench run --scenario mixed --seed 7 --duration 1800 --agent reference --output results/run.json
python3 -m atc_bench run --scenario mixed --seed 7 --duration 1800 --agent noop --output results/noop.json
python3 -m atc_bench benchmark --seeds 1 2 3 --scenarios mixed emergency --duration 1800 --output results/benchmark.json
python3 -m unittest discover -s tests -v
```

Use the same scenario, seed, duration and decision interval when comparing agents. Run several seeds and report individual safety outcomes alongside averages. JSON episode results retain configuration, metrics and command/event replay data. Benchmark means exclude aborted runs and null diagnostics; `metric_sample_counts` records how many completed runs contributed to each mean.

## Controller interface

The agent receives a JSON-compatible observation and returns a list of commands. Coordinates are local nautical miles: x points east, y points north. Headings are degrees clockwise from north; speeds are knots; altitudes are feet above the simulated airport; times are seconds.

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

The full observation includes visible aircraft and their performance limits, current weather, runway geometry and availability, recent events, current conflicts, metrics and previous command results. Future traffic and future weather are not supplied to the controller. Finished aircraft remain visible with a terminal status. The LM Studio adapter packs this value into its compact request format before sending it to the model.

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
| `GET /api/state` | Current observation, including selected controller and its status. |
| `GET /api/scenarios` | Scenario names. |
| `POST /api/step` | `{"commands":[],"seconds":10,"autopilot":false}` → observation. Set `autopilot` to `true` to call the selected reference or LM Studio controller. |
| `POST /api/controller` | `{"kind":"lmstudio","base_url":"http://127.0.0.1:1234","model":null}` → select the local LLM. Use `{"kind":"reference"}` for the heuristic. |
| `POST /api/reset` | `{"seed":7,"scenario":"mixed","duration_s":1800}` → initial observation; controller selection/settings persist. |

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

Aircraft have different speed envelopes, wake categories, runway distance requirements and crosswind limits. Emergencies carry an absolute deadline in simulation time. Runway occupancy and wake clearance are shared between reciprocal runway ends, so `25C` and `07C` are one physical resource.

Metrics expose collisions, separation losses and duration, runway incursions, wake violations, weather exposure, emergency outcomes, landed/departed/diverted aircraft, unfinished traffic, ground time and airborne time. Departure `ground_wait_s` measures only time queued before takeoff clearance. `emergency_wait_s` measures elapsed time from public declaration until touchdown, crash, diversion or the current horizon. Ground and airborne time accrue for unresolved traffic too. Diversions are recorded separately from successful airport throughput. `emergencies_unresolved` reports urgent flights still awaiting an outcome; a horizon that ends before a deadline does not prematurely mark that emergency as failed.

The corresponding `ground_wait_seconds` and `emergency_wait_seconds` totals also have counts, means, maxima and 0–100 delay diagnostics. A five-minute mean ground queue scores 50; a three-minute mean emergency response scores 50. `emergency_wait_by_outcome` separates resolved, pending and failed emergencies. No eligible flights gives `null` for the mean, maximum and diagnostic score. These diagnostics include incomplete observations, so early episode termination can make waiting appear shorter. Compare equal seeds/scenarios/horizons and report safety, unfinished counts and emergency outcomes alongside waiting.

Benchmark version **0.2.0** adds scalar penalties of 1 point per departure queue minute and 5 points per emergency response minute, in addition to the existing total ground/airborne costs. Its scalar scores are not directly comparable to saved version 0.1 results. The ten-field lexicographic rank schema is unchanged.

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
