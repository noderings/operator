"""Tests for ipfilter-* IPSet reconciliation (primary + additional IPv4, IPv6 CIDRs)."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from proxmox_operator.client import ProxmoxClient
from proxmox_operator.config import ProxmoxSettings
from proxmox_operator.constants import (
    IPFILTER_IPV4_HOST_PREFIX_LEN,
    IPFILTER_IPV6_HOST_PREFIX_LEN,
)
from proxmox_operator.types import AdditionalIPv4Address, StaticIPConfig


def _host_cidr(address: str, prefix_len: int) -> str:
    return f"{address}/{prefix_len}"


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


def _vm_config_with_ips(
    primary: str = "10.0.0.5",
    additional: list[str] | None = None,
    ipv6: str | None = None,
    ipv6_prefix: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        cloudInit=SimpleNamespace(
            staticIpConfig=StaticIPConfig(
                ipv4Address=primary,
                ipv6Address=ipv6,
                ipv6Prefix=ipv6_prefix,
                additionalIpv4Addresses=[
                    AdditionalIPv4Address(address=ip) for ip in (additional or [])
                ]
                or None,
            )
        )
    )


class TestStaticIpfilterCidrs:
    def test_collects_primary_and_additional_with_prefix(self, client):
        vm_config = _vm_config_with_ips(additional=["10.0.0.6", "10.0.0.7"])
        static = vm_config.cloudInit.staticIpConfig
        cidrs = client._static_ipfilter_cidrs(static)
        assert cidrs == {
            _host_cidr("10.0.0.5", IPFILTER_IPV4_HOST_PREFIX_LEN),
            _host_cidr("10.0.0.6", IPFILTER_IPV4_HOST_PREFIX_LEN),
            _host_cidr("10.0.0.7", IPFILTER_IPV4_HOST_PREFIX_LEN),
        }

    def test_ipv4_always_uses_host_32_even_when_cr_has_subnet_prefix(self, client):
        vm_config = _vm_config_with_ips()
        static = vm_config.cloudInit.staticIpConfig
        static.ipv4Prefix = "24"
        cidrs = client._static_ipfilter_cidrs(static)
        assert cidrs == {_host_cidr("10.0.0.5", IPFILTER_IPV4_HOST_PREFIX_LEN)}

    def test_ipv6_block_uses_prefix_length(self, client):
        vm_config = _vm_config_with_ips(
            ipv6="2001:db8:abcd:0001::1",
            ipv6_prefix="64",
        )
        static = vm_config.cloudInit.staticIpConfig
        cidrs = client._static_ipfilter_cidrs(static)
        assert "2001:db8:abcd:1::/64" in cidrs
        assert "2001:db8:abcd:0001::1/64" not in cidrs

    def test_ipv6_host_mode_uses_prefix_length(self, client):
        vm_config = _vm_config_with_ips(
            ipv6="2001:db8::1",
            ipv6_prefix=str(IPFILTER_IPV6_HOST_PREFIX_LEN),
        )
        static = vm_config.cloudInit.staticIpConfig
        cidrs = client._static_ipfilter_cidrs(static)
        assert cidrs == {
            _host_cidr("10.0.0.5", IPFILTER_IPV4_HOST_PREFIX_LEN),
            _host_cidr("2001:db8::1", IPFILTER_IPV6_HOST_PREFIX_LEN),
        }

    def test_ipv6_delegated_48_uses_network_cidr(self, client):
        vm_config = _vm_config_with_ips(
            ipv6="2001:db8:abcd:1234::1",
            ipv6_prefix="48",
        )
        static = vm_config.cloudInit.staticIpConfig
        cidrs = client._static_ipfilter_cidrs(static)
        assert "2001:db8:abcd::/48" in cidrs

    def test_ipv6_defaults_to_128_when_prefix_omitted(self, client):
        vm_config = _vm_config_with_ips(ipv6="2001:db8::1", ipv6_prefix=None)
        cidrs = client._static_ipfilter_cidrs(vm_config.cloudInit.staticIpConfig)
        assert cidrs == {
            _host_cidr("10.0.0.5", IPFILTER_IPV4_HOST_PREFIX_LEN),
            _host_cidr("2001:db8::1", IPFILTER_IPV6_HOST_PREFIX_LEN),
        }


class TestReconcileIpfilterIpsets:
    def test_adds_missing_and_removes_stale_auto_entries(self, client):
        vm_config = _vm_config_with_ips(additional=["10.0.0.6"])
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "net0": "virtio,bridge=vmbr0",
        }
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.get.return_value = [
            {"name": "ipfilter-net0"},
        ]
        primary_cidr = _host_cidr("10.0.0.5", IPFILTER_IPV4_HOST_PREFIX_LEN)
        stale_cidr = _host_cidr("10.0.0.8", IPFILTER_IPV4_HOST_PREFIX_LEN)
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value.get.return_value = [
            {"cidr": primary_cidr, "comment": f"auto-{primary_cidr}"},
            {"cidr": stale_cidr, "comment": f"auto-{stale_cidr}"},
        ]

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.reconcile_ipfilter_ipsets("pve01", 1200, vm_config)

        ipset = mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value
        ipset(stale_cidr).delete.assert_called_once()
        added_cidr = _host_cidr("10.0.0.6", IPFILTER_IPV4_HOST_PREFIX_LEN)
        ipset.post.assert_called_once_with(cidr=added_cidr, comment=f"auto-{added_cidr}")

    def test_ipv6_block_cidr_in_ipset(self, client):
        vm_config = _vm_config_with_ips(
            ipv6="2001:db8:abcd:0001::1",
            ipv6_prefix="64",
        )
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "net0": "virtio,bridge=vmbr0",
        }
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.get.return_value = [
            {"name": "ipfilter-net0"},
        ]
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value.get.return_value = []

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.reconcile_ipfilter_ipsets("pve01", 1200, vm_config)

        ipset = mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value
        ipset.post.assert_any_call(
            cidr="2001:db8:abcd:1::/64",
            comment="auto-2001:db8:abcd:1::/64",
        )

    def test_treats_bare_host_ips_as_equivalent_to_host_cidrs(self, client):
        """Proxmox often lists /32 and /128 members as bare addresses — do not churn."""
        vm_config = _vm_config_with_ips(
            additional=["10.0.0.6"],
            ipv6="2001:db8:abcd:1234::1",
            ipv6_prefix=str(IPFILTER_IPV6_HOST_PREFIX_LEN),
        )
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "net0": "virtio,bridge=vmbr0",
        }
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.get.return_value = [
            {"name": "ipfilter-net0"},
        ]
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value.get.return_value = [
            {"cidr": "10.0.0.5", "comment": "auto-10.0.0.5/32"},
            {"cidr": "10.0.0.6", "comment": "auto-10.0.0.6/32"},
            {"cidr": "2001:db8:abcd:1234::1", "comment": "auto-2001:db8:abcd:1234::1/128"},
        ]

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.reconcile_ipfilter_ipsets("pve01", 1200, vm_config)

        ipset = mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value
        ipset.return_value.delete.assert_not_called()
        ipset.post.assert_not_called()

    def test_replaces_wrong_prefix_with_expected_host_cidr(self, client):
        """/31 is not /32 — replace incorrect prefix with the expected host CIDR."""
        vm_config = _vm_config_with_ips()
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "net0": "virtio,bridge=vmbr0",
        }
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.get.return_value = [
            {"name": "ipfilter-net0"},
        ]
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value.get.return_value = [
            {"cidr": "10.0.0.5/31", "comment": "auto-10.0.0.5/31"},
        ]
        expected = _host_cidr("10.0.0.5", IPFILTER_IPV4_HOST_PREFIX_LEN)

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.reconcile_ipfilter_ipsets("pve01", 1200, vm_config)

        ipset = mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value
        ipset("10.0.0.5/31").delete.assert_called_once()
        ipset.post.assert_called_once_with(cidr=expected, comment=f"auto-{expected}")

    def test_adds_missing_when_only_bare_primary_present(self, client):
        vm_config = _vm_config_with_ips(additional=["10.0.0.6"])
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "net0": "virtio,bridge=vmbr0",
        }
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.get.return_value = [
            {"name": "ipfilter-net0"},
        ]
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value.get.return_value = [
            {"cidr": "10.0.0.5", "comment": "auto-10.0.0.5"},
        ]

        added_additional = _host_cidr("10.0.0.6", IPFILTER_IPV4_HOST_PREFIX_LEN)

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.reconcile_ipfilter_ipsets("pve01", 1200, vm_config)

        ipset = mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value
        ipset("10.0.0.5").delete.assert_not_called()
        ipset.post.assert_called_once_with(
            cidr=added_additional, comment=f"auto-{added_additional}"
        )

    def test_ipv6_block_removes_stale_bare_ipv6_entry(self, client):
        vm_config = _vm_config_with_ips(
            ipv6="2001:db8:abcd:0001::1",
            ipv6_prefix="64",
        )
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "net0": "virtio,bridge=vmbr0",
        }
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.get.return_value = [
            {"name": "ipfilter-net0"},
        ]
        primary_cidr = _host_cidr("10.0.0.5", IPFILTER_IPV4_HOST_PREFIX_LEN)
        mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value.get.return_value = [
            {"cidr": primary_cidr, "comment": f"auto-{primary_cidr}"},
            {"cidr": "2001:db8:abcd:0001::1", "comment": "auto-2001:db8:abcd:0001::1"},
        ]

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.reconcile_ipfilter_ipsets("pve01", 1200, vm_config)

        ipset = mock_proxmox.nodes.return_value.qemu.return_value.firewall.ipset.return_value
        ipset("2001:db8:abcd:0001::1").delete.assert_called_once()
        ipset.post.assert_any_call(
            cidr="2001:db8:abcd:1::/64",
            comment="auto-2001:db8:abcd:1::/64",
        )

    def test_skips_when_no_static_ip_config(self, client):
        mock_proxmox = MagicMock()
        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.reconcile_ipfilter_ipsets("pve01", 1200, SimpleNamespace(cloudInit=None))
        mock_proxmox.nodes.assert_not_called()
