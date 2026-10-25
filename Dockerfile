# syntax=docker/dockerfile:1.7
# Crossfact image: the FastAPI app and the Claude Code CLI.
# CI builds it natively on a GitHub-hosted arm64 runner (the cluster is an Ampere A1 VM),
# checks the CLI still reads its token, scans it with Trivy, signs it with cosign
# (keyless), and pins it by digest in GitOps.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim

# ---- Claude Code, pinned by image/package-lock.json ------------------------------------
# Node is only needed to install it: the package's postinstall puts a native binary
# (glibc, no other dependencies) in place, and that binary is all the runtime gets.
FROM ${NODE_IMAGE} AS claude
WORKDIR /opt/clis
COPY image/package.json image/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
 && cp node_modules/@anthropic-ai/claude-code/bin/claude.exe /opt/claude \
 && /opt/claude --version \
 && npm cache clean --force

# ---- Python dependencies, hash-pinned ------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt

# ---- runtime -------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="crossfact" \
      org.opencontainers.image.description="Self-hosted multi-model answers: Claude, Gemini and GPT-OSS" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# tini reaps the CLI's children and forwards SIGTERM, so graceful drain works as PID 1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home --home-dir /home/crossfact --shell /usr/sbin/nologin crossfact

COPY --from=claude /opt/claude /usr/local/bin/claude
COPY --from=deps /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CROSSFACT_VERSION=${VERSION} \
    CROSSFACT_CLI_HOME=/tmp/cli-home \
    CROSSFACT_RUNS_DIR=/tmp/runs

WORKDIR /app
COPY app ./app
COPY scripts ./scripts
COPY config ./config

USER 10001:10001
EXPOSE 8080 9095
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.server"]
