# Run the interactive web demo

The human-controller demo lets you experience the benchmark from the model's perspective. Inspect the radar and the model's text telemetry, choose clickable commands, keep a plan and see what happens during the next two simulated minutes. It uses the same flight dynamics and scores as the recorded LLM runs.

## Quick start

You need **Python 3.10 or newer**, Git and a modern web browser. The simulator and web server use Python's standard library. You do not need an API key, a model download, Node.js or a paid account to play.

```sh
git clone https://github.com/MaxxP0/hackathonAirTraffic.git
cd hackathonAirTraffic
python3 -m atc_bench serve --port 8000
```

On Windows, use `py -3` in place of `python3` if necessary.

Keep the terminal running and open:

| Page | Local address |
| --- | --- |
| **Play as the controller** | http://127.0.0.1:8000/human.html |
| Radar and simulation controls | http://127.0.0.1:8000/ |
| Recorded model results | http://127.0.0.1:8000/benchmark-results.html |
| Scenario videos | http://127.0.0.1:8000/replays.html |

The server binds to your own computer. A teammate should clone the repository and run these commands on their machine. A `127.0.0.1` link does not share your running server over the internet. GitHub contains the files, results and presentation, but does not run the Python simulation.

## Play an episode

1. Open the human-controller page, choose a scenario and start an episode. Seed **7** matches the published LLM comparison.
2. Inspect your episode’s radar and read the aircraft, runway and weather telemetry. Click an aircraft on the radar to select it in the command builder; zoom in to inspect the airport. The map updates after each decision and shows observed aircraft, runway closures, weather cells and current conflicts. Expand the exact model observation and instructions to inspect what the LLM receives.
3. Review the prefilled operational plan and edit it if useful. This field persists across turns, so you can keep future sequencing and emergency priorities in it.
4. Use the clickable command builder: select an aircraft, choose an action, select its runway or value, and add it to the queue. Repeat for additional commands. Remove an item if you change your mind. You can optionally edit the plan and add a brief decision summary.
5. Submit the decision. The simulator applies the batch and advances **120 simulated seconds**, then pauses again. An empty batch lets existing clearances continue.
6. Check accepted and rejected commands, events, proximity penalties and waiting times before your next decision. After **15 turns / 30 simulated minutes**, inspect your final score and download your decisions.

The available scenarios are a surprise runway closure, emergency arrivals and a wind reversal. Their future events stay hidden until they occur. Your last four observation/decision exchanges and latest plan provide memory, just as they do for the model controller.

Human sessions are separate from the main radar episode and never call a language model. Reloading resumes the browser's session while the server remains running. Restarting the server clears sessions. The server retains up to 32 human sessions and expires the oldest when another starts.

## Scores and safety

Higher benchmark scores are better. Compare the score with completed flights, diversions, unfinished traffic, emergencies and separation losses.

- Ground wait measures departure queue time.
- Landing wait measures sector entry to touchdown, including normal approach flight. Pending and failed arrivals remain in the statistics, with outcomes reported separately.
- Emergency wait begins at the public emergency declaration.
- Below **3 NM horizontally and 1,000 ft vertically**, a pair incurs **0.5 penalty points per simulated second**.
- Below **0.06 NM horizontally and 100 ft vertically**, the simulator records a collision, with a **1,000,000-point penalty** plus crash penalties.

The simulation pauses while you think. Your wall-clock thinking time does not affect simulated waiting. See [the scoring specification](../docs/SCORING.md) for the full formula.

## Presentation and videos

[Download the PowerPoint](../presentation/ATC-Benchmark-Luna-vs-GLM.pptx). It compares GPT-6 Luna and GLM 5.3 Flash and embeds three Luna scenario videos. Open it in desktop PowerPoint and click a video during Slide Show. The video files travel inside the deck.

The video gallery also works without a model or API key. All MP4s are already included in the repository. Pillow and FFmpeg are needed only if you want to render new videos, as described in the [main README](../README.md#event-videos).

## Optional: let a model control the radar

For a local model, start LM Studio's OpenAI-compatible server on port 1234 and load a model, then run:

```sh
python3 -m atc_bench serve --agent lmstudio --base-url http://127.0.0.1:1234 --port 8000
```

For a hosted model, set your own `OPENROUTER_API_KEY` in the terminal environment, then run:

```sh
python3 -m atc_bench serve --agent openrouter --model openai/gpt-6-luna --port 8000
```

Never put the key in a committed file or the browser. The hosted adapter keeps a local $10 spending ledger. Also set your desired account-side key limit in OpenRouter. Starting the human demo still makes no model calls, even when the radar has a hosted controller selected.

## Troubleshooting

- **Address already in use:** stop the other server or use `--port 8001`, then open the same page on port 8001.
- **Page cannot connect:** run the command from the repository directory and keep its terminal open. Open an HTTP address above rather than double-clicking an HTML file.
- **Unknown or expired human session:** start a new episode. The server may have restarted.
- **Stale observation:** refresh the human page before resubmitting. The server rejects duplicate submissions against an earlier simulation time.
- **A command is rejected:** inspect the feedback. Rejected commands still count as a decision, and the simulation advances for that turn.
- **Video does not play in a slide preview:** use desktop PowerPoint Slide Show or the web video gallery. Embedded playback depends on the presentation viewer.

Press **Ctrl+C** in the server terminal to stop the demo.
