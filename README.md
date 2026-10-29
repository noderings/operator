## Proxmox VM Operator

NodeRings is a marketplace and control plane where independent hosting providers sell VPS and game servers. The provider keeps the hardware, the customer, and the hypervisor credential. Buyers compare live plans and run them from one console.

This repository is the operator that runs on the provider's cluster. NodeRings writes a VM order as a custom resource. This process reads the Proxmox API token from a secret in the provider's cluster and calls Proxmox on their network. The control plane does not receive that token. Guests it creates are named `NR-` plus an id.

A Kubernetes operator for managing Proxmox VMs through Custom Resources. Supports managing VMs across multiple Proxmox instances with separate credentials for each.

### Features

- ✅ **Multi-Instance Support**: Manage VMs across multiple Proxmox clusters/instances
- ✅ **Automatic Credential Selection**: Credentials are automatically selected based on the Proxmox endpoint resolved from the VM's `spec.proxmoxNodeRef` (which points at a ProxmoxNode CR holding `spec.proxmoxUrl`)
- ✅ **Full VM Lifecycle**: Create, start, stop, restart, reinstall, and delete VMs
- ✅ **Template Cloning**: Clone VMs from existing templates
- ✅ **Resource Configuration**: Configure CPU, memory, disks, and networks
- ✅ **VMID Management**: Automatic VMID allocation with optional seed-based numbering
- ✅ **Declarative API**: Manage VMs using Kubernetes Custom Resources

### Local development

Point the operator at a cluster and a Proxmox API token, then run it on the host:

```bash
export KUBECONFIG="$HOME/.kube/config"
# PROXMOX_URL, PROXMOX_USERNAME, PROXMOX_TOKEN_ID, PROXMOX_TOKEN_SECRET
make run
```

### Quick Start

See [MULTI_INSTANCE.md](./docs/MULTI_INSTANCE.md) for detailed configuration of multiple Proxmox instances.

### Install from release

Container images and Helm charts are published to GitHub Container Registry on `main` and version tags (`v*`).

**Helm (OCI chart):**

```bash
helm install proxmox-operator oci://ghcr.io/noderings/proxmox-operator \
  --version 0.1.0 \
  --namespace proxmox-operator \
  --create-namespace
```

**Operator image** (default in chart values): `harbor.noderings.com/nrings/proxmox-operator:dev`. That Harbor project is public. Images are also published to `ghcr.io/noderings/proxmox-operator` on `main` and semver tags (e.g. `v0.1.0` → `0.1.0`).

Override Proxmox credentials and endpoints in `values.yaml` or via `--set` (see [docs/MULTI_INSTANCE.md](./docs/MULTI_INSTANCE.md)).

### Helm charts

Two charts live under `charts/`:

| Chart | Deploy on | Contents |
|-------|-----------|----------|
| **`proxmox-operator-crds`** | Platform (NodeRings hub) | `vm.proxmox.com` CRDs only |
| **`proxmox-operator`** | Provider (agent) cluster | CRDs (subchart) + operator, exporter, vnc-gateway, alloy |

**Platform cluster** — install the CRDs before creating `ProxmoxNode` / `ProxmoxVM` objects:

```bash
helm upgrade --install proxmox-crds charts/proxmox-operator-crds \
  --namespace proxmox-system --create-namespace
```

**Provider cluster** — full operator runtime near Proxmox:

```bash
helm dependency build charts/proxmox-operator
helm upgrade --install proxmox-operator charts/proxmox-operator \
  --namespace proxmox-system --create-namespace \
  -f values.yaml
```

The parent chart depends on `proxmox-operator-crds` (`crds.enabled: true`). To install CRDs without the operator (e.g. local testing):

```bash
helm upgrade --install proxmox-crds charts/proxmox-operator-crds -n proxmox-system --create-namespace
# or parent chart with runtime disabled:
helm upgrade --install proxmox-operator charts/proxmox-operator -n proxmox-system \
  --set operator.enabled=false --set prometheusExporter.enabled=false \
  --set vncGateway.enabled=false --set alloy.enabled=false \
  --set rbac.create=false --set serviceAccount.create=false
```

**CRD source of truth:** `charts/proxmox-operator-crds/templates/crds/` — update the schema there and keep `src/proxmox_operator/types.py` in sync.

**Cut a release:** tag the repo (chart `version` in `Chart.yaml` should match, without the `v` prefix):

```bash
git tag v0.1.0
git push origin v0.1.0
```

This triggers the Release workflow (GitHub release + Helm chart push) and publishes a versioned container image.

### Layout
```
src/proxmox_operator/
  handlers.py       # Kopf handlers for create/update/delete
  client.py         # Proxmox client using proxmoxer library
  types.py          # Pydantic models & CRD identity
  status.py         # Status patch helpers
  config.py         # Settings from env vars & multi-instance support
charts/
  proxmox-operator-crds/   # vm.proxmox.com CRDs (platform + provider)
  proxmox-operator/        # Operator runtime (provider); depends on crds subchart
samples/
  proxmoxvm.yaml              # Basic single-instance example
docs/MULTI_INSTANCE.md        # Multi-instance configuration guide
docs/hooks.md                 # Lifecycle hook Job sandbox and NetworkPolicy
```

### Development with Poetry and a local virtualenv
```bash
# Create a local in-project venv and install deps
poetry config virtualenvs.in-project true
poetry env use python3.12
make poetry-install

# Activate the venv shell (optional)
poetry shell

# Run the operator in dev mode
make run

# Export requirements.txt for Docker builds
make poetry-export

# Install pre-commit hooks (Ruff, mypy, etc.)
make pre-commit-install

# Run pre-commit checks manually
make pre-commit-run
```

### Build & deploy
```bash
make docker-build TAG=v0.1.0
make docker-push TAG=v0.1.0
make helm-install NAMESPACE=proxmox-operator TAG=v0.1.0

# Custom registry (optional):
# make docker-build IMAGE_REGISTRY=harbor.example.com IMAGE_REPOSITORY=myorg/proxmox-operator TAG=v0.1.0
```

### Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Security issues: [SECURITY.md](./SECURITY.md).

### License

Apache License 2.0 — see [LICENSE](./LICENSE).
