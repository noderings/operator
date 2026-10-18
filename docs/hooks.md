# Provider lifecycle hook scripts

Operators run provider-supplied Python scripts as isolated Kubernetes Jobs after a **successful** VM operation (`created`, `started`, `stopped`, `deleted`, `restarted`, `reinstalled`). Hook failure never fails the VM operation.

Providers write and enable scripts in the console. The public guide is **Hook Scripts** in NodeRings docs (`provider-guides/infrastructure/hook-scripts`).

## Preconditions

- The cluster CNI must **enforce NetworkPolicy**. Do not set `hooks.enabled=true` on clusters whose CNI ignores NetworkPolicy; hook pods would otherwise have unrestricted egress.
- Hook Jobs and script Secrets run in a dedicated namespace `{Release.Namespace}-hooks` (override with `hooks.jobNamespace`). The NetworkPolicy lives in that namespace. VM CRs and hypervisor credential Secrets stay in the operator namespace.
- CRDs must include `spec.hooks` and `status.lastHook` before scripts are written onto the VM (unknown fields are pruned).

## Security model

Jobs are never executed in the operator process. Each hook pod has:

- `automountServiceAccountToken: false` (no kube API access)
- non-root uid/gid `65532`, read-only root filesystem, `emptyDir` `/tmp`
- dropped capabilities, no privilege escalation, RuntimeDefault seccomp
- CPU/memory limits, `activeDeadlineSeconds` (default 300s), `backoffLimit: 0`
- stdlib-only Python (no pip; read-only FS and no PyPI egress)
- no proxy env, no internal URLs, no hypervisor credential Secrets
- optional provider-owned Secret only (see [Provider secrets](#provider-secrets)); the platform does not store those values

Default egress (SSRF-hardened):

- TCP 80/443 to the public internet, **except** RFC1918, link-local (`169.254.0.0/16`, including cloud metadata), CGNAT (`100.64.0.0/10`), loopback, multicast
- IPv6 public: `::/0` except ULA (`fc00::/7`), link-local (`fe80::/10`), `::1/128` on TCP 80/443
- Provider private ranges (RFC1918, CGNAT/Tailscale, IPv6 ULA) on any TCP port, **except** Kubernetes / docker / kind CIDRs listed in `hooks.privateEgress[].except`
- Still blocked: cluster pod/service CIDRs, link-local / IMDS, loopback, multicast
- DNS (53 UDP/TCP) only to kube-dns in `kube-system`
- Extra one-off targets via `hooks.extraEgress` (`{cidr, port}`)

## Execution contract

- At-most-once per operation: Job name is `hook-<sha1[:16]>` of `{namespace}/{name}/{op}/{generation}`. `AlreadyExists` means the hook already ran.
- If the VM operation itself is skipped (idempotency / already handled), the hook does **not** fire.
- `reinstall` and `reinstalled` both use the `reinstalled` script.
- Concurrency cap: `HOOKS_MAX_CONCURRENT` (default 5) active hook Jobs in the hook-job namespace (best-effort list; TOCTOU with create). Excess raises a transient error so Kopf retries dispatch; the Job name stays at-most-once.
- Proxmox hook IPs come from `spec.vmConfig.cloudInit.staticIpConfig` only. DHCP guests get empty `NODERINGS_IPV4` / `NODERINGS_IPV6` until that static config is set.
- Script Secret is owned by the Job; Job TTL (default 24h) garbage-collects both.
- The control plane stores the script source. The Job Secret is the cluster-side copy of that snapshot.

## Environment

| Variable | Meaning |
| --- | --- |
| `NODERINGS_HOOK` | Operation type (`created`, `started`, …) |
| `NODERINGS_VM_NAME` | VM CR name |
| `NODERINGS_VM_ID` | Hypervisor VM/server id when known |
| `NODERINGS_IPV4` | Primary IPv4 when known at hook dispatch |
| `NODERINGS_IPV4_PREFIX` | Primary IPv4 CIDR prefix length (e.g. `24`) |
| `NODERINGS_IPV4_NETMASK` | Dotted IPv4 netmask derived from the prefix (e.g. `255.255.255.0`) |
| `NODERINGS_IPV6` | Primary IPv6 when known at hook dispatch |
| `NODERINGS_IPV6_PREFIX` | Primary IPv6 CIDR prefix length (e.g. `64`) |
| `NODERINGS_EVENT_JSON` | Curated JSON: hook, vmName, namespace, vmId, operation, ipv4[], ipv6[], gateways, prefixes, ipv4Netmask |

Event JSON never includes `spec.hooks`, secret refs, or cloud-init data.

## Provider secrets

Providers who need credentials inside the Job (without storing them on the platform) create an Opaque Secret in the **hooks job namespace**:

```bash
kubectl create secret generic noderings-hook-secrets -n <hooks-namespace> \
  --from-literal=USERNAME=admin \
  --from-literal=PASSWORD='...'
```

Default Secret name: `noderings-hook-secrets` (`hooks.providerSecretName` / `HOOKS_PROVIDER_SECRET_NAME`). Set the value to `""` to disable injection.

When present, every key is:

- injected as an environment variable (`envFrom`, `optional: true`)
- mounted as a file under `/var/run/secrets/noderings-hooks/<key>` (`optional: true`)

A missing Secret does **not** fail the Job. Explicit `NODERINGS_*` env vars always win over Secret keys with the same name.

Requirements:

- Secret keys must be valid environment variable names (`[A-Za-z_][A-Za-z0-9_]*`) or the pod fails with `CreateContainerConfigError`
- Do not put hypervisor tokens or NodeRings credentials here; prefer a webhook you control when possible
- Values stay on the provider cluster (hooks namespace). The control plane does not store them.

## Feedback

- CR `status.lastHook`: `{operation, state, ranAt, message, jobName}`
- Kubernetes Event on the VM CR
- Job logs (existing Alloy scrape)

## Example script

Stdlib-only Python 3 POST (Jobs run `python3 /hook/script.py`):

```python
#!/usr/bin/env python3
import json
import os
import urllib.request

payload = {
    "event": os.environ.get("NODERINGS_EVENT_JSON", "{}"),
    "hook": os.environ.get("NODERINGS_HOOK", ""),
    "vm_name": os.environ.get("NODERINGS_VM_NAME", ""),
    "vm_id": os.environ.get("NODERINGS_VM_ID", ""),
    "ipv4": os.environ.get("NODERINGS_IPV4", ""),
    "ipv6": os.environ.get("NODERINGS_IPV6", ""),
    "ipv4_prefix": os.environ.get("NODERINGS_IPV4_PREFIX", ""),
    "ipv4_netmask": os.environ.get("NODERINGS_IPV4_NETMASK", ""),
    "ipv6_prefix": os.environ.get("NODERINGS_IPV6_PREFIX", ""),
}
req = urllib.request.Request(
    "https://hooks.example.com/noderings",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as resp:
    resp.read()
```

## Chart values

```yaml
hooks:
  enabled: true
  jobNamespace: ""  # empty → {Release.Namespace}-hooks
  providerSecretName: "noderings-hook-secrets"  # "" disables
  image: ""  # empty → operator image
  timeoutSeconds: 300
  maxConcurrent: 5
  ttlSecondsAfterFinished: 86400
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: 100m
      memory: 128Mi
  extraEgress: []
  # - cidr: 203.0.113.10/32
  #   port: 8443
  privateEgress:
    - cidr: 10.0.0.0/8
      except: [10.42.0.0/16, 10.43.0.0/16, 10.96.0.0/12, 10.100.0.0/16, 10.101.0.0/16, 10.200.0.0/16, 10.201.0.0/16, 10.244.0.0/16]
    - cidr: 172.16.0.0/12
      except: [172.17.0.0/16, 172.18.0.0/16]
    - cidr: 192.168.0.0/16
    - cidr: 100.64.0.0/10
    - cidr: fc00::/7
```
