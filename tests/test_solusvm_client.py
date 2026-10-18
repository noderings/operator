"""SolusVM HTTP client: Laravel pagination and 422 never retried."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from solusvm_operator.client import SolusVMClient
from solusvm_operator.errors import SolusVMHTTPError
from solusvm_operator.servers import parse_ipv4, parse_ipv4_set


class _SVMState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, dict[str, list[str]]]] = []
        self.pages_served = 0
        self.post_servers = 0


def _handler(state: _SVMState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return None

        def _send(self, code: int, body: dict[str, Any] | None = None) -> None:
            payload = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            with state.lock:
                state.calls.append(("GET", parsed.path, qs))
            if parsed.path.endswith("/compute_resources") or parsed.path.endswith(
                "/compute_resources/"
            ):
                page = int((qs.get("page") or ["1"])[0])
                with state.lock:
                    state.pages_served += 1
                items = [{"id": page, "name": f"cr-{page}"}]
                self._send(
                    200,
                    {
                        "data": items,
                        "meta": {"current_page": page, "last_page": 3},
                        # Present on purpose: operator must ignore VF-style pagination.
                        "next_page_url": "http://example.invalid/should-not-follow",
                        "links": {"next": "http://example.invalid/should-not-follow"},
                    },
                )
                return
            self._send(404, {"message": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            _ = self._read_json()
            with state.lock:
                state.calls.append(("POST", parsed.path, qs))
                state.post_servers += 1
            if parsed.path.rstrip("/").endswith("/servers"):
                self._send(
                    422,
                    {
                        "message": "The given data was invalid.",
                        "errors": {"primary_ip": ["IP is not in the compute resource block."]},
                    },
                )
                return
            self._send(404, {"message": "not found"})

    return Handler


@pytest.fixture
def svm_http():
    state = _SVMState()
    httpd = HTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}"
    yield url, state
    httpd.shutdown()
    thread.join(timeout=2)
    httpd.server_close()


def test_paginate_uses_page_until_last_page(svm_http):
    url, state = svm_http
    client = SolusVMClient(url, "token", verify_ssl=False)
    items = client.list_compute_resources()
    assert [row["id"] for row in items] == [1, 2, 3]
    paths = [c[1] for c in state.calls]
    assert all(
        "/api/v1/" in p or p.endswith("/compute_resources") or "/compute_resources" in p
        for p in paths
    )
    pages = [int(c[2]["page"][0]) for c in state.calls if "page" in c[2]]
    assert pages == [1, 2, 3]
    assert state.pages_served == 3
    # Must not chase next_page_url (would be extra requests or a different host).
    assert len(state.calls) == 3


def test_422_is_not_retried(svm_http):
    url, state = svm_http
    client = SolusVMClient(url, "token", verify_ssl=False)
    with pytest.raises(SolusVMHTTPError) as exc:
        client.create_server({"name": "NR-x", "plan": 1})
    assert exc.value.status_code == 422
    assert "primary_ip" in str(exc.value)
    assert state.post_servers == 1


def test_parse_ipv4_whmcs_shape_not_sdk_ips():
    body = {
        "data": {
            "ips": [{"ip": "198.51.100.9"}],
            "ip_addresses": {
                "ipv4": [
                    {"id": 10, "ip": "203.0.113.10", "is_primary": True},
                    {"id": 11, "ip": "203.0.113.11", "is_primary": False},
                ]
            },
        }
    }
    rows = parse_ipv4(body)
    assert [r["ip"] for r in rows] == ["203.0.113.10", "203.0.113.11"]
    assert parse_ipv4_set(body) == {"203.0.113.10", "203.0.113.11"}
    assert "198.51.100.9" not in parse_ipv4_set(body)
    from solusvm_operator.servers import parse_ipv4_ordered

    # Extra 10.0.0.2 sorts before primary 192.168.1.129 — must not become primary.
    swapped = {
        "data": {
            "ip_addresses": {
                "ipv4": [
                    {"id": 11, "ip": "10.0.0.2", "is_primary": False},
                    {"id": 10, "ip": "192.168.1.129", "is_primary": True},
                ]
            }
        }
    }
    assert parse_ipv4_ordered(swapped) == ["192.168.1.129", "10.0.0.2"]


def test_parse_ipv6_primary():
    from solusvm_operator.servers import parse_ipv6_primary

    body = {
        "data": {
            "ip_addresses": {
                "ipv4": [{"id": 10, "ip": "203.0.113.10", "is_primary": True}],
                "ipv6": {"primary_ip": "2001:db8::1"},
            }
        }
    }
    assert parse_ipv6_primary(body) == "2001:db8::1"
    assert parse_ipv6_primary({"data": {}}) == ""


def test_parse_gateway_and_dns_from_ip_block():
    from solusvm_operator.servers import (
        parse_dns_servers,
        parse_ipv4_gateway,
        parse_ipv4_prefix,
        parse_ipv6_gateway,
        parse_ipv6_prefix,
    )

    body = {
        "data": {
            "ip_addresses": {
                "ipv4": [
                    {
                        "id": 10,
                        "ip": "203.0.113.10",
                        "is_primary": True,
                        "gateway": "203.0.113.1",
                        "cidr": 24,
                    }
                ],
                "ipv6": {"primary_ip": "2001:db8::1", "gateway": "2001:db8::1", "prefix": 64},
                "ns_1": "1.1.1.1",
                "ns_2": "8.8.8.8",
            }
        }
    }
    assert parse_ipv4_gateway(body) == "203.0.113.1"
    assert parse_ipv6_gateway(body) == "2001:db8::1"
    assert parse_dns_servers(body) == ["1.1.1.1", "8.8.8.8"]
    assert parse_ipv4_prefix(body) == "24"
    assert parse_ipv6_prefix(body) == "64"
    assert (
        parse_ipv4_prefix(
            {"data": {"ip_addresses": {"ipv4": [{"ip": "10.0.0.2", "netmask": "255.255.255.0"}]}}}
        )
        == "24"
    )
