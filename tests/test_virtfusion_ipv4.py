"""VirtFusion IPv4 desired-state helpers (no HTTP)."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from virtfusion_operator.errors import VirtFusionHTTPError
from virtfusion_operator.servers import (
    _extract_queue_id,
    dns_servers_from_server,
    ipv4_addresses_from_server,
    ipv4_from_qty_result,
    ipv4_gateway_from_server,
    ipv4_prefix_from_server,
    ipv6_address_from_server,
    ipv6_prefix_from_server,
    network_status_from_server,
    server_is_built,
    wait_until_server_built,
)


def test_ipv4_from_top_level_list():
    got = ipv4_addresses_from_server(
        {"data": {"ipv4": [{"address": "203.0.113.10"}, "203.0.113.11"]}}
    )
    assert got == ["203.0.113.10", "203.0.113.11"]


def test_ipv4_from_network_interfaces():
    got = ipv4_addresses_from_server(
        {
            "data": {
                "network": {
                    "interfaces": [
                        {"ipv4": [{"address": "198.51.100.8"}]},
                    ]
                }
            }
        }
    )
    assert got == ["198.51.100.8"]


def test_ipv4_dedupes_and_skips_empty():
    got = ipv4_addresses_from_server({"data": {"ipv4": ["10.0.0.1", "10.0.0.1", {"address": ""}]}})
    assert got == ["10.0.0.1"]


def test_ipv4_empty_payload():
    assert ipv4_addresses_from_server(None) == []
    assert ipv4_addresses_from_server({}) == []


def test_server_is_built_from_state_and_timestamp():
    assert server_is_built({"data": {"state": "complete"}})
    assert server_is_built({"data": {"built": "2026-01-01T00:00:00Z"}})
    assert not server_is_built({"data": {"state": "allocated", "built": None}})
    assert not server_is_built(None)


class _SeqClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.i = 0

    def get_server(self, _server_id: int) -> dict:
        if not self._responses:
            raise AssertionError("unexpected get_server")
        item = self._responses[min(self.i, len(self._responses) - 1)]
        self.i += 1
        return item


def test_wait_until_server_built_stamp_change_without_qemu():
    """Fake / older panels omit qemuAgent; a new built stamp is enough."""
    client = _SeqClient(
        [
            {"data": {"built": "2026-01-01T00:00:00Z"}},
            {"data": {"built": "2026-09-07T00:14:04+00:00"}},
        ]
    )
    with patch("virtfusion_operator.servers.time.sleep", return_value=None):
        wait_until_server_built(
            client, 54, timeout=5, interval=0, previous_built="2026-01-01T00:00:00Z"
        )


def test_wait_until_server_built_ignores_stale_qemu_os():
    """VF flips built at rebuild start; leftover Ubuntu qemuAgent is not ready."""
    old = "2026-09-06T23:00:00+00:00"
    started = "2026-09-07T00:14:04+00:00"
    client = _SeqClient(
        [
            {
                "data": {
                    "built": started,
                    "rebuild": True,
                    "qemuAgent": {"os": {"dist": "ubuntu", "name": "Ubuntu 26.04", "updated": old}},
                }
            },
            {
                "data": {
                    "built": started,
                    "rebuild": False,
                    "qemuAgent": {"os": {"dist": "ubuntu", "name": "Ubuntu 26.04", "updated": old}},
                }
            },
            {
                "data": {
                    "built": started,
                    "rebuild": False,
                    "qemuAgent": {
                        "os": {
                            "dist": "rocky",
                            "name": "Rocky Linux 10.2",
                            "updated": "2026-09-07T00:15:00.000000Z",
                        }
                    },
                }
            },
        ]
    )
    with patch("virtfusion_operator.servers.time.sleep", return_value=None):
        wait_until_server_built(client, 54, timeout=5, interval=0, previous_built=old)
    assert client.i >= 3


def test_wait_until_server_built_times_out_while_rebuild_flag_set():
    client = _SeqClient([{"data": {"built": "2026-09-07T00:14:04+00:00", "rebuild": True}}])
    with patch("virtfusion_operator.servers.time.sleep", return_value=None):
        with patch("virtfusion_operator.servers.time.time", side_effect=[0, 1, 10]):
            try:
                wait_until_server_built(
                    client,
                    54,
                    timeout=5,
                    interval=0,
                    previous_built="2026-01-01T00:00:00Z",
                )
            except VirtFusionHTTPError:
                return
    raise AssertionError("expected timeout")


def test_ipv6_prefers_generated_host_over_subnet():
    """VF GET /servers/{id} nested ipv6 uses subnet + addresses (lab server 27)."""
    server = {
        "data": {
            "network": {
                "interfaces": [
                    {
                        "ipv6": [
                            {
                                "id": 1,
                                "block": {"id": 2, "name": "lab-ula-fd12"},
                                "addresses": ["fd12:3456:7890:1::a"],
                                "addressesDetailed": [
                                    {
                                        "address": "fd12:3456:7890:1::a",
                                        "rdns": None,
                                        "enabled": True,
                                    }
                                ],
                                "subnet": "fd12:3456:7890:1::",
                                "cidr": 64,
                                "gateway": "fd12:3456:7890::1",
                            }
                        ]
                    }
                ]
            }
        }
    }
    assert ipv6_address_from_server(server) == "fd12:3456:7890:1::a"
    status = network_status_from_server(server)
    assert status["ipv6"] == "fd12:3456:7890:1::a"
    assert status["ipv6_gateway"] == "fd12:3456:7890::1"
    assert status["ipv6_prefix"] == "64"


def test_ipv6_falls_back_to_subnet_when_no_host():
    server = {"data": {"ipv6": [{"subnet": "2001:db8:abcd:12::", "cidr": 64, "addresses": []}]}}
    assert ipv6_address_from_server(server) == "2001:db8:abcd:12::"


def test_network_status_from_ipv4_objects():
    server = {
        "data": {
            "ipv4": [
                {
                    "address": "203.0.113.10",
                    "gateway": "203.0.113.1",
                    "cidr": 24,
                    "dns1": "1.1.1.1",
                    "dns2": "8.8.8.8",
                }
            ],
            "ipv6": [{"address": "2001:db8::10", "gateway": "2001:db8::1"}],
        }
    }
    assert ipv4_gateway_from_server(server) == "203.0.113.1"
    assert ipv6_address_from_server(server) == "2001:db8::10"
    assert dns_servers_from_server(server) == ["1.1.1.1", "8.8.8.8"]
    status = network_status_from_server(server)
    assert status["ipv4"] == ["203.0.113.10"]
    assert status["ipv6"] == "2001:db8::10"
    assert status["ipv4_gateway"] == "203.0.113.1"
    assert status["ipv4_prefix"] == "24"
    assert status["dns_servers"] == ["1.1.1.1", "8.8.8.8"]
    assert ipv4_prefix_from_server(server) == "24"
    assert ipv6_prefix_from_server({"data": {"ipv6": [{"cidr": 64}]}}) == "64"


def test_ipv4_from_qty_result_list_and_objects():
    assert ipv4_from_qty_result({"data": ["192.168.1.120", "192.168.1.122"]}) == [
        "192.168.1.120",
        "192.168.1.122",
    ]
    assert ipv4_from_qty_result({"data": [{"address": "192.168.1.120"}]}) == ["192.168.1.120"]
    assert ipv4_from_qty_result(None) == []


def test_extract_queue_id_ignores_list_data():
    assert _extract_queue_id({"data": ["192.168.1.120"]}) is None
    assert _extract_queue_id({"data": {"queueId": 42}}) == 42
