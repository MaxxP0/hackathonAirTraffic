# Optional BlueSky evaluation

The main Frankfurt benchmark uses its lightweight Python standard-library engine. BlueSky is being evaluated as a possible later physics backend; the optional smoke test is a separate script and does not change `AirTrafficEnv`, the benchmark runner or the radar UI.

[TU Delft's BlueSky repository](https://github.com/TUDelft-CNS-ATM/bluesky) provides its MIT license, simulator source, command interface, aircraft performance models, navigation data and conflict-detection functionality.

## Run the smoke test

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install bluesky-simulator==1.1.1
.venv/bin/python examples/bluesky_smoke.py
```

Use the existing `.venv` if it is already present. BlueSky and its dependencies are optional; the main benchmark runs without them. The script sends initialization logs to standard error and a JSON evidence report to standard output. It keeps generated BlueSky caches/settings in a temporary directory.

## Verified evidence

The local smoke test completed successfully with these installed versions:

| Component | Version |
| --- | --- |
| `bluesky-simulator` | 1.1.1 |
| `openap` | 2.6.2 |
| `numpy` | 2.5.3 |

The script initializes a detached native BlueSky simulation, selects OpenAP performance, sets a fixed seed, disables noise and automatic conflict resolution, and creates an A320 and B738 near EDDF. It applies a 15 kt wind from 250° and native `HDG`, `ALT` and `SPD` commands. The A320 descends from 9,000 toward 6,000 ft MSL and slows from 240 toward 210 kt; the B738 climbs from 11,000 toward 14,000 ft MSL.

It advances 120 one-second native simulation steps and asserts that aircraft move, the A320 turns/slows/descends, the B738 climbs, and the simulation clock reaches 120 seconds. It verifies the wind field is configured, OpenAP is selected, and automatic conflict resolution is off. A native reset clears traffic and resets the clock; running the same commands again produces identical serialized initial and final snapshots. Intermediate states are not compared by this smoke test.

This is evidence of a working native airborne API path. It does not implement or test landings, runway scheduling, emergencies, ground queues, benchmark scoring or an adapter to the main simulator. Dependency versions other than those recorded above have not been verified here; the JSON report records the actual installed versions on each run.

## Notes for a future backend

- Initialization uses `bs.init(mode="sim", detached=True, workdir=...)` without a GUI or network server.
- `bs.stack.stack(...)` queues native commands; `bs.stack.process()` applies them before stepping.
- Set the integration interval with the native `DT 1` command, start with `bs.sim.op()`, and call `bs.sim.step()`. Its optional argument is recovery time, **not a requested simulation duration**.
- Native traffic arrays use altitude in meters above mean sea level and speed in meters per second. The smoke converts altitude to feet MSL and calibrated airspeed to knots for JSON output. The prototype's altitude is feet above the simulated airport; an adapter must convert units and altitude reference explicitly, and preserve the distinction between calibrated airspeed and ground speed.
- BlueSky uses global simulation objects. Independent parallel environments should use separate processes.
- Leave native automatic conflict resolution disabled when evaluating an agent's separation decisions, or document it as part of the controller being evaluated.
- The benchmark still needs to own traffic demand, weather scenarios, emergency objectives, runway/ground state, command validation, observations, scoring and result export when a future dynamics adapter is introduced.

The command interface, initial coordinates, assertions and JSON report are all in [examples/bluesky_smoke.py](../examples/bluesky_smoke.py).
