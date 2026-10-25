#!/usr/bin/env bash
# Run before this repository goes public (and any time you suspect a leak).
#
# A secret that was ever pushed is compromised even if a later commit deleted
# it: anyone with a clone, a fork or a cached page can read the old commit. So
# this scans EVERY commit on every branch and tag, not just the new ones that CI
# checks on each push. Anything it finds: rotate the credential first (the
# rotation steps are in the deploy repo's docs/OPERATIONS.md, "Rotating
# secrets"), then rewrite history or start the public repo from a fresh commit.
#
#   scripts/prepublish-check.sh          needs git and gitleaks (brew install gitleaks)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
command -v gitleaks >/dev/null || { echo "install gitleaks first (https://github.com/gitleaks/gitleaks)"; exit 2; }

problems=0
section() { printf '\n== %s\n' "$1"; }

section "fetching every branch and tag from origin, so nothing is missed"
git fetch --all --tags --quiet 2>/dev/null || echo "(no remote to fetch; scanning local refs only)"
echo "refs: $(git for-each-ref --format='%(refname:short)' refs/heads refs/remotes refs/tags | wc -l | tr -d ' '), commits: $(git rev-list --all | wc -l | tr -d ' ')"

section "secrets anywhere in history (gitleaks, all refs)"
if ! gitleaks git --redact --no-banner --log-opts="--all" .; then problems=1; fi

section "files that should never have been committed, in any commit"
bad=$(git log --all --name-only --pretty=format: | sort -u \
  | grep -E '(^|/)(\.env(\..+)?|[^/]*\.env|[^/]*\.tfstate(\..*)?|[^/]*\.tfvars|[^/]*\.pem|id_(rsa|ed25519|ecdsa)|kubeconfig[^/]*|k3s\.yaml|credentials(\.json)?)$' || true)
if [ -n "$bad" ]; then echo "$bad"; problems=1; else echo "none"; fi

section "large blobs (>5 MB) in history"
big=$(git rev-list --objects --all \
  | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | awk '$1 == "blob" && $3 > 5000000 { printf "%8.1f MB  %s\n", $3/1048576, $4 }')
if [ -n "$big" ]; then echo "$big"; else echo "none"; fi

section "unreachable commits still in the local object store (not pushed, but check)"
git fsck --unreachable --no-reflogs 2>/dev/null | grep -c commit || true

echo
if [ $problems -ne 0 ]; then
  cat <<'MSG'
FOUND SOMETHING. Do not make the repository public yet.
  1. Rotate every credential reported above, now: assume it is already known.
  2. Then either rewrite history (git filter-repo --invert-paths --path <file>, or
     --replace-text) and force-push every branch, or publish a fresh repository
     whose first commit is today's tree.
  3. Run this script again until it comes back clean.
MSG
  exit 1
fi
echo "clean: no secrets or forbidden files anywhere in history"
