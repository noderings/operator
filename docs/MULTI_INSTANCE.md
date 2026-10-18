# Multi-Instance Proxmox Configuration

This guide explains how to configure the Proxmox Operator to manage VMs across multiple Proxmox instances, each with its own credentials.

## Overview

The Proxmox Operator supports managing VMs across multiple Proxmox clusters or standalone instances. Each instance can have its own:
- URL/endpoint
- Username
- API token credentials

A ProxmoxVM does **not** carry a `proxmoxUrl` field directly. Instead, the VM references a `ProxmoxNode` CR (via `spec.proxmoxNodeRef.name`) and the operator resolves the Proxmox endpoint from that ProxmoxNode's `spec.proxmoxUrl`. Credentials are then selected by matching this resolved URL against the configured instances.

## Configuration

### 1. Configure Multiple Instances in Helm Values

Edit your `values.yaml` to include multiple Proxmox instances:

```yaml
proxmox:
  # Global settings
  verifySSL: false
  timeoutSeconds: 30

  # Define multiple Proxmox instances
  instances:
    # Production Proxmox cluster
    - id: "proxmox-prod"
      url: "https://10.0.0.10:8006"
      username: "kopfoperator@pve"
      tokenId: "kopf-operator-token"
      tokenSecret: "your-production-token-secret-here"
      enabled: true

    # Development Proxmox instance
    - id: "proxmox-dev"
      url: "https://192.168.1.100:8006"
      username: "kopfoperator@pve"
      tokenId: "kopf-dev-token"
      tokenSecret: "your-dev-token-secret-here"
      enabled: true

    # Staging Proxmox cluster
    - id: "proxmox-staging"
      url: "https://10.0.0.50:8006"
      username: "kopfoperator@pve"
      tokenId: "kopf-staging-token"
      tokenSecret: "your-staging-token-secret-here"
      enabled: true
```

### 2. Deploy the Helm Chart

Deploy or upgrade the Helm chart with your updated values (provider cluster; includes CRDs via `proxmox-operator-crds` subchart):

```bash
helm dependency build ./charts/proxmox-operator
helm upgrade --install proxmox-operator ./charts/proxmox-operator \
  --namespace proxmox-operator \
  --create-namespace \
  -f values.yaml
```

On the platform cluster, install CRDs before creating Proxmox objects — see [charts/proxmox-operator-crds/README.md](../charts/proxmox-operator-crds/README.md).

### 3. Create ProxmoxNode and ProxmoxVM Resources

First create a `ProxmoxNode` CR for each Proxmox instance you want to use. The
ProxmoxNode owns the endpoint URL (`spec.proxmoxUrl`) and node-level policy
such as `maintenanceMode`. Then create `ProxmoxVM` CRs that reference the
appropriate ProxmoxNode via `spec.proxmoxNodeRef.name`.

```yaml
# ProxmoxNode for the production cluster
apiVersion: vm.proxmox.com/v1alpha1
kind: ProxmoxNode
metadata:
  name: node-prod-1
  namespace: default
spec:
  nodeId: "00000000-0000-0000-0000-000000000001"
  name: "pve-prod-1"
  proxmoxUrl: "https://10.0.0.10:8006"   # Matches proxmox-prod
  maintenanceMode: false
---
# ProxmoxNode for the development instance
apiVersion: vm.proxmox.com/v1alpha1
kind: ProxmoxNode
metadata:
  name: node-dev-1
  namespace: default
spec:
  nodeId: "00000000-0000-0000-0000-000000000002"
  name: "pve-dev-1"
  proxmoxUrl: "https://192.168.1.100:8006"  # Matches proxmox-dev
  maintenanceMode: false
---
# VM on production cluster — references node-prod-1
apiVersion: vm.proxmox.com/v1alpha1
kind: ProxmoxVM
metadata:
  name: prod-vm
  namespace: default
spec:
  proxmoxNodeRef:
    name: node-prod-1
  vmConfig:
    templateId: "9000"
    name: "production-vm"
    nodeName: "pve-prod-1"
    # ... rest of configuration
  operation:
    type: created
---
# VM on development instance — references node-dev-1
apiVersion: vm.proxmox.com/v1alpha1
kind: ProxmoxVM
metadata:
  name: dev-vm
  namespace: default
spec:
  proxmoxNodeRef:
    name: node-dev-1
  vmConfig:
    templateId: "9001"
    name: "dev-vm"
    nodeName: "pve-dev-1"
    # ... rest of configuration
  operation:
    type: created
```

## How It Works

### URL Matching

The operator uses intelligent URL matching to find the correct credentials:

1. **Exact Match**: Tries to match the full normalized URL (e.g., `10.0.0.10:8006`)
2. **Hostname Match**: Falls back to hostname-only matching if port is omitted
3. **Case Insensitive**: URL matching is case-insensitive
4. **Protocol Agnostic**: Both `https://10.0.0.10:8006` and `10.0.0.10:8006` work

### URL Normalization

All URLs are normalized for comparison:
- Protocol (`https://`, `http://`) is removed
- Trailing slashes are removed
- Converted to lowercase
- Default port `:8006` is added if missing

Examples:
- `https://10.0.0.10:8006` → `10.0.0.10:8006`
- `HTTPS://192.168.1.100:8006/` → `192.168.1.100:8006`
- `10.0.0.50` → `10.0.0.50:8006`

### Credential Selection

The operator performs these steps for each VM operation:

1. Read `spec.proxmoxNodeRef.name` from the ProxmoxVM CR spec
2. Fetch the referenced ProxmoxNode CR (same namespace) and read its `spec.proxmoxUrl`
3. Discover all configured Proxmox instances from environment variables
4. Normalize the resolved URL and compare with configured instances
5. Select matching credentials
6. Connect to the specified Proxmox instance with the correct credentials

### Environment Variables

The Helm chart creates environment variables for each instance:

```bash
# For instance with id "proxmox-prod":
PROXMOX_PROXMOX_PROD_URL=https://10.0.0.10:8006
PROXMOX_PROXMOX_PROD_USERNAME=kopfoperator@pve
PROXMOX_PROXMOX_PROD_TOKEN_ID=kopf-operator-token
PROXMOX_PROXMOX_PROD_TOKEN_SECRET=your-production-token-secret-here

# For instance with id "proxmox-dev":
PROXMOX_PROXMOX_DEV_URL=https://192.168.1.100:8006
PROXMOX_PROXMOX_DEV_USERNAME=kopfoperator@pve
PROXMOX_PROXMOX_DEV_TOKEN_ID=kopf-dev-token
PROXMOX_PROXMOX_DEV_TOKEN_SECRET=your-dev-token-secret-here
```

Note: The instance ID is converted to uppercase and hyphens are replaced with underscores.

## Kubernetes Secrets

The Helm chart creates separate Kubernetes secrets for each instance:

```bash
kubectl get secrets -n proxmox-operator
NAME                           TYPE     DATA   AGE
proxmox-credentials-proxmox-prod      Opaque   4      1m
proxmox-credentials-proxmox-dev       Opaque   4      1m
proxmox-credentials-proxmox-staging   Opaque   4      1m
```

Each secret contains:
- `PROXMOX_URL`
- `PROXMOX_USERNAME`
- `PROXMOX_TOKEN_ID`
- `PROXMOX_TOKEN_SECRET`
