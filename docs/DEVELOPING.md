# Developing

## Run it on a laptop

Python 3.12. The Claude side additionally needs the Claude Code CLI and a token;
without one, use Gemini for both sides or single mode.

    python3.12 -m venv .venv && . .venv/bin/activate
    pip install --require-hashes -r requirements.txt -r requirements-dev.txt

    export CROSSFACT_SECRETS_MODE=env CROSSFACT_DATA_DIR=$PWD/data CROSSFACT_CONFIG=/nonexistent
    export SESSION_SECRET=dev-only FIELD_ENCRYPTION_KEY=dev-only ADMIN_BOOTSTRAP_PASSWORD=change-me-123
    export GEMINI_API_KEY=...                   # aistudio.google.com
    # export CLAUDE_OAUTH_TOKEN=...             # from `claude setup-token` (your own account), with Claude Code installed
    python -m app.server                        # http://localhost:8080, metrics on :9095

Log in as `admin` with the bootstrap password; you'll be asked to enrol TOTP.
In env mode a secret named `gemini_api_key` is read from `GEMINI_API_KEY`.
Other overrides: `CROSSFACT_PROVIDERS` (registry file), `CROSSFACT_PORT`,
`CROSSFACT_METRICS_PORT`, `OTEL_EXPORTER_OTLP_ENDPOINT` (turns tracing on; any
OTLP/HTTP collector works locally, e.g. Jaeger all-in-one listening on 4318).

## Checks (the same ones CI runs)

    ruff check .                                # lint
    pytest                                      # about 225 tests, ~35 s, no network
    python charts/crossfact/dashboards/generate.py && git diff --exit-code charts/crossfact/dashboards/
    charts/crossfact/tests/render.sh              # helm template + kubeconform + promtool rules and tests
    hadolint Dockerfile
    python scripts/check_claude_cli.py --bin "$(command -v claude)"   # the pinned CLI still reads its token
    python scripts/gameday_sim.py --list        # game days on the laptop: docs/RESILIENCE.md

The tests drive the real call path with a scriptable fake model
(`tests/conftest.py::FakeLLM`): fault injection, timeouts, retries, breakers,
metrics and spans all run. Notable ones: `test_drain.py` sends a real SIGTERM to
a real server process mid-run and checks the run finishes; the Claude tests run
a fake `claude` binary as a real subprocess to check the sandboxing flags,
environment and process-group kill. `test_bench.py` runs the measurement
harness end to end with fake models that know some answers and not others,
through the real Crossfact pipeline: grading, resuming, budget pauses, version
pinning and the statistics.

## The pipeline (`.github/workflows/pipeline.yml`)

Every pull request and push:

| job | does | fails on |
|---|---|---|
| lint | ruff, actionlint, hadolint | any finding |
| test | pytest; then the pinned Claude Code (installed with the Dockerfile's Node image, binary only) run with the adapter's exact command, a fake token and a localhost proxy (`scripts/check_claude_cli.py`) | any failure; a rejected flag; "Not logged in" (the token ignored, as `--bare` does) |
| chart | helm lint/template, kubeconform (`-strict`, k8s 1.36 + CRD schemas), promtool rule tests, dashboards up to date | invalid manifests, SLO alerts not behaving |
| security | gitleaks on the commits in the change, Trivy on the lockfiles (to the Security tab) and on Dockerfile + chart | a leaked secret; a HIGH/CRITICAL misconfiguration |

On `main` only, after all of those pass:

| job | does |
|---|---|
| build | native arm64 build on GitHub's free Arm runners; push `sha-<commit>` and `main` tags to GHCR; the CLI check again inside the built image with no network; Trivy gate on fixable CRITICAL CVEs; CycloneDX SBOM; **keyless cosign signature and SBOM attestation**, bound to this workflow on `main` by GitHub's OIDC token |
| promote | commits the image **digest** and the chart's commit SHA to `gitops/apps/crossfact.yaml` in your deploy repo (a fine-grained token in the `production` environment; skipped without `vars.DEPLOY_REPO`). Argo CD deploys from there. Deploy repos that track this repository instead pin new builds through their own weekly pull request (`update-crossfact.yml`) |

Weekly (`security-history.yml`): gitleaks over the entire history (new rules
catch old mistakes) and a Trivy scan of the latest image for CVEs published
since it was built.

### Supply chain, end to end

1. Inputs are pinned: Python wheels by hash (`requirements.txt` from `uv pip
   compile --generate-hashes`), the CLIs by `package-lock.json`, actions by
   commit SHA, CI tools by checksum. Dependabot proposes updates; each one
   runs the whole pipeline.
2. The image is scanned before it is signed: nothing with a known, fixable
   critical CVE gets a signature.
3. The signature says *who built it*: this repository's `pipeline.yml`, on
   `main`. There is no key to steal.
4. Deploys reference the digest, so what runs is byte-for-byte what was scanned
   and signed.
5. The cluster checks: the Sigstore policy controller admits an image into the
   `crossfact` namespace only with that signature, and an admission policy
   refuses tag-only references there. Signing without verification would be
   decoration.

## Before making this repository public

    scripts/prepublish-check.sh        # needs gitleaks

CI scans each change; this scans **every commit on every branch and tag**,
because a secret deleted in a later commit is still in the history anyone can
clone. It also lists files that should never have been committed (`.tfvars`,
`.tfstate`, `.env`, keys, kubeconfigs) and large blobs. If it finds anything:
rotate that credential first (assume it is already known), then rewrite
history or publish from a fresh commit, and run it again until it is clean.

## Changing things

- **A model or provider**: edit the registry (`config/providers.example.yaml`
  for the default; in the cluster, the chart's `providers` value in the deploy
  repo). New OpenAI-compatible provider: a block with `kind: openai`,
  `base_url`, `secret`, then add the secret name to the chart's
  `secrets.externalSecret.extraKeys` and to the ExternalSecret allowlist in the
  deploy repo's admission policy.
- **An SLO number**: `charts/crossfact/values.yaml` (`slo:`), then update the
  promtool tests and docs/SLO.md together.
- **A dashboard**: edit `dashboards/generate.py`, run it, commit both.
- **The run budget**: `runDeadlineSeconds`; the chart refuses to render unless
  `terminationGracePeriodSeconds` exceeds it by 60 s.
- **The pipeline, while a measurement is running**: a run of `python -m bench`
  refuses to resume on changed code (anything under `app/`, or the harness's
  calls, datasets, grader and systems), so finish it first or start a new one
  under a new config name; the call cache keeps that cheap. docs/EVAL.md.
