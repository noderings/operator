# proxmox-operator-crds

Helm chart that installs **CustomResourceDefinitions** for the Proxmox operator (`vm.proxmox.com` API group).

## CRDs

| Resource | Kind |
|----------|------|
| `proxmoxnodes` | ProxmoxNode |
| `proxmoxvms` | ProxmoxVM |
| `firewallrulesets` | FirewallRuleSet |
| `vmbackups` | VMBackup |
| `vmsnapshots` | VMSnapshot |
| `vncrequests` | VNCRequest |

Definitions live in `templates/crds/`.

## Install

**Platform cluster** (the cluster where Proxmox CRs are created):

```bash
helm upgrade --install proxmox-crds . \
  --namespace proxmox-system --create-namespace
```

**As dependency** of `proxmox-operator` on provider clusters (`crds.enabled: true` by default).

## Values

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Install CRDs when `false` skips all manifests |

## Schema changes

When changing a CRD:

1. Edit the YAML under `templates/crds/`
2. Update `operator/src/proxmox_operator/types.py`
3. Update whatever creates those objects, if it emits the field
4. Bump chart `version` and roll out CRDs **before** that component starts emitting new fields

### Provider outbound SMTP (`ProxmoxVM.spec.blockOutboundSmtp`)

When `true`, the operator ensures guest firewall DROP rules for outbound TCP **25 / 465 / 587** (comment prefix `noderings:smtp-outbound-block:`). These rules are owned by the VM CR, not by `FirewallRuleSet` — ruleset reconcile never deletes them. Customer FirewallRuleSet CRs stay unchanged.
