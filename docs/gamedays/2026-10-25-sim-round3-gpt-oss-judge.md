# Simulator round 3: GPT-OSS as the judge (2026-10-25)

Raw output of `scripts/gameday_sim.py --runs 10` after GPT-OSS 120B became the default
judge (Groq first, Workers AI second, then the sides' own families) and the
judge's points were labelled to match the shuffled answers. Groq and Workers AI
latencies are assumptions, like the rest of the model (see the script).

<!-- gameday_sim.py: 10 runs per scenario, 120 s apart, clock x100, seed 7 -->

### No faults (`baseline`)

*Hypothesis:* Every run ends as a full two-model result (agreed, debated or judged); no retries; p95 well under the 240 s latency threshold.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 74 s | 218 s | 218 s | 0 |

Model calls by result: ok 61. Retries: none. Breaker openings: 0. Substitutions: none.

### No faults, every question contested to the end (`always-contested`)

*Hypothesis:* Every run debates three rounds and is judged by GPT-OSS on Groq; runs take longer but stay inside the 600 s budget; no degraded runs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | judged 10 | - | 190 s | 236 s | 236 s | 0 |

Model calls by result: ok 140. Retries: none. Breaker openings: 0. Substitutions: none.

### Gemini 3.8 Flash out of daily quota (`gemini-flash-quota`)

*Hypothesis:* Side B fails fast (no retries on quota) and its configured substitute answers (Gemini 3.6 Flash); the judge is unaffected (GPT-OSS on Groq). Runs stay two-model, each recording the substitution; the success SLI is unaffected.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 72 s | 216 s | 216 s | 0 |

Model calls by result: circuit_open 3, ok 61, quota_exhausted 7. Retries: none. Breaker openings: 5. Substitutions: to gemini-3.6-flash 10.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | agreed | 61 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 2 | agreed | 71 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 3 | agreed | 70 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 4 | agreed | 72 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 5 | agreed | 72 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 6 | debated | 114 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 7 | agreed | 71 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 8 | debated | 216 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 9 | debated | 161 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |
| 10 | debated | 87 s | A sonnet; B gemini-3.6-flash (for gemini/gemini-3.8-flash) |

### Groq out of daily quota (the judge's first choice) (`groq-quota`)

*Hypothesis:* The judge moves to the same model on Workers AI with no retries on Groq; runs that need a ruling are still judged, a little slower; nothing degrades.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | judged 10 | - | 241 s | 277 s | 277 s | 5 |

Model calls by result: circuit_open 2, ok 140, quota_exhausted 8. Retries: none. Breaker openings: 6. Substitutions: none.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | judged | 207 s | A sonnet; B gemini-3.8-flash |
| 2 | judged | 253 s | A sonnet; B gemini-3.8-flash |
| 3 | judged | 213 s | A sonnet; B gemini-3.8-flash |
| 4 | judged | 230 s | A sonnet; B gemini-3.8-flash |
| 5 | judged | 204 s | A sonnet; B gemini-3.8-flash |
| 6 | judged | 183 s | A sonnet; B gemini-3.8-flash |
| 7 | judged | 253 s | A sonnet; B gemini-3.8-flash |
| 8 | judged | 269 s | A sonnet; B gemini-3.8-flash |
| 9 | judged | 277 s | A sonnet; B gemini-3.8-flash |
| 10 | judged | 259 s | A sonnet; B gemini-3.8-flash |

### GPT-OSS unavailable on both hosts (`gpt-oss-everywhere-down`)

*Hypothesis:* After the retries, the judge falls back to Gemini (a side's own family: the transcript says so); runs are still judged; both GPT-OSS breakers open after three failures.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | judged 10 | - | 225 s | 281 s | 281 s | 4 |

Model calls by result: circuit_open 18, ok 140, server 2. Retries: server 14. Breaker openings: 12. Substitutions: none.

Run by run:

| run | outcome | took | answered by |
|---|---|---|---|
| 1 | judged | 210 s | A sonnet; B gemini-3.8-flash |
| 2 | judged | 200 s | A sonnet; B gemini-3.8-flash |
| 3 | judged | 220 s | A sonnet; B gemini-3.8-flash |
| 4 | judged | 248 s | A sonnet; B gemini-3.8-flash |
| 5 | judged | 210 s | A sonnet; B gemini-3.8-flash |
| 6 | judged | 218 s | A sonnet; B gemini-3.8-flash |
| 7 | judged | 246 s | A sonnet; B gemini-3.8-flash |
| 8 | judged | 281 s | A sonnet; B gemini-3.8-flash |
| 9 | judged | 229 s | A sonnet; B gemini-3.8-flash |
| 10 | judged | 261 s | A sonnet; B gemini-3.8-flash |

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
| 3 | degraded (side_a_failed) | 173 s | B gemini-3.8-flash |
| 4 | degraded (side_a_failed) | 172 s | B gemini-3.8-flash |
| 5 | degraded (side_a_failed) | 168 s | B gemini-3.8-flash |
| 6 | degraded (side_a_failed) | 23 s | B gemini-3.8-flash |
| 7 | degraded (side_a_failed) | 36 s | B gemini-3.8-flash |
| 8 | degraded (side_a_failed) | 28 s | B gemini-3.8-flash |
| 9 | degraded (side_a_failed) | 35 s | B gemini-3.8-flash |
| 10 | degraded (side_a_failed) | 177 s | B gemini-3.8-flash |

### Claude token expired (401) (`claude-auth`)

*Hypothesis:* Auth errors are not retried: runs degrade to Gemini alone within Gemini's answer time; the AuthFailing alert's metric (calls with result=auth) rises with every run.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 0 | degraded 10 | side_a_failed 10 | 24 s | 36 s | 36 s | 0 |

Model calls by result: auth 6, circuit_open 5, ok 10. Retries: timeout 1. Breaker openings: 5. Substitutions: none.

### Gemini rate-limited on half of calls (429, retry in 20 s) (`gemini-429`)

*Hypothesis:* Retries honour the 20 s hint; nearly every run stays two-model but gets slower; retries by reason rate_limited show up; few or no degraded runs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 104 s | 346 s | 346 s | 1 |

Model calls by result: ok 61, rate_limited 3. Retries: rate_limited 29. Breaker openings: 0. Substitutions: none.

### Gemini returns 503 on 30% of calls (`gemini-flaky`)

*Hypothesis:* Jittered retries absorb almost all failures; outcomes match baseline; the breaker may open briefly after three failures in a row, then recover.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 6, debated 4 | - | 83 s | 222 s | 222 s | 0 |

Model calls by result: ok 61. Retries: server 12. Breaker openings: 0. Substitutions: none.

### Gemini 90 s slower on every call (`gemini-slow`)

*Hypothesis:* Runs complete but most exceed the 240 s threshold (latency SLO burn); longer debates hit the 600 s run budget and end degraded with reason deadline; nothing hangs.

| runs | full two-model | outcomes | degraded reasons | p50 | p95 | max | over 240 s |
|---|---|---|---|---|---|---|---|
| 10 | 10 | agreed 7, debated 1, judged 2 | - | 310 s | 509 s | 509 s | 10 |

Model calls by result: ok 51. Retries: none. Breaker openings: 0. Substitutions: none.

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
| 8 | judged | 502 s | A sonnet; B gemini-3.8-flash |
| 9 | agreed | 310 s | A sonnet; B gemini-3.8-flash |
| 10 | judged | 506 s | A sonnet; B gemini-3.8-flash |

