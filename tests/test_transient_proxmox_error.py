"""Tests for transient vs permanent ProxmoxError classification in handlers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import kopf
import pytest

from proxmox_operator.client import ProxmoxError
from proxmox_operator.constants import (
    OP_CREATED,
    PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS,
)
from proxmox_operator.handlers import _handle_operation, _is_transient_proxmox_error
from proxmox_operator.types import (
    CPUConfig,
    DiskConfig,
    NetworkConfig,
    Operation,
    ProxmoxNodeRef,
    ProxmoxVMSpec,
    VMConfig,
)


@pytest.fixture()
def create_spec() -> ProxmoxVMSpec:
    return ProxmoxVMSpec(
        proxmoxNodeRef=ProxmoxNodeRef(name="node-1"),
        operation=Operation(type="created"),
        vmConfig=VMConfig(
            templateId="14002",
            name="NR-44d3f84e",
            nodeName="proxmox01-lab01-dev",
            cpu=CPUConfig(cores=1),
            memory="1G",
            disks=[DiskConfig(name="rootdisk", storage="local-lvm", size="20G")],
            networks=[NetworkConfig(name="net0", bridge="vmbr0")],
        ),
    )


@pytest.mark.parametrize(
    "message",
    [
        "Failed to find VM by name 'NR-x' on node pve: SSL EOF",
        "Failed to connect to Proxmox: timeout",
        "HTTPSConnectionPool: Max retries exceeded",
        ("Connection aborted.", "Remote end closed connection without response"),
    ],
)
def test_is_transient_proxmox_error_true(message: str | tuple[str, ...]) -> None:
    text = message if isinstance(message, str) else " ".join(message)
    assert _is_transient_proxmox_error(ProxmoxError(text)) is True


@pytest.mark.parametrize(
    "message",
    [
        "Failed to clone template 14002: template does not exist",
        "No block disk found on VM 11001 for resize",
        "Task UPID:… failed: error",
    ],
)
def test_is_transient_proxmox_error_false(message: str) -> None:
    assert _is_transient_proxmox_error(ProxmoxError(message)) is False


def test_temporary_error_delay_constant() -> None:
    err = kopf.TemporaryError("retry", delay=PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS)
    assert err.delay == PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS


def test_handle_operation_retries_transient_find_error(create_spec: ProxmoxVMSpec) -> None:
    client = MagicMock()
    client.find_vm_by_name.side_effect = ProxmoxError(
        "Failed to find VM by name 'NR-44d3f84e' on node proxmox01-lab01-dev: SSL EOF"
    )
    patch_obj: Any = MagicMock()

    with (
        patch("proxmox_operator.handlers.check_maintenance_mode", return_value=False),
        patch("proxmox_operator.handlers._client_for_vm", return_value=client),
        patch("proxmox_operator.handlers.should_skip_operation", return_value=False),
        patch(
            "proxmox_operator.handlers.get_operation_handler",
            return_value=MagicMock(
                execute=MagicMock(
                    side_effect=ProxmoxError(
                        "Failed to find VM by name 'NR-44d3f84e' on node "
                        "proxmox01-lab01-dev: SSL EOF"
                    )
                )
            ),
        ),
        pytest.raises(kopf.TemporaryError, match="Failed to created VM") as exc_info,
    ):
        _handle_operation(
            create_spec,
            {"generation": 1},
            {},
            patch_obj,
            MagicMock(),
            OP_CREATED,
        )

    assert exc_info.value.delay == PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS


def test_handle_operation_permanent_on_clone_error(create_spec: ProxmoxVMSpec) -> None:
    client = MagicMock()

    with (
        patch("proxmox_operator.handlers.check_maintenance_mode", return_value=False),
        patch("proxmox_operator.handlers._client_for_vm", return_value=client),
        patch("proxmox_operator.handlers.should_skip_operation", return_value=False),
        patch(
            "proxmox_operator.handlers.get_operation_handler",
            return_value=MagicMock(
                execute=MagicMock(
                    side_effect=ProxmoxError(
                        "Failed to clone template 14002: template does not exist"
                    )
                )
            ),
        ),
        pytest.raises(kopf.PermanentError, match="Failed to created VM"),
    ):
        _handle_operation(
            create_spec,
            {"generation": 1},
            {},
            MagicMock(),
            MagicMock(),
            OP_CREATED,
        )
