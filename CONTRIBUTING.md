# Contributing

Use Python 3.10+ for the simulator and a modern browser for the radar. The core benchmark needs no third-party Python packages. Node.js is needed only for the UI checks below; there is no npm install step.

## Fresh checkout

Clone the repository using its GitHub **Code** button, then run from the checkout root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m atc_bench serve --port 8000 --agent reference
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. Open [localhost:8000](http://localhost:8000). The reference controller is a local Python heuristic and works without LM Studio or a model download. A short headless run is:

```sh
python -m atc_bench run --agent reference --scenario mixed --seed 7 --duration 300 --output results/local-reference.json
```

## Checks

```sh
python -m unittest discover -s tests -v
node --check atc_bench/web/app.js
node --test tests/test_web_controls.cjs
```

The Python suite exercises the simulator and local HTTP/CLI adapters, including mocked model-server responses; it does not require a live language model. The Node tests run the actual UI control flow with mocked DOM, HTTP and timers. They do not replace a browser check of rendering and interaction. For UI changes, open the radar and check the affected controls as well.

Add focused regression coverage for behavioral changes. Keep seed, scenario, duration and decision interval identical when comparing controllers. Report collisions, emergency outcomes, unfinished traffic and waiting metrics alongside score; retain the benchmark version and controller configuration.

## Optional LM Studio

Start LM Studio's local server on [localhost:1234](http://127.0.0.1:1234) and preload a language model there before using:

```sh
python -m atc_bench serve --port 8000 --agent lmstudio --base-url http://127.0.0.1:1234
```

The adapter discovers an already loaded model; `--model` can select its model key or loaded instance ID. It does not download/load a model or substitute the reference controller on failure. Model inference pauses simulated time, with latency recorded separately. Failed runs saved as `status: "aborted"` are partial results, not completed benchmarks. Mocked integration checks do not establish a successful live-model episode.

See [README.md](README.md) for commands, [docs/AGENT_PROMPT.md](docs/AGENT_PROMPT.md) for the compact model protocol, and [docs/SCORING.md](docs/SCORING.md) for scoring and waiting-time semantics. BlueSky remains an optional, separate smoke test; it is not the benchmark's active backend.

## Branches and pull requests

Create a branch from the current default branch for each change, push that branch, and open a pull request against the default branch. Contributors without write access can use a fork. Keep each PR focused so teammates can review it independently.

Describe the problem, the resulting behavior, the checks run and any remaining limitations. Include before/after screenshots for visual changes and reproducible scenario/seed/interval details for simulation changes. Distinguish mocked tests, reference-controller runs and real model runs. Update the protocol/scoring documentation when behavior changes, and avoid comparing scores across incompatible benchmark versions.

Keep virtual environments, model files, credentials and generated local results out of commits. Include small, intentional fixtures only when they are needed to reproduce a test or explain a result. Resolve review comments and rerun the relevant checks before merging.
