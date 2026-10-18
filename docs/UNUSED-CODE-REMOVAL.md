# Unused / orphaned code removal

High-confidence dead code and obsolete docs removed from the operator. Live reconcile handlers, CRDs, charts RBAC, and status models (e.g. `ProxmoxVMStatus`) were left untouched.

## Removed

| Area | What |
|------|------|
| `client.py` | `vm_exists_with_name`, `vm_exists_by_name`, `_find_iso_storage`, `create_ipfilter_ipsets` (alias), `pause_vm` |
| `reconcile.py` | `_FIREWALL_RULESET_UUID_PATTERN`, `_extract_firewall_ruleset_name`, unused `import re` |
| `operations.py` | `CreateOperationHandler._read_auth_secret`; unused `read_auth_secret` import (`read_secret` kept) |
| `template_compiler.py` | Module-level `compile_template` (`compile_if_template` + class kept) |
| `constants.py` | `SnapshotPhase.DISABLED`, `VM_STATUS_PAUSED`, `DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT`, `ISO_UPLOAD_MIN_TIMEOUT`, `BACKUP_RECONCILE_INTERVAL`, `BACKUP_VM_STOP_*`, `BACKUP_RESTORE_STATUS_RETENTION_TIME`, `SNAPSHOT_TASK_POLL_*` |
| Dependencies | `tenacity` from `pyproject.toml` / lock / `requirements.txt` |
| Docs | Obsolete backup/snapshot design notes (see below) |

## Kept (intentionally)

- Multi-instance docs: [MULTI_INSTANCE.md](./MULTI_INSTANCE.md), [ARCHITECTURE_MULTI_INSTANCE.md](./ARCHITECTURE_MULTI_INSTANCE.md)
- Active constants such as `BACKUP_VZDUMP_POST_DELAY_SECONDS`, `FIREWALL_*`, `SNAPSHOT_RECONCILE_INTERVAL`, `OP_*`, phase enums (minus `DISABLED`)
- Transient Proxmox retry constants (`PROXMOX_TRANSIENT_*`, `PROXMOX_VM_ABSENT_*`) used by handlers / VNC reconcile
- `utils.read_auth_secret` (still available; create path uses `_parse_auth_secret_data` + `read_secret`)
- `TemplateCompiler` / `compile_if_template`
- Operation types: `created` / `started` / `stopped` / `deleted` / `restarted` / `reinstalled`

## Doc drift

- README Features and `samples/proxmoxvm.yaml` operation comments aligned with real `OperationType` values (no pause/reboot).
