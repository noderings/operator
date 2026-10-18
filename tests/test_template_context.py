"""Tests for Jinja template context built by CreateOperationHandler."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from proxmox_operator.operations import CreateOperationHandler
from proxmox_operator.types import (
    CPUConfig,
    DiskConfig,
    NetworkConfig,
    ProxmoxNodeRef,
    ProxmoxVMSpec,
    VMConfig,
)


def make_handler(networks: list[NetworkConfig]) -> CreateOperationHandler:
    spec = ProxmoxVMSpec(
        proxmoxNodeRef=ProxmoxNodeRef(name="node-1"),
        vmConfig=VMConfig(
            templateId="9000",
            name="NR-test-vm",
            nodeName="pve01",
            cpu=CPUConfig(cores=2),
            memory="4096",
            disks=[DiskConfig(name="rootdisk", storage="local-lvm", size="40G")],
            networks=networks,
        ),
    )
    return CreateOperationHandler(
        client=MagicMock(),
        spec=spec,
        patch=MagicMock(),
        meta={},
        logger=MagicMock(),
    )


class TestTemplateContextMAC:
    def test_mac_address_lowercased_in_vm_network(self):
        handler = make_handler(
            [
                NetworkConfig(
                    name="net0",
                    bridge="vmbr0",
                    macAddress="BC:24:11:00:00:2A",
                )
            ]
        )
        context = handler._build_template_context(None, None, None)
        assert context["vm_network"]["mac_address"] == "bc:24:11:00:00:2a"

    def test_no_mac_leaves_vm_network_without_key(self):
        handler = make_handler([NetworkConfig(name="net0", bridge="vmbr0")])
        context = handler._build_template_context(None, None, None)
        assert "mac_address" not in context["vm_network"]

    def test_no_networks_still_builds_context(self):
        handler = make_handler([])
        context = handler._build_template_context("host-1", ["ssh-ed25519 AAA"], None)
        assert context["vm"]["hostname"] == "host-1"
        assert context["vm_network"] == {}

    def test_password_exposed_as_hashed_passwd(self):
        handler = make_handler([])
        hashed = (
            "$6$rounds=656000$saltsalt$hashhashhashhashhashhashhashhashhashhashhashhashhashhashha"
        )
        context = handler._build_template_context("host-1", None, hashed)
        assert context["vm"]["hashed_passwd"] == hashed
        assert "password" not in context["vm"]


class TestNetworkConfigValidation:
    def test_invalid_mac_rejected(self):
        with pytest.raises(ValueError):
            NetworkConfig(name="net0", bridge="vmbr0", macAddress="not-a-mac")

    def test_vlan_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            NetworkConfig(name="net0", bridge="vmbr0", vlanId=4095)

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            NetworkConfig(name="net0", bridge="vmbr0", rateLimitMBps=-1)
