#!/usr/bin/env bash
# Package hypervisor CRD charts and push them to Harbor OCI.
#
# CLI `nr cluster register` Helm-installs ALL of these on every provider
# (Liqo AllowList includes every hypervisor API group), pinned to the chart
# versions in Chart.yaml (override with --version for a one-off alias).
#
# Usage:
#   HARBOR_USERNAME=... HARBOR_PASSWORD=... bash scripts/publish-crd-charts.sh
#   # or helm registry login harbor.noderings.com first, then:
#   bash scripts/publish-crd-charts.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${HARBOR_OCI_REGISTRY:-oci://harbor.noderings.com/nrings}"
OUT="${TMPDIR:-/tmp}/noderings-crd-charts-$$"
mkdir -p "$OUT"
trap 'rm -rf "$OUT"' EXIT

CHARTS=(
  proxmox-operator-crds
  virtfusion-operator-crds
  solusvm-operator-crds
  pterodactyl-operator-crds
)

if [[ -n "${HARBOR_USERNAME:-}" && -n "${HARBOR_PASSWORD:-}" ]]; then
  echo "$HARBOR_PASSWORD" | helm registry login harbor.noderings.com \
    --username "$HARBOR_USERNAME" --password-stdin
fi

for name in "${CHARTS[@]}"; do
  dir="${ROOT}/charts/${name}"
  if [[ ! -f "${dir}/Chart.yaml" ]]; then
    echo "missing chart ${dir}" >&2
    exit 1
  fi
  helm lint "${dir}"
  extra=()
  if [[ -n "${FORCE_CHART_VERSION:-}" ]]; then
    extra+=(--version "${FORCE_CHART_VERSION}" --app-version "${FORCE_CHART_VERSION}")
  fi
  helm package "${dir}" --destination "$OUT" "${extra[@]}"
done

for tgz in "$OUT"/*.tgz; do
  echo "push $(basename "$tgz") -> ${REGISTRY}"
  helm push "$tgz" "${REGISTRY}"
done

echo "published ${#CHARTS[@]} CRD charts to ${REGISTRY}"
