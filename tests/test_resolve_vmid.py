"""Tests for ProxmoxVM VMID resolution (status-first for Liqo status mirroring)."""

from proxmox_operator.operations import _parse_vmid_raw
from proxmox_operator.utils import resolve_vmid_str_from_vm_cr


def test_resolve_prefers_status_vmId() -> None:
    cr = {"status": {"vmId": "100"}, "spec": {"vmConfig": {"vmId": "99"}}}
    assert resolve_vmid_str_from_vm_cr(cr) == "100"


def test_resolve_legacy_status_vmid() -> None:
    cr = {"status": {"vmid": "101"}, "spec": {"vmConfig": {"vmId": "99"}}}
    assert resolve_vmid_str_from_vm_cr(cr) == "101"


def test_resolve_falls_back_to_spec_vmId() -> None:
    cr = {"status": {}, "spec": {"vmConfig": {"vmId": "102"}}}
    assert resolve_vmid_str_from_vm_cr(cr) == "102"


def test_resolve_strips_whitespace() -> None:
    cr = {"status": {"vmId": "  103  "}, "spec": {"vmConfig": {}}}
    assert resolve_vmid_str_from_vm_cr(cr) == "103"


def test_resolve_empty_returns_none() -> None:
    assert resolve_vmid_str_from_vm_cr({}) is None
    assert resolve_vmid_str_from_vm_cr({"status": {}, "spec": {"vmConfig": {}}}) is None
    assert (
        resolve_vmid_str_from_vm_cr({"status": {"vmId": ""}, "spec": {"vmConfig": {"vmId": ""}}})
        is None
    )


def test_parse_vmid_raw() -> None:
    assert _parse_vmid_raw("100") == 100
    assert _parse_vmid_raw(100) == 100
    assert _parse_vmid_raw(None) is None
    assert _parse_vmid_raw("") is None
    assert _parse_vmid_raw("0") is None
    assert _parse_vmid_raw(-1) is None
    assert _parse_vmid_raw("not-a-number") is None
