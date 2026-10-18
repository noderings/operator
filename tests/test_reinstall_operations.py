"""Tests for VM reinstall idempotency and handler wiring."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from proxmox_operator.constants import (
    OP_REINSTALLED,
    PHASE_DELETING,
    PHASE_PROVISIONING,
    PHASE_READY,
)
from proxmox_operator.operations import ReinstallOperationHandler, get_operation_handler
from proxmox_operator.utils import should_skip_operation


@pytest.fixture
def reinstall_spec():
    return SimpleNamespace(
        vmConfig=SimpleNamespace(name="NR-test-vm", nodeName="pve01"),
        operation=SimpleNamespace(force=False),
    )


class TestShouldSkipReinstallOperation:
    def test_skips_when_reinstall_deleting_same_generation(self, reinstall_spec):
        client = MagicMock()
        logger = MagicMock()
        status = {"observedGeneration": 3, "phase": PHASE_DELETING}
        meta = {"generation": 3}

        assert should_skip_operation(
            client,
            reinstall_spec,
            OP_REINSTALLED,
            logger,
            status=status,
            meta=meta,
        )

    def test_skips_when_reinstall_provisioning_same_generation(self, reinstall_spec):
        client = MagicMock()
        logger = MagicMock()
        status = {"observedGeneration": 2, "phase": PHASE_PROVISIONING}
        meta = {"generation": 2}

        assert should_skip_operation(
            client,
            reinstall_spec,
            OP_REINSTALLED,
            logger,
            status=status,
            meta=meta,
        )

    def test_skips_when_reinstall_ready_and_vm_exists(self, reinstall_spec):
        client = MagicMock()
        client.find_vm_by_name.return_value = 1200
        logger = MagicMock()
        status = {"observedGeneration": 5, "phase": PHASE_READY}
        meta = {"generation": 5}

        assert should_skip_operation(
            client,
            reinstall_spec,
            OP_REINSTALLED,
            logger,
            status=status,
            meta=meta,
        )

    def test_does_not_skip_when_generation_differs(self, reinstall_spec):
        client = MagicMock()
        logger = MagicMock()
        status = {"observedGeneration": 1, "phase": PHASE_DELETING}
        meta = {"generation": 2}

        assert not should_skip_operation(
            client,
            reinstall_spec,
            OP_REINSTALLED,
            logger,
            status=status,
            meta=meta,
        )


class TestReinstallOperationHandler:
    def test_get_operation_handler_returns_reinstall_handler(self, reinstall_spec):
        handler = get_operation_handler(
            OP_REINSTALLED,
            MagicMock(),
            reinstall_spec,
            MagicMock(),
            {"generation": 1},
            MagicMock(),
        )
        assert isinstance(handler, ReinstallOperationHandler)

    def test_execute_deletes_then_clones(self, reinstall_spec):
        client = MagicMock()
        client.find_vm_by_name.return_value = 1100
        status_patch = MagicMock()
        logger = MagicMock()
        handler = ReinstallOperationHandler(
            client=client,
            spec=reinstall_spec,
            patch=status_patch,
            meta={"generation": 1},
            logger=logger,
        )

        with patch.object(
            handler, "_clone_and_configure_vm", return_value=("cloned", 1200)
        ) as clone:
            message, vmid = handler.execute()

        client.delete_vm.assert_called_once_with("pve01", 1100, force=False)
        clone.assert_called_once_with(
            skip_if_exists=False, provisioning_message="Cloning new template"
        )
        assert vmid == 1200
        assert "reinstalled" in message.lower()
