"""Tests for VNCRequest reconcile ownership (status-only; no provider TTL delete)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import kopf
import pytest
from kubernetes import client as k8s_client

from proxmox_operator import vnc_reconcile
from proxmox_operator.client import ProxmoxError
from proxmox_operator.constants import (
    PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS,
    VNC_REQUEST_TTL_DEFAULT,
)


def test_no_cleanup_expired_handler_registered() -> None:
    assert not hasattr(vnc_reconcile, "cleanup_expired_vnc_requests")


def test_reconcile_skips_when_already_ready() -> None:
    patch_obj = MagicMock()
    logger = MagicMock()

    with patch.object(vnc_reconcile, "_get_proxmox_client_from_vm_ref") as get_client:
        vnc_reconcile.reconcile_vnc_request(
            spec={
                "proxmoxVMRef": {"name": "virtualmachine-1"},
                "ttl": VNC_REQUEST_TTL_DEFAULT,
            },
            meta={"namespace": "ns-1", "name": "vnc-1"},
            status={"ready": True},
            patch=patch_obj,
            logger=logger,
        )

    get_client.assert_not_called()
    patch_obj.status.update.assert_not_called()


def test_reconcile_retries_transient_proxmox_error() -> None:
    patch_obj = MagicMock()
    logger = MagicMock()
    client = MagicMock()
    client.get_vnc_ticket.side_effect = ProxmoxError("SSL connection timed out")

    with patch.object(
        vnc_reconcile,
        "_get_proxmox_client_from_vm_ref",
        return_value=(client, "pve1", "100", "https://pve.example:8006"),
    ):
        with pytest.raises(kopf.TemporaryError) as exc_info:
            vnc_reconcile.reconcile_vnc_request(
                spec={
                    "proxmoxVMRef": {"name": "virtualmachine-1"},
                    "ttl": VNC_REQUEST_TTL_DEFAULT,
                },
                meta={"namespace": "ns-1", "name": "vnc-1"},
                status={},
                patch=patch_obj,
                logger=logger,
            )

    assert exc_info.value.delay == PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS
    patch_obj.status.update.assert_called_once()
    status_arg: dict[str, Any] = patch_obj.status.update.call_args.args[0]
    assert status_arg.get("ready") is False
    assert "SSL" in (status_arg.get("error") or "")


def test_reconcile_permanent_proxmox_error_does_not_retry() -> None:
    patch_obj = MagicMock()
    logger = MagicMock()
    client = MagicMock()
    client.get_vnc_ticket.side_effect = ProxmoxError("permission denied for vncproxy")

    with patch.object(
        vnc_reconcile,
        "_get_proxmox_client_from_vm_ref",
        return_value=(client, "pve1", "100", "https://pve.example:8006"),
    ):
        vnc_reconcile.reconcile_vnc_request(
            spec={
                "proxmoxVMRef": {"name": "virtualmachine-1"},
                "ttl": VNC_REQUEST_TTL_DEFAULT,
            },
            meta={"namespace": "ns-1", "name": "vnc-1"},
            status={},
            patch=patch_obj,
            logger=logger,
        )

    patch_obj.status.update.assert_called_once()
    status_arg: dict[str, Any] = patch_obj.status.update.call_args.args[0]
    assert status_arg.get("ready") is False


def _make_api_exception(status: int, reason: str) -> k8s_client.ApiException:
    return k8s_client.ApiException(status=status, reason=reason)


def test_get_proxmox_client_non_404_is_temporary() -> None:
    logger = MagicMock()
    custom_api = MagicMock()
    custom_api.get_namespaced_custom_object.side_effect = _make_api_exception(
        503, "Service Unavailable"
    )

    with patch.object(vnc_reconcile, "_get_k8s_client", return_value=(MagicMock(), custom_api)):
        with pytest.raises(kopf.TemporaryError, match="failed to fetch ProxmoxVM"):
            vnc_reconcile._get_proxmox_client_from_vm_ref(
                {"name": "virtualmachine-1"},
                "ns-1",
                logger,
            )


def test_get_proxmox_client_404_is_permanent() -> None:
    logger = MagicMock()
    custom_api = MagicMock()
    custom_api.get_namespaced_custom_object.side_effect = _make_api_exception(404, "Not Found")

    with patch.object(vnc_reconcile, "_get_k8s_client", return_value=(MagicMock(), custom_api)):
        with pytest.raises(kopf.PermanentError, match="not found"):
            vnc_reconcile._get_proxmox_client_from_vm_ref(
                {"name": "virtualmachine-1"},
                "ns-1",
                logger,
            )


def test_reconcile_rethrows_temporary_error_from_vm_lookup() -> None:
    patch_obj = MagicMock()
    logger = MagicMock()

    with patch.object(
        vnc_reconcile,
        "_get_proxmox_client_from_vm_ref",
        side_effect=kopf.TemporaryError("failed to fetch ProxmoxVM"),
    ):
        with pytest.raises(kopf.TemporaryError, match="failed to fetch ProxmoxVM"):
            vnc_reconcile.reconcile_vnc_request(
                spec={
                    "proxmoxVMRef": {"name": "virtualmachine-1"},
                    "ttl": VNC_REQUEST_TTL_DEFAULT,
                },
                meta={"namespace": "ns-1", "name": "vnc-1"},
                status={},
                patch=patch_obj,
                logger=logger,
            )

    patch_obj.status.update.assert_not_called()
