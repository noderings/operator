"""Tests for VM provisioning: configure_net, configure_cpu, and disk resize helpers."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from proxmox_operator.client import (
    ProxmoxClient,
    ProxmoxError,
    compute_resize_delta,
    first_block_disk_key_from_config,
    parse_disk_size_gib,
)
from proxmox_operator.config import ProxmoxSettings


@pytest.fixture
def client():
    config = ProxmoxSettings(
        host="test.example.com:8006",
        username="testuser",
        token_name="test-token",
        token_value="test-value",
        verify_ssl=True,
        timeout=30,
    )
    return ProxmoxClient(config)


def _config_post_kwargs(mock_proxmox):
    return mock_proxmox.nodes.return_value.qemu.return_value.config.post.call_args.kwargs


class TestConfigureNet:
    def test_full_net_string_with_mac_vlan_and_rate(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_net(
                "pve01",
                1200,
                net_name="net0",
                model="virtio",
                bridge="vmbr0",
                mac_address="BC:24:11:00:00:01",
                vlan_id=100,
                rate_limit_mbs=12.5,
            )
        kwargs = _config_post_kwargs(mock_proxmox)
        assert kwargs == {
            "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1,tag=100,rate=12.5"
        }

    def test_untagged_unlimited_without_mac(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_net(
                "pve01",
                1200,
                net_name="net1",
                model="e1000",
                bridge="vmbr1",
                vlan_id=0,
                rate_limit_mbs=0,
            )
        kwargs = _config_post_kwargs(mock_proxmox)
        # No MAC -> model alone (Proxmox auto-assigns); no tag=/rate= when 0
        assert kwargs == {"net1": "e1000,bridge=vmbr1,firewall=1"}

    def test_wraps_errors_as_proxmox_error(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.post.side_effect = RuntimeError(
            "boom"
        )
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with pytest.raises(ProxmoxError, match="net0"):
                client.configure_net("pve01", 1200, net_name="net0", model="virtio", bridge="vmbr0")


class TestConfigureCpu:
    def test_posts_type_limit_and_units(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_cpu("pve01", 1200, cpu_type="host", cpu_limit=0, cpu_units=1024)
        kwargs = _config_post_kwargs(mock_proxmox)
        assert kwargs == {"cpu": "host", "cpulimit": 0, "cpuunits": 1024, "numa": 1}

    def test_dedicated_omits_limit_and_units(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_cpu("pve01", 1200, cpu_type="host", affinity="0-3,64-67")
        kwargs = _config_post_kwargs(mock_proxmox)
        assert "cpulimit" not in kwargs
        assert "cpuunits" not in kwargs
        assert kwargs == {"cpu": "host", "affinity": "0-3,64-67", "numa": 1}

    def test_always_posts_numa_even_when_nothing_else_set(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_cpu("pve01", 1200)
        kwargs = _config_post_kwargs(mock_proxmox)
        assert kwargs == {"numa": 1}

    def test_posts_affinity_and_numa_for_dedicated_vm(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_cpu("pve01", 1200, cpu_type="host", affinity="0-3,64-67", numa=True)
        kwargs = _config_post_kwargs(mock_proxmox)
        assert kwargs == {"cpu": "host", "affinity": "0-3,64-67", "numa": 1}

    def test_numa_always_one_even_when_caller_passes_false(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_cpu("pve01", 1200, affinity="8-15", numa=False)
        kwargs = _config_post_kwargs(mock_proxmox)
        assert kwargs == {"affinity": "8-15", "numa": 1}

    def test_shared_vm_posts_numa_without_affinity(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_cpu("pve01", 1200, cpu_type="host", numa=True)
        kwargs = _config_post_kwargs(mock_proxmox)
        assert kwargs == {"cpu": "host", "numa": 1}

    def test_empty_affinity_treated_as_absent(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.configure_cpu("pve01", 1200, cpu_type="host", affinity="", numa=False)
        kwargs = _config_post_kwargs(mock_proxmox)
        assert kwargs == {"cpu": "host", "numa": 1}


class TestFirstBlockDiskKey:
    def test_single_scsi0(self):
        config = {"scsi0": "local-lvm:vm-100-disk-0,size=8G", "memory": "4096"}
        assert first_block_disk_key_from_config(config) == "scsi0"

    def test_skips_cloudinit_and_cdrom(self):
        config = {
            "ide2": "local:iso/cloudinit-x.iso,media=cdrom",
            "scsi1": "local-lvm:vm-100-cloudinit,media=cdrom",
            "scsi0": "local-lvm:vm-100-disk-0,size=32G",
        }
        assert first_block_disk_key_from_config(config) == "scsi0"

    def test_lexicographic_first_across_buses(self):
        config = {
            "virtio0": "local-lvm:vm-100-disk-1,size=10G",
            "sata0": "local-lvm:vm-100-disk-0,size=8G",
        }
        assert first_block_disk_key_from_config(config) == "sata0"

    def test_no_block_disk_returns_none(self):
        config = {"ide2": "local:iso/img.iso,media=cdrom", "net0": "virtio,bridge=vmbr0"}
        assert first_block_disk_key_from_config(config) is None


class TestGetVmConfig:
    def test_get_vm_config(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "scsi0": "local-lvm:vm-100-disk-0,size=32G"
        }
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            config = client.get_vm_config("pve01", 1200)
        assert config["scsi0"] == "local-lvm:vm-100-disk-0,size=32G"

    def test_disk_helpers_reuse_config_single_api_call(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "scsi0": "local-lvm:vm-100-disk-0,size=32G"
        }
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            config = client.get_vm_config("pve01", 1200)
            disk_key = client.first_block_disk_key("pve01", 1200, config=config)
            size_gib = client.get_disk_size_gib("pve01", 1200, disk_key, config=config)
        assert disk_key == "scsi0"
        assert size_gib == 32.0
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.assert_called_once()


class TestParseDiskSize:
    def test_gigabytes(self):
        assert parse_disk_size_gib("local-lvm:vm-100-disk-0,size=32G") == 32.0

    def test_megabytes(self):
        assert parse_disk_size_gib("local-lvm:vm-100-disk-0,size=512M") == 0.5

    def test_terabytes(self):
        assert parse_disk_size_gib("local-lvm:vm-100-disk-0,size=1T") == 1024.0

    def test_missing_size_returns_none(self):
        assert parse_disk_size_gib("local-lvm:vm-100-disk-0") is None


class TestComputeResizeDelta:
    def test_grow_from_8_to_40(self):
        assert compute_resize_delta(8, "40G") == "+32G"

    def test_equal_returns_none(self):
        assert compute_resize_delta(40, "40G") is None

    def test_template_larger_than_plan_returns_none(self):
        assert compute_resize_delta(50, "40G") is None

    def test_invalid_target_raises(self):
        with pytest.raises(ValueError):
            compute_resize_delta(8, "banana")


class TestResizeDisk:
    def test_resize_put_and_task_wait(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.resize.put.return_value = (
            "UPID:pve01:0001:resize"
        )
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with patch.object(client, "_wait_for_task") as wait:
                client.resize_disk("pve01", 1200, "scsi0", "+32G")
        mock_proxmox.nodes.return_value.qemu.return_value.resize.put.assert_called_once_with(
            disk="scsi0", size="+32G"
        )
        wait.assert_called_once_with("pve01", "UPID:pve01:0001:resize")

    def test_resize_put_dict_data_upid(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.resize.put.return_value = {
            "data": "UPID:pve01:0002:resize"
        }
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with patch.object(client, "_wait_for_task") as wait:
                client.resize_disk("pve01", 1200, "scsi0", "+32G")
        wait.assert_called_once_with("pve01", "UPID:pve01:0002:resize")

    def test_resize_error_wrapped(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.resize.put.side_effect = RuntimeError(
            "disk busy"
        )
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with pytest.raises(ProxmoxError, match="scsi0"):
                client.resize_disk("pve01", 1200, "scsi0", "+32G")
