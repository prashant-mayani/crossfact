# Simulator round 1: before the fixes (2026-10-22)

Raw output of `scripts/gameday_sim.py --runs 10` against the code as it was before the four
fixes described in docs/RESILIENCE.md. Kept as the record of what the first game day found.

<!-- gameday_sim.py: 10 runs per scenario, 120 s apart, clock x100, seed 7 -->

### No faults (`baseline`)

*Hypothesis:* Every run ends as a full two-model result (agreed, debated or judged); no retries; p95 well under the 240 s latency threshold.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 80 s | 164 s | 164 s | 0 |

Model calls by result: ok 61. Retries: none. Breaker openings: 0. Substitutions: none.

### Gemini 3.8 Flash out of daily quota (`gemini-flash-quota`)

*Hypothesis:* Side B and the judge fail fast (no retries on quota) and their configured substitutes answer: Gemini 3.6 Flash for side B, Claude Sonnet as judge. Runs stay two-model, each recording the substitution; the success SLI is unaffected.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 75 s | 170 s | 170 s | 0 |

Model calls by result: circuit_open 3, ok 61, quota_exhausted 7. Retries: none. Breaker openings: 5. Substitutions: to gemini-3.6-flash 10.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | agreed | 61 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 2 | agreed | 71 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 3 | agreed | 71 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 4 | agreed | 72 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 5 | agreed | 72 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 6 | debated | 112 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 7 | agreed | 77 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 8 | debated | 170 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 9 | debated | 161 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 10 | debated | 87 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |

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
| 10 | 0 | degraded 10 | side_a_failed 10 | 168 s | 600 s | 600 s | 5 |

Model calls by result: circuit_open 9, ok 10, timeout 1. Retries: timeout 5. Breaker openings: 4. Substitutions: none.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | degraded (side_a_failed) | 600 s | B gemini-3.8-flash |
| 2 | degraded (side_a_failed) | 301 s | B gemini-3.8-flash |
| 3 | degraded (side_a_failed) | 301 s | B gemini-3.8-flash |
| 4 | degraded (side_a_failed) | 22 s | B gemini-3.8-flash |
| 5 | degraded (side_a_failed) | 302 s | B gemini-3.8-flash |
| 6 | degraded (side_a_failed) | 22 s | B gemini-3.8-flash |
| 7 | degraded (side_a_failed) | 36 s | B gemini-3.8-flash |
| 8 | degraded (side_a_failed) | 28 s | B gemini-3.8-flash |
| 9 | degraded (side_a_failed) | 303 s | B gemini-3.8-flash |
| 10 | degraded (side_a_failed) | 27 s | B gemini-3.8-flash |

### Claude token expired (401) (`claude-auth`)

*Hypothesis:* Auth errors are not retried: runs degrade to Gemini alone within Gemini's answer time; the AuthFailing alert's metric (calls with result=auth) rises with every run.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 0 | degraded 10 | side_a_failed 10 | 25 s | 36 s | 36 s | 0 |

Model calls by result: auth 6, circuit_open 4, ok 10. Retries: none. Breaker openings: 4. Substitutions: none.

### Gemini rate-limited on half of calls (429, retry in 20 s) (`gemini-429`)

*Hypothesis:* Retries honour the 20 s hint; nearly every run stays two-model but gets slower; retries by reason rate_limited show up; few or no degraded runs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 8 | agreed 7, debated 1, degraded 2 | side_b_failed 2 | 118 s | 257 s | 257 s | 1 |

Model calls by result: circuit_open 15, ok 40, rate_limited 6. Retries: rate_limited 32. Breaker openings: 12. Substitutions: none.

### Gemini returns 503 on 30% of calls (`gemini-flaky`)

*Hypothesis:* Jittered retries absorb almost all failures; outcomes match baseline; the breaker may open briefly after three failures in a row, then recover.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 76 s | 175 s | 175 s | 0 |

Model calls by result: ok 61, server 1. Retries: server 13. Breaker openings: 1. Substitutions: to gemini-3.6-flash 1.

### Gemini 90 s slower on every call (`gemini-slow`)

*Hypothesis:* Runs complete but most exceed the 240 s threshold (latency SLO burn); longer debates hit the 600 s run budget and end degraded with reason deadline; nothing hangs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 7, debated 1, judged 2 | - | 310 s | 606 s | 606 s | 10 |

Model calls by result: ok 51. Retries: none. Breaker openings: 0. Substitutions: none.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | agreed | 309 s | A sonnet; B gemini-3.8-flash |
| 2 | agreed | 307 s | A sonnet; B gemini-3.8-flash |
| 3 | agreed | 308 s | A sonnet; B gemini-3.8-flash |
| 4 | agreed | 322 s | A sonnet; B gemini-3.8-flash |
| 5 | agreed | 305 s | A sonnet; B gemini-3.8-flash |
| 6 | debated | 508 s | A sonnet; B gemini-3.8-flash |
| 7 | agreed | 311 s | A sonnet; B gemini-3.8-flash |
| 8 | judged | 606 s | A sonnet; B gemini-3.8-flash |
| 9 | agreed | 310 s | A sonnet; B gemini-3.8-flash |
| 10 | judged | 605 s | A sonnet; B gemini-3.8-flash |

