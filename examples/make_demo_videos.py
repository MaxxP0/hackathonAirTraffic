"""Generate reproducible reference-policy event videos; never calls an LLM."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atc_bench.cli import run_episode
from render_replay import render


DEMOS = [
    {"id": "runway-closure", "scenario": "runway_closure", "title": "Runway closure & rerouting",
     "start": 490, "end": 1360, "speed": 30,
     "description": "An unexpected closure of runway 25R forces UAL102 to go around. The reference controller assigns another runway; the flight later touches down on 25L."},
    {"id": "emergency-landing", "scenario": "emergency", "title": "Emergency arrival & touchdown",
     "start": 0, "end": 480, "speed": 20,
     "description": "DLH100 declares a medical emergency shortly after arrival. Follow the descent, landing clearance, and recorded touchdown on runway 25C."},
    {"id": "wind-shift", "scenario": "wind_shift", "title": "Wind shift & approach changes",
     "start": 690, "end": 1620, "speed": 30,
     "description": "A reversal in wind changes the active runway direction from 25 to 07. Two approaches go around, then the reference controller starts new approaches from the other side."},
]


def main():
    directory = ROOT / "atc_bench" / "web" / "videos"
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    demo_ids = {demo["id"] for demo in DEMOS}
    manifest = [item for item in existing if item["id"] not in demo_ids]
    for demo in DEMOS:
        run = run_episode(seed=7, scenario=demo["scenario"], duration=1800,
                          agent_spec="reference", step_seconds=10)
        source = directory / f"{demo['id']}.run.json"
        source.write_text(json.dumps(run, separators=(",", ":")) + "\n")
        report = render(source, directory / f"{demo['id']}.mp4", start=demo["start"], end=demo["end"],
                        speed=demo["speed"], title=demo["title"])
        manifest.append({**demo, "video": f"/videos/{demo['id']}.mp4", "poster": f"/videos/{demo['id']}.jpg",
                         "source": f"/videos/{demo['id']}.run.json", "provenance": report["provenance"],
                         "duration_s": report["duration_s"], "metrics_verified": True,
                         "note": "Reference controller demonstration; this clip does not show LLM decisions."})
        print(f"Created {demo['id']}: {report['duration_s']:.1f}s, {report['bytes']/1_000_000:.2f} MB", flush=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
