# Design

How Crossfact works inside, and why each part is the way it is. The cluster and
cloud side (k3s, Argo CD, tunnels, Vault, policy) is described in the deploy
repository's `docs/ARCHITECTURE.md`.

## 1. The pipeline

```
question ─┬─▶ side A answers ─┐
          └─▶ side B answers ─┴─▶ comparator ─▶ agreements, unique points, conflicts
                                                     │
                     conflicts? ── no ──────────────▶│
                        │ yes                        │
                        ▼                            │
             debate, conflicts only (≤3 rounds,      │
             settled points locked, in budget)       │
                        │ still conflicting          │
                        ▼                            │
             anonymized judge rules ────────────────▶ merge: the union of what
                                                      survived, with rulings
```

- **Both sides answer independently and in parallel.** Neither sees the other
  first, so agreement means something.
- **The comparator only extracts** (agreements, each side's unique points,
  conflicts) and returns JSON. It is a cheap, fast utility model with minimal
  thinking: this is reading, not reasoning.
- **Debate is about the conflicts only.** Each side sees the settled points as
  fixed and argues only the open topics. Rounds stop at three, or earlier when
  the remaining time budget couldn't fit another round plus the judge and merge
  (`reserve_judge_s`, `reserve_merge_s`).
- **The judge doesn't know who is who.** Positions are presented as A/B in a
  random order and the ruling is mapped back, so a judge from the same family
  as one side can't favour it by name.
- **The answer is a union, not a vote**: everything both agreed on, each side's
  unique points, and the judge's rulings, merged by the utility model.

Modes: `consensus` (the above), `union` (no debate or judge; conflicts are shown
side by side), `single` (one model, labelled as such).

### Outcomes say exactly what you got

| outcome | meaning |
|---|---|
| `agreed` | full two-model result, no conflicts |
| `debated` | conflicts settled by debate |
| `judged` | conflicts settled by the judge |
| `union` | union mode result |
| `single` | single mode, as asked |
| `degraded` | an answer, but less than asked for; the reason is shown (below) |
| `error` | no answer (both sides failed); the daily quota is refunded |
| `abandoned` | the pod running it died; refunded, the UI offers Retry |

Degraded reasons: `side_a_failed` / `side_b_failed` (one model failed or was
too slow; the answer is one model's, and the UI says it is not a consensus),
`comparator_failed` and `merge_failed` (both answers shown in full),
`judge_failed` (open conflicts shown side by side), `deadline` (the run budget
ran out before a step; whatever was unfinished is shown as it stood).

The consensus-success SLO counts only `agreed`, `debated` and `judged` as good.
A run answered by a substitute model is still a two-model result and counts as
good; the transcript says who stood in.

## 2. Providers

`config/providers.yaml` is the registry: every model the UI offers, how each
provider is called, and its failure-handling knobs. Adding a model is one line;
adding an OpenAI-compatible provider is one block plus a secret.

| kind | how | effort becomes |
|---|---|---|
| `claude_cli` | Claude Code in print mode, with your Pro/Max token | `--effort low/medium/high/max` ("off" maps to low: current Claude models can't turn thinking off) |
| `gemini_api` | `generateContent`, key in the `x-goog-api-key` header | Gemini 3.x `thinkingLevel`, clamped to the levels each model accepts; Gemini 2.5 `thinkingBudget` |
| `gemini_cli` | Gemini CLI with its one-time OAuth login | the CLI's defaults |
| `openai` | any OpenAI-compatible endpoint | `reasoning_effort` where supported |

If the Gemini API rejects a thinking config, the call is retried once without
it. A 429 is read properly: a `RetryInfo` hint means "per-minute limit, retry
in N s"; a `QuotaFailure` mentioning a per-day quota means the free daily quota
is gone, which no retry fixes.

### Claude Code, locked down

The Claude side runs the real Claude Code CLI so it can use a Pro/Max plan.
That CLI is an agent that can read files and run tools, so it is fenced in:

- `--restricted`, `--permission-mode dontAsk`, `--no-session-persistence`,
  `--disallowedTools "mcp__*"`, `--max-turns` 2 (8 with files)
- `--tools ""` for text questions; only `Read` when the user attached files
- settings that block reads outside the working directory and deny `/proc`
- a fresh working directory per call, holding only that call's files, deleted after
- an environment containing its own token and nothing else (no Vault paths, no
  service-account token, no other provider's key)
- the whole process group is killed on timeout

## 3. Failure handling

Every model call goes through the same path: fault injection (game days) →
per-provider timeout → retries with backoff → per-model circuit breaker →
metrics and a trace span. Adapters only translate a request and classify
failures.

| kind | retried | trips the breaker | typical cause |
|---|---|---|---|
| `timeout` | yes | yes | model too slow for the per-call timeout |
| `rate_limited` | yes, if the hint fits the budget | only without a retry hint we honour | per-minute 429 |
| `quota_exhausted` | no | yes | daily free quota gone |
| `server`, `network`, `unknown` | yes | yes | 5xx, resets, DNS |
| `auth` | no | yes | expired or wrong key/token |
| `bad_request` | no | no | our request was wrong |
| `circuit_open` | no | no | the breaker refused; nothing was sent |
| `deadline` | no | no | not enough run budget left to try |

Why each rule:

- **Full-jitter backoff** (`uniform(0, min(cap, base·2^n))`) so retries from
  parallel calls don't synchronise. A `Retry-After` hint is honoured as given.
- **A timeout is not retried with less than half the time that just failed.**
  If 120 s wasn't enough, 30 s won't be.
- **Retries never outlive the run.** Every call's timeout is capped by the time
  left in the 600 s run budget, and a call with under 5 s left is not started.
- **Breakers are per provider/model**, so Gemini Flash being out of quota
  doesn't stop Gemini Flash-Lite. After 3 consecutive failures the breaker
  opens; after the cooldown one trial call is let through (half-open); a failed
  trial doubles the cooldown, up to a cap. An open breaker fails a call in
  microseconds instead of making a user wait for a timeout.
- **Hinted 429s don't count against the breaker.** The provider is pacing us,
  not failing; we wait as asked. (The first game day showed what counting them
  does: see RESILIENCE.md.)
- **Substitutes**: when a side's model fails after its own retries, the
  configured fallbacks for that model are tried in order. Utility and judge
  steps have fallback chains too, so the judge can be Claude when Gemini is out.
- **Straggler cutoff**: once one side has answered, the other gets
  `straggler_grace_s` (150 s) more, then the run continues without it. Its call
  keeps running in the background until its own timeout and the result is
  dropped. Without this, a hung provider made every user wait out its full
  timeout for an answer the other model had given in 25 s.
- **Failing honestly**: a run that loses a side, a step, or its budget still
  returns what it has, labelled with the reason. Nothing pretends to be
  consensus.

Fault injection (`app/faults.py`) uses the same path: it raises the same
errors a real outage would, in modes `error`, `timeout`, `rate_limit`, `quota`,
`auth`, `latency`, for a target (`provider`, `provider/model` or `*`), with a
probability, and always with an expiry (at most 4 hours). Faults live in the
database, so every pod sees them within seconds.

## 4. Lifecycle: deploys never cut a run short

A consensus run takes one to ten minutes, far longer than an HTTP request, so
it runs in a background thread and the browser polls for progress.

**Graceful drain.** On SIGTERM (a deploy, a node drain, a reboot) the server
stops accepting new questions (`/ask` returns 503 "restarting", `/readyz` fails
so the Service stops routing new requests here), keeps answering polls, waits
for the runs in flight, then exits. The numbers are chained so this always fits:

    run budget 600 s  <  drain timeout 630 s  <  pod grace period 690 s  <  node shutdown grace 720 s

The chart refuses to render if the grace period doesn't exceed the run budget
by 60 s, and the node's kubelet gives pods 690 s on shutdown, so a kernel-update
reboot drains too. Rollouts surge one pod (`maxSurge: 1`, `maxUnavailable: 0`):
the new pod takes new questions while the old one finishes its runs.

**Two pods, one SQLite file.** During that overlap both pods share the
node-local volume. SQLite in WAL mode with `BEGIN IMMEDIATE` transactions and a
busy timeout handles two writers on one host; migrations are additive only, so
old code keeps working against a newer schema for those few minutes.

**Orphans.** A pod that dies without draining (OOM kill, crash, forced delete)
leaves runs "running" forever. Each pod refreshes a heartbeat on its runs every
15 s; any pod closes out runs whose heartbeat is older than 90 s, marks them
`abandoned`, refunds the user's daily quota, and counts them in
`crossfact_orphaned_runs_total` (which alerts).

## 5. Telemetry

- **Metrics** on port 9095, a separate listener the tunnel never routes:
  runs by mode and outcome, run duration, degraded reasons, model calls by
  provider/model/purpose/result, call duration, retries by cause, sides cut
  off as stragglers, breaker state and transitions, substitutions, HTTP
  traffic, drain state, active faults. No prompt text, no user names, nothing
  that could leak content.
- **Traces**: one `consensus.run` span per run with a `provider.call` child per
  model call (provider, model, effort, purpose, attempts, result, retry
  events). Context is carried into the worker threads, so parallel calls nest
  correctly. Exported over OTLP/HTTP to Tempo.
- **Exemplars**: latency histograms carry the trace ID of a sampled run, so a
  slow dot on a Grafana panel opens the trace that shows which provider held it
  up.
- **Dashboards** are generated from Python (`charts/crossfact/dashboards/generate.py`)
  so they are diffable; CI fails if the committed JSON is stale.

## 6. Security

- **Accounts**: argon2id passwords, TOTP 2FA with QR enrolment, server-side
  sessions referenced by a signed cookie (revocable), CSRF tokens, lockout
  after 5 failures per user, per IP or per TOTP (15 min). Cloudflare's edge
  rate limit on `/login` and `/totp` is the outer layer.
- **Privacy between users**: every thread, message and file query is scoped by
  owner; admins manage users, not other users' content.
- **Headers**: strict CSP (no inline script; boot data travels as a JSON
  script block), HSTS, `X-Frame-Options: DENY`, same-origin referrers, nosniff.
- **Secrets**: read from files that External Secrets keeps in sync with OCI
  Vault, re-read every 60 s, so a rotated token is picked up without a
  restart. Placeholder values (`UNSET`) count as missing. TOTP seeds are
  encrypted with a key that exists only in Vault; any random material is
  stretched into a Fernet key with HKDF, so Vault can generate it.
- **The pod**: non-root, read-only root filesystem, no capabilities, seccomp
  RuntimeDefault, and NetworkPolicies that let it reach the internet but not
  the cluster, the node, or the cloud's metadata service (which would let it
  act as the VM). Backups run as separate Jobs that alone may use the VM's
  identity.
- **The image**: built in CI, scanned, signed; the cluster admits only images
  signed by this repository's main-branch pipeline.

## 7. Backups

Nightly: a consistent SQLite snapshot (the online backup API, safe while the
app writes) plus uploads, as `crossfact-YYYYMMDD-HHMM.tar.gz` and
`crossfact-latest.tar.gz` in a versioned bucket. The VM's identity can create and
overwrite objects but not delete them, so a compromised node can't destroy the
history; a lifecycle rule ages old copies out. Weekly, a separate Job downloads
the latest backup, runs `PRAGMA integrity_check`, counts rows, and checks the
bucket still fits the free 20 GB. On a fresh cluster, an Argo CD sync hook
restores the latest backup into the empty volume before the app starts, and
reports how old it was (the data lost in that rebuild).
