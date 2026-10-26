# Crossfact

Ask one question. Two AI models from different families answer it
independently. A comparator splits their answers into what they agree on, what
each adds, and where they really conflict. The models debate only the
conflicts (up to three rounds, with settled points locked). A judge from a
third family, which argued neither side, rules on anything still open without
knowing which model said what. You get the **union** of what survived, with
the full transcript attached.

By default: Claude (through Claude Code, on your own subscription) and Gemini
answer; GPT-OSS 120B, OpenAI's open-weight model, judges on Groq's free tier,
with Workers AI as its fallback.

**Single-user, self-hosted.** Each person deploys their own copy, with their
own keys, on their own free Oracle Cloud account. There is one account per
deployment, so a Claude subscription only ever serves the person it belongs to.

Crossfact runs around the clock on Oracle Cloud's Always Free Arm VM, as a
single-node Kubernetes cluster (k3s) that Argo CD deploys from Git. You reach
it from any browser on your own domain through a Cloudflare Tunnel; the VM
accepts no inbound connections at all. Sign-in is a password plus TOTP.

This repository is the application: code, container image, Helm chart, SLOs,
dashboards, and the pipeline that tests, scans, signs and promotes it. The
cluster and the cloud live in a second, private repository, made from the
[`crossfact-deploy`](#two-repositories) template, whose `scripts/setup.sh` walks
you from accounts to a running app.

```
 browser ──HTTPS──▶ Cloudflare edge ──(tunnel, outbound from the VM)──▶ cloudflared pod
                    TLS, WAF, rate limit,                                    │
                    Access for admin names                                   ▼
                                                     crossfact pod (FastAPI, this repo)
                                                      │     │      │         │
            answer:  Claude Code CLI (your plan) ◀────┘     │      │         └──▶ SQLite on local disk
            answer:  Gemini API (free tier)       ◀─────────┘      │              nightly backup ─▶ Object Storage
            judge:   GPT-OSS 120B on Groq (free), Workers AI ◀─────┘              (as its own bucket-only user)

 k3s on 1 × A1 VM (2 OCPU, 12 GB, $0) · Argo CD · External Secrets ← OCI Vault
 Prometheus + Grafana + Alertmanager + Tempo · Sigstore policy controller
```

## Status, honestly

The plumbing is built and tested: pipeline, failure handling, SLOs, backups
and restore, supply chain, the guided setup. What is **not** shown yet is that
the consensus answer beats simply asking the best single model. That is an
empirical question. One 2026 study ("The Cost of Consensus", arXiv 2605.00914)
found that among identical small models, a model correcting itself matched or
beat unguided debate at a half to a third of the tokens. That isn't Crossfact's
setup (different families, debate limited to the conflicts, a neutral judge),
but it is a warning and a baseline to beat. So it gets measured: `bench/`
runs Crossfact and the baselines (each model alone, a model checking its own
answer, self-consistency, majority vote, Mixture-of-Agents) on the same
SimpleQA Verified questions, closed-book, and reports correct, wrong and "not
sure" side by side, because a system that abstains more can look more accurate
while answering less. The harness is built and tested; the first run, on free
models (about four days of free-tier quota), hasn't happened yet, so there are
no numbers. Method, caveats and results, wins or not:
[docs/EVAL.md](docs/EVAL.md).

## What is in it

- **Consensus pipeline** with three modes (consensus, union, single) and a
  transcript of every stage. Outcomes say exactly what you got: `agreed`,
  `debated`, `judged`, `union`, `single`, or `degraded` with a reason
  (`side_b_failed`, `deadline`, ...). A single-model answer is never presented
  as consensus. The judge sees the answers in random order with every point
  labelled to match. [docs/DESIGN.md](docs/DESIGN.md)
- **Failure handling per provider**: timeouts, retries with full-jitter backoff
  that honour `Retry-After`, circuit breakers per model, substitute models and
  fallback judges, a 600 s run budget, and a cutoff for a side that is still
  running long after the other answered. Free-tier daily quotas are told apart
  from per-minute limits. A run still finishes with the models that work.
- **Break it on purpose**: fault injection from the admin page or
  `python -m app.faults`, a simulator that replays provider outages on a
  laptop in minutes, and written-up results of three rounds, including what
  they found and how it was fixed. [docs/RESILIENCE.md](docs/RESILIENCE.md)
- **A measurement harness**: `python -m bench` answers a pinned question set
  with every system through the app's own call path, grades with SimpleQA's
  rules, resumes across days within free-tier budgets, and refuses to mix code
  versions in one result. An optional nightly CronJob tracks your own
  deployment on ten fixed questions. [docs/EVAL.md](docs/EVAL.md)
- **Three SLOs** (availability from synthetic probes, consensus success,
  consensus latency) with multiwindow burn-rate alerts, a low-traffic guard,
  and promtool unit tests that prove the alerts fire and stay quiet when they
  should. [docs/SLO.md](docs/SLO.md)
- **Telemetry**: Prometheus metrics on a port the tunnel never routes, with
  every bounded series created at zero so the first failure counts; an
  OpenTelemetry trace per run with a span per model call; exemplars linking
  latency panels to traces.
- **Backups that can't quietly lose data**: restore is the app pod's first
  step, and it only starts fresh when no backup exists at all; any other error
  keeps the app down and says why. A backup that would replace your
  conversations with an empty database is refused.
- **Graceful everything**: on SIGTERM the pod stops taking new questions and
  finishes the runs in flight (grace period 690 s > the 600 s run budget); a pod
  that dies anyway has its runs closed out and the daily quota refunded.
- **Supply chain**: hash-pinned Python and npm dependencies, actions pinned by
  SHA, native arm64 build, a check that the pinned Claude Code still reads its
  token, Trivy gate, keyless cosign signature and SBOM attestation, promotion
  by digest. The cluster refuses images this pipeline did not sign.

## Cost

| Item | Monthly cost | How |
|---|---|---|
| Compute, storage, secrets, backups | $0 | OCI Always Free (A1 2 OCPU / 12 GB), enforced by quotas and a $1 budget alert |
| Claude side | $0 extra | your own Claude subscription through Claude Code (`claude setup-token`); messages count toward your plan's usage like any other |
| Gemini side | $0 | free AI Studio key (the free tier may use prompts to improve Google's products; the UI says so) |
| Judge | $0 | GPT-OSS 120B on Groq's free plan; Workers AI's daily free allowance as the fallback |
| Edge, tunnel, Access, rate limit | $0 | Cloudflare free plan; Zero Trust free tier |
| CI/CD | $0 | GitHub Actions: free for this public repo; the private repo stays far inside its free minutes |
| Domain | about $10/year | the only cash cost |

Free tiers change; check each provider's current limits. The deploy
repository's `docs/COST.md` lists every resource against its free limit and the
guardrails that keep it there.

## Two repositories

| | `crossfact` (this, public) | `crossfact-deploy` (private, from the template) |
|---|---|---|
| Holds | app, image, chart, SLO rules, dashboards | Terraform (OCI + Cloudflare), k3s bootstrap, Argo CD apps, platform policy |
| CI | lint, tests, chart checks, CLI check, scans, build, sign, promote | fmt/validate/tests, `terraform plan` on every change, cluster policy tests |
| Deploys | the promote job commits the signed digest to your deploy repo | Argo CD syncs from it |

To deploy without building the app yourself, point your deploy repo at this
repository: it trusts this pipeline's signatures, and a weekly pull request
brings each new signed build. Fork it if you want to change the code.

## Read next

- [docs/DESIGN.md](docs/DESIGN.md): how the pipeline, failure handling,
  lifecycle, telemetry, backups and security work, and why
- [docs/SLO.md](docs/SLO.md): the SLOs, every number behind them, alert runbooks
- [docs/RESILIENCE.md](docs/RESILIENCE.md): game days, before and after
- [docs/DEVELOPING.md](docs/DEVELOPING.md): run it locally, test it, the pipeline,
  and the check to run before making a repository public

## Repository map

    app/                FastAPI app: auth (argon2id + TOTP, one account), threads, uploads,
                        provider adapters, resilience, orchestrator, telemetry, lifecycle, UI;
                        admin.py: account recovery (python -m app.admin)
    config/             providers.example.yaml: the model registry and resilience knobs
    scripts/            maintain.py (cleanup, backup, restore, verify), check_claude_cli.py,
                        gameday_sim.py, prepublish-check.sh
    charts/crossfact/   Helm chart: Deployment (with the restore step), backup CronJobs,
                        NetworkPolicies, ExternalSecrets, ServiceMonitor, Probe, SLO rules,
                        dashboards (+ tests)
    image/              pinned Claude Code (package-lock.json); only its native binary ships
    tests/              pytest: pipeline, resilience, providers, web, admin, drain, tracing, backups
    .github/            pipeline.yml (every change), security-history.yml (weekly)
