"""Render an existing benchmark replay to MP4 without calling an agent.

Optional dependencies: Pillow and ffmpeg. Example:
  python examples/render_replay.py results/run.json --output replay.mp4 --speed 24

The saved commands are replayed through the real environment, including the
entire episode outside a selected clip, and final metrics must match exactly.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atc_bench import AirTrafficEnv


W, H = 1280, 720
BG = "#081019"
PANEL = "#101d29"
LINE = "#243747"
TEXT = "#e6f1f7"
MUTED = "#839baa"
CYAN = "#61ddd2"
BLUE = "#7ca9ff"
AMBER = "#ffbd69"
RED = "#ff7285"
FINISHED = {"landed", "departed", "diverted"}


def load_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as error:
        raise RuntimeError("Rendering needs optional Pillow: python -m pip install Pillow") from error
    return Image, ImageDraw, ImageFont


def timecode(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def source_label(run):
    kind = run["configuration"].get("agent", "unknown")
    model = run.get("controller", {}).get("model")
    if kind == "reference":
        return "Reference controller demonstration", "REFERENCE POLICY"
    if kind == "noop":
        return "No-op controller demonstration", "NO-OP POLICY"
    if kind in {"lmstudio", "openrouter"}:
        return f"Recorded {kind} model · {model or 'model ID in source replay'}", "RECORDED AI RUN"
    return f"Recorded controller · {kind}", "RECORDED POLICY"


def reconstruct(run, sample_times):
    """Return selected real observations after validating the full saved run."""
    config = run["configuration"]
    env = AirTrafficEnv(seed=config["seed"], scenario=config["scenario"], duration_s=config["duration_s"])
    samples, index, feedback = [], 0, []
    observation = env.observe()

    def capture(current):
        nonlocal index
        current["command_results"] = deepcopy(feedback)
        while index < len(sample_times) and sample_times[index] <= current["time_s"] + 1e-6:
            samples.append(deepcopy(current))
            index += 1

    for entry_index, entry in enumerate(run["replay"]):
        if not math.isclose(env.time_s, entry["time_s"], abs_tol=1e-6):
            raise ValueError(f"Replay timeline discontinuity: env={env.time_s}, recorded={entry['time_s']}")
        commands = entry.get("commands", [])
        observation = env.step(commands, seconds=0)
        feedback = deepcopy(observation["command_results"])
        if feedback != entry.get("command_results", []):
            raise ValueError(f"Command results differ at T+{entry['time_s']}; use the matching benchmark version")
        capture(observation)
        target = min(env.duration_s, entry["time_s"] + entry["seconds"])
        if not float(target).is_integer():
            raise ValueError("Recorded decision boundaries must be whole simulation seconds")
        while env.time_s < target:
            observation = env.step([], seconds=1)
            # At a decision boundary, render after that boundary's commands
            # have been applied, rather than one frame before the clearance.
            if env.time_s < target or entry_index == len(run["replay"]) - 1:
                capture(observation)
        env.command_results = deepcopy(feedback)
    actual, expected = env.metrics(), run["metrics"]
    if actual != expected:
        different = {key: {"recorded": expected.get(key), "replayed": actual.get(key)}
                     for key in actual.keys() | expected.keys() if actual.get(key) != expected.get(key)}
        raise ValueError("Final replay metrics do not match the saved run: " + json.dumps(different))
    # Airport coordinates are tuples in Python and arrays in saved JSON.
    final = json.loads(json.dumps(env.observe()))
    recorded_final = json.loads(json.dumps(run["final_observation"]))
    for field in ("aircraft", "airport", "weather", "time_s", "done"):
        if final[field] != recorded_final[field]:
            raise ValueError(f"Final replay {field} differs from the recorded state")
    if index != len(sample_times):
        raise ValueError("Requested clip extends beyond the recorded simulation")
    return samples, actual


class RadarRenderer:
    def __init__(self, run, title, start, end, speed):
        self.Image, self.ImageDraw, self.ImageFont = load_pillow()
        self.run, self.title = run, title
        self.start, self.end, self.speed = start, end, speed
        self.provenance, self.badge = source_label(run)
        self.fonts = {}
        self.reroutes = []
        assigned = {}
        for step in run["replay"]:
            for command, result in zip(step.get("commands", []), step.get("command_results", [])):
                if not result.get("accepted"):
                    continue
                if isinstance(command, str):
                    words = command.split()
                    if len(words) != 3 or words[0].upper() != "APPROACH":
                        continue
                    _, callsign, runway = words
                elif command.get("action", "").lower() == "approach":
                    callsign, runway = command["callsign"], command["runway"]
                else:
                    continue
                if callsign in assigned and assigned[callsign] != runway:
                    self.reroutes.append({"time_s": step["time_s"], "type": "reroute",
                                          "message": f"{callsign} rerouted: approach {assigned[callsign]} → {runway}"})
                assigned[callsign] = runway
        self.base = self._background()

    def font(self, size, bold=False, mono=False):
        key = size, bold, mono
        if key not in self.fonts:
            options = (["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"] if mono else
                       ["/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
            path = next((name for name in options if Path(name).exists()), None)
            self.fonts[key] = self.ImageFont.truetype(path, size) if path else self.ImageFont.load_default(size=size)
        return self.fonts[key]

    def text(self, draw, xy, value, size=16, fill=TEXT, bold=False, mono=False):
        draw.text(xy, str(value), font=self.font(size, bold, mono), fill=fill)

    def wrapped(self, draw, xy, text, width, *, size=16, fill=TEXT, max_lines=3, gap=5):
        words, line, lines = str(text).split(), "", []
        for word in words:
            candidate = f"{line} {word}".strip()
            if draw.textlength(candidate, font=self.font(size)) > width and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        for index, line in enumerate(lines[:max_lines]):
            if index == max_lines - 1 and len(lines) > max_lines:
                line = line.rstrip(" .") + "…"
            self.text(draw, (xy[0], xy[1] + index * (size + gap)), line, size, fill)
        return min(len(lines), max_lines) * (size + gap)

    @staticmethod
    def point(x, y):
        return 472 + x * 7.2, 411 - y * 7.2

    def _background(self):
        image = self.Image.new("RGB", (W, H), BG)
        draw = self.ImageDraw.Draw(image)
        draw.rounded_rectangle((24, 143, 926, 644), radius=18, fill="#0c1721", outline=LINE)
        for x in range(-50, 51, 10):
            px, _ = self.point(x, 0)
            draw.line((px, 155, px, 632), fill="#152735", width=1)
        for y in range(-30, 31, 10):
            _, py = self.point(0, y)
            draw.line((36, py, 914, py), fill="#152735", width=1)
        cx, cy = self.point(0, 0)
        for radius in (10, 20, 30):
            r = radius * 7.2
            draw.ellipse((cx-r, cy-r, cx+r, cy+r), outline="#243745", width=1)
            self.text(draw, (cx+r-43, cy+4), f"{radius} NM", 10, MUTED, mono=True)
        self.text(draw, (40, 157), "FRANKFURT TERMINAL AREA", 12, MUTED, bold=True)
        self.text(draw, (40, 177), "Schematic geography · altitude AGL · time accelerated", 11, MUTED)
        draw.line((889, 186, 889, 164), fill=MUTED, width=2)
        draw.polygon([(889, 157), (885, 166), (893, 166)], fill=MUTED)
        self.text(draw, (884, 190), "N", 11, MUTED, bold=True)
        return image

    def _runways(self, draw, observation):
        active = observation["weather"]["active_direction"]
        for runway in observation["airport"]["runways"]:
            if runway["id"] != "18" and not runway["id"].startswith(active):
                continue
            p, q = self.point(*runway["threshold"]), self.point(*runway["end"])
            color = RED if runway["closed"] else AMBER if runway["occupied_by"] else "#adc8d5"
            draw.line((*p, *q), fill=color, width=4)
            if runway["arrival"] and not runway["closed"]:
                fix = self.point(*runway["approach_fix"])
                for part in range(0, 20, 2):
                    f, g = part / 20, (part + 1) / 20
                    draw.line((p[0]+(fix[0]-p[0])*f, p[1]+(fix[1]-p[1])*f,
                               p[0]+(fix[0]-p[0])*g, p[1]+(fix[1]-p[1])*g), fill="#41636a", width=1)
        self.text(draw, (445, 441), "EDDF", 13, TEXT, bold=True)
        # Detailed runway inset preserves the geography while making closures
        # and the arrival/departure roles legible at terminal-area scale.
        draw.rounded_rectangle((41, 473, 365, 628), radius=12, fill="#10212c", outline=LINE)
        self.text(draw, (57, 484), "RUNWAY STATUS", 11, MUTED, bold=True)
        for runway in observation["airport"]["runways"]:
            if runway["id"] != "18" and not runway["id"].startswith(active):
                continue
            def zoom(point):
                return 224 + point[0] * 20, 552 - point[1] * 20
            p, q = zoom(runway["threshold"]), zoom(runway["end"])
            color = RED if runway["closed"] else AMBER if runway["occupied_by"] else CYAN
            draw.line((*p, *q), fill=color, width=6)
            midpoint = ((p[0]+q[0])/2, (p[1]+q[1])/2)
            if runway["closed"]:
                for dx in (-4, 4):
                    draw.line((midpoint[0]+dx-3, midpoint[1]-5, midpoint[0]+dx+3, midpoint[1]+5), fill=TEXT, width=2)
            label = runway["id"] + (" CLOSED" if runway["closed"] else " IN USE" if runway["occupied_by"] else " OPEN")
            offsets = {"NW": (-95, -20), "CENTER": (32, -18), "SOUTH": (26, 0), "WEST": (-110, 6)}
            dx, dy = offsets[runway["physical_id"]]
            self.text(draw, (midpoint[0]+dx, midpoint[1]+dy), label, 10, color, bold=True)
        self.text(draw, (57, 609), "Ground queues are abstracted in this benchmark", 10, MUTED)

    def _traffic(self, image, draw, observation):
        overlay = self.Image.new("RGBA", image.size)
        paint = self.ImageDraw.Draw(overlay)
        for cell in observation["weather"]["cells"]:
            cx, cy = self.point(cell["x_nm"], cell["y_nm"])
            radius = cell["radius_nm"] * 7.2
            polygon = [(cx + math.cos(i * math.tau/12) * radius * (1 + .13 * math.sin(i*3)),
                        cy + math.sin(i * math.tau/12) * radius * (1 + .13 * math.sin(i*3))) for i in range(12)]
            paint.polygon(polygon, fill=(242, 145, 84, 36), outline=(255, 171, 94, 155), width=2)
            self.text(paint, (cx-radius/2, cy-8), f"{cell['id']} · STORM", 11, AMBER, bold=True)
        image.paste(overlay, (0, 0), overlay)
        placed_labels = [(41, 465, 373, 633), (440, 437, 489, 459)]
        planes = sorted(observation["aircraft"], key=lambda plane: (plane["emergency"] is None,
                                                                  plane["status"] != "approach", plane["callsign"]))
        for plane in planes:
            if plane["status"] in FINISHED or plane["status"] in {"ground", "taxi_in"}:
                continue
            x, y = self.point(plane["x_nm"], plane["y_nm"])
            if not 43 <= x <= 887 or not 216 <= y <= 623:
                continue
            if 36 <= x <= 376 and y >= 463:
                continue
            color = AMBER if plane["emergency"] else BLUE if plane["kind"] == "departure" else CYAN
            if plane["status"] == "crashed":
                color = RED
            trail = [self.point(*point) for point in plane["history"]]
            for a, b in zip(trail, trail[1:]):
                if all(38 <= p[0] <= 910 and 205 <= p[1] <= 632 for p in (a, b)):
                    draw.line((*a, *b), fill="#5a4b32" if plane["emergency"] else "#24474a" if plane["kind"] == "arrival" else "#2a3b58", width=2)
            angle = math.radians(plane["heading_deg"])
            ux, uy = math.sin(angle), -math.cos(angle)
            draw.polygon([(x+ux*8, y+uy*8), (x-ux*5-uy*4, y-uy*5+ux*4),
                          (x-ux*2, y-uy*2), (x-ux*5+uy*4, y-uy*5-ux*4)], fill=color)
            if plane["emergency"]:
                draw.ellipse((x-13, y-13, x+13, y+13), outline=AMBER, width=1)
            label = plane["callsign"] + (" !" if plane["emergency"] else "")
            offsets = [(11,-14),(11,17),(-104,-14),(-104,17),(11,-47),(-104,-47),(11,48),(-104,48)]
            candidates = []
            for dx, dy in offsets:
                box = (x+dx, y+dy, x+dx+100, y+dy+28)
                if not (40 <= box[0] and box[2] <= 913 and 211 <= box[1] and box[3] <= 632):
                    continue
                overlap = sum(max(0, min(box[2], used[2])-max(box[0], used[0])) *
                              max(0, min(box[3], used[3])-max(box[1], used[1])) for used in placed_labels)
                candidates.append((overlap, abs(dx)+abs(dy), box))
            box = min(candidates)[2] if candidates else (x+11, y-14, x+111, y+14)
            placed_labels.append(box)
            if box[0] != x+11 or box[1] != y-14:
                anchor_x = box[0] if box[0] > x else box[2]
                draw.line((x, y, anchor_x, box[1]+12), fill="#3c5660", width=1)
            self.text(draw, (box[0], box[1]), label, 12, color, bold=True)
            self.text(draw, (box[0], box[1]+15), f"{plane['altitude_ft']/1000:.1f}k · {int(plane['speed_kt'])}kt", 10, MUTED, mono=True)
        for conflict in observation["conflicts"]:
            planes = [a for a in observation["aircraft"] if a["callsign"] in conflict["callsigns"]]
            if len(planes) == 2:
                points = [self.point(a["x_nm"], a["y_nm"]) for a in planes]
                if all(40 < x < 910 and 211 < y < 630 for x, y in points):
                    draw.line((*points[0], *points[1]), fill=RED, width=2)

    def _sidebar(self, draw, observation):
        metrics, weather = observation["metrics"], observation["weather"]
        draw.rounded_rectangle((944, 143, 1256, 644), radius=18, fill=PANEL, outline=LINE)
        self.text(draw, (963, 159), "TRAFFIC OUTCOMES", 11, MUTED, bold=True)
        for x, field, label in ((963, "landed", "LANDED"), (1059, "departed", "DEPARTED"), (1160, "unfinished", "ACTIVE")):
            self.text(draw, (x, 181), str(metrics[field]), 34, TEXT, bold=True)
            self.text(draw, (x, 219), label, 10, MUTED, bold=True)
        draw.line((963, 245, 1237, 245), fill=LINE)
        for y, label, value, color in ((261, "Collisions", metrics["collisions"], RED if metrics["collisions"] else CYAN),
                                     (291, "Separation losses", metrics["separation_losses"], AMBER if metrics["separation_losses"] else CYAN),
                                     (321, "Emergency landings", metrics["emergency_landings"], CYAN)):
            self.text(draw, (963, y), label, 14, MUTED)
            self.text(draw, (1204, y-2), value, 18, color, bold=True, mono=True)
        draw.line((963, 355, 1237, 355), fill=LINE)
        self.text(draw, (963, 371), "WAIT / PERFORMANCE", 11, MUTED, bold=True)
        wait = metrics.get("ground_wait_mean_seconds")
        self.text(draw, (963, 395), "Mean ground wait", 14, MUTED)
        self.text(draw, (1138, 394), f"{wait/60:.1f} min" if wait is not None else "—", 16, TEXT, mono=True)
        emergency_wait = metrics.get("emergency_wait_mean_seconds")
        self.text(draw, (963, 423), "Emergency wait", 14, MUTED)
        self.text(draw, (1138, 422), f"{emergency_wait:.0f} sec" if emergency_wait is not None else "—", 16, AMBER, mono=True)
        self.text(draw, (963, 451), "Benchmark score", 14, MUTED)
        self.text(draw, (1120, 450), f"{metrics['score']:,.0f}", 16, TEXT, mono=True)
        draw.line((963, 486, 1237, 486), fill=LINE)
        self.text(draw, (963, 502), "WEATHER / OPERATIONS", 11, MUTED, bold=True)
        self.text(draw, (963, 525), f"Wind {weather['wind_from_deg']:03.0f}° / {weather['wind_speed_kt']:.0f} kt", 17, TEXT, bold=True)
        self.text(draw, (963, 552), f"Visibility {weather['visibility_m']/1000:g} km · gust {weather['gust_kt']:.0f} kt", 12, MUTED)
        self.text(draw, (963, 577), f"ACTIVE DIRECTION {weather['active_direction']}", 12, CYAN, bold=True)
        closed = sorted(r["id"] for r in observation["airport"]["runways"] if r["closed"])
        self.wrapped(draw, (963, 603), "Closed: " + " / ".join(closed) if closed else "All runway strips available", 272,
                     size=12, fill=RED if closed else MUTED, max_lines=2)

    def frame(self, observation, *, overlay=None):
        image = self.base.copy()
        draw = self.ImageDraw.Draw(image)
        self.text(draw, (29, 25), "EDDF  /  AIR TRAFFIC BENCHMARK", 12, CYAN, bold=True)
        self.text(draw, (27, 52), self.title, 31, TEXT, bold=True)
        self.text(draw, (29, 100), self.provenance, 14, MUTED)
        draw.rounded_rectangle((1011, 25, 1255, 52), radius=13, fill="#16342f")
        self.text(draw, (1028, 31), self.badge, 11, CYAN, bold=True)
        self.text(draw, (1038, 67), "T+ " + timecode(observation["time_s"]), 20, TEXT, mono=True)
        self.text(draw, (1053, 101), f"{self.speed:g}× replay  ·  seed {observation['seed']}", 12, MUTED)
        self._traffic(image, draw, observation)
        self._runways(draw, observation)
        self._sidebar(draw, observation)
        # A caption remains visible for at least three video seconds. Prefer
        # operational events to ordinary command chatter, with true sim time.
        notable = [event for event in observation["events"]
                   if event["type"] in {"runway_closed", "runway_reopened", "emergency", "go_around", "touchdown", "weather", "crash"}
                   and 0 <= observation["time_s"] - event["time_s"] <= self.speed * 4]
        notable.extend(event for event in self.reroutes if 0 <= observation["time_s"]-event["time_s"] <= self.speed*4)
        event = max(notable, key=lambda item: item["time_s"]) if notable else None
        caption = event["message"] if event else "Inbound traffic is sequenced while departure queues wait for suitable runways."
        kind = event["type"].upper().replace("_", " ") if event else "LIVE SIMULATION STATE"
        color = RED if event and event["type"] in {"runway_closed", "go_around", "crash"} else AMBER if event and event["type"] == "emergency" else CYAN
        draw.rounded_rectangle((24, 656, 1256, 705), radius=10, fill=PANEL)
        draw.rounded_rectangle((24, 656, 29, 705), radius=2, fill=color)
        self.text(draw, (43, 665), kind, 10, color, bold=True)
        self.text(draw, (225, 666), caption[:123], 14, TEXT)
        progress = min(1, max(0, (observation["time_s"] - self.start) / max(1, self.end-self.start)))
        draw.rectangle((24, 712, 1256, 715), fill=LINE)
        draw.rectangle((24, 712, 24+1232*progress, 715), fill=CYAN)
        if overlay:
            dim = self.Image.new("RGBA", image.size, (3, 9, 16, 132))
            image = self.Image.alpha_composite(image.convert("RGBA"), dim).convert("RGB")
            draw = self.ImageDraw.Draw(image)
            draw.rounded_rectangle((196, 244, 1084, 463), radius=22, fill="#102331", outline="#335361", width=2)
            self.text(draw, (227, 268), "RECORDED SIMULATION" if overlay == "intro" else "END OF EVENT CLIP", 12, CYAN, bold=True)
            self.text(draw, (226, 300), self.title if overlay == "intro" else "Every frame follows the saved commands", 29, TEXT, bold=True)
            if overlay == "intro":
                detail = f"{self.provenance}. {timecode(self.start)}–{timecode(self.end)} of simulated traffic, shown at {self.speed:g}× speed."
            else:
                detail = f"Recorded trajectory verified against saved metrics. At this clip's end: {observation['metrics']['landed']} landed, {observation['metrics']['departed']} departed, {observation['metrics']['collisions']} collisions."
            self.wrapped(draw, (229, 348), detail, 804, size=18, fill=MUTED, max_lines=3)
            self.text(draw, (229, 427), "Frankfurt-inspired airport mock · simplified dynamics and ground queues", 12, MUTED)
        return image


def render(run_path, output, *, start=0, end=None, speed=24, fps=20, title=None, intro_s=1.8, outro_s=2.2):
    if not 1 <= speed <= 120 or not 1 <= fps <= 60:
        raise ValueError("speed must be 1..120 and fps must be 1..60")
    run_path, output = Path(run_path), Path(output)
    run = json.loads(run_path.read_text())
    final_time = run["final_observation"]["time_s"]
    end = final_time if end is None else min(end, final_time)
    if not 0 <= start < end:
        raise ValueError("clip must satisfy 0 <= start < end <= recorded final time")
    frames = max(1, math.ceil((end-start)/speed*fps))
    times = [min(end, start + index*speed/fps) for index in range(frames)]
    samples, metrics = reconstruct(run, times)
    title = title or run["configuration"]["scenario"].replace("_", " ").title()
    renderer = RadarRenderer(run, title, start, end, speed)
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        raise RuntimeError("ffmpeg was not found on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    poster_path = output.with_suffix(".jpg")
    renderer.frame(samples[min(len(samples)-1, len(samples)//3)]).save(poster_path, quality=92)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
               "-video_size", f"{W}x{H}", "-framerate", str(fps), "-i", "-", "-an", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    written = 0
    try:
        intro = renderer.frame(samples[0], overlay="intro").tobytes()
        for _ in range(round(intro_s*fps)):
            process.stdin.write(intro)
            written += 1
        for index, observation in enumerate(samples):
            process.stdin.write(renderer.frame(observation).tobytes())
            written += 1
            if index and index % 200 == 0:
                print(f"{output.name}: frame {index}/{len(samples)}", file=sys.stderr, flush=True)
        outro = renderer.frame(samples[-1], overlay="outro").tobytes()
        for _ in range(round(outro_s*fps)):
            process.stdin.write(outro)
            written += 1
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("ffmpeg did not encode the replay successfully")
    except BaseException:
        process.kill()
        process.wait()
        raise
    report = {"title": title, "source": str(run_path), "video": str(output), "poster": str(poster_path),
              "scenario": run["configuration"]["scenario"], "seed": run["configuration"]["seed"],
              "provenance": source_label(run)[0], "start_s": start, "end_s": end, "speed": speed,
              "fps": fps, "width": W, "height": H, "frames": written, "duration_s": written/fps,
              "bytes": output.stat().st_size, "full_episode_metrics_verified": True, "metrics": metrics}
    output.with_suffix(".render.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def publish_gallery(report, description=None):
    """Publish a rendered replay in the local gallery, preserving other clips."""
    web = ROOT / "atc_bench" / "web"
    output = Path(report["video"]).resolve()
    output.relative_to(web)  # Gallery assets must be under the static web root.
    source = output.with_suffix(".run.json")
    original = Path(report["source"])
    if original.resolve() != source:
        shutil.copyfile(original, source)
    manifest_path = output.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    entry = {"id": output.stem, "title": report["title"], "scenario": report["scenario"],
             "description": description or "Recorded controller commands and measured outcomes, replayed through the full simulator.",
             "video": "/" + output.relative_to(web).as_posix(),
             "poster": "/" + Path(report["poster"]).resolve().relative_to(web).as_posix(),
             "source": "/" + source.relative_to(web).as_posix(), "provenance": report["provenance"],
             "start": report["start_s"], "end": report["end_s"], "speed": report["speed"],
             "duration_s": report["duration_s"], "metrics_verified": report["full_episode_metrics_verified"]}
    manifest = [entry] + [item for item in manifest if item["id"] != entry["id"]]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--speed", type=float, default=24)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--title")
    parser.add_argument("--gallery", action="store_true", help="add this video to its directory's gallery manifest")
    parser.add_argument("--description", help="gallery caption when --gallery is used")
    args = parser.parse_args()
    report = render(args.run_json, args.output, start=args.start, end=args.end,
                    speed=args.speed, fps=args.fps, title=args.title)
    if args.gallery:
        publish_gallery(report, args.description)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
