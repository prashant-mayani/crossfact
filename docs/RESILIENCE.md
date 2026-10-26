# Resilience: breaking it on purpose

The promise: **a run finishes inside its 600 s budget with whatever models
work, and says honestly what it is.** One provider failing, hanging, throttling
or running out of quota must not take the app down, make users wait for
nothing, or pass off one model's answer as a consensus. And every failure must
show up in metrics, traces and, when it matters, an alert.

Promises like that are only worth what has been tested. This page records the
tests: what was expected before each one, what actually happened, and what
changed because of it.

## How things get broken

| where | tool | what it can break |
|---|---|---|
| a laptop, in minutes | `scripts/gameday_sim.py` | any provider failure mode, against the real pipeline code |
| the real cluster, app level | admin page → *Faults*, or `kubectl -n crossfact exec deploy/crossfact -- python -m app.faults add <target> <mode>` | provider calls: `error`, `timeout`, `rate_limit`, `quota`, `auth`, `latency` |
| the real cluster, platform level | `scripts/game-day.sh` in the deploy repo | tunnel, pods, the node, Vault access, alert delivery |

Injected faults raise the same errors a real outage would and flow through
the same retry, breaker, metrics and tracing path. They always expire (at most
4 hours), so a forgotten experiment can't become an outage.

**The simulator** runs the real orchestrator, timeouts, retries, breakers,
fallbacks, run budget, fault injection and metrics. Only the models are fake:
each call sleeps for a modelled latency and returns canned JSON, and the clock
runs 100 times faster, so ten 10-minute runs take seconds. The latency model is
an assumption, not a measurement: medians of 55 s (Claude answer) and 24 s
(Gemini answer), smaller for comparator, debate and merge steps, with
log-normal noise; the comparator finds conflicts 45% of the time. It shows the
*shape* of the behaviour under failure. Real numbers come from the cluster
experiments below.

## The game-day template

Every experiment, simulated or real, is written down the same way
(`scripts/game-day.sh` starts the file for you):

1. **Hypothesis**, written before starting: what users will see, what the
   metrics and alerts will do, and how fast.
2. **Blast radius and abort**: what is affected, and the condition to stop early.
3. **Method**: the exact command and duration.
4. **What happened**, with timestamps, numbers, traces.
5. **Did the hypothesis hold?** Yes, no or partly, and why.
6. **Follow-ups**: code, alert or runbook changes, linked.

## Round 1: the simulator, before any fixes

Ten runs per scenario, two minutes apart, production registry (Claude Sonnet as
side A, Gemini 3.8 Flash as side B, Flash-Lite utility, fallbacks as in
`config/providers.example.yaml`), seed 7. Durations are simulated seconds.

| scenario | hypothesis (abridged) | held? | full results | p50 | p95 | over 240 s | what happened |
|---|---|---|---|---|---|---|---|
| no faults | all full results, p95 < 240 s | yes | 10/10 | 80 s | 164 s | 0 | baseline |
| Gemini 3.8 Flash out of daily quota | substitutes answer; runs stay two-model | yes | 10/10 | 75 s | 170 s | 0 | Gemini 3.6 Flash stood in on all 10 runs; zero retries on quota; the breaker paused calls to the exhausted model |
| all Gemini out of quota | Claude alone, labelled `side_b_failed`, in Claude's time | yes | 0/10 | 56 s | 82 s | 0 | as predicted |
| **Claude hangs** | Gemini alone, labelled; slow at first, then fast once the breaker opens | **partly** | 0/10 | 168 s | **600 s** | **5** | labelled correctly, but run 1 took the whole 600 s budget and every half-open trial cost a user 300 s, for an answer Gemini gave in 25 s |
| Claude token expired | fail fast, Gemini alone | yes | 0/10 | 25 s | 36 s | 0 | no retries; breaker opened after 3 auth failures |
| **Gemini 429 on half of calls** (retry in 20 s) | retries absorb it; few or no degraded runs | **no** | **8/10** | 118 s | 257 s | 1 | the breaker counted honoured 429s as failures: **12 openings** in 10 runs, turning throttling into 2 degraded answers |
| Gemini 503 on 30% of calls | retries absorb it | yes | 10/10 | 76 s | 175 s | 0 | 13 retries, 1 brief breaker opening, 1 substitution |
| **Gemini 90 s slower** | all over 240 s; long debates end degraded at the 600 s budget | **partly** | 10/10 | 310 s | **606 s** | 10 | two runs went *past* the budget (606 s) and none was labelled `deadline` |

### Findings and fixes

1. **A hung provider made users wait for nothing** (Claude hangs). The run
   waited for both sides even after one had answered.
   *Fix:* a straggler cutoff. Once one side has answered, the other gets
   `straggler_grace_s` (150 s) more, then the run continues without it,
   labelled `side_a_failed`; the stage records `straggler: true` and
   `crossfact_straggler_cutoffs_total` counts it (charted next to retries). The cut-off
   call keeps running in the background until its own timeout, so the breaker
   still learns the provider is sick. (`app/orchestrator.py::_gather`;
   `tests/test_pipeline.py::test_a_hung_side_is_cut_off_once_the_other_answers`)
2. **Throttling tripped the breaker** (Gemini 429). A 429 that says "retry in
   20 s" means the provider is pacing us, not failing; counting it opened the
   breaker and blocked the retries that would have worked.
   *Fix:* a rate limit with a retry hint we honour no longer counts against the
   breaker; an unhinted 429, or one asking for longer than our backoff cap,
   still does. (`app/resilience.py::trips_breaker`; two tests in
   `tests/test_resilience.py`)
3. **The fault injector itself was wrong** (Gemini slow). Injected latency was
   added on top of the call's timeout instead of spending it, so a "slow"
   provider could exceed limits a real slow provider can't.
   *Fix:* injected latency now counts against the call's own timeout; latency
   longer than the timeout becomes a timeout. (`app/providers.py::ask`; two
   tests in `tests/test_providers.py`)
4. **The cause was mislabelled** (found while re-running Gemini slow). A merge
   refused because the run budget was spent was reported as `merge_failed`,
   pointing at the merge model instead of the budget.
   *Fix:* a step refused for lack of time degrades as `deadline`, and the
   answer says the run used its whole budget. (`Run.failed`; two tests)

## Round 2: the same scenarios after the fixes

Same seed and setup, final code.

| scenario | full results | p50 | p95 | max | over 240 s | change |
|---|---|---|---|---|---|---|
| no faults | 10/10 | 75 s | 169 s | 169 s | 0 | none (as it should be) |
| Gemini 3.8 Flash out of quota | 10/10 | 90 s | 175 s | 175 s | 0 | none |
| all Gemini out of quota | 0/10 | 56 s | 82 s | 82 s | 0 | none |
| **Claude hangs** | 0/10 | 170 s | **179 s** | **179 s** | **0** (was 5) | worst case 600 s → 179 s |
| Claude token expired | 0/10 | 25 s | 36 s | 36 s | 0 | none |
| **Gemini 429 on half of calls** | **10/10** (was 8/10) | 110 s | 368 s | 368 s | 2 | breaker openings 12 → 0 |
| Gemini 503 on 30% of calls | 10/10 | 88 s | 214 s | 214 s | 0 | none |
| **Gemini 90 s slower** | 8/10 | 310 s | 600 s | **600 s** (was 606 s) | 10 | the budget holds; the two late runs end as `deadline` |

Ten runs make a noisy p95: it is simply the slowest run. The scenarios whose
numbers moved most were re-run on the final code with 30 runs each:

| scenario (30 runs) | full results | p50 | p95 | max | over 240 s | breaker openings |
|---|---|---|---|---|---|---|
| no faults | 30/30 | 77 s | 204 s | 230 s | 0 | 0 |
| Claude hangs | 0/30 (all labelled `side_a_failed`) | **29 s** | 178 s | 179 s | 0 | 6 |
| Gemini 429 on half of calls | 29/30 | 141 s | 245 s | 282 s | 2 | 0 |
| Gemini 503 on 30% of calls | 30/30 | 83 s | 217 s | 233 s | 0 | 7 |

With Claude hanging, the median run takes the healthy model's time (29 s).
The first five runs paid the 150 s grace while the breaker was still learning
(a cut-off call reports its timeout only when it finally ends, minutes later);
after that, only the half-open trials did (runs 10, 18 and 26, as the cooldown
doubled). The one degraded
run in the 429 scenario had all three Gemini models throttled on every attempt,
roughly a 1-in-500 event at that rate; it ended in 122 s as `side_b_failed`.

### What the second round says

- **Every run finishes inside its budget, in every scenario**, and every
  degraded answer carries the right reason.
- **One provider hanging now costs a user at most about 150 s past the other
  model's answer**, and only until the breaker opens; after that, runs degrade
  in the healthy model's time. Each half-open trial still costs one user that
  150 s: the price of noticing recovery without a separate health checker.
- **Throttling now costs time instead of answers.** With half of Gemini's calls
  throttled, every run stayed a full two-model result, but runs that hit
  several 20 s waits ran past 240 s. That is the trade this app should make (a
  slower full answer beats a fast half answer), and the latency SLO is what
  will say when throttling becomes chronic: then lower effort, cap debate
  rounds, or move a side to a model with more headroom.
- **A provider that is merely slow is the expensive failure.** Nothing breaks,
  so nothing trips: every run is a full result and every one is late. Only the
  latency SLO catches it, which is exactly why there is a latency SLO.

## Round 3: GPT-OSS as the judge

The judge moved to a third model family: GPT-OSS 120B on Groq's free tier,
the same weights on Workers AI when Groq's daily quota runs out, and only then
the sides' own families. Three scenarios were added; `always-contested` and the
two below make every question go to the judge (the comparator never finds
agreement), which ordinary scenarios rarely do. Groq and Workers AI latencies
are assumptions (8 s and 25 s for a ruling), like the rest of the latency model.
Raw output: [gamedays/2026-10-25-sim-round3-gpt-oss-judge.md](gamedays/2026-10-25-sim-round3-gpt-oss-judge.md).

| scenario (10 runs) | hypothesis | held? | full results | p50 | p95 | over 240 s | what happened |
|---|---|---|---|---|---|---|---|
| every question contested | three debate rounds, judged by GPT-OSS on Groq, inside the budget | yes | 10/10 judged | 190 s | 236 s | 0 | close to the latency threshold: three rounds plus a ruling is the slow path by design |
| Groq out of daily quota | the judge moves to Workers AI with no retries; still judged, a little slower; nothing degrades | **partly** | 10/10 judged | 241 s | 277 s | **5** | nothing degraded and there were no retries on quota, but Workers AI's slower ruling pushed half the contested runs past 240 s |
| GPT-OSS down on both hosts | after retries, Gemini judges; still judged; both breakers open | yes | 10/10 judged | 225 s | 281 s | 4 | 14 retries on the dead hosts before the breakers opened (12 openings counting re-opens after half-open trials); the transcript names the fallback judge |
| no faults, Gemini Flash quota, and the rest | unchanged from round 2 | yes | as round 2 | | | | the judge change touches only runs that need a ruling |

What it says: losing the free judge costs **time, not answers**. A day on which
Groq's quota is gone shows up as latency-SLO burn on contested questions, not as
degraded runs. If that becomes common, the levers are a judge effort of
`medium` instead of `high`, one debate round fewer, or Groq's paid tier. The
retries on a host that is down hard are the expensive part until its breaker
opens; three failures is the threshold.

One case the simulator doesn't model: a judge prompt bigger than Groq's free
per-minute token allowance (8,000 tokens; the prompt carries both full answers)
can never fit, so the answer "request too large" is a bad request, not a rate
limit: no retries, no breaker, the next judge at once
(`tests/test_providers.py::test_a_request_over_the_per_minute_token_limit_fails_fast`).
On long contested questions, expect Workers AI to rule.

### What the simulator can't tell you

Real latency distributions and their tails; failures that are correlated
(both providers behind the same network trouble); two OCPUs shared with
Prometheus and the Claude CLI's start-up; memory with several runs at
once; real `Retry-After` values; the tunnel and Cloudflare. Those need the
cluster.

## On the cluster: experiments to run

Pre-registered: the hypotheses are written now, before running them. Run them
on a quiet evening with `scripts/game-day.sh` from the deploy repo, then
replace "not run yet" with the date, what happened, and follow-ups.

| # | experiment | hypothesis | command | result |
|---|---|---|---|---|
| C1 | Gemini 3.8 Flash quota for 20 min | runs stay two-model through Gemini 3.6 Flash; `CrossfactQuotaExhausted` (info) appears; no SLO alert | `game-day.sh provider gemini/gemini-3.8-flash quota 20` | not run yet |
| C2 | Claude hangs for 15 min | answers arrive within ~150 s of Gemini's, labelled `side_a_failed`; `CrossfactCircuitOpen` after 10 min; traces show the cut-off span | `game-day.sh provider claude timeout 15` | not run yet |
| C3 | Gemini +90 s for 15 min | runs complete but late; exemplars on *Run duration* open traces dominated by Gemini spans; latency burn alert only with ≥ 6 runs | `game-day.sh provider gemini latency 15 90` | not run yet |
| C4 | app tunnel down 10 min | probes fail at once; `CrossfactAvailabilityFastBurn` reaches Telegram in 6-8 min; recovery within a minute of restoring | `game-day.sh tunnel-outage 10` | not run yet |
| C5 | web pod killed mid-run | run shows "interrupted" within ~2 min; quota refunded; `CrossfactRunsAbandoned` fires | `game-day.sh kill-pod` | not run yet |
| C6 | node reboot | in-flight runs finish before shutdown; serving again within ~3 min of boot; only availability budget spent | `game-day.sh node-reboot` | not run yet |
| C7 | Vault unreachable 40 min | app unaffected (Secrets already in the cluster); `ExternalSecretNotSynced` after 30 min | `game-day.sh vault-cut 40` | not run yet |
| C8 | Alertmanager down 20 min | no Telegram, but healthchecks.io emails within about 16 min (period 1 + grace 15) | `game-day.sh alertmanager-down 20` | not run yet |

Raw outputs of both laptop rounds are in [docs/gamedays/](gamedays/). Round 2 reflects the current code; re-run it with:

    python scripts/gameday_sim.py                  # all scenarios, 10 runs each
    python scripts/gameday_sim.py --runs 30 --scenario claude-hangs
