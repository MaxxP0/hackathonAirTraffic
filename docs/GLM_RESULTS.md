# GLM 5.3 Flash and GPT 6 Luna — 26 September 2026

Completed episodes: **GLM 5.3 Flash: 3 / 3**; **GPT 6 Luna: 3 / 3**. This is a small matched comparison, not a safety certification or a general model ranking.

[Interactive comparison](../atc_bench/web/benchmark-results.html) · [Event videos](../atc_bench/web/replays.html) · [Public record manifest](evaluations/manifest.json)

## Observed tradeoffs

- Across 3 completed scenarios, GLM 5.3 Flash completed 46 flights versus 42 for the matched reference policy, with 3,412 versus 583 separation-loss pair-seconds. Read individual safety and service outcomes below.
- In the emergency episode, GLM 5.3 Flash resolved 3 emergency landings with 0 failures; mean emergency wait was 510.7s versus reference 645.3s. Separation loss was 506 versus 136 pair-seconds, and mean departure queue wait 306.8s versus 296.8s.
- Across 3 completed scenarios, GPT 6 Luna completed 42 flights versus 42 for the matched reference policy, with 880 versus 583 separation-loss pair-seconds. Read individual safety and service outcomes below.
- GPT 6 Luna's runway closure episode ended with 3 failed arrival outcome(s), including diversions or crashes. Its landing-wait mean includes those flights and cannot by itself demonstrate better landing service.
- In the emergency episode, GPT 6 Luna resolved 3 emergency landings with 0 failures; mean emergency wait was 519.3s versus reference 645.3s. Separation loss was 378 versus 136 pair-seconds, and mean departure queue wait 466.8s versus 296.8s.

## Measurement conditions

- One seed (7), three synthetic scenarios, one trajectory per model/scenario; no uncertainty estimate or claim of general superiority.
- All controllers use the same 30-minute horizon and 120-second control windows.
- The simulation pauses during inference and advances immediately afterward; API latency is separate from simulated aircraft waiting.
- Both models use low reasoning effort, an 8,192-token output limit, four recent dialogue exchanges and a persistent operational plan. GLM uses temperature 0; Luna omits temperature because its provider catalog does not support that parameter.
- The three original GLM processes ran concurrently alongside two dashboard calls, sharing a provider budget and default prompt caching. All stopped after HTTP 429 interruptions at 1,440 simulated seconds. Saved GLM continuations restored simulator state and conversation memory: runway closure at 1,440s, 1,680s; emergency arrivals at 1,440s; wind shift at 1,440s. GPT 6 Luna ran sequentially between the interrupted GLM runs and the later GLM completion attempts. Two earlier closure decisions ran sequentially; the final Sail Research continuation then ran one closure, three emergency and three wind windows in sequence. An excluded duplicate emergency attempt through OpenInference overlapped this last phase; its calls and costs are outside the selected episode totals. Wall times are not an isolated throughput benchmark.
- Active wall time includes initial execution plus continuation/backoff, but excludes the manual checkpoint gap. Summed call latency includes failed controller attempts and internal request retries/backoff where recorded. Resumed runs retain their interruption in the replay.
- Per-run confirmed API cost sums replay decision.cost_usd; other tests/dashboard calls are excluded. Three original GLM HTTP 429 charges were reconciled at zero using provider-account usage; their historical records retain null cost.
- Recorded GLM routing: runway closure: price preference from 0s; throughput preference from 1,680s; throughput preference, pinned to deepinfra/fp4 from 1,680s; throughput preference, pinned to sail-research/fp8 from 1,680s | emergency arrivals: price preference from 0s; throughput preference, pinned to sail-research/fp8 from 1,440s | wind shift: price preference from 0s; throughput preference, pinned to sail-research/fp8 from 1,440s. Luna used throughput preference. The GLM model, prompt and provider price caps were retained across provider changes. Later timeout costs remain unconfirmed unless independently reconciled. Routing, provider/quantization changes and interruptions confound wall-time comparisons.
- Partial or failed runs receive no completed score or waiting-time comparison; their raw records remain available.
- Landing wait is arrival sector entry to touchdown, including normal approach flight, holding and go-arounds. Pending arrivals retain elapsed time at the horizon; failed diversions count from the diversion command but accrue airborne time until exit. Mean/max and score include all spawned arrivals. Landed outcome means touchdown, before rollout/taxi are complete. The supplemental score is 100/(1 + mean_seconds/600) and does not change the benchmark score or rank; read it with landed/pending/failed counts.
- Doing nothing can produce few separation losses while completing no flights. Read safety, completions, emergency outcomes and waiting together.
- The reference-policy event demonstration videos use 10-second control windows. Their video statistics are separate from this matched 120-second comparison; they are not substituted for the reference rows here.

## Completed-episode outcomes

Ground wait means the departure queue only. Emergency wait includes resolved, failed and pending emergencies. All wait and separation durations below are simulated time. An em dash means no applicable event or no completed result.

### Runway closure

| Controller | Completed / spawned | Score | Queue mean / max, min | Emergency mean / max, min | Resolved / failed / pending emergencies | Collisions / crashes | Separation events / pair-seconds | Rejected commands |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GLM 5.3 Flash | 13 / 25 | -311.9 | 5.95 / 15.03 | — / — | 0 / 0 / 0 | 0 / 0 | 21 / 1,107 | 10 |
| GPT 6 Luna | 13 / 25 | 121.5 | 5.11 / 6.52 | — / — | 0 / 0 / 0 | 0 / 0 | 3 / 132 | 0 |
| Reference policy | 14 / 25 | 324.2 | 4.95 / 6.52 | — / — | 0 / 0 / 0 | 0 / 0 | 2 / 69 | 0 |
| No commands | 0 / 25 | -973.5 | 21.78 / 30.00 | — / — | 0 / 0 / 0 | 0 / 0 | 0 / 0 | 0 |

Landing service diagnostic (arrival sector entry to touchdown, including normal approach time):

| Controller | Mean / max landing wait, min | Diagnostic score / 100 | Landed / pending / failed arrivals |
|---|---:|---:|---:|
| GLM 5.3 Flash | 13.94 / 30.00 | 41.8 | 7 / 6 / 0 |
| GPT 6 Luna | 14.79 / 21.43 | 40.3 | 5 / 5 / 3 |
| Reference policy | 16.50 / 26.63 | 37.7 | 5 / 8 / 0 |
| No commands | 15.19 / 17.77 | 39.7 | 0 / 3 / 10 |

All spawned arrivals contribute, including pending and failed arrivals. A landed outcome starts at touchdown; completed flights above require the later stand arrival. Wait score = 100 / (1 + mean seconds / 600); it is supplemental and does not change the benchmark score or rank. A short failed flight can reduce mean wait, so read this diagnostic with outcome counts.

#### GLM 5.3 Flash: execution and response evidence

15 successful decisions from 19 recorded attempts; $0.010077 confirmed; 2 request cost(s) remain unconfirmed. Mean / median call latency: 45.986 / 29.624 seconds. Summed API latency: 14.56 minutes. Active episode wall time: 14.57 minutes; simulation reached 30 / 30 minutes.

Resumed from T+28:00; prior observations, commands and plan memory were restored. Manual checkpoint waiting is excluded from active wall time.

Provider routing: price preference from T+00:00; throughput preference from T+28:00; throughput preference, pinned to deepinfra/fp4 from T+28:00; throughput preference, pinned to sail-research/fp8 from T+28:00. Recorded providers: DeepInfra, InferenceNet, Sail Research.

Recorded interruptions: T+24:00: OpenRouter returned HTTP 429; response details withheld; T+28:00: OpenRouter connection, timeout, or response-decoding failure; T+28:00: OpenRouter returned HTTP 429; response details withheld; T+28:00: OpenRouter returned HTTP 429: Provider returned error (DeepInfra).

2 interrupted request(s) reconciled at zero cost; [billing evidence](evaluations/billing-reconciliation.json).

**First decision after the closure.** Event at T+09:45; first model observation at T+10:00.
The observation marked 07L, 25R closed. 0 approach/takeoff assignments targeted an observed closed runway during closure observations.

- `APPROACH CFG101 25L` — accepted: CFG101 APPROACH accepted
- `ALTITUDE DLH112 6000` — accepted: DLH112 ALTITUDE accepted
- `TAKEOFF CFG113 25C` — accepted: CFG113 cleared for takeoff runway 25C

Closure go-arounds can be initiated automatically by the simulator; they are not proof of a model command.

Memory evidence: 14 / 14 consecutive successful decision inputs contain the exact previous output plan; the final input reports 14 prior successful decisions and 4 retained dialogue turns. The final controller counter is 15. This verifies persisted inputs, not the quality of the plan.

<details><summary>Final recorded operational plan</summary>

AFR116 on 25L final (5.5NM, 1595ft) lands ~2min; CFG101 on 25L intercept (7.8NM) behind it — spacing OK. DLH118 on 25C final (4.6NM) with UAL120 trailing on intercept (10NM) — spacing fine. AFR116/DLH118 flagged 1.52NM on converging finals but different runways; DLH118 lands first, monitor next window. UAL108 (3.2NM, 6000ft): APPROACH 25L, slots behind CFG101. AFR122 (holding 9000ft): APPROACH 25R behind DLH124 (2.6NM intercept, lands first). Departures: TAKEOFF BAW121 18 (free). CFG119/KLM123 queue: CFG119 on 25L after UAL108 lands, KLM123 on 25C after UAL120. KLM117/BAW115 outbound clear sector. Next window: launch CFG119, verify AFR116/DLH118 resolved, slot remaining arrivals.

</details>

[Interruption record at T+24:00](evaluations/glm-5.3-flash-runway_closure-seed7-interrupted-1440s-1failures.json)

[Interruption record at T+28:00](evaluations/glm-5.3-flash-runway_closure-seed7-interrupted-1680s-3failures.json)

[Interruption record at T+28:00](evaluations/glm-5.3-flash-runway_closure-seed7-interrupted-1680s-4failures.json)

[Interruption record at T+28:00](evaluations/glm-5.3-flash-runway_closure-seed7-interrupted-1680s-2failures.json)

#### GPT 6 Luna: execution and response evidence

15 successful decisions from 15 recorded attempts; $0.022365 confirmed. Mean / median call latency: 4.078 / 3.873 seconds. Summed API latency: 1.02 minutes. Active episode wall time: 1.02 minutes; simulation reached 30 / 30 minutes.

Provider routing: throughput preference from T+00:00. Recorded providers: OpenAI.

**First decision after the closure.** Event at T+09:45; first model observation at T+10:00.
The observation marked 07L, 25R closed. 0 approach/takeoff assignments targeted an observed closed runway during closure observations.

- `APPROACH CFG101 25C` — accepted: CFG101 APPROACH accepted
- `TAKEOFF BAW109 18` — accepted: BAW109 cleared for takeoff runway 18

Closure go-arounds can be initiated automatically by the simulator; they are not proof of a model command.

Memory evidence: 14 / 14 consecutive successful decision inputs contain the exact previous output plan; the final input reports 14 prior successful decisions and 4 retained dialogue turns. The final controller counter is 15. This verifies persisted inputs, not the quality of the plan.

<details><summary>Final recorded operational plan</summary>

Preserve the existing runway reservations: UAL114 on 25R, AFR116 on 25L, and DLH118 on 25C. UAL114 is very close to touchdown; do not interrupt its final. Keep UAL120 holding at 11,000 ft, AFR122 at 9,000 ft, and DLH124 at 7,000 ft until runway capacity frees, maintaining their altitude separation. Runway 18 has a 65-second availability timer; do not launch another departure until clear, then prioritize KLM123 if still waiting. Reassess touchdown, runway status, and holds at the next observation.

</details>

Raw records: [GLM 5.3 Flash](evaluations/glm-5.3-flash-runway_closure-seed7.json) · [GPT 6 Luna](evaluations/gpt-6-luna-runway_closure-seed7.json) · [Reference policy](evaluations/reference-runway_closure-seed7.json) · [No commands](evaluations/noop-runway_closure-seed7.json)

### Emergency arrivals

| Controller | Completed / spawned | Score | Queue mean / max, min | Emergency mean / max, min | Resolved / failed / pending emergencies | Collisions / crashes | Separation events / pair-seconds | Rejected commands |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GLM 5.3 Flash | 16 / 25 | 24.5 | 5.11 / 6.52 | 8.51 / 10.63 | 3 / 0 / 0 | 0 / 0 | 10 / 506 | 3 |
| GPT 6 Luna | 16 / 25 | 99.6 | 7.78 / 14.52 | 8.66 / 11.30 | 3 / 0 / 0 | 0 / 0 | 7 / 378 | 1 |
| Reference policy | 15 / 25 | 179.5 | 4.95 / 6.52 | 10.76 / 15.13 | 3 / 0 / 0 | 0 / 0 | 4 / 136 | 0 |
| No commands | 0 / 25 | -401,266.0 | 21.78 / 30.00 | 16.17 / 16.65 | 0 / 3 / 0 | 0 / 1 | 0 / 0 | 0 |

Landing service diagnostic (arrival sector entry to touchdown, including normal approach time):

| Controller | Mean / max landing wait, min | Diagnostic score / 100 | Landed / pending / failed arrivals |
|---|---:|---:|---:|
| GLM 5.3 Flash | 13.91 / 22.22 | 41.8 | 8 / 5 / 0 |
| GPT 6 Luna | 13.35 / 23.98 | 42.8 | 9 / 4 / 0 |
| Reference policy | 15.10 / 28.07 | 39.8 | 7 / 6 / 0 |
| No commands | 15.19 / 17.77 | 39.7 | 0 / 3 / 10 |

All spawned arrivals contribute, including pending and failed arrivals. A landed outcome starts at touchdown; completed flights above require the later stand arrival. Wait score = 100 / (1 + mean seconds / 600); it is supplemental and does not change the benchmark score or rank. A short failed flight can reduce mean wait, so read this diagnostic with outcome counts.

#### GLM 5.3 Flash: execution and response evidence

15 successful decisions from 16 recorded attempts; $0.009519 confirmed. Mean / median call latency: 23.899 / 20.815 seconds. Summed API latency: 6.37 minutes. Active episode wall time: 6.38 minutes; simulation reached 30 / 30 minutes.

Resumed from T+24:00; prior observations, commands and plan memory were restored. Manual checkpoint waiting is excluded from active wall time.

Provider routing: price preference from T+00:00; throughput preference, pinned to sail-research/fp8 from T+24:00. Recorded providers: InferenceNet, Sail Research.

Recorded interruptions: T+24:00: OpenRouter returned HTTP 429; response details withheld.

1 interrupted request(s) reconciled at zero cost; [billing evidence](evaluations/billing-reconciliation.json).

**Emergency response: DLH100.** Event at T+00:15; first model observation at T+02:00.
Final status: landed; emergency wait: 274 simulated seconds.

No command for this emergency aircraft was issued in that first observed decision.

This is the first model observation containing the declared emergency. Simulated emergency wait is measured from declaration to touchdown or terminal failure, not API latency.

**Emergency response: UAL108.** Event at T+04:22; first model observation at T+06:00.
Final status: landed; emergency wait: 620 simulated seconds.

- `APPROACH UAL108 25C` — accepted: UAL108 APPROACH accepted

This is the first model observation containing the declared emergency. Simulated emergency wait is measured from declaration to touchdown or terminal failure, not API latency.

**Emergency response: UAL114.** Event at T+11:02; first model observation at T+12:00.
Final status: landed; emergency wait: 638 simulated seconds.

- `APPROACH UAL114 25C` — accepted: UAL114 APPROACH accepted

This is the first model observation containing the declared emergency. Simulated emergency wait is measured from declaration to touchdown or terminal failure, not API latency.

Memory evidence: 14 / 14 consecutive successful decision inputs contain the exact previous output plan; the final input reports 14 prior successful decisions and 4 retained dialogue turns. The final controller counter is 15. This verifies persisted inputs, not the quality of the plan.

<details><summary>Final recorded operational plan</summary>

DLH112 on final 25C (~4NM); DLH118 intercepting 25R, AFR116 intercepting 25L. APPROACH UAL120 25C now (transit covers DLH112 landing). APPROACH AFR122 25L (long transit covers AFR116 landing). DLH124 stays 7000ft, APPROACH 25R next window after DLH118 clears. KLM123 climbing out of 18. Recheck DLH112 touchdown and spacing between DLH118/AFR116 finals next window.

</details>

[Interruption record at T+24:00](evaluations/glm-5.3-flash-emergency-seed7-interrupted-1440s-1failures.json)

#### GPT 6 Luna: execution and response evidence

15 successful decisions from 15 recorded attempts; $0.023106 confirmed. Mean / median call latency: 4.226 / 4.345 seconds. Summed API latency: 1.06 minutes. Active episode wall time: 1.06 minutes; simulation reached 30 / 30 minutes.

Provider routing: throughput preference from T+00:00. Recorded providers: OpenAI.

**Emergency response: DLH100.** Event at T+00:15; first model observation at T+02:00.
Final status: landed; emergency wait: 274 simulated seconds.

No command for this emergency aircraft was issued in that first observed decision.

This is the first model observation containing the declared emergency. Simulated emergency wait is measured from declaration to touchdown or terminal failure, not API latency.

**Emergency response: UAL108.** Event at T+04:22; first model observation at T+06:00.
Final status: landed; emergency wait: 606 simulated seconds.

- `APPROACH UAL108 25L` — accepted: UAL108 APPROACH accepted

This is the first model observation containing the declared emergency. Simulated emergency wait is measured from declaration to touchdown or terminal failure, not API latency.

**Emergency response: UAL114.** Event at T+11:02; first model observation at T+12:00.
Final status: landed; emergency wait: 678 simulated seconds.

- `APPROACH UAL114 25R` — accepted: UAL114 APPROACH accepted

This is the first model observation containing the declared emergency. Simulated emergency wait is measured from declaration to touchdown or terminal failure, not API latency.

Memory evidence: 14 / 14 consecutive successful decision inputs contain the exact previous output plan; the final input reports 14 prior successful decisions and 4 retained dialogue turns. The final controller counter is 15. This verifies persisted inputs, not the quality of the plan.

<details><summary>Final recorded operational plan</summary>

Protect AFR110’s SOUTH/25L final; DLH112 is still at intercept and conflicts with UAL120 at 2.898 NM/279.5 ft, so go around DLH112 and reassess separation next turn. Do not reassign SOUTH until AFR110 lands and DLH112 is safely clear; AFR122 remains inbound without an approach clearance. UAL120 continues on CENTER/25C; protect that runway through touchdown. DLH124 remains inbound at 11000 ft; monitor its separation from the approaches before assigning a runway. Runway 18 is available, so KLM123 is cleared for takeoff; queue remaining departures until it clears.

</details>

Raw records: [GLM 5.3 Flash](evaluations/glm-5.3-flash-emergency-seed7.json) · [GPT 6 Luna](evaluations/gpt-6-luna-emergency-seed7.json) · [Reference policy](evaluations/reference-emergency-seed7.json) · [No commands](evaluations/noop-emergency-seed7.json)

### Wind shift

| Controller | Completed / spawned | Score | Queue mean / max, min | Emergency mean / max, min | Resolved / failed / pending emergencies | Collisions / crashes | Separation events / pair-seconds | Rejected commands |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GLM 5.3 Flash | 17 / 25 | -451.0 | 5.45 / 8.42 | — / — | 0 / 0 / 0 | 0 / 0 | 23 / 1,799 | 7 |
| GPT 6 Luna | 13 / 25 | 107.5 | 6.11 / 10.52 | — / — | 0 / 0 / 0 | 0 / 0 | 9 / 370 | 1 |
| Reference policy | 13 / 25 | 120.3 | 4.95 / 6.52 | — / — | 0 / 0 / 0 | 0 / 0 | 6 / 378 | 0 |
| No commands | 0 / 25 | -973.2 | 21.78 / 30.00 | — / — | 0 / 0 / 0 | 0 / 0 | 0 / 0 | 0 |

Landing service diagnostic (arrival sector entry to touchdown, including normal approach time):

| Controller | Mean / max landing wait, min | Diagnostic score / 100 | Landed / pending / failed arrivals |
|---|---:|---:|---:|
| GLM 5.3 Flash | 13.65 / 26.63 | 42.3 | 8 / 5 / 0 |
| GPT 6 Luna | 16.00 / 26.63 | 38.5 | 5 / 8 / 0 |
| Reference policy | 16.43 / 28.82 | 37.8 | 5 / 8 / 0 |
| No commands | 14.73 / 17.50 | 40.4 | 0 / 3 / 10 |

All spawned arrivals contribute, including pending and failed arrivals. A landed outcome starts at touchdown; completed flights above require the later stand arrival. Wait score = 100 / (1 + mean seconds / 600); it is supplemental and does not change the benchmark score or rank. A short failed flight can reduce mean wait, so read this diagnostic with outcome counts.

#### GLM 5.3 Flash: execution and response evidence

15 successful decisions from 16 recorded attempts; $0.009460 confirmed. Mean / median call latency: 21.649 / 17.882 seconds. Summed API latency: 5.77 minutes. Active episode wall time: 5.78 minutes; simulation reached 30 / 30 minutes.

Resumed from T+24:00; prior observations, commands and plan memory were restored. Manual checkpoint waiting is excluded from active wall time.

Provider routing: price preference from T+00:00; throughput preference, pinned to sail-research/fp8 from T+24:00. Recorded providers: InferenceNet, Sail Research.

Recorded interruptions: T+24:00: OpenRouter returned HTTP 429; response details withheld.

1 interrupted request(s) reconciled at zero cost; [billing evidence](evaluations/billing-reconciliation.json).

**First decision after the wind shift.** Event at T+13:30; first model observation at T+14:00.
The observation showed active flow 07.

- `APPROACH DLH112 07C` — accepted: DLH112 APPROACH accepted
- `APPROACH AFR110 07L` — accepted: AFR110 APPROACH accepted
- `HOLD UAL108` — accepted: UAL108 HOLD accepted
- `APPROACH UAL114 07R` — accepted: UAL114 APPROACH accepted
- `TAKEOFF CFG113 18` — accepted: CFG113 cleared for takeoff runway 18

Accepted new-flow clearances show a command response; separation and throughput outcomes still determine its quality.

Memory evidence: 14 / 14 consecutive successful decision inputs contain the exact previous output plan; the final input reports 14 prior successful decisions and 4 retained dialogue turns. The final controller counter is 15. This verifies persisted inputs, not the quality of the plan.

<details><summary>Final recorded operational plan</summary>

AFR122 and UAL120 finals converged (1.4NM/93ft): GO_AROUND AFR122 to restore separation; re-clear AFR122 APPROACH 07L next window once clear of UAL120. UAL120 lands 07C shortly; then APPROACH UAL108 07C. DLH118 went around and is climbing to 4000ft near the field; re-clear APPROACH 07R after DLH124 lands. DLH124 continues 07R final. KLM123 TAKEOFF 18 now. Keep UAL108 holding until UAL120 clears 07C.

</details>

[Interruption record at T+24:00](evaluations/glm-5.3-flash-wind_shift-seed7-interrupted-1440s-1failures.json)

#### GPT 6 Luna: execution and response evidence

15 successful decisions from 15 recorded attempts; $0.023295 confirmed. Mean / median call latency: 4.673 / 4.341 seconds. Summed API latency: 1.17 minutes. Active episode wall time: 1.17 minutes; simulation reached 30 / 30 minutes.

Provider routing: throughput preference from T+00:00. Recorded providers: OpenAI.

**First decision after the wind shift.** Event at T+13:30; first model observation at T+14:00.
The observation showed active flow 07.

- `APPROACH DLH106 07C` — accepted: DLH106 APPROACH accepted
- `TAKEOFF CFG113 18` — accepted: CFG113 cleared for takeoff runway 18

Accepted new-flow clearances show a command response; separation and throughput outcomes still determine its quality.

Memory evidence: 14 / 14 consecutive successful decision inputs contain the exact previous output plan; the final input reports 14 prior successful decisions and 4 retained dialogue turns. The final controller counter is 15. This verifies persisted inputs, not the quality of the plan.

<details><summary>Final recorded operational plan</summary>

AFR110 remains committed to NW/07L until landing and runway clearance. DLH112 has landed, freeing SOUTH/07R. UAL114 is at 13,000 ft and the prior UAL114–UAL120 conflict is no longer present; clear UAL114 for CENTER/07C and monitor its intercept and spacing against AFR110. Keep AFR116, DLH118, UAL108, UAL120, AFR122, and DLH124 on their current clearances; sequence only after checking altitude and proximity. KLM123 remains queued for departure; use runway 18 when available and suitable. Reassess final progress, runway occupancy, and conflicts next turn.

</details>

Raw records: [GLM 5.3 Flash](evaluations/glm-5.3-flash-wind_shift-seed7.json) · [GPT 6 Luna](evaluations/gpt-6-luna-wind_shift-seed7.json) · [Reference policy](evaluations/reference-wind_shift-seed7.json) · [No commands](evaluations/noop-wind_shift-seed7.json)

## Provenance and limits

Published JSON files retain full synthetic initial/final observations, compact model observations, command results, plans, latencies and per-call costs. Local machine paths and budget reservation IDs are removed. SHA-256 hashes are in the manifest. Regenerate this report from the saved outputs with `python3 docs/evaluations/build_glm_report.py`.

Unexpected closures, wind changes and emergency declarations are hidden until they occur. The airport, dynamics, wake handling and ground queues are simplified. Efficiency scores must never substitute for safety outcomes. More seeds, traffic densities and model replicates are needed before broader conclusions.
