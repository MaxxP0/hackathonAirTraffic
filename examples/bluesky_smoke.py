"""Optional native BlueSky smoke test; separate from the benchmark engine.

Install bluesky-simulator==1.1.1, then run:
    python examples/bluesky_smoke.py

Creates two aircraft near Frankfurt, applies native heading/altitude/speed and
wind commands, advances 120 simulated seconds, and repeats after a native reset.
BlueSky initialization logs go to stderr; stdout contains a JSON evidence report.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
import json
import math
import sys
from tempfile import TemporaryDirectory
import time


COMMANDS = [
    "DT 1",
    "SEED 7",
    "NOISE OFF",
    "PERF OPENAP",
    "RESO OFF",
    "CRE DLH101,A320,50.18,8.90,250,9000,240",
    "CRE DLH202,B738,50.03,8.10,070,11000,260",
    "WIND 50.0379,8.5622,250,15",
    "HDG DLH101,220",
    "ALT DLH101,6000",
    "SPD DLH101,210",
    "HDG DLH202,040",
    "ALT DLH202,14000",
    "SPD DLH202,250",
]


def snapshot(bs) -> list[dict]:
    from bluesky.tools.aero import ft, kts
    return [
        {"callsign": callsign, "type": bs.traf.type[index],
         "latitude_deg": round(float(bs.traf.lat[index]), 7),
         "longitude_deg": round(float(bs.traf.lon[index]), 7),
         "altitude_ft_msl": round(float(bs.traf.alt[index] / ft), 3),
         "heading_deg": round(float(bs.traf.hdg[index]), 3),
         "cas_kt": round(float(bs.traf.cas[index] / kts), 3),
         "ground_speed_kt": round(float(bs.traf.gs[index] / kts), 3)}
        for index, callsign in enumerate(bs.traf.id)
    ]


def episode(bs) -> dict:
    from bluesky.tools.aero import ft, kts
    from bluesky.traffic.asas.resolution import ConflictResolution
    from bluesky.traffic.performance.perfbase import PerfBase

    # stack queues commands; process applies them before stepping physics.
    bs.stack.stack(*COMMANDS)
    bs.stack.process()
    assert bs.traf.id == ["DLH101", "DLH202"], "native aircraft creation failed"
    assert math.isclose(float(bs.traf.selalt[0] / ft), 6000), "ALT was not applied"
    assert math.isclose(float(bs.traf.selspd[0] / kts), 210), "SPD was not applied"
    assert bs.traf.wind.winddim > 0, "WIND was not applied"
    assert ConflictResolution.selected() is ConflictResolution, "automatic resolution is still active"
    assert PerfBase.selected().__name__ == "OpenAP", "OpenAP performance is not active"
    initial = snapshot(bs)
    bs.sim.op()
    for _ in range(120):
        # step's optional argument is recovery time, NOT desired step size.
        # The native DT command above configures the actual integration step.
        bs.sim.step()
    final = snapshot(bs)
    assert math.isclose(bs.sim.simt, 120), "simulation did not advance by 120 seconds"
    assert final[0]["altitude_ft_msl"] < initial[0]["altitude_ft_msl"], "aircraft did not descend"
    assert final[1]["altitude_ft_msl"] > initial[1]["altitude_ft_msl"], "aircraft did not climb"
    assert abs(final[0]["heading_deg"] - 220) < 10, "heading command was not followed"
    assert final[0]["latitude_deg"] != initial[0]["latitude_deg"], "aircraft did not move"
    assert final[0]["cas_kt"] < initial[0]["cas_kt"], "speed command was not followed"
    return {"simulated_seconds": bs.sim.simt, "initial": initial, "final": final}


def main() -> int:
    try:
        installed = {name: version(name) for name in ("bluesky-simulator", "openap", "numpy")}
    except PackageNotFoundError:
        print("Optional dependency missing. Install: python -m pip install bluesky-simulator==1.1.1", file=sys.stderr)
        return 2
    if installed["bluesky-simulator"] != "1.1.1":
        print("This smoke test targets bluesky-simulator==1.1.1; other versions are unverified.", file=sys.stderr)
    started = time.perf_counter()
    with TemporaryDirectory(prefix="atc-bluesky-smoke-") as workdir, redirect_stdout(sys.stderr):
        import bluesky as bs
        # A detached sim has no GUI or network server. All generated caches,
        # settings, and plugin folders remain in the temporary directory.
        bs.init(mode="sim", detached=True, workdir=workdir)
        first = episode(bs)
        bs.sim.reset()
        assert bs.traf.ntraf == 0 and bs.sim.simt == 0, "native reset did not clear traffic and clock"
        second = episode(bs)
        assert first == second, "serialized trajectory changed after native reset"
        bs.sim.reset()
        bs.net.close()
    result = {"purpose": "standalone BlueSky API smoke test; main benchmark still uses its lightweight engine",
              "versions": installed, "performance_model": "OpenAP", "automatic_conflict_resolution": "OFF",
              "commands": COMMANDS, "episode": first, "native_reset_repeated_trajectory": first == second,
              "wall_seconds_including_initialization": round(time.perf_counter() - started, 3)}
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
