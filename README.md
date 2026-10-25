# Crossfact

Ask one question. Two AI models answer it independently. A comparator splits
their answers into what they agree on, what each adds, and where they really
conflict. The models debate only the conflicts (up to three rounds, with
settled points locked). An anonymized judge rules on anything still open. You
get the **union** of what they got right, with the full transcript attached.

Crossfact runs around the clock on Oracle Cloud's Always Free Arm VM, as a
single-node Kubernetes cluster (k3s) that Argo CD deploys from Git. You reach
it from any browser on your own domain, through a Cloudflare Tunnel; the VM
accepts no inbound connections at all. Login with TOTP 2FA, several users, and
private threads and files for each.

This repository is the application: code, container image, Helm chart, SLOs,
dashboards, and the pipeline that tests, scans, signs and promotes it. The
cluster and the cloud live in a second, private repository
([`crossfact-deploy`](#two-repositories)).

```
 browser ──HTTPS──▶ Cloudflare edge ──(tunnel, outbound from the VM)──▶ cloudflared pod
                    TLS, WAF, rate limit,                                    │
                    Access for admin names                                   ▼
                                                          crossfact pod (FastAPI, this repo)
                                                           │      │        │
                         Claude Code CLI (your Pro/Max) ◀──┘      │        └──▶ SQLite on local disk
                         Gemini API (free tier)          ◀────────┘             nightly backup ─▶ Object Storage
                         optional: Groq, OpenRouter, Cerebras

 k3s on 1 × A1 VM (2 OCPU, 12 GB, $0) · Argo CD · External Secrets ← OCI Vault
 Prometheus + Grafana + Alertmanager + Tempo · Sigstore policy controller
```

## What is in it

- **Consensus pipeline** with three modes (consensus, union, single) and a
  transcript of every stage. Outcomes say exactly what you got: `agreed`,
  `debated`, `judged`, `union`, `single`, or `degraded` with a reason
  (`side_b_failed`, `deadline`, ...). A single-model answer is never presented
  as consensus. [docs/DESIGN.md](docs/DESIGN.md)
- **Failure handling per provider**: timeouts, retries with full-jitter backoff
  that honour `Retry-After`, circuit breakers per model, substitute models, a
  600 s run budget, and a cutoff for a side that is still running long after
  the other answered. A run still finishes with the models that work.
- **Break it on purpose**: fault injection from the admin page or
  `python -m app.faults`, a simulator that replays provider outages on a
  laptop in minutes, and written-up results, including the four problems
  the first round found and how they were fixed. [docs/RESILIENCE.md](docs/RESILIENCE.md)
- **Three SLOs** (availability from synthetic probes, consensus success,
  consensus latency) with multiwindow burn-rate alerts, a low-traffic guard,
  and promtool unit tests that prove the alerts fire and stay quiet when they
  should. [docs/SLO.md](docs/SLO.md)
- **Telemetry**: Prometheus metrics on a port the tunnel never routes, an
  OpenTelemetry trace per run with a span per model call (so you can see which
  provider was slow), and exemplars linking latency panels to traces.
- **Graceful everything**: on SIGTERM the pod stops taking new questions,
  finishes the runs in flight (grace period 690 s > the 600 s run budget), and
  a pod that dies anyway has its runs closed out and the user's quota refunded.
- **Supply chain**: hash-pinned Python and npm dependencies, actions pinned by
  SHA, native arm64 build, Trivy gate, keyless cosign signature and SBOM
  attestation, promotion by digest. The cluster refuses images this
  pipeline did not sign.

## Cost

| Item | Monthly cost | How |
|---|---|---|
| Compute, storage, secrets, backups | $0 | OCI Always Free (A1 2 OCPU / 12 GB since June 2026), enforced by quotas |
| Claude side | $0 extra | your existing Pro/Max plan, via `claude setup-token` and Claude Code |
| Gemini side | $0 | free AI Studio key (the free tier may train on prompts; the UI says so) |
| Edge, tunnel, Access, rate limit | $0 | Cloudflare free plan; Zero Trust free up to 50 users |
| CI/CD | $0 | GitHub Actions: free for this public repo; the private repo stays far inside its free minutes |
| Domain | about $10/year | the only cash cost |

The deploy repository's `docs/COST.md` lists every resource against its free
limit and the guardrails that keep it there.

## Two repositories

| | `crossfact` (this, public) | `crossfact-deploy` (private) |
|---|---|---|
| Holds | app, image, chart, SLO rules, dashboards | Terraform (OCI + Cloudflare), k3s bootstrap, Argo CD apps, platform policy |
| CI | lint, tests, chart checks, scans, build, sign, promote | fmt/validate/tests, `terraform plan` on every change, cluster policy tests |
| Deploys | promote job commits the signed digest to the deploy repo | Argo CD syncs from it |

Why split them: the app is safe to show; the deploy repo names your tenancy,
your hostnames and your email, and its CI holds a (read-only) cloud identity.

## Read next

- [docs/DESIGN.md](docs/DESIGN.md): how the pipeline, failure handling,
  lifecycle, telemetry and security work, and why
- [docs/SLO.md](docs/SLO.md): the SLOs, every number behind them, alert runbooks
- [docs/RESILIENCE.md](docs/RESILIENCE.md): game days, before and after
- [docs/DEVELOPING.md](docs/DEVELOPING.md): run it locally, test it, the pipeline,
  and the check to run before making this repository public

## Repository map

    app/              FastAPI app: auth (argon2id + TOTP), threads, uploads, provider
                      adapters, resilience, orchestrator, telemetry, lifecycle, UI
    config/           providers.example.yaml: the model registry and resilience knobs
    scripts/          maintain.py (cleanup, backup, restore, verify), gameday_sim.py,
                      prepublish-check.sh
    charts/crossfact/   Helm chart: Deployment, jobs, NetworkPolicies, ExternalSecret,
                      ServiceMonitor, Probe, SLO rules, dashboards (+ tests)
    image/            pinned Claude Code and Gemini CLI (package-lock.json)
    tests/            pytest: pipeline, resilience, providers, web, drain, tracing, backups
    .github/          pipeline.yml (every change), security-history.yml (weekly)
