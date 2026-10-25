# Simulator round 2: after the fixes (2026-10-22)

Raw output of `scripts/gameday_sim.py --runs 10` on the final code.

<!-- gameday_sim.py: 10 runs per scenario, 120 s apart, clock x100, seed 7 -->

### No faults (`baseline`)

*Hypothesis:* Every run ends as a full two-model result (agreed, debated or judged); no retries; p95 well under the 240 s latency threshold.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 75 s | 169 s | 169 s | 0 |

Model calls by result: ok 61. Retries: none. Breaker openings: 0. Substitutions: none.

### Gemini 3.8 Flash out of daily quota (`gemini-flash-quota`)

*Hypothesis:* Side B and the judge fail fast (no retries on quota) and their configured substitutes answer: Gemini 3.6 Flash for side B, Claude Sonnet as judge. Runs stay two-model, each recording the substitution; the success SLI is unaffected.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 90 s | 175 s | 175 s | 0 |

Model calls by result: circuit_open 3, ok 61, quota_exhausted 7. Retries: none. Breaker openings: 5. Substitutions: to gemini-3.6-flash 10.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | agreed | 61 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 2 | agreed | 62 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 3 | agreed | 75 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 4 | agreed | 102 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 5 | agreed | 72 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 6 | debated | 112 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 7 | agreed | 77 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 8 | debated | 175 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 9 | debated | 165 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 10 | debated | 104 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |

### All Gemini models out of daily quota (`gemini-all-quota`)

*Hypothesis:* Side B has no working substitute, so every run finishes with Claude's answer alone, labelled degraded (side_b_failed), in about Claude's answer time. Zero retries.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 0 | degraded 10 | side_b_failed 10 | 56 s | 82 s | 82 s | 0 |

Model calls by result: circuit_open 9, ok 10, quota_exhausted 21. Retries: none. Breaker openings: 15. Substitutions: none.

### Claude hangs on every call (`claude-hangs`)

*Hypothesis:* Runs still finish with Gemini's answer, labelled degraded (side_a_failed). The first runs wait out Claude's 300 s timeout; after 3 failures the breaker opens and later runs degrade in about Gemini's answer time, except one half-open trial per cooldown.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 0 | degraded 10 | side_a_failed 10 | 170 s | 179 s | 179 s | 0 |

Model calls by result: circuit_open 7, ok 10, timeout 2. Retries: timeout 5. Breaker openings: 3. Substitutions: none.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | degraded (side_a_failed) | 173 s | B gemini-3.8-flash |
| 2 | degraded (side_a_failed) | 179 s | B gemini-3.8-flash |
| 3 | degraded (side_a_failed) | 172 s | B gemini-3.8-flash |
| 4 | degraded (side_a_failed) | 172 s | B gemini-3.8-flash |
| 5 | degraded (side_a_failed) | 168 s | B gemini-3.8-flash |
| 6 | degraded (side_a_failed) | 22 s | B gemini-3.8-flash |
| 7 | degraded (side_a_failed) | 36 s | B gemini-3.8-flash |
| 8 | degraded (side_a_failed) | 28 s | B gemini-3.8-flash |
| 9 | degraded (side_a_failed) | 35 s | B gemini-3.8-flash |
| 10 | degraded (side_a_failed) | 177 s | B gemini-3.8-flash |

### Claude token expired (401) (`claude-auth`)

*Hypothesis:* Auth errors are not retried: runs degrade to Gemini alone within Gemini's answer time; the AuthFailing alert's metric (calls with result=auth) rises with every run.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 0 | degraded 10 | side_a_failed 10 | 25 s | 36 s | 36 s | 0 |

Model calls by result: auth 6, circuit_open 5, ok 10. Retries: timeout 1. Breaker openings: 5. Substitutions: none.

### Gemini rate-limited on half of calls (429, retry in 20 s) (`gemini-429`)

*Hypothesis:* Retries honour the 20 s hint; nearly every run stays two-model but gets slower; retries by reason rate_limited show up; few or no degraded runs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 110 s | 368 s | 368 s | 2 |

Model calls by result: ok 61, rate_limited 2. Retries: rate_limited 32. Breaker openings: 0. Substitutions: none.

### Gemini returns 503 on 30% of calls (`gemini-flaky`)

*Hypothesis:* Jittered retries absorb almost all failures; outcomes match baseline; the breaker may open briefly after three failures in a row, then recover.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 88 s | 214 s | 214 s | 0 |

Model calls by result: ok 61. Retries: server 12. Breaker openings: 0. Substitutions: none.

### Gemini 90 s slower on every call (`gemini-slow`)

*Hypothesis:* Runs complete but most exceed the 240 s threshold (latency SLO burn); longer debates hit the 600 s run budget and end degraded with reason deadline; nothing hangs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 8 | agreed 7, debated 1, degraded 2 | deadline 2 | 310 s | 600 s | 600 s | 10 |

Model calls by result: deadline 2, ok 49, timeout 2. Retries: none. Breaker openings: 0. Substitutions: none.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | agreed | 309 s | A sonnet; B gemini-3.8-flash |
| 2 | agreed | 307 s | A sonnet; B gemini-3.8-flash |
| 3 | agreed | 308 s | A sonnet; B gemini-3.8-flash |
| 4 | agreed | 322 s | A sonnet; B gemini-3.8-flash |
| 5 | agreed | 305 s | A sonnet; B gemini-3.8-flash |
| 6 | debated | 509 s | A sonnet; B gemini-3.8-flash |
| 7 | agreed | 311 s | A sonnet; B gemini-3.8-flash |
| 8 | degraded (deadline) | 600 s | A sonnet; B gemini-3.8-flash |
| 9 | agreed | 300 s | A sonnet; B gemini-3.8-flash |
| 10 | degraded (deadline) | 600 s | A sonnet; B gemini-3.8-flash |


## 30 runs each, final code

<!-- gameday_sim.py: 30 runs per scenario, 120 s apart, clock x100, seed 7 -->

### No faults (`baseline`)

*Hypothesis:* Every run ends as a full two-model result (agreed, debated or judged); no retries; p95 well under the 240 s latency threshold.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 30 | 30 | agreed 19, debated 10, judged 1 | - | 77 s | 204 s | 230 s | 0 |

Model calls by result: ok 190. Retries: none. Breaker openings: 0. Substitutions: none.

### Claude hangs on every call (`claude-hangs`)

*Hypothesis:* Runs still finish with Gemini's answer, labelled degraded (side_a_failed). The first runs wait out Claude's 300 s timeout; after 3 failures the breaker opens and later runs degrade in about Gemini's answer time, except one half-open trial per cooldown.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 30 | 0 | degraded 30 | side_a_failed 30 | 29 s | 178 s | 179 s | 0 |

Model calls by result: circuit_open 28, ok 30, timeout 2. Retries: timeout 8. Breaker openings: 6. Substitutions: none.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | degraded (side_a_failed) | 172 s | B gemini-3.8-flash |
| 2 | degraded (side_a_failed) | 179 s | B gemini-3.8-flash |
| 3 | degraded (side_a_failed) | 172 s | B gemini-3.8-flash |
| 4 | degraded (side_a_failed) | 172 s | B gemini-3.8-flash |
| 5 | degraded (side_a_failed) | 168 s | B gemini-3.8-flash |
| 6 | degraded (side_a_failed) | 23 s | B gemini-3.8-flash |
| 7 | degraded (side_a_failed) | 36 s | B gemini-3.8-flash |
| 8 | degraded (side_a_failed) | 28 s | B gemini-3.8-flash |
| 9 | degraded (side_a_failed) | 35 s | B gemini-3.8-flash |
| 10 | degraded (side_a_failed) | 177 s | B gemini-3.8-flash |
| 11 | degraded (side_a_failed) | 28 s | B gemini-3.8-flash |
| 12 | degraded (side_a_failed) | 26 s | B gemini-3.8-flash |
| 13 | degraded (side_a_failed) | 14 s | B gemini-3.8-flash |
| 14 | degraded (side_a_failed) | 33 s | B gemini-3.8-flash |
| 15 | degraded (side_a_failed) | 29 s | B gemini-3.8-flash |
| 16 | degraded (side_a_failed) | 29 s | B gemini-3.8-flash |
| 17 | degraded (side_a_failed) | 14 s | B gemini-3.8-flash |
| 18 | degraded (side_a_failed) | 163 s | B gemini-3.8-flash |
| 19 | degraded (side_a_failed) | 18 s | B gemini-3.8-flash |
| 20 | degraded (side_a_failed) | 21 s | B gemini-3.8-flash |
| 21 | degraded (side_a_failed) | 27 s | B gemini-3.8-flash |
| 22 | degraded (side_a_failed) | 24 s | B gemini-3.8-flash |
| 23 | degraded (side_a_failed) | 29 s | B gemini-3.8-flash |
| 24 | degraded (side_a_failed) | 19 s | B gemini-3.8-flash |
| 25 | degraded (side_a_failed) | 27 s | B gemini-3.8-flash |
| 26 | degraded (side_a_failed) | 178 s | B gemini-3.8-flash |
| 27 | degraded (side_a_failed) | 19 s | B gemini-3.8-flash |
| 28 | degraded (side_a_failed) | 44 s | B gemini-3.8-flash |
| 29 | degraded (side_a_failed) | 29 s | B gemini-3.8-flash |
| 30 | degraded (side_a_failed) | 37 s | B gemini-3.8-flash |

### Gemini rate-limited on half of calls (429, retry in 20 s) (`gemini-429`)

*Hypothesis:* Retries honour the 20 s hint; nearly every run stays two-model but gets slower; retries by reason rate_limited show up; few or no degraded runs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 30 | 29 | agreed 17, debated 9, degraded 1, judged 3 | side_b_failed 1 | 141 s | 245 s | 282 s | 2 |

Model calls by result: ok 159, rate_limited 22. Retries: rate_limited 101. Breaker openings: 0. Substitutions: to gemini-3.6-flash 7.

### Gemini returns 503 on 30% of calls (`gemini-flaky`)

*Hypothesis:* Jittered retries absorb almost all failures; outcomes match baseline; the breaker may open briefly after three failures in a row, then recover.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 30 | 30 | agreed 19, debated 10, judged 1 | - | 83 s | 217 s | 233 s | 0 |

Model calls by result: circuit_open 3, ok 190, server 5. Retries: server 47. Breaker openings: 7. Substitutions: to gemini-3.6-flash 5.

