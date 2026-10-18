#!/usr/bin/env bash
# Package hypervisor operator Helm charts and push them to Harbor OCI.
#
# CLI `nr cluster register` Helm-installs these at the Chart.yaml version.
# Image registry host is harbor.noderings.com; public
# project is nrings (harbor.nrings.io hostname is retired).
#
# Usage:
#   HARBOR_USERNAME=... HARBOR_PASSWORD=... bash scripts/publish-operator-charts.sh
#   # or helm registry login harbor.noderings.com first, then:
#   bash scripts/publish-operator-charts.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${HARBOR_OCI_REGISTRY:-oci://harbor.noderings.com/nrings}"
OUT="${TMPDIR:-/tmp}/noderings-operator-charts-$$"
mkdir -p "$OUT/tgz"
trap 'rm -rf "$OUT"' EXIT

CHARTS=(
  proxmox-operator
  virtfusion-operator
  solusvm-operator
  pterodactyl-operator
)

if [[ -n "${HARBOR_USERNAME:-}" && -n "${HARBOR_PASSWORD:-}" ]]; then
  echo "$HARBOR_PASSWORD" | helm registry login harbor.noderings.com \
    --username "$HARBOR_USERNAME" --password-stdin
fi

helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana >/dev/null

# file:// CRD subcharts need siblings; copy the whole charts tree.
cp -a "${ROOT}/charts" "$OUT/charts"

for name in "${CHARTS[@]}"; do
  dir="${OUT}/charts/${name}"
  if [[ ! -f "${dir}/Chart.yaml" ]]; then
    echo "missing chart ${dir}" >&2
    exit 1
  fi
  echo "dependency build ${name}"
  helm dependency build "${dir}"
  extra=()
  if [[ -n "${FORCE_CHART_VERSION:-}" ]]; then
    extra+=(--version "${FORCE_CHART_VERSION}" --app-version "${FORCE_CHART_VERSION}")
  fi
  helm package "${dir}" --destination "$OUT/tgz" "${extra[@]}"
done

for tgz in "$OUT/tgz"/*.tgz; do
  echo "push $(basename "$tgz") -> ${REGISTRY}"
  helm push "$tgz" "${REGISTRY}"
done

echo "published ${#CHARTS[@]} operator charts to ${REGISTRY}"
