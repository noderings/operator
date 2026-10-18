"""SolusVM extra IPv4: succeed when GET /servers shows the address even if the task stays pending."""

from __future__ import annotations

from solusvm_operator.errors import SolusVMHTTPError
from solusvm_operator.servers import add_ips, wait_for_extra_ipv4


class _FakeSVM:
    def __init__(self, *, extra_on_get: bool, task_status: str = "pending") -> None:
        self.extra_on_get = extra_on_get
        self.task_status = task_status
        self.get_task_calls = 0
        self.add_calls = 0

    def get_server(self, server_id: int) -> dict:
        ipv4 = [{"id": 18, "ip": "192.168.1.129", "is_primary": True}]
        if self.extra_on_get:
            ipv4.append({"id": 22, "ip": "192.168.1.133", "is_primary": False})
        return {"data": {"id": server_id, "ip_addresses": {"ipv4": ipv4}}}

    def add_ips(self, server_id: int, body: dict) -> dict:
        self.add_calls += 1
        self.extra_on_get = True
        return {"data": {"id": 134, "status": "pending"}}

    def get_task(self, task_id: int) -> dict:
        self.get_task_calls += 1
        return {"data": {"id": task_id, "status": self.task_status, "output": "still going"}}


def test_wait_for_extra_ipv4_succeeds_while_task_pending():
    client = _FakeSVM(extra_on_get=True)
    before = {"192.168.1.129"}
    data = wait_for_extra_ipv4(
        client,
        511,
        before,
        task_result={"data": {"id": 134, "status": "pending"}},
        timeout=2,
        interval=0.01,
    )
    assert "192.168.1.133" in {item["ip"] for item in data["ip_addresses"]["ipv4"]}
    assert client.get_task_calls == 0


def test_wait_for_extra_ipv4_fails_when_task_fails_without_ip():
    client = _FakeSVM(extra_on_get=False, task_status="failed")
    before = {"192.168.1.129"}
    try:
        wait_for_extra_ipv4(
            client,
            511,
            before,
            task_result={"data": {"id": 134, "status": "pending"}},
            timeout=2,
            interval=0.01,
        )
        raise AssertionError("expected SolusVMHTTPError")
    except SolusVMHTTPError as e:
        assert "134" in str(e)


def test_add_ips_does_not_block_on_pending_task():
    client = _FakeSVM(extra_on_get=False)
    data = add_ips(client, 511, 1)
    assert client.add_calls == 1
    ips = {item["ip"] for item in data["ip_addresses"]["ipv4"]}
    assert ips == {"192.168.1.129", "192.168.1.133"}
    assert client.get_task_calls == 0
