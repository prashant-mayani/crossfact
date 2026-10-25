#!/usr/bin/env bash
# Render the chart with CI values, validate every object against the Kubernetes
# and CRD JSON schemas, then unit-test the SLO alerts with promtool.
# Needs: helm, kubeconform, promtool, yq (all installed by the CI job).
set -euo pipefail
cd "$(dirname "$0")/.."
out=$(mktemp -d)
helm lint . -f ci-values.yaml
helm template crossfact . -n crossfact -f ci-values.yaml > "$out/rendered.yaml"
kubeconform -strict -summary -kubernetes-version "${KUBE_VERSION:-1.36.0}" \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  "$out/rendered.yaml"
yq 'select(.kind == "PrometheusRule") | .spec' "$out/rendered.yaml" > tests/slo-rules.yaml
promtool check rules tests/slo-rules.yaml
promtool test rules tests/slo-rules.test.yaml
echo "chart OK"
