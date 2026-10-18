"""SolusVM restart is an action, not a desired state (same rule as VirtFusion)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch as mock_patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from solusvm_operator.handlers.vm import _handle_operation, should_skip_generation_scoped_op
from solusvm_operator.types import SolusVMVMSpec

VM_CR = "solusvmvm-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _KopfPatch:
    def __init__(self) -> None:
        self.status: dict = {}


def _spec(**overrides):
    base = {
        "solusvmNodeRef": {"name": "node-1"},
        "planId": 1,
        "osImageVersionId": 27,
        "locationId": 1,
        "computeResourceId": 1,
        "userEmail": "org-uuid@users.noderings.invalid",
        "hostname": "guest.noderings.internal",
        "name": "NR-vm-uuid",
        "operation": {"type": "restarted"},
    }
    base.update(overrides)
    return SolusVMVMSpec.model_validate(base)


def test_vm_spec_ipv6_defaults_false_and_accepts_true():
    assert _spec().ipv6 is False
    assert _spec(ipv6=True).ipv6 is True


def _run(spec, status, op, generation=1, node_spec=None):
    status_patch = _KopfPatch()
    meta = {"name": VM_CR, "namespace": "svm-sim", "generation": generation}
    node = {
        "spec": node_spec
        or {"computeResourceId": 1, "locationId": 1, "controlRef": {"name": "control-1"}},
    }
    client = MagicMock()
    with (
        mock_patch("solusvm_operator.handlers.vm._build_client", return_value=(client, node)),
        mock_patch("solusvm_operator.handlers.vm.restart_server") as restart,
        mock_patch("solusvm_operator.handlers.vm.start_server") as start,
        mock_patch("solusvm_operator.handlers.vm.stop_server") as stop,
    ):
        _handle_operation(spec, meta, status, status_patch, MagicMock(), op)
    return status_patch, restart, start, stop


def test_should_skip_generation_scoped_op_matches_proxmox_restart_rule():
    meta = {"generation": 5}
    assert should_skip_generation_scoped_op(
        "restarted", {"phase": "Ready", "observedGeneration": 5}, meta
    )
    assert should_skip_generation_scoped_op(
        "restarted", {"phase": "Pending", "observedGeneration": 5}, meta
    )
    assert not should_skip_generation_scoped_op(
        "restarted", {"phase": "Ready", "observedGeneration": 4}, meta
    )
    assert not should_skip_generation_scoped_op(
        "created", {"phase": "Ready", "observedGeneration": 5}, meta
    )
    assert not should_skip_generation_scoped_op(
        "restarted",
        {"phase": "Ready", "observedGeneration": 8, "observedRequestedAt": "old"},
        meta,
        requested_at="new",
    )
    assert should_skip_generation_scoped_op(
        "restarted",
        {"phase": "Ready", "observedGeneration": 8, "observedRequestedAt": "same"},
        meta,
        requested_at="same",
    )


def test_restarted_runs_when_generation_differs():
    patch, restart, _, _ = _run(
        _spec(operation={"type": "restarted"}),
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM created",
            "observedGeneration": 1,
        },
        "restarted",
        generation=2,
    )
    restart.assert_called_once()
    assert patch.status.get("observedGeneration") == 2
    assert patch.status.get("message") == "VM restarted"


def test_restarted_skipped_when_same_generation_ready():
    _, restart, _, _ = _run(
        _spec(operation={"type": "restarted"}),
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM restarted",
            "observedGeneration": 4,
        },
        "restarted",
        generation=4,
    )
    restart.assert_not_called()


def test_restarted_runs_when_requested_at_differs_same_generation():
    patch, restart, _, _ = _run(
        _spec(operation={"type": "restarted", "requestedAt": "2026-08-29T16:00:00Z"}),
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM restarted",
            "observedGeneration": 8,
            "observedRequestedAt": "2026-08-29T15:00:00Z",
        },
        "restarted",
        generation=8,
    )
    restart.assert_called_once()
    assert patch.status.get("observedRequestedAt") == "2026-08-29T16:00:00Z"


def test_restarted_runs_when_requested_at_set_and_unobserved():
    """UI Restart while spec.type is already restarted: generation does not bump."""
    patch, restart, _, _ = _run(
        _spec(operation={"type": "restarted", "requestedAt": "2026-08-29T16:10:00Z"}),
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM restarted",
            "observedGeneration": 8,
        },
        "restarted",
        generation=8,
    )
    restart.assert_called_once()
    assert patch.status.get("observedRequestedAt") == "2026-08-29T16:10:00Z"


def test_restarted_skipped_when_same_requested_at():
    _, restart, _, _ = _run(
        _spec(operation={"type": "restarted", "requestedAt": "2026-08-29T16:00:00Z"}),
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM restarted",
            "observedGeneration": 8,
            "observedRequestedAt": "2026-08-29T16:00:00Z",
        },
        "restarted",
        generation=8,
    )
    restart.assert_not_called()


def test_started_skipped_when_same_generation_ready():
    _, _, start, _ = _run(
        _spec(operation={"type": "started"}),
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM started",
            "observedGeneration": 3,
        },
        "started",
        generation=3,
    )
    start.assert_not_called()
