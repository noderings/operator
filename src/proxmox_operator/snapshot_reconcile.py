"""Reconciliation logic for VMSnapshot CRs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import kopf

from .client import ProxmoxClient
from .config import load_settings_from_env
from .constants import (
    EXIT_STATUS_OK,
    RESTORE_STATE_COMPLETED,
    RESTORE_STATE_FAILED,
    RESTORE_STATE_RESTORING,
    SNAPSHOT_RECONCILE_INTERVAL,
    SnapshotPhase,
)
from .node_handler import resolve_proxmox_url
from .status import set_status
from .types import VMSnapshotSpec, VMSnapshotStatus
from .utils import _get_k8s_client, resolve_vmid_str_from_vm_cr

logger = logging.getLogger(__name__)
_PREFIX = "Snapshot"

logger.debug("%s reconcile timer: interval=%ds", _PREFIX, SNAPSHOT_RECONCILE_INTERVAL)


# ---------------------------------------------------------------------------
# Helpers: CR and VM resolution
# ---------------------------------------------------------------------------


def _get_client(proxmox_url: str) -> ProxmoxClient:
    """Proxmox client with settings from environment."""
    return ProxmoxClient(load_settings_from_env(proxmox_url))


def _get_vmsnapshot_cr(namespace: str, name: str) -> dict[str, Any] | None:
    """Fetch current VMSnapshot CR from the API. Returns None on failure."""
    try:
        _, custom_api = _get_k8s_client()
        return custom_api.get_namespaced_custom_object(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace=namespace,
            plural="vmsnapshots",
            name=name,
        )
    except Exception as e:
        logger.error("%s failed to get CR %s/%s: %s", _PREFIX, namespace, name, e)
        return None


def _get_proxmoxvm_cr(vm_ref_name: str, namespace: str) -> dict[str, Any] | None:
    """Get ProxmoxVM CR by name for proxmoxUrl, node, vmid."""
    logger.debug("%s get ProxmoxVM CR: %s/%s", _PREFIX, namespace, vm_ref_name)
    try:
        _, custom_api = _get_k8s_client()
        vm_cr = custom_api.get_namespaced_custom_object(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace=namespace,
            plural="proxmoxvms",
            name=vm_ref_name,
        )
        logger.debug("%s ProxmoxVM CR: %s", _PREFIX, json.dumps(vm_cr, indent=2, default=str))
        return vm_cr
    except Exception as e:
        logger.error("%s ProxmoxVM CR %s/%s: %s", _PREFIX, namespace, vm_ref_name, e)
        return None


def _vm_info_from_cr(
    vm_cr: dict[str, Any], log: kopf.Logger | None = None
) -> tuple[str | None, str | None, int | None]:
    """Extract (proxmox_url, node, vmid) from ProxmoxVM CR.

    The Proxmox URL is resolved via the linked ProxmoxNode (single source of
    truth). Best-effort: returns ``None`` for proxmox_url if the lookup fails,
    so callers can degrade gracefully.
    """
    spec = vm_cr.get("spec", {})
    namespace = vm_cr.get("metadata", {}).get("namespace", "default")
    node_ref = spec.get("proxmoxNodeRef") or {}
    node_ref_name = node_ref.get("name") if isinstance(node_ref, dict) else None
    log_ = log or logger
    try:
        proxmox_url: str | None = resolve_proxmox_url(node_ref_name, namespace, log_)
    except Exception as e:
        log_.debug(
            "%s resolve proxmoxUrl for VM %s/%s via ProxmoxNode %s: %s",
            _PREFIX,
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
    vmid = int(vmid_str) if vmid_str else None

    if vmid is None and proxmox_url and node and vm_name:
        logger.debug("%s vmid missing, resolving by name: node=%s vm=%s", _PREFIX, node, vm_name)
        try:
            vmid = _get_client(proxmox_url).find_vm_by_name(node, vm_name)
            if vmid:
                logger.debug("%s resolved vmid=%s", _PREFIX, vmid)
        except Exception as e:
            logger.warning("%s find VM by name failed: %s", _PREFIX, e)

    logger.debug("%s VM info: url=%s node=%s vmid=%s", _PREFIX, proxmox_url, node, vmid)
    return proxmox_url, node, vmid


def _resolve_vm_connection(
    vm_ref_name: str,
    namespace: str,
    patch: kopf.Patch,
    log: kopf.Logger,
) -> tuple[ProxmoxClient | None, str | None, int | None]:
    """
    Resolve Proxmox client and (node, vmid) from VM reference.
    On failure sets status to Error and returns (None, None, None).
    """
    vm_cr = _get_proxmoxvm_cr(vm_ref_name, namespace)
    if not vm_cr:
        set_status(
            patch, phase=SnapshotPhase.ERROR, message=f"ProxmoxVM CR not found: {vm_ref_name}"
        )
        return None, None, None

    proxmox_url, node, vmid = _vm_info_from_cr(vm_cr, log)
    if not proxmox_url or not node or vmid is None:
        set_status(
            patch, phase=SnapshotPhase.ERROR, message="Missing proxmoxUrl, node, or vmid from VM CR"
        )
        return None, None, None

    try:
        client = _get_client(proxmox_url)
        return client, node, vmid
    except Exception as e:
        logger.error("%s Proxmox client failed: %s", _PREFIX, e)
        set_status(patch, phase=SnapshotPhase.ERROR, message=str(e))
        return None, None, None


# ---------------------------------------------------------------------------
# Helpers: Proxmox snapshot API
# ---------------------------------------------------------------------------


def _format_datetime_for_k8s(dt: datetime) -> str:
    """RFC3339-style UTC string for Kubernetes."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.isoformat()
    if iso.endswith("+00:00"):
        return iso.replace("+00:00", "Z")
    if not iso.endswith("Z"):
        dt = dt.astimezone(timezone.utc)
        iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
        if dt.microsecond:
            iso += f".{dt.microsecond:06d}"
        iso += "Z"
    return iso


def _list_snapshots(client: ProxmoxClient, node: str, vmid: int) -> list[dict[str, Any]]:
    """List snapshots for VM from Proxmox."""
    logger.debug("%s list snapshots node=%s vmid=%s", _PREFIX, node, vmid)
    try:
        proxmox = client._get_connection()
        snapshots = proxmox.nodes(node).qemu(vmid).snapshot.get()
        if isinstance(snapshots, list):
            logger.debug("%s found %d snapshots vmid=%s", _PREFIX, len(snapshots), vmid)
            return snapshots
        logger.warning(
            "%s Proxmox snapshots not a list vmid=%s type=%s", _PREFIX, vmid, type(snapshots)
        )
        return []
    except Exception as e:
        logger.warning("%s list snapshots failed vmid=%s: %s", _PREFIX, vmid, e)
        return []


def _create_snapshot(
    client: ProxmoxClient, node: str, vmid: int, snapshot_name: str, description: str
) -> str | None:
    """Create snapshot in Proxmox. Returns task UPID or None."""
    logger.debug("%s create snapshot node=%s vmid=%s name=%s", _PREFIX, node, vmid, snapshot_name)
    try:
        proxmox = client._get_connection()
        task = (
            proxmox.nodes(node)
            .qemu(vmid)
            .snapshot.post(
                snapname=snapshot_name,
                description=description or "",
                vmstate=1,  # Always include RAM in snapshot
            )
        )
        upid = (
            task.get("UPID")
            if isinstance(task, dict)
            else (task if isinstance(task, str) else None)
        )
        if upid:
            logger.info("%s snapshot create started vmid=%s task=%s", _PREFIX, vmid, upid)
            return upid
        logger.warning("%s Proxmox returned no UPID for snapshot create vmid=%s", _PREFIX, vmid)
        return None
    except Exception as e:
        logger.error("%s create snapshot failed vmid=%s: %s", _PREFIX, vmid, e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Proxmox task status (shared by create and restore)
# ---------------------------------------------------------------------------


def _check_proxmox_task_status(
    client: ProxmoxClient,
    node: str,
    task_upid: str,
) -> tuple[bool, bool, str | None]:
    """
    Check Proxmox task status by UPID. Used for both snapshot create and snapshot restore.
    Returns (is_finished, success, exit_status).
    - is_finished: True when task is stopped or status unknown (no more polling needed).
    - success: True only if task completed with OK exit status; False if failed or unknown.
    - exit_status: Proxmox exitstatus when task is stopped, else None.
    """
    task_status = client.get_task_status(node, task_upid)
    if not task_status:
        return True, False, None
    status_str = task_status.get("status")
    exit_status = task_status.get("exitstatus")
    if status_str != "stopped":
        return False, False, None
    return True, exit_status == EXIT_STATUS_OK, exit_status


# ---------------------------------------------------------------------------
# Helpers: CR status shapes (single place for field names and structure)
# ---------------------------------------------------------------------------


def _make_restore_status(
    state: str,
    requested_snapshot_name: str,
    requested_at: str,
    started_at: str,
    *,
    completed_at: str | None = None,
    task_upid: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """
    Build status.restoreStatus dict for the CR.
    Job: single definition of restoreStatus shape so all writers stay consistent.
    """
    return {
        "state": state,
        "requestedSnapshotName": requested_snapshot_name,
        "requestedAt": requested_at,
        "startedAt": started_at,
        "completedAt": completed_at,
        "taskUpid": task_upid,
        "error": error,
    }


def _require_snapshot_name(request: dict[str, Any] | None) -> str | None:
    """
    Extract required snapshotName from a request dict (createRequest, restoreRequest, deleteRequest).
    Job: one place for "get snapshotName and treat missing/empty as invalid".
    Returns the non-empty name or None.
    """
    if not request:
        return None
    name = request.get("snapshotName")
    return name if (name and isinstance(name, str) and name.strip()) else None


# ---------------------------------------------------------------------------
# Business logic: create request, in-flight task, sync status
# ---------------------------------------------------------------------------


def _process_restore_request(
    client: ProxmoxClient,
    node: str,
    vmid: int,
    restore_request: dict[str, Any],
    patch: kopf.Patch,
) -> None:
    """Handle spec.restoreRequest: start rollback in Proxmox, store task UPID in status.restoreStatus, set state=restoring."""
    snapshot_name = _require_snapshot_name(restore_request)
    if not snapshot_name:
        logger.error("%s restoreRequest missing snapshotName", _PREFIX)
        set_status(patch, phase=SnapshotPhase.ERROR, message="Restore request missing snapshotName")
        return

    now_iso = _format_datetime_for_k8s(datetime.now(timezone.utc))
    try:
        task_upid = client.restore_snapshot(node, vmid, snapshot_name)
    except Exception as e:
        logger.error(
            "%s restore snapshot failed vmid=%s name=%s: %s", _PREFIX, vmid, snapshot_name, e
        )
        patch.status["restoreStatus"] = _make_restore_status(
            "failed",
            snapshot_name,
            now_iso,
            now_iso,
            completed_at=now_iso,
            task_upid=None,
            error=str(e),
        )
        # Backend clears spec.restoreRequest after terminal status (Liqo status-only).
        return

    if not task_upid:
        patch.status["restoreStatus"] = _make_restore_status(
            "failed",
            snapshot_name,
            now_iso,
            now_iso,
            completed_at=now_iso,
            task_upid=None,
            error="Proxmox did not return task UPID for restore",
        )
        return

    patch.status["restoreStatus"] = _make_restore_status(
        "restoring",
        snapshot_name,
        now_iso,
        now_iso,
        completed_at=None,
        task_upid=task_upid,
        error=None,
    )
    logger.info(
        "%s restore started vmid=%s snapshot=%s task=%s", _PREFIX, vmid, snapshot_name, task_upid
    )


def _check_in_flight_restore_task(
    client: ProxmoxClient,
    node: str,
    restore_status: dict[str, Any],
    patch: kopf.Patch,
) -> None:
    """If status.restoreStatus has taskUpid and state is restoring, poll task; on completion set state/lastRestore."""
    task_upid = (restore_status or {}).get("taskUpid")
    state = (restore_status or {}).get("state")
    if not task_upid or state != "restoring":
        return

    snapshot_name = (restore_status or {}).get("requestedSnapshotName", "")
    logger.debug("%s check in-flight restore task %s", _PREFIX, task_upid)
    finished, success, exit_status = _check_proxmox_task_status(client, node, task_upid)

    if not finished:
        return

    now_iso = _format_datetime_for_k8s(datetime.now(timezone.utc))
    req_at = (restore_status or {}).get("requestedAt") or now_iso
    started_at = (restore_status or {}).get("startedAt") or now_iso

    if not success:
        if exit_status is None:
            logger.warning("%s no status for restore task %s", _PREFIX, task_upid)
            err_msg = "Task status unknown"
        else:
            logger.error("%s snapshot restore failed exitstatus=%s", _PREFIX, exit_status)
            err_msg = f"Restore failed: exitstatus={exit_status}"
        patch.status["restoreStatus"] = {
            **restore_status,
            **_make_restore_status(
                "failed",
                snapshot_name,
                req_at,
                started_at,
                completed_at=now_iso,
                task_upid=None,
                error=err_msg,
            ),
        }
        return

    logger.info("%s snapshot restore task completed snapshot=%s", _PREFIX, snapshot_name)
    patch.status["restoreStatus"] = {
        **restore_status,
        **_make_restore_status(
            "completed",
            snapshot_name,
            req_at,
            started_at,
            completed_at=now_iso,
            task_upid=None,
            error=None,
        ),
    }
    patch.status["lastRestore"] = {
        "snapshotName": snapshot_name,
        "restoredAt": now_iso,
        "restoredBy": None,
    }


def _process_delete_request(
    client: ProxmoxClient,
    node: str,
    vmid: int,
    delete_request: dict[str, Any],
    patch: kopf.Patch,
) -> None:
    """Handle spec.deleteRequest: delete snapshot from Proxmox. Backend clears Spec after sync."""
    snapshot_name = _require_snapshot_name(delete_request)
    if not snapshot_name:
        logger.error("%s deleteRequest missing snapshotName", _PREFIX)
        set_status(patch, phase=SnapshotPhase.ERROR, message="Delete request missing snapshotName")
        return

    try:
        client.delete_snapshot(node, vmid, snapshot_name)
        logger.info("%s deleted snapshot %s from Proxmox vmid=%s", _PREFIX, snapshot_name, vmid)
    except Exception as e:
        logger.error(
            "%s delete snapshot failed vmid=%s name=%s: %s", _PREFIX, vmid, snapshot_name, e
        )
        set_status(patch, phase=SnapshotPhase.ERROR, message=f"Failed to delete snapshot: {e}")
        return

    # Clear stale Error from earlier failed delete attempts (e.g. after idempotent delete in client).
    set_status(patch, phase=SnapshotPhase.ACTIVE, message="")
    now_iso = _format_datetime_for_k8s(datetime.now(timezone.utc))
    patch.status["lastDelete"] = {
        "snapshotName": snapshot_name,
        "deletedAt": now_iso,
    }


def _process_create_request(
    client: ProxmoxClient,
    node: str,
    vmid: int,
    create_request: dict[str, Any],
    patch: kopf.Patch,
) -> None:
    """Handle spec.createRequest: create snapshot in Proxmox, store task UPID. Backend clears Spec."""
    snapshot_name = _require_snapshot_name(create_request)
    if not snapshot_name:
        logger.error("%s createRequest missing snapshotName", _PREFIX)
        set_status(
            patch,
            phase=SnapshotPhase.ERROR,
            message="Create request missing snapshotName (backend must set it)",
        )
        return

    description = create_request.get("description", "")
    task_upid = _create_snapshot(client, node, vmid, snapshot_name, description)
    if not task_upid:
        set_status(
            patch,
            phase=SnapshotPhase.ERROR,
            message="Proxmox did not return task UPID for snapshot create",
        )
        return

    patch.status["taskUpid"] = task_upid
    # Do not clear createRequest; Backend clears Spec after status shows the snapshot.


def _check_in_flight_task(
    client: ProxmoxClient,
    node: str,
    snapshot_status: VMSnapshotStatus,
    patch: kopf.Patch,
) -> bool:
    """If status has taskUpid, check task. On success clear taskUpid. On lock failure retry next reconcile.

    Returns True when create finished this pass (success or non-lock failure). Caller must not start
    another create in the same pass while Spec still has createRequest in memory.
    """
    if not snapshot_status.taskUpid:
        return False

    task_upid = snapshot_status.taskUpid
    logger.debug("%s check in-flight create task %s", _PREFIX, task_upid)
    finished, success, exit_status = _check_proxmox_task_status(client, node, task_upid)

    if not finished:
        return False

    patch.status["taskUpid"] = None
    if success:
        logger.info("%s snapshot create task completed", _PREFIX)
        return True

    # Task failed
    exit_str = (exit_status or "").lower()
    if "lock" in exit_str:
        logger.warning(
            "%s snapshot create failed (VM locked), will retry on next reconcile exitstatus=%s",
            _PREFIX,
            exit_status,
        )
        return False

    # Non-lock failure: mark failed; Backend clears createRequest after seeing Error.
    msg = (
        f"Task {task_upid} status unknown"
        if exit_status is None
        else f"Snapshot creation failed: exitstatus={exit_status}"
    )
    if exit_status is not None:
        logger.error("%s snapshot create failed exitstatus=%s", _PREFIX, exit_status)
    else:
        logger.warning("%s no status for create task %s", _PREFIX, task_upid)
    set_status(patch, phase=SnapshotPhase.ERROR, message=msg)
    return True


def _sync_snapshots_to_status(
    client: ProxmoxClient, node: str, vmid: int, patch: kopf.Patch, log: kopf.Logger
) -> None:
    """Set status.snapshots / snapshotCount from Proxmox and phase=Active if any snapshots.
    Builds each snapshot with consistent key order and omits createdAt when None to avoid
    patch inconsistencies (server may omit the key; sending createdAt: null can conflict).
    """
    snapshots = _list_snapshots(client, node, vmid)
    details = []
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        name = snap.get("name", "")
        snaptime = snap.get("snaptime")
        created_at: str | None = None
        if snaptime:
            created_at = _format_datetime_for_k8s(datetime.fromtimestamp(snaptime, tz=timezone.utc))
        # Consistent key order; omit createdAt when None so we don't send createdAt: null
        # (avoids "Patching failed with inconsistencies" when server stores without the key).
        item: dict[str, Any] = {"name": name, "status": "active"}
        if created_at is not None:
            item["createdAt"] = created_at
        details.append(item)
        logger.debug("%s snapshot: %s", _PREFIX, name)

    patch.status["snapshots"] = details
    patch.status["snapshotCount"] = len(details)
    if details and patch.status.get("phase") != SnapshotPhase.ERROR:
        set_status(patch, phase=SnapshotPhase.ACTIVE)
    logger.debug("%s synced %d snapshot(s) to status", _PREFIX, len(details))


# ---------------------------------------------------------------------------
# Reconcile entrypoint
# ---------------------------------------------------------------------------


@kopf.timer(
    "vm.proxmox.com",
    "v1alpha1",
    "vmsnapshots",
    interval=SNAPSHOT_RECONCILE_INTERVAL,
    idle=SNAPSHOT_RECONCILE_INTERVAL,
)
def reconcile_vmsnapshot(
    spec: dict,
    meta: dict,
    status: dict,
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """Timer-driven reconciliation for VMSnapshot. Uses fresh CR and kopf patch for status."""
    name = meta.get("name", "")
    namespace = meta.get("namespace", "default")

    cr = _get_vmsnapshot_cr(namespace, name)
    if not cr:
        set_status(patch, phase=SnapshotPhase.ERROR, message="Failed to read VMSnapshot CR")
        return

    cr_spec = cr.get("spec", {})
    cr_status = cr.get("status", {})
    logger.debug(
        "%s reconcile %s/%s createRequest=%s restoreRequest=%s deleteRequest=%s phase=%s taskUpid=%s",
        _PREFIX,
        namespace,
        name,
        "set" if cr_spec.get("createRequest") else None,
        "set" if cr_spec.get("restoreRequest") else None,
        "set" if cr_spec.get("deleteRequest") else None,
        cr_status.get("phase"),
        cr_status.get("taskUpid"),
    )
    logger.debug("%s CR body: %s", _PREFIX, json.dumps(cr, indent=2, default=str))

    try:
        snapshot_spec = VMSnapshotSpec.model_validate(cr_spec)
        snapshot_status = (
            VMSnapshotStatus.model_validate(cr_status) if cr_status else VMSnapshotStatus()
        )
    except Exception as e:
        logger.error("%s invalid CR %s/%s: %s", _PREFIX, namespace, name, e)
        set_status(patch, phase=SnapshotPhase.ERROR, message=str(e))
        return

    client, node, vmid = _resolve_vm_connection(snapshot_spec.vmRef.name, namespace, patch, logger)
    if not client or node is None or vmid is None:
        return

    try:
        restore_status_dict = cr_status.get("restoreStatus")
        if isinstance(restore_status_dict, dict):
            _check_in_flight_restore_task(client, node, restore_status_dict, patch)
        create_finished_this_pass = _check_in_flight_task(client, node, snapshot_status, patch)

        existing_snapshot_names = {
            s.get("name")
            for s in (cr_status.get("snapshots") or [])
            if isinstance(s, dict) and s.get("name")
        }

        if snapshot_spec.restoreRequest:
            req_name = snapshot_spec.restoreRequest.snapshotName
            rs = restore_status_dict if isinstance(restore_status_dict, dict) else {}
            already_done = (
                rs.get("state") in (RESTORE_STATE_COMPLETED, RESTORE_STATE_FAILED)
                and rs.get("requestedSnapshotName") == req_name
            )
            in_flight = rs.get("state") == RESTORE_STATE_RESTORING and rs.get("taskUpid")
            if already_done:
                logger.debug(
                    "%s restore already %s for %s, waiting for Spec clear",
                    _PREFIX,
                    rs.get("state"),
                    req_name,
                )
            elif not in_flight:
                _process_restore_request(
                    client,
                    node,
                    vmid,
                    snapshot_spec.restoreRequest.model_dump(),
                    patch,
                )

        if snapshot_spec.deleteRequest:
            del_name = snapshot_spec.deleteRequest.snapshotName
            last_delete = cr_status.get("lastDelete")
            last_delete_name = (
                last_delete.get("snapshotName") if isinstance(last_delete, dict) else None
            )
            if del_name and last_delete_name == del_name:
                logger.debug(
                    "%s deleteRequest %s already handled (lastDelete), waiting for Spec clear",
                    _PREFIX,
                    del_name,
                )
            elif del_name and del_name not in existing_snapshot_names:
                # Already gone from Proxmox (synced); Backend clears Spec.
                if cr_status.get("phase") == SnapshotPhase.ERROR:
                    set_status(patch, phase=SnapshotPhase.ACTIVE, message="")
                logger.debug(
                    "%s deleteRequest %s already absent, waiting for Spec clear",
                    _PREFIX,
                    del_name,
                )
            else:
                _process_delete_request(
                    client,
                    node,
                    vmid,
                    snapshot_spec.deleteRequest.model_dump(),
                    patch,
                )

        # Only start a new create when there is no task in flight and snapshot not already present.
        create_name = (
            snapshot_spec.createRequest.snapshotName if snapshot_spec.createRequest else None
        )
        if (
            snapshot_spec.createRequest
            and not create_finished_this_pass
            and create_name
            and create_name not in existing_snapshot_names
            and (not snapshot_status.taskUpid or "taskUpid" in patch.status)
        ):
            _process_create_request(
                client,
                node,
                vmid,
                snapshot_spec.createRequest.model_dump(),
                patch,
            )
        elif (
            snapshot_spec.createRequest
            and create_name
            and create_name in existing_snapshot_names
            and not snapshot_status.taskUpid
        ):
            logger.debug(
                "%s createRequest %s already in status.snapshots, waiting for Spec clear",
                _PREFIX,
                create_name,
            )

        _sync_snapshots_to_status(client, node, vmid, patch, logger)
        logger.debug("%s reconcile done %s/%s", _PREFIX, namespace, name)
    except Exception as e:
        logger.error("%s reconcile failed %s/%s: %s", _PREFIX, namespace, name, e, exc_info=True)
        set_status(patch, phase=SnapshotPhase.ERROR, message=str(e))
