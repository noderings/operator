# Multi-Instance Architecture

> **Architectural refresh (current code):**
> The diagrams below show `proxmoxUrl` directly on each ProxmoxVM CR. That is
> **outdated**. ProxmoxVM CRs reference a `ProxmoxNode` via
> `spec.proxmoxNodeRef.name`, and the operator resolves `proxmoxUrl` from
> `ProxmoxNode.spec.proxmoxUrl` (single source of truth). Wherever the
> diagrams below mention "read `proxmoxUrl` from VM CR", read it as "read
> `proxmoxNodeRef.name` from VM CR and resolve `proxmoxUrl` from the linked
> ProxmoxNode CR".

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Kubernetes Cluster                          │
│                                                                     │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │                    ProxmoxVM Custom Resources               │   │
│  │                                                              │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │   │
│  │  │   VM-Prod-1  │  │   VM-Dev-1   │  │  VM-Stage-1  │     │   │
│  │  │              │  │              │  │              │     │   │
│  │  │ proxmoxUrl:  │  │ proxmoxUrl:  │  │ proxmoxUrl:  │     │   │
│  │  │ 192.0.2.10 │  │ 192.168.1.100│  │ 10.0.0.50    │     │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘     │   │
│  └───────────────────────────┬──────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │             Proxmox Operator (Deployment)                    │  │
│  │                                                              │  │
│  │  ┌────────────────────────────────────────────────────────┐ │  │
│  │  │  Handlers (handlers.py)                                │ │  │
│  │  │  • on_create()                                         │ │  │
│  │  │  • on_update()                                         │ │  │
│  │  │  • _get_client(proxmoxUrl) ─────────┐                 │ │  │
│  │  └──────────────────────────────────────┼─────────────────┘ │  │
│  │                                         │                   │  │
│  │  ┌──────────────────────────────────────▼─────────────────┐ │  │
│  │  │  Config (config.py)                                    │ │  │
│  │  │  • load_settings_from_env(proxmoxUrl)                  │ │  │
│  │  │  • _discover_proxmox_instances() ──┐                   │ │  │
│  │  │  • _find_credentials_for_url()     │                   │ │  │
│  │  │  • _normalize_url()                │                   │ │  │
│  │  └────────────────────────────────────┼───────────────────┘ │  │
│  │                                       │                     │  │
│  │  Environment Variables: ◄─────────────┘                     │  │
│  │  ┌────────────────────────────────────────────────────────┐ │  │
│  │  │ PROXMOX_PROXMOX_1_URL=https://192.0.2.10:8006        │ │  │
│  │  │ PROXMOX_PROXMOX_1_USERNAME=kopfoperator@pve            │ │  │
│  │  │ PROXMOX_PROXMOX_1_TOKEN_ID=kopf-operator-token         │ │  │
│  │  │ PROXMOX_PROXMOX_1_TOKEN_SECRET=secret-1                │ │  │
│  │  │                                                         │ │  │
│  │  │ PROXMOX_PROXMOX_DEV_URL=https://192.168.1.100:8006     │ │  │
│  │  │ PROXMOX_PROXMOX_DEV_USERNAME=kopfoperator@pve          │ │  │
│  │  │ PROXMOX_PROXMOX_DEV_TOKEN_ID=kopf-dev-token            │ │  │
│  │  │ PROXMOX_PROXMOX_DEV_TOKEN_SECRET=secret-2              │ │  │
│  │  └────────────────────────────────────────────────────────┘ │  │
│  │                              ▲                               │  │
│  └──────────────────────────────┼───────────────────────────────┘  │
│                                 │                                  │
│  ┌──────────────────────────────┴───────────────────────────────┐  │
│  │                    Kubernetes Secrets                        │  │
│  │                                                              │  │
│  │  ┌─────────────────────┐  ┌─────────────────────┐          │  │
│  │  │ proxmox-credentials-│  │ proxmox-credentials-│          │  │
│  │  │ proxmox-1           │  │ proxmox-dev         │          │  │
│  │  └─────────────────────┘  └─────────────────────┘          │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                 ▲                                  │
│                                 │                                  │
│  ┌──────────────────────────────┴───────────────────────────────┐  │
│  │                     Helm Chart Deployment                    │  │
│  │                                                              │  │
│  │  values.yaml:                                                │  │
│  │  proxmox:                                                    │  │
│  │    instances:                                                │  │
│  │      - id: "proxmox-1"                                       │  │
│  │        url: "https://192.0.2.10:8006"                      │  │
│  │        username: "kopfoperator@pve"                          │  │
│  │        tokenId: "kopf-operator-token"                        │  │
│  │        tokenSecret: "secret-1"                               │  │
│  │      - id: "proxmox-dev"                                     │  │
│  │        url: "https://192.168.1.100:8006"                     │  │
│  │        ...                                                   │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                 │
                                 │ Network Connection
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      External Proxmox Instances                     │
│                                                                     │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│  │  Proxmox Prod    │  │  Proxmox Dev     │  │  Proxmox Stage   │ │
│  │  192.0.2.10    │  │  192.168.1.100   │  │  10.0.0.50       │ │
│  │  :8006           │  │  :8006           │  │  :8006           │ │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

## Component Interaction Flow

### 1. Deployment Time (Helm Chart)

```
User ──┐
       │ helm install/upgrade
       ▼
values.yaml (proxmox.instances[])
       │
       ▼
Helm Templates
       ├──► secret.yaml ──► Creates Kubernetes Secrets (one per instance)
       │                    - proxmox-credentials-proxmox-1
       │                    - proxmox-credentials-proxmox-dev
       │
       └──► deployment.yaml ──► Creates Deployment with Env Vars
                                 - PROXMOX_PROXMOX_1_URL
                                 - PROXMOX_PROXMOX_1_USERNAME
                                 - PROXMOX_PROXMOX_1_TOKEN_ID
                                 - PROXMOX_PROXMOX_1_TOKEN_SECRET
                                 - PROXMOX_PROXMOX_DEV_URL
                                 - ...
```

### 2. Runtime (VM Creation)

```
User creates ProxmoxVM CR
       │
       │ proxmoxUrl: "https://192.0.2.10:8006"
       ▼
Kopf Handler (on_create)
       │
       │ Extract proxmoxUrl from spec
       ▼
_get_client(proxmoxUrl)
       │
       ▼
load_settings_from_env(proxmoxUrl)
       │
       ├──► _discover_proxmox_instances()
       │    │
       │    ├──► Scan environment for PROXMOX_*_URL vars
       │    │
       │    ├──► Extract credentials for each instance
       │    │
       │    └──► Build dict: {normalized_url: credentials}
       │
       ├──► _normalize_url(proxmoxUrl)
       │    │
       │    └──► "https://192.0.2.10:8006" → "192.0.2.10:8006"
       │
       ├──► _find_credentials_for_url(proxmoxUrl)
       │    │
       │    ├──► Exact match on normalized URL
       │    │
       │    └──► Returns ProxmoxInstanceCredentials or None
       │
       └──► Create ProxmoxSettings with matched credentials
              │
              ▼
ProxmoxClient initialized with correct credentials
       │
       ▼
Connect to specific Proxmox instance
       │
       ▼
Execute VM operations (clone, configure, start)
```

### 3. Credential Discovery Algorithm

```python
def _discover_proxmox_instances():
    """
    1. Scan all environment variables
    2. Find variables matching pattern: PROXMOX_<INSTANCE_ID>_URL
    3. For each match:
       a. Extract instance ID
       b. Load PROXMOX_<INSTANCE_ID>_USERNAME
       c. Load PROXMOX_<INSTANCE_ID>_TOKEN_ID
       d. Load PROXMOX_<INSTANCE_ID>_TOKEN_SECRET
    4. Normalize the URL
    5. Store in dictionary: {normalized_url: credentials}
    6. Return dictionary
    """

Example:
    Environment: PROXMOX_PROXMOX_1_URL=https://192.0.2.10:8006
    Result: {
        "192.0.2.10:8006": ProxmoxInstanceCredentials(
            instance_id="PROXMOX_1",
            url="https://192.0.2.10:8006",
            username="kopfoperator@pve",
            token_id="kopf-operator-token",
            token_secret="secret-1"
        )
    }
```

### 4. URL Matching Logic

```
Input: proxmoxUrl from CR spec
       │
       ▼
_normalize_url()
       │
       ├──► Remove protocol (https://, http://)
       ├──► Remove trailing slash
       ├──► Convert to lowercase
       └──► Add default port :8006 if missing
       │
       │ Examples:
       │ "https://192.0.2.10:8006" → "192.0.2.10:8006"
       │ "HTTPS://192.0.2.10:8006/" → "192.0.2.10:8006"
       │ "192.0.2.10" → "192.0.2.10:8006"
       │
       ▼
Normalized URL: "192.0.2.10:8006"
       │
       ▼
_find_credentials_for_url()
       │
       ├──► Exact match in discovered instances
       │    └──► Return credentials if found
       │
       └──► Hostname-only match (without port)
            └──► Return first matching credentials
```

## Data Flow Diagram

### Configuration Data Flow

```
values.yaml
    ↓
Helm Template Processing
    ↓
Kubernetes Secrets (per instance)
    ↓
Environment Variables (mounted in Pod)
    ↓
Operator Config Discovery
    ↓
In-Memory Credentials Map
    ↓
ProxmoxSettings (per VM operation)
    ↓
ProxmoxClient Connection
    ↓
Proxmox API Calls
```

### VM Creation Data Flow

```
kubectl apply -f proxmoxvm.yaml
    ↓
Kubernetes API Server
    ↓
Custom Resource Created
    ↓
Kopf Operator (watches CR)
    ↓
Handler: on_create()
    ↓
Extract: spec.proxmoxUrl
    ↓
Config: load_settings_from_env(proxmoxUrl)
    ↓
Discover & Match Credentials
    ↓
Create: ProxmoxClient(settings)
    ↓
API Call: Clone Template
    ↓
API Call: Configure VM
    ↓
API Call: Start VM
    ↓
Update CR Status: phase=Ready
```

## Security Architecture

### Secret Management

```
┌─────────────────────────────────────────────────┐
│         Helm Chart (values.yaml)                │
│         ┌─────────────────────────────┐         │
│         │  proxmox.instances:         │         │
│         │    - id: "proxmox-1"        │         │
│         │      url: "..."             │         │
│         │      username: "..."        │         │
│         │      tokenSecret: "..."     │         │
│         └─────────────────────────────┘         │
└─────────────────────────────────────────────────┘
                    │
                    │ Helm template
                    ▼
┌─────────────────────────────────────────────────┐
│      Kubernetes Secrets (Namespaced)            │
│  ┌───────────────────────────────────────────┐  │
│  │ proxmox-credentials-proxmox-1             │  │
│  │   PROXMOX_URL: <base64>                   │  │
│  │   PROXMOX_USERNAME: <base64>              │  │
│  │   PROXMOX_TOKEN_ID: <base64>              │  │
│  │   PROXMOX_TOKEN_SECRET: <base64>          │  │
│  │                                           │  │
│  │ Labels:                                   │  │
│  │   proxmox.operator/instance-id: proxmox-1│  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
                    │
                    │ Mounted as env vars
                    ▼
┌─────────────────────────────────────────────────┐
│        Operator Pod (ServiceAccount)            │
│  ┌───────────────────────────────────────────┐  │
│  │ Environment Variables (in memory)         │  │
│  │   PROXMOX_PROXMOX_1_URL                   │  │
│  │   PROXMOX_PROXMOX_1_USERNAME              │  │
│  │   PROXMOX_PROXMOX_1_TOKEN_ID              │  │
│  │   PROXMOX_PROXMOX_1_TOKEN_SECRET          │  │
│  │                                           │  │
│  │ Only accessible by operator pod           │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

### Credential Isolation

- **Per-Instance Secrets**: Each Proxmox instance has its own Kubernetes secret
- **RBAC**: Operator service account has read access only to its namespace secrets
- **No Sharing**: Credentials are never shared between instances
- **Token-Based**: Uses API tokens (not passwords) for better security
- **Rotation**: Each instance can have its tokens rotated independently

## Scalability Considerations

### Multi-Instance Support

```
┌────────────────────────────────────────────────────────┐
│                    Operator Pod                        │
│                                                        │
│  In-Memory Credentials Cache:                         │
│  {                                                     │
│    "192.0.2.10:8006": credentials_1,                │
│    "192.168.1.100:8006": credentials_2,               │
│    "10.0.0.50:8006": credentials_3,                   │
│    ...                                                 │
│    "N instances": credentials_n                        │
│  }                                                     │
│                                                        │
│  ✓ O(1) credential lookup per VM operation            │
│  ✓ Discovery happens once at startup                  │
│  ✓ Minimal memory footprint per instance              │
│  ✓ No external dependencies                           │
└────────────────────────────────────────────────────────┘
```

### Performance Characteristics

- **Discovery**: O(E) where E = number of environment variables (done once at startup)
- **Lookup**: O(I) where I = number of instances (typically < 10)
- **Memory**: ~1KB per instance configuration
- **Network**: One TCP connection per Proxmox instance (connection pooling by proxmoxer)

## Failure Modes & Handling

### Missing Credentials

```
ProxmoxVM CR (proxmoxUrl: "https://unknown.com:8006")
       │
       ▼
load_settings_from_env()
       │
       ├──► _find_credentials_for_url() ──► None
       │
       ├──► Fallback to legacy env vars ──► None
       │
       └──► Raise ValueError with clear message
              │
              ▼
Handler catches error
       │
       ▼
Update CR status: phase=Error, message="No credentials found..."
```

### Connection Failure

```
ProxmoxClient.connect()
       │
       ├──► Network timeout
       ├──► Certificate validation failure
       ├──► Authentication failure
       │
       └──► ProxmoxError raised
              │
              ▼
Handler catches error
       │
       ▼
Update CR status: phase=Error, message="Failed to connect..."
```

## Extension Points

### Adding New Authentication Methods

Current support:
- API Tokens (recommended)
- Username/Password (legacy)

Future extensions:
- OAuth2/OIDC
- Certificate-based auth
- External credential providers (Vault, AWS Secrets Manager)

### Custom URL Resolution

Current: Direct URL matching

Potential extensions:
- Regex-based matching
- Tag-based selection
- Load balancing across multiple instances
- Geographic routing

## Summary

The multi-instance architecture provides:

✅ **Flexibility**: Support for unlimited Proxmox instances
✅ **Security**: Isolated credentials per instance
✅ **Scalability**: Efficient O(1) credential lookup
✅ **Reliability**: Clear error handling and status reporting
✅ **Compatibility**: Backward compatible with single-instance mode
✅ **Simplicity**: Minimal configuration required
