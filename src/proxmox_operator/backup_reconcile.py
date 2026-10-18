"""Reconciliation for VMBackup CRs (one CR per backup run)."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import kopf

from .client import ProxmoxClient
from .config import load_settings_from_env
from .constants import (
    BACKUP_VZDUMP_POST_DELAY_SECONDS,
    BackupPhase,
    EXIT_STATUS_OK,
    RESTORE_STATE_COMPLETED,
    RESTORE_STATE_FAILED,
    RESTORE_STATE_RESTORING,
    VM_STATUS_RUNNING,
)
from .node_handler import resolve_proxmox_url
from .types import VMBackupSpec
from .utils import _get_k8s_client, resolve_vmid_str_from_vm_cr

logger = logging.getLogger(__name__)
PREFIX = "BackupRun"
RECONCILE_INTERVAL = 30


def _get_backup_vzdump_post_delay_seconds() -> int:
    """Seconds to wait after vzdump POST returns; env BACKUP_VZDUMP_POST_DELAY_SECONDS overrides default."""
    raw = os.getenv("BACKUP_VZDUMP_POST_DELAY_SECONDS")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning(
                "Invalid BACKUP_VZDUMP_POST_DELAY_SECONDS value: %s, using default: %s",
                raw,
                BACKUP_VZDUMP_POST_DELAY_SECONDS,
            )
    return BACKUP_VZDUMP_POST_DELAY_SECONDS


VZDUMP_POST_DELAY_SECONDS = _get_backup_vzdump_post_delay_seconds()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_client(proxmox_url: str) -> ProxmoxClient:
    return ProxmoxClient(load_settings_from_env(proxmox_url))


def _get_proxmoxvm_cr(vm_ref_name: str, namespace: str) -> dict[str, Any] | None:
    try:
        _, custom_api = _get_k8s_client()
        return custom_api.get_namespaced_custom_object(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace=namespace,
            plural="proxmoxvms",
            name=vm_ref_name,
        )
    except Exception as e:
        logger.error("%s get ProxmoxVM CR %s/%s: %s", PREFIX, namespace, vm_ref_name, e)
        return None


def _vm_info_from_cr(
    vm_cr: dict[str, Any], log: kopf.Logger | None = None
) -> tuple[str | None, str | None, int | None]:
    """Extract (proxmox_url, node_name, vmid) from a ProxmoxVM CR.

    The Proxmox URL is resolved via the linked ProxmoxNode (single source of
    truth). Failure to resolve returns ``None`` for proxmox_url so callers
    can degrade gracefully instead of crashing the reconcile loop.
    """
    spec = vm_cr.get("spec", {})
    namespace = vm_cr.get("metadata", {}).get("namespace", "default")
    node_ref = spec.get("proxmoxNodeRef") or {}
    node_ref_name = node_ref.get("name") if isinstance(node_ref, dict) else None
    proxmox_url: str | None
    log_ = log or logger
    try:
        proxmox_url = resolve_proxmox_url(node_ref_name, namespace, log_)
    except Exception as e:
        log_.debug(
            "%s resolve proxmoxUrl for VM %s/%s via ProxmoxNode %s: %s",
            PREFIX,
            namespace,
            vm_cr.get("metadata", {}).get("name", "?"),
            node_ref_name,
            e,
        )
        proxmox_url = None

    vm_config = spec.get("vmConfig", {})
    node = vm_config.get("nodeName")
    vm_name = vm_config.get("name")
    vmid_str = resolve_vmid_str_from_vm_cr(vm_cr)
    vmid: int | None = None
    if vmid_str:
        try:
            vmid = int(vmid_str)
        except ValueError:
            log_.warning("%s invalid status.vmId=%r on VM CR", PREFIX, vmid_str)
    if vmid is None and proxmox_url and node and vm_name:
        try:
            vmid = _get_client(proxmox_url).find_vm_by_name(node, vm_name)
        except Exception:
            pass
    return proxmox_url, node, vmid


def _patch_status(
    namespace: str, name: str, status_updates: dict[str, Any], log: kopf.Logger
) -> None:
    try:
        _, custom_api = _get_k8s_client()
        cr = custom_api.get_namespaced_custom_object(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace=namespace,
            plural="vmbackups",
            name=name,
        )
        status = {**cr.get("status", {}), **status_updates}
        custom_api.patch_namespaced_custom_object_status(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace=namespace,
            plural="vmbackups",
            name=name,
            body={"status": status},
        )
    except Exception as e:
        logger.warning("%s patch status %s/%s: %s", PREFIX, namespace, name, e)


def _get_vmbackup_cr(namespace: str, name: str) -> dict[str, Any] | None:
    """Fetch the current VMBackup CR from the API server. Use this so we see latest spec (e.g. expiresAt set by backend)."""
    try:
        _, custom_api = _get_k8s_client()
        return custom_api.get_namespaced_custom_object(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace=namespace,
            plural="vmbackups",
            name=name,
        )
    except Exception as e:
        logger.warning("%s get VMBackup CR %s/%s: %s", PREFIX, namespace, name, e)
        return None


def _list_backups(client: ProxmoxClient, node: str, vmid: int) -> list[dict[str, Any]]:
    try:
        proxmox = client._get_connection()
        storages = proxmox.nodes(node).storage.get()
        out = []
        for storage in storages:
            storage_name = storage.get("storage")
            if not storage_name:
                continue
            try:
                content = proxmox.nodes(node).storage(storage_name).content.get()
                if not isinstance(content, list):
                    continue
                for item in content:
                    if (
                        str(item.get("vmid")) == str(vmid) or item.get("vmid") == vmid
                    ) and item.get("content") == "backup":
                        out.append(item)
            except Exception:
                continue
        return out
    except Exception as e:
        logger.error("%s list backups vmid=%s: %s", PREFIX, vmid, e)
        return []


def _newest_backup_volid_and_size(
    client: ProxmoxClient, node: str, vmid: int
) -> tuple[str | None, int]:
    """Pick the backup volume with the latest ctime from all storages for this vmid."""
    backups = _list_backups(client, node, vmid)
    volid = None
    size_bytes = 0
    latest_ctime = 0
    for b in backups:
        ctime = b.get("ctime") or 0
        if isinstance(ctime, str):
            try:
                ctime = int(ctime)
            except ValueError:
                continue
        if ctime >= latest_ctime:
            latest_ctime = ctime
            volid = b.get("volid")
            size_bytes = int(b.get("size", 0) or 0)
    return volid, size_bytes


def _create_backup(
    client: ProxmoxClient, node: str, vmid: int, storage_config: dict[str, Any]
) -> str | None:
    try:
        sid = storage_config.get("storageId")
        mode = storage_config.get("mode")
        compression = storage_config.get("compression")
        if not sid or not mode or not compression:
            logger.error(
                "%s _create_backup missing storage fields storage_config=%s", PREFIX, storage_config
            )
            return None
        proxmox = client._get_connection()
        params = {"vmid": vmid, "mode": mode, "storage": sid, "compress": compression}
        task = proxmox.nodes(node).vzdump.post(**params)
        upid = (
            task.get("UPID")
            if isinstance(task, dict)
            else (task if isinstance(task, str) else None)
        )
        if upid:
            time.sleep(VZDUMP_POST_DELAY_SECONDS)
            return upid
    except Exception as e:
        logger.error("%s create backup vmid=%s: %s", PREFIX, vmid, e)
    return None


def _reconcile_vmbackup_expiry(
    spec: dict[str, Any],
    status: dict[str, Any],
    client: ProxmoxClient,
    node: str,
    namespace: str,
    name: str,
    logger: kopf.Logger,
) -> bool:
    """If expiresAt has passed and volid is set, delete the backup in Proxmox and mark the CR Deleted.

    Returns:
        True if reconciliation should stop (deleted, or delete failed).
    """
    expires_at_str = spec.get("expiresAt")
    volid = (status or {}).get("volid")

    if not expires_at_str:
        if volid:
            logger.debug(
                "%s CR %s/%s: no expiresAt set, retention not requested", PREFIX, namespace, name
            )
        return False

    try:
        expires_at = datetime.fromisoformat(str(expires_at_str).replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        logger.warning("%s invalid expiresAt name=%s expiresAt=%s", PREFIX, name, expires_at_str)
        return False

    now_utc = datetime.now(timezone.utc)
    if now_utc < expires_at:
        logger.debug(
            "%s CR %s/%s: expiresAt not yet reached (expiresAt=%s now=%s)",
            PREFIX,
            namespace,
            name,
            expires_at_str,
            now_utc.isoformat(),
        )
        return False

    if not volid:
        logger.debug(
            "%s CR %s/%s: expiresAt reached but no volid yet, skipping delete",
            PREFIX,
            namespace,
            name,
        )
        return False

    vlog = str(volid)[:50] + "..." if len(str(volid)) > 50 else volid
    logger.info(
        "%s CR %s/%s: expiresAt reached, deleting backup from Proxmox volid=%s",
        PREFIX,
        namespace,
        name,
        vlog,
    )
    try:
        client.delete_backup(node, volid)
    except Exception as e:
        logger.warning("%s delete backup from Proxmox failed name=%s: %s", PREFIX, name, e)
        _patch_status(
            namespace,
            name,
            {"phase": BackupPhase.FAILED, "message": f"Failed to delete from Proxmox: {e}"},
            logger,
        )
        return True

    _patch_status(
        namespace,
        name,
        {
            "phase": BackupPhase.DELETED,
            "volid": None,
            "sizeBytes": None,
        },
        logger,
    )
    # Do not clear spec.expiresAt — Backend owns Spec under Liqo (status-only mirror).
    logger.info("%s CR %s/%s: backup deleted from Proxmox, phase=Deleted", PREFIX, namespace, name)
    return True


def _reconcile_vmbackup_restore(
    spec: dict[str, Any],
    status: dict[str, Any],
    client: ProxmoxClient,
    node: str,
    vmid: int,
    namespace: str,
    name: str,
    logger: kopf.Logger,
) -> bool:
    """Handle spec.restoreRequest: validate volid, poll restore task, or start restore.

    Returns:
        True if reconciliation should stop after handling restore (including no-op restoring wait).
    """
    restore_request = spec.get("restoreRequest")
    if not restore_request:
        return False

    restore_status = (status or {}).get("restoreStatus") or {}
    volid = restore_request.get("backupVolid") or restore_status.get("requestedBackupVolid")
    state = restore_status.get("state", "none")
    volid_log = (str(volid)[:50] + "...") if volid and len(str(volid)) > 50 else volid
    logger.debug("%s restore path name=%s volid=%s state=%s", PREFIX, name, volid_log, state)

    # Terminal restore — wait for Backend to clear Spec (Liqo status-only).
    if state in (RESTORE_STATE_COMPLETED, RESTORE_STATE_FAILED):
        logger.debug(
            "%s restore already %s, waiting for Spec clear name=%s",
            PREFIX,
            state,
            name,
        )
        return True

    if not volid:
        logger.warning("%s restoreRequest missing backupVolid", PREFIX)
        _patch_status(
            namespace,
            name,
            {
                "restoreStatus": {
                    **restore_status,
                    "state": RESTORE_STATE_FAILED,
                    "requestedBackupVolid": restore_request.get("backupVolid"),
                    "error": "missing backupVolid",
                }
            },
            logger,
        )
        return True

    if state == RESTORE_STATE_RESTORING:
        task_upid = restore_status.get("taskUpid")
        if task_upid:
            task_status = client.get_task_status(node, task_upid)
            logger.debug(
                "%s restore task check name=%s task_upid=%s task_status=%s",
                PREFIX,
                name,
                task_upid,
                task_status.get("status") if task_status else None,
            )
            if task_status and task_status.get("status") == "stopped":
                exit_status = task_status.get("exitstatus")
                now_iso = _utc_timestamp()
                if exit_status == EXIT_STATUS_OK:
                    logger.info("%s restore succeeded name=%s", PREFIX, name)
                    _patch_status(
                        namespace,
                        name,
                        {
                            "restoreStatus": {
                                **restore_status,
                                "state": "completed",
                                "completedAt": now_iso,
                            },
                        },
                        logger,
                    )
                else:
                    logger.warning(
                        "%s restore failed name=%s exit_status=%s", PREFIX, name, exit_status
                    )
                    _patch_status(
                        namespace,
                        name,
                        {
                            "restoreStatus": {
                                **restore_status,
                                "state": "failed",
                                "error": exit_status or "unknown",
                                "completedAt": now_iso,
                            },
                        },
                        logger,
                    )
        return True

    stop_vm = restore_request.get("stopVm", True)
    start_after = restore_request.get("startAfterRestore", False)
    logger.debug(
        "%s starting restore name=%s node=%s vmid=%s stop_vm=%s start_after=%s",
        PREFIX,
        name,
        node,
        vmid,
        stop_vm,
        start_after,
    )
    try:
        if stop_vm:
            try:
                status_before = client.get_vm_status(node, vmid)
                logger.debug(
                    "%s pre-restore VM status name=%s vmid=%s status=%s",
                    PREFIX,
                    name,
                    vmid,
                    status_before,
                )
                if status_before == VM_STATUS_RUNNING:
                    logger.debug(
                        "%s stopping VM before restore name=%s vmid=%s", PREFIX, name, vmid
                    )
                    client.stop_vm(node, vmid)
                    time.sleep(3)
            except Exception as e:
                logger.warning("%s stop VM before restore: %s", PREFIX, e)
        task_upid = client.restore_backup(node, vmid, volid, start=start_after)
        logger.info("%s restore started name=%s task_upid=%s", PREFIX, name, task_upid)
    except Exception as e:
        logger.error("%s restore failed: %s", PREFIX, e)
        now_iso = _utc_timestamp()
        _patch_status(
            namespace,
            name,
            {
                "restoreStatus": {
                    **restore_status,
                    "state": "failed",
                    "requestedBackupVolid": volid,
                    "requestedAt": now_iso,
                    "startedAt": now_iso,
                    "completedAt": now_iso,
                    "error": str(e),
                },
            },
            logger,
        )
        return True

    now_iso = _utc_timestamp()
    _patch_status(
        namespace,
        name,
        {
            "restoreStatus": {
                **restore_status,
                "state": "restoring",
                "requestedBackupVolid": volid,
                "requestedAt": now_iso,
                "startedAt": now_iso,
                "taskUpid": task_upid,
            },
        },
        logger,
    )
    logger.debug("%s restore status set to restoring name=%s task_upid=%s", PREFIX, name, task_upid)
    return True


def _reconcile_vmbackup_running_task(
    status: dict[str, Any],
    client: ProxmoxClient,
    node: str,
    vmid: int,
    namespace: str,
    name: str,
    logger: kopf.Logger,
) -> None:
    """Phase Running: poll vzdump task; on success set Succeeded with volid/size from newest backup."""
    task_upid = status.get("taskUpid")
    if not task_upid:
        logger.warning("%s Running but missing taskUpid name=%s", PREFIX, name)
        _patch_status(
            namespace, name, {"phase": BackupPhase.FAILED, "message": "Missing taskUpid"}, logger
        )
        return

    task_status = client.get_task_status(node, task_upid)
    logger.debug(
        "%s backup task check name=%s task_upid=%s task_status=%s",
        PREFIX,
        name,
        task_upid,
        task_status.get("status") if task_status else None,
    )
    if not task_status or task_status.get("status") != "stopped":
        return

    exit_status = task_status.get("exitstatus")
    logger.debug("%s backup task stopped name=%s exit_status=%s", PREFIX, name, exit_status)
    if exit_status != EXIT_STATUS_OK:
        logger.warning("%s backup failed name=%s error=%s", PREFIX, name, exit_status or "unknown")
        _patch_status(
            namespace,
            name,
            {
                "phase": BackupPhase.FAILED,
                "error": exit_status or "unknown",
                "completedAt": _utc_timestamp(),
            },
            logger,
        )
        return

    volid, size_bytes = _newest_backup_volid_and_size(client, node, vmid)
    _patch_status(
        namespace,
        name,
        {
            "phase": BackupPhase.SUCCEEDED,
            "volid": volid or "",
            "sizeBytes": size_bytes,
            "completedAt": _utc_timestamp(),
            "error": None,
        },
        logger,
    )
    logger.info(
        "%s backup succeeded name=%s volid=%s size_bytes=%s", PREFIX, name, volid, size_bytes
    )


@kopf.timer(
    "vm.proxmox.com", "v1alpha1", "vmbackups", interval=RECONCILE_INTERVAL, idle=RECONCILE_INTERVAL
)
def reconcile_vmbackup(
    spec: dict, meta: dict, status: dict, patch: kopf.Patch, logger: kopf.Logger, **_: Any
) -> None:
    name = meta.get("name", "")
    namespace = meta.get("namespace", "default")

    # Re-fetch CR from API so we see latest spec (e.g. spec.expiresAt set by backend when user deletes backup)
    cr = _get_vmbackup_cr(namespace, name)
    if not cr:
        return
    spec = cr.get("spec", spec)
    status = cr.get("status", status or {})

    current_phase = (status or {}).get("phase", BackupPhase.PENDING)
    has_restore = bool(spec.get("restoreRequest"))
    expires_at_str = spec.get("expiresAt")
    volid_status = (status or {}).get("volid")
    addon_id = spec.get("addonId", "")
    vm_ref_name_log = (spec.get("vmRef") or {}).get("name", "")
    volid_preview = (
        (volid_status[:40] + "...")
        if volid_status and len(str(volid_status)) > 40
        else (volid_status or "")
    )
    logger.debug(
        "%s reconcile CR name=%s namespace=%s phase=%s addonId=%s vmRef.name=%s expiresAt=%s status.volid=%s has_restore=%s",
        PREFIX,
        name,
        namespace,
        current_phase,
        addon_id,
        vm_ref_name_log,
        expires_at_str,
        volid_preview,
        has_restore,
    )

    # One CR = one backup. Never run backup again if phase is Deleted or Succeeded.
    if current_phase == BackupPhase.DELETED:
        logger.debug("%s CR %s/%s: phase=Deleted, nothing to do", PREFIX, namespace, name)
        return
    if current_phase == BackupPhase.SUCCEEDED:
        if not expires_at_str or not volid_status:
            logger.debug(
                "%s CR %s/%s: phase=Succeeded, backup already taken, skipping",
                PREFIX,
                namespace,
                name,
            )
            return
        logger.debug(
            "%s CR %s/%s: phase=Succeeded, checking expiry (expiresAt=%s)",
            PREFIX,
            namespace,
            name,
            expires_at_str,
        )
    if expires_at_str and volid_status:
        logger.debug(
            "%s CR %s/%s: has expiresAt and volid; will delete from Proxmox when now >= expiresAt",
            PREFIX,
            namespace,
            name,
        )

    try:
        desired = VMBackupSpec.model_validate(spec)
    except Exception as e:
        logger.error("%s invalid spec %s/%s: %s", PREFIX, namespace, name, e)
        _patch_status(namespace, name, {"phase": BackupPhase.FAILED, "message": str(e)}, logger)
        return

    vm_ref_name = desired.vmRef.name
    vm_namespace = desired.vmRef.namespace or namespace
    vm_cr = _get_proxmoxvm_cr(vm_ref_name, vm_namespace)
    if not vm_cr:
        logger.warning("%s ProxmoxVM CR not found vm_ref=%s", PREFIX, vm_ref_name)
        _patch_status(
            namespace,
            name,
            {"phase": BackupPhase.FAILED, "message": f"ProxmoxVM CR not found: {vm_ref_name}"},
            logger,
        )
        return

    proxmox_url, node, vmid = _vm_info_from_cr(vm_cr, logger)
    if not proxmox_url or not node or vmid is None:
        logger.warning(
            "%s missing proxmox info url=%s node=%s vmid=%s", PREFIX, bool(proxmox_url), node, vmid
        )
        _patch_status(
            namespace,
            name,
            {"phase": BackupPhase.FAILED, "message": "Missing proxmoxUrl, node, or vmid"},
            logger,
        )
        return

    try:
        client = _get_client(proxmox_url)
    except Exception as e:
        logger.warning("%s get client failed: %s", PREFIX, e)
        _patch_status(namespace, name, {"phase": BackupPhase.FAILED, "message": str(e)}, logger)
        return

    if _reconcile_vmbackup_expiry(spec, status, client, node, namespace, name, logger):
        return

    if _reconcile_vmbackup_restore(spec, status, client, node, vmid, namespace, name, logger):
        return

    if current_phase == BackupPhase.PENDING:
        storage_config = desired.storage.model_dump()
        logger.debug(
            "%s backup Pending name=%s node=%s vmid=%s storage_config=%s",
            PREFIX,
            name,
            node,
            vmid,
            storage_config,
        )
        task_upid = _create_backup(client, node, vmid, storage_config)
        if not task_upid:
            logger.warning("%s create_backup returned no task_upid name=%s", PREFIX, name)
            _patch_status(
                namespace,
                name,
                {"phase": BackupPhase.FAILED, "message": "Failed to start backup in Proxmox"},
                logger,
            )
            return
        _patch_status(
            namespace,
            name,
            {
                "phase": BackupPhase.RUNNING,
                "taskUpid": task_upid,
                "startedAt": _utc_timestamp(),
            },
            logger,
        )
        logger.info("%s backup started name=%s task_upid=%s", PREFIX, name, task_upid)
        return

    if current_phase == BackupPhase.RUNNING:
        _reconcile_vmbackup_running_task(status, client, node, vmid, namespace, name, logger)
