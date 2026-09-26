# Local Qwen evaluation — 26 September 2026

The current local model was `qwen3.8-27b-splash` in LM Studio. It did not
complete an episode in either recorded configuration. These are failures of the
tested configurations, not a claim about all Qwen deployments.

## Current persistent controller

Benchmark 0.3.0, prompt `frankfurt-controller-v3`, emergency scenario, seed 7,
1,800-second horizon, 300-second decision windows. Temperature 0; 8,192 output
tokens; 900-second timeout. The loaded model's default reasoning was enabled.
The controller supports four recent exchanges and a persistent operational plan.

The first request took **419.706 seconds** and exhausted all **8,192 output
tokens on reasoning**. It returned `finish_reason: length` without a command
response. The harness rejected it and kept the simulation at **0 seconds**.
No model commands or persistent-plan exchanges were completed, so this run has
**no completed benchmark score**. Prompt usage was 3,254 tokens.

The matched 300-second-window reference controller completed 11 of 25 flights,
resolved all three emergencies, had no collisions, averaged 13.03 minutes of
departure-queue waiting and 11.22 minutes of emergency waiting, and scored
−111.8. The no-op controller completed zero flights and failed all three
emergencies. Neither full result should be compared to Qwen's zero-time partial
waiting metrics.

## Earlier stateless configuration

The earlier `frankfurt-controller-v2` experiment disabled reasoning and used
60-second windows on mixed traffic, seed 7. It reached 780 simulated seconds,
then truncated a repeated command response. Thirteen decisions succeeded and the
fourteenth failed; total inference time was 305.881 seconds. It did not finish
the planned 1,800-second episode.

At the matched 780-second cutoff, preserving the original 1,800-second traffic
schedule:

| Controller | Flights completed | Mean departure queue | Separation loss, pair-seconds | Collisions |
|---|---:|---:|---:|---:|
| Qwen, stateless | 2 | 5.18 min | 1,191 | 0 |
| Reference | 3 | 3.93 min | 69 | 0 |

Qwen issued 76 commands, of which 39 were rejected. One emergency was still
pending after 255 seconds in both trajectories. This partial experiment is
legacy evidence; it does not validate the current persistent controller.

## Interpretation

The local configuration currently fails response completion before a valid
full-episode comparison is possible. Keeping simulation time paused avoids
charging inference latency to aircraft waiting scores, but does not solve model
latency or output-limit failures. Hosted GLM results use a different model and
explicit low reasoning effort and must be reported separately.

Ground waiting includes unfinished departures. Emergency waiting includes
pending and failed aircraft and must be read alongside their outcomes. These
small synthetic experiments are not operational aviation safety validation.
