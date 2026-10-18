"""Tests for ProxmoxClient name/VMID lookup error handling.

Transport/API failures must raise ProxmoxError (not return None/False), so create
idempotency never clones a duplicate when Proxmox is briefly unreachable.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from proxmox_operator.client import ProxmoxClient, ProxmoxError
from proxmox_operator.config import ProxmoxSettings


@pytest.fixture()
def client() -> ProxmoxClient:
    return ProxmoxClient(
        ProxmoxSettings(
            host="test.example.com:8006",
            username="testuser",
            token_name="test-token",
            token_value="test-value",
            verify_ssl=False,
            timeout=30,
        )
    )


def _patch_qemu_list(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch, vms: list[dict] | Exception
) -> None:
    qemu = MagicMock()
    if isinstance(vms, Exception):
        qemu.get.side_effect = vms
    else:
        qemu.get.return_value = vms
    proxmox = MagicMock()
    proxmox.nodes.return_value.qemu = qemu
    monkeypatch.setattr(client, "_get_connection", lambda: proxmox)


def test_find_vm_by_name_returns_vmid_when_found(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_qemu_list(
        client,
        monkeypatch,
        [
            {"vmid": 100, "name": "other"},
            {"vmid": 11001, "name": "NR-target"},
        ],
    )
    assert client.find_vm_by_name("proxmox01", "NR-target") == 11001


def test_find_vm_by_name_returns_none_when_absent(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_qemu_list(client, monkeypatch, [{"vmid": 100, "name": "other"}])
    assert client.find_vm_by_name("proxmox01", "NR-missing") is None


def test_find_vm_by_name_raises_on_transport_error(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_qemu_list(client, monkeypatch, ConnectionError("SSL: UNEXPECTED_EOF_WHILE_READING"))
    with pytest.raises(ProxmoxError, match="Failed to find VM by name"):
        client.find_vm_by_name("proxmox01", "NR-target")


def test_find_vm_by_name_propagates_proxmox_error_from_connection(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        client,
        "_get_connection",
        MagicMock(side_effect=ProxmoxError("Failed to connect to Proxmox: timeout")),
    )
    with pytest.raises(ProxmoxError, match="Failed to connect to Proxmox"):
        client.find_vm_by_name("proxmox01", "NR-target")


def test_find_vm_by_name_raises_on_invalid_list_entry(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_qemu_list(client, monkeypatch, [{"name": "NR-target"}])  # missing vmid
    with pytest.raises(ProxmoxError, match="Invalid VM list entry"):
        client.find_vm_by_name("proxmox01", "NR-target")


def test_vm_exists_raises_on_transport_error(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_qemu_list(client, monkeypatch, ConnectionError("connection aborted"))
    with pytest.raises(ProxmoxError, match="Failed to check if VM"):
        client.vm_exists("proxmox01", 11001)


def test_vm_exists_true_when_present(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_qemu_list(client, monkeypatch, [{"vmid": 11001, "name": "NR-target"}])
    assert client.vm_exists("proxmox01", 11001) is True


def test_get_vm_name_raises_on_transport_error(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    qemu_vm = MagicMock()
    qemu_vm.config.get.side_effect = ConnectionError("SSL EOF")
    proxmox = MagicMock()
    proxmox.nodes.return_value.qemu.return_value = qemu_vm
    monkeypatch.setattr(client, "_get_connection", lambda: proxmox)

    with pytest.raises(ProxmoxError, match="Failed to get VM .* name"):
        client.get_vm_name("proxmox01", 11001)


def test_get_vm_name_returns_none_when_absent(
    client: ProxmoxClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    qemu_vm = MagicMock()
    qemu_vm.config.get.side_effect = Exception("Configuration file does not exist")
    proxmox = MagicMock()
    proxmox.nodes.return_value.qemu.return_value = qemu_vm
    monkeypatch.setattr(client, "_get_connection", lambda: proxmox)

    assert client.get_vm_name("proxmox01", 11001) is None
