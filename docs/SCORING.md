# Benchmark outcomes and scoring

These rules describe the synthetic benchmark. They are not operational aviation standards.

Version **0.3.0** keeps the version 0.2 score formula and paused-inference timing, adds persistent operational plans/recent dialogue, and supports larger control windows. Saved version 0.2 stateless-model experiments are legacy evidence, not validation of the new controller. Match window sizes and other settings before comparing outcomes. Version 0.1 used different scalar weights.

## Ranking

The saved `rank_key` is compared lexicographically, with **lower values better**. Its fields are, in order:

1. Collision pairs.
2. Crashed aircraft, including fuel-exhaustion crashes.
3. Failed emergencies.
4. Runway incursions.
5. Wake violations.
6. Separation-loss pair-seconds.
7. Weather-exposure aircraft-seconds.
8. Negative completed traffic: `-(landed + departed)`.
9. Total ground seconds.
10. Total airborne seconds.

Compare safety outcomes before efficiency. A policy can finish more traffic and still rank worse because it has safety violations. The reference controller is a baseline with residual separation losses, not a guarantee of safety or optimality. Read the safety rank alongside completion, diverted and unfinished counts. An inactive policy can avoid separation losses while serving no traffic, so the safety rank alone is not a useful overall leaderboard; the scalar score also penalizes non-service and rewards completed flights.

## Scalar score

Let `completion_rate = (landed + departed) / max(1, spawned)`.

```text
efficiency = 1000 * completion_rate
           - 50 * diverted
           - 10 * unfinished
           - 100 * crashed
           - 0.2 * ground_delay_seconds / 60
           - 1 * ground_wait_seconds / 60
           - 5 * emergency_wait_seconds / 60
           - 0.05 * airborne_seconds / 60
           - 0.5 * invalid_commands
           - 0.5 * separation_loss_seconds
           - 0.2 * weather_exposure_seconds
           - 100 * runway_incursions
           - 50 * wake_violations

score = clamp(efficiency, -9999, 9999)
      - 1000000 * collisions
      - 100000 * emergencies_failed
      - 100000 * crashed
```

Higher scalar scores are better. The bounded efficiency term prevents throughput from offsetting even one collision, crash or failed emergency relative to an episode with none of these outcomes. The scalar score is a convenience for plotting; it does not have exactly the same ordering as the lexicographic rank.

The two explicit waiting components are `ground_wait_score_component = -ground_wait_seconds / 60` and `emergency_wait_score_component = -5 * emergency_wait_seconds / 60`. They are signed contributions to the efficiency term before clipping. Departure queue time also remains part of total modeled ground time, so it incurs both the existing 0.2 points/minute ground-time cost and the additional 1 point/minute queue cost. Emergency response time also remains part of airborne time. These priorities are benchmark design choices, not operational cost estimates. The lexicographic `rank_key` retains its original ten fields for compatibility.

## Waiting diagnostics

Each aircraft exposes `ground_wait_s`, `emergency_wait_s`, `emergency_declared_time_s` and `emergency_touchdown_time_s`. Timers use **simulated seconds**. The clock pauses while the model reasons, then advances the configured control window after its commands are applied. Ten minutes of inference costs ten minutes of wall time but does not increase simulated waiting or burn fuel. Model latency is reported separately in wall-clock seconds.

- **Ground waiting:** a departure accumulates queue time from spawning ready for departure while its status is `ground`. The timer stops as soon as an accepted takeoff clearance starts the takeoff roll. It excludes takeoff/landing roll and the fixed taxi-in time. All spawned departures form the denominator, including departures with zero waiting and departures still queued at the measurement time. Unspawned flights and arrivals are excluded.
- **Emergency waiting:** elapsed time from the public emergency declaration until touchdown. It includes approach flight time and is therefore response time, not just holding delay. Crash or sector exit freezes the observed timer; an unfinished flight accumulates through the measurement time or episode horizon. Missing the emergency deadline records a failure but does not stop the timer while the aircraft still needs to land. Subsequent touchdown records its timestamp even after a deadline failure. All declared emergencies form the denominator, including pending and failed emergencies; future scheduled emergencies are never exposed or counted.

Totals are `ground_wait_seconds` and `emergency_wait_seconds`; denominators are `ground_wait_count` and `emergency_wait_count`. Each family also provides `*_mean_seconds` and `*_max_seconds`. Empty populations have a total/count of zero and a mean/max/diagnostic score of JSON `null`.

The delay diagnostics range from 0 to 100 (higher is better):

```text
ground_wait_score    = 100 / (1 + ground_wait_mean_seconds / 300)
emergency_wait_score = 100 / (1 + emergency_wait_mean_seconds / 180)
```

Thus zero waiting scores 100; a five-minute mean departure queue scores 50; a three-minute mean emergency response scores 50. These are **delay diagnostics, not standalone rankings**: an early crash may have little observed waiting, so always read safety and service outcomes alongside them. Metrics are rounded to three decimal places; scalar calculations use unrounded timers.

`emergency_wait_by_outcome` contains `resolved`, `pending` and `failed` groups, each with `count`, `seconds`, `mean_seconds` and `max_seconds`. Resolved means successful touchdown before a recorded failure; pending means neither successful touchdown nor failure yet; failed includes missed deadlines, crashes and diversions. The groups are disjoint and cover every declared emergency. A failed emergency can still be airborne and accumulating waiting time, or can subsequently touch down, without moving into the resolved group.

These means include **censored observations**: unfinished queues and emergencies have only their observed waiting time, while crashes and diversions have their observed time until termination, not a successful landing time. Ending an episode early can artificially reduce a mean. Compare the same scenario, seed, horizon and control-window length, and report the outcome breakdown and unfinished counts. Include wall-clock duration and actual controller latency to distinguish simulated performance from computational throughput. Larger windows reduce the number of model calls but also give the controller fewer chances to react. Do not compare successful emergency response times using the overall mean; use the resolved subgroup and report how many emergencies failed or remain pending.

These full metrics are available through the environment, radar/HTTP state and saved results. The LM Studio `compact-v2` input supplies per-aircraft waiting timers, the two waiting totals and their 0–100 diagnostics, but omits aggregate waiting counts, means, maxima, signed components and outcome groups. Use the full result metrics for evaluation rather than treating the compact decision input as a complete metric report. Model request latency is a separate wall-clock measurement and does not itself contribute to simulated waiting or the score. Waiting, fuel burn, deadline failures and other outcomes evolve during the simulated window after a decision. High-level windows are split into at most 60-second environment steps; movement and safety remain integrated in substeps no longer than one second.

## Metric interpretation

| Metric | Definition |
| --- | --- |
| `collisions` | Distinct colliding aircraft pairs, using swept motion through each simulation substep. Collision envelope: less than 0.06 NM horizontally and 100 ft vertically at overlapping times. |
| `crashed` | Number of aircraft ending in the crashed state, including collisions and fuel exhaustion. One collision pair normally creates two crashed aircraft. |
| `separation_losses` | Distinct continuous pair-conflict intervals: less than 3 NM horizontally and 1,000 ft vertically. A pair that recovers separation and loses it again starts another interval. |
| `separation_loss_seconds` | Sum of conflicted pair-seconds, evaluated in substeps of at most one second. Several simultaneous pairs can produce more pair-seconds than elapsed simulation time. |
| `runway_incursions` | Unsafe arrival sequencing detected near an occupied runway and followed by an automatic go-around. Invalid takeoff commands are rejected before creating an incursion. |
| `wake_violations` | Arrival sequencing that violates the benchmark's runway wake timer and triggers a go-around. |
| `weather_exposure_seconds` | Airborne aircraft-seconds inside hazardous weather cells. |
| `emergency_landings` | Emergency aircraft that touch down before failure; they need not have finished taxiing yet. |
| `emergencies_failed` | Emergencies whose deadlines are missed, whose aircraft crash, or whose aircraft leave the sector without landing. Each emergency can fail once. |
| `emergencies_unresolved` | Announced emergencies with neither successful touchdown nor recorded failure. This includes deadlines extending beyond the episode horizon. |
| `landed` | Arrivals that complete their landing roll and abstract taxi-in. |
| `departed` | Departures that take off and leave the 40 NM simulated sector. |
| `diverted` | Arrivals that leave the sector, including aircraft the controller lets fly out without an explicit diversion command. |
| `ground_delay_seconds` | Total time in ground queue, takeoff roll, landing roll and taxi-in states. This is total modeled ground time, not delay above an ideal schedule. |
| `ground_wait_seconds` | Total departure queue time; includes observed waiting of unfinished departures. |
| `emergency_wait_seconds` | Total elapsed response time after declared emergencies, including pending/failed flights, frozen at touchdown, crash or sector exit. |
| `airborne_seconds` | Total modeled airborne time, including unresolved aircraft. |
| `unfinished` | Aircraft not yet landed, departed, diverted or crashed when measured. |
| `completion_rate` | Successfully landed/departed aircraft divided by spawned traffic. |

Ground and airborne time continue accumulating for unfinished aircraft. Do not drop unfinished or diverted traffic from a comparison. Always include episode duration and pending emergency counts: stopping before an emergency deadline is not evidence that the emergency was resolved. Benchmark summaries retain each run's outcomes as well as means so that rare failures stay inspectable. A model error leaves the world at its previous valid state; an interrupted evaluation is partial and must not be compared to a full-horizon baseline. Reference/no-op baselines use the same control windows without model requests.

For a given scenario and applied command timeline, the simulator is deterministic. Model outputs can still vary across runs. Retain simulation timestamps, model/prompt/reasoning configuration, control window, simulated horizon, model latency and elapsed `wall_duration_s`; replay the saved commands at their recorded simulation timestamps to reproduce the trajectory. Do not infer end-to-end computational speed from the instantaneous simulation advance or a single unusually fast model call.

## Simplifications that affect results

The simulation uses bounded turn/climb/speed changes, automatic approach guidance, synthetic fuel budgets, shared reciprocal-runway occupancy, leader/follower wake timers and adverse-weather runway checks. A wet runway increases the modeled distance requirement by 15%; low visibility extends wake intervals. Approach assignments need flight time to reach and capture the intercept, so a clearance is not immediate landing credit.

Taxi-in currently lasts 120 seconds after landing-roll completion. There is no taxi routing, gate competition, pushback planning or turnaround optimization. Future ground-activity benchmarks should report additional metrics rather than interpreting current queue time as a detailed surface-operations model.
