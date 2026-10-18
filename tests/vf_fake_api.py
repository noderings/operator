"""In-process fake VirtFusion Admin API (no real control panel).

Also runnable on Multipass / a sidecar VM:

    python3 vf_fake_api.py --bind 0.0.0.0 --port 8080 --token sim-token
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse


class FakeVirtFusionState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Any]] = []
        self.users: dict[str, dict[str, Any]] = {}
        self.next_user_id = 10
        self.servers: dict[int, dict[str, Any]] = {}
        self.next_server_id = 100
        self.ssh_keys: dict[int, list[dict[str, Any]]] = {}
        self.next_key_id = 50
        self.queue_finished = True
        self.fail_queue_ids: set[int] = set()
        self.fail_queue_job = "App\\Jobs\\Server\\KVM\\Boot"
        self.token = "sim-token"
        self.next_user_task_id = 9
        self.user_tasks: dict[int, dict[str, Any]] = {}
        self.firewall_rulesets: dict[int, dict[str, Any]] = {
            1: {"id": 1, "name": "asd", "enabled": True},
            2: {"id": 2, "name": "Block SMTP", "enabled": True},
        }
        self.server_firewall: dict[int, dict[str, Any]] = {}


def _firewall_rules_for(state: FakeVirtFusionState, ids: list[int]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    rid = 1
    for set_id in ids:
        rs = state.firewall_rulesets.get(int(set_id))
        if not rs:
            continue
        name = str(rs.get("name") or "")
        if "smtp" in name.lower():
            for port in (25, 465, 587):
                rules.append(
                    {
                        "id": rid,
                        "rulesetId": int(set_id),
                        "name": name,
                        "port_src_start": 0,
                        "port_src_end": 0,
                        "port_dst_start": port,
                        "port_dst_end": port,
                        "protocol": 1,
                        "direction": 3,
                        "action": 1,
                        "ip_version": 4,
                        "priority": 100,
                        "enabled": 1,
                    }
                )
                rid += 1
        else:
            rules.append(
                {
                    "id": rid,
                    "rulesetId": int(set_id),
                    "name": name,
                    "port_src_start": 0,
                    "port_src_end": 0,
                    "port_dst_start": 0,
                    "port_dst_end": 0,
                    "protocol": 1,
                    "direction": 1,
                    "action": 1,
                    "ip_version": 4,
                    "priority": 500,
                    "enabled": 1,
                }
            )
            rid += 1
    return rules


def _server_firewall_payload(state: FakeVirtFusionState, server_id: int) -> dict[str, Any]:
    fw = state.server_firewall.get(server_id) or {"enabled": False, "ruleset_ids": []}
    ids = [int(i) for i in fw.get("ruleset_ids") or []]
    rulesets = []
    for i in ids:
        rs = state.firewall_rulesets.get(i)
        if rs:
            rulesets.append({"id": i, "name": rs.get("name")})
    return {
        "enabled": bool(fw.get("enabled")),
        "rules": _firewall_rules_for(state, ids),
        "rulesets": rulesets,
        "rulesetIds": ids,
    }


def _simple_server(srv: dict[str, Any]) -> dict[str, Any]:
    """Cheap list row (type=simple): identity only, no libvirt remoteState."""
    return {
        "id": srv.get("id"),
        "uuid": srv.get("uuid"),
        "name": srv.get("name") or f"server-{srv.get('id')}",
        "hypervisorId": srv.get("hypervisorId") or (srv.get("hypervisor") or {}).get("id"),
        "commissioned": srv.get("commissioned", 3),
        "suspended": bool(srv.get("suspended")),
        "status": srv.get("status") or "running",
    }


def seed_exporter_inventory(state: FakeVirtFusionState) -> None:
    """NR + foreign guests so the exporter can prove prefix filtering."""
    state.servers[1] = {
        "id": 1,
        "uuid": "nr-uuid-1",
        "name": "NR-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "hypervisorId": 2,
        "commissioned": 3,
        "suspended": False,
        "status": "running",
        "ipv4": [],
        "hypervisor": {"id": 2},
    }
    state.servers[2] = {
        "id": 2,
        "uuid": "cust-uuid-2",
        "name": "customer-web-1",
        "hypervisorId": 2,
        "commissioned": 3,
        "suspended": False,
        "status": "running",
        "ipv4": [],
        "hypervisor": {"id": 2},
    }
    state.servers[3] = {
        "id": 3,
        "uuid": "nr-uuid-3",
        "name": "nr-bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        "hypervisorId": 2,
        "commissioned": 3,
        "suspended": False,
        "status": "running",
        "ipv4": [],
        "hypervisor": {"id": 2},
    }


def make_handler(state: FakeVirtFusionState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        def _auth_ok(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {state.token}"

        def _read_json(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if n == 0:
                return None
            raw = self.rfile.read(n)
            if not raw:
                return None
            return json.loads(raw.decode())

        def _send(self, code: int, body: Any = None) -> None:
            data = b""
            if body is not None:
                data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._send(401, {"message": "unauthorized"})
                return
            path = urlparse(self.path).path
            with state.lock:
                state.calls.append(("GET", path, None))

            if path == "/api/v1/connect":
                self._send(200, {"data": {"connected": True}})
                return
            if path == "/api/v1/firewall/rulesets":
                with state.lock:
                    rows = [dict(v) for _, v in sorted(state.firewall_rulesets.items())]
                self._send(
                    200,
                    {
                        "data": rows,
                        "meta": {"current_page": 1, "last_page": 1},
                        "next_page_url": None,
                    },
                )
                return
            if path == "/api/v1/compute/hypervisors":
                self._send(
                    200,
                    {
                        "data": [
                            {
                                "id": 2,
                                "name": "hv-1",
                                "group": {"id": 7},
                                "group_id": 7,
                                "ip": "10.0.0.9",
                                "maxCpu": 32,
                                "maxMemory": 65536,
                                "enabled": True,
                                "maintenance": False,
                            }
                        ],
                        "meta": {"current_page": 1, "last_page": 1},
                        "next_page_url": None,
                    },
                )
                return
            if path.startswith("/api/v1/compute/hypervisors/groups/") and path.endswith(
                "/resources"
            ):
                # Per-hypervisor allocated/max for every guest on the host (NR and not).
                self._send(
                    200,
                    {
                        "data": [
                            {
                                "hypervisor": {
                                    "id": 2,
                                    "name": "hv-1",
                                    "enabled": True,
                                    "commissioned": True,
                                },
                                "resources": {
                                    "cpuCores": {"allocated": 10, "max": 32},
                                    "memory": {
                                        "allocated": 12288,
                                        "max": 65536,
                                        "units": "MB",
                                    },
                                    "localStorage": {
                                        "allocated": 160,
                                        "max": 1000,
                                        "units": "GB",
                                    },
                                    "otherStorage": [],
                                },
                            }
                        ],
                        "meta": {"current_page": 1, "last_page": 1},
                    },
                )
                return
            if path.startswith("/api/v1/users/") and path.endswith("/byExtRelation"):
                ext = path.split("/")[4]
                with state.lock:
                    user = state.users.get(ext)
                    if user is None:
                        try:
                            user = state.users.get(str(int(ext)))
                        except ValueError:
                            user = None
                if not user:
                    self._send(404, {"message": "not found"})
                    return
                self._send(200, {"data": user})
                return
            if path.startswith("/api/v1/ssh_keys/user/"):
                uid = int(path.rsplit("/", 1)[-1])
                with state.lock:
                    keys = list(state.ssh_keys.get(uid, []))
                self._send(200, {"data": keys})
                return
            if path.startswith("/api/v1/queue/"):
                try:
                    qid = int(path.rstrip("/").rsplit("/", 1)[-1])
                except ValueError:
                    qid = -1
                if qid in state.fail_queue_ids:
                    self._send(
                        200,
                        {
                            "data": {
                                "failed": True,
                                "finished": False,
                                "job": state.fail_queue_job,
                                "errors": {"exception": {"message": "[]"}},
                            }
                        },
                    )
                    return
                self._send(
                    200,
                    {"data": {"failed": False, "finished": state.queue_finished, "progress": 100}},
                )
                return
            if path.startswith("/api/v1/servers/") and path.endswith("/vnc"):
                self._send(
                    200,
                    {"data": {"vnc": {"ip": "10.0.0.9", "port": 5903, "password": "rfb-secret"}}},
                )
                return
            if path.startswith("/api/v1/servers/") and path.endswith("/traffic"):
                self._send(
                    200,
                    {
                        "data": {
                            "monthly": [
                                {
                                    "rx": 1_000_000,
                                    "tx": 2_000_000,
                                    "total": 3_000_000,
                                    "limit": 1000,
                                }
                            ]
                        }
                    },
                )
                return
            if path == "/api/v1/servers":
                with state.lock:
                    rows = [_simple_server(s) for s in state.servers.values()]
                self._send(
                    200,
                    {
                        "data": rows,
                        "meta": {"current_page": 1, "last_page": 1},
                    },
                )
                return
            if path.startswith("/api/v1/servers/") and "/firewall/" in path:
                parts = path.strip("/").split("/")
                # api/v1/servers/{id}/firewall/{iface}
                try:
                    sid = int(parts[3])
                    iface = parts[5] if len(parts) > 5 else "primary"
                except (IndexError, ValueError):
                    self._send(404, {"message": f"unhandled GET {path}"})
                    return
                if iface not in ("primary", "secondary"):
                    self._send(404, {"message": f"unhandled GET {path}"})
                    return
                with state.lock:
                    payload = _server_firewall_payload(state, sid)
                self._send(200, {"data": payload})
                return
            if path.startswith("/api/v1/servers/"):
                tail = path.rstrip("/").rsplit("/", 1)[-1]
                try:
                    sid = int(tail)
                except ValueError:
                    self._send(404, {"message": f"unhandled GET {path}"})
                    return
                with state.lock:
                    srv = state.servers.get(sid)
                if not srv:
                    self._send(404, {"message": "server not found"})
                    return
                out = dict(srv)
                qs = urlparse(self.path).query
                if "remoteState=true" in qs or "remoteState=1" in qs:
                    out["remoteState"] = {
                        "cpuUsage": 12.5,
                        "memory": {"used": 536870912, "size": 2147483648},
                        "disk": {"read": 1000, "write": 2000},
                        "net": {"rx": 3000, "tx": 4000},
                    }
                self._send(200, {"data": out})
                return
            # User API: GET /api/server/{uuid}/task/{id}
            if path.startswith("/api/server/") and "/task/" in path:
                try:
                    tid = int(path.rstrip("/").rsplit("/", 1)[-1])
                except ValueError:
                    tid = -1
                with state.lock:
                    task = state.user_tasks.get(
                        tid, {"id": tid, "status": "complete", "completed": True}
                    )
                self._send(200, {"data": task})
                return
            if path == "/api/connect":
                self._send(200, {"data": {"connected": True}})
                return
            self._send(404, {"message": f"unhandled GET {path}"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._send(401, {"message": "unauthorized"})
                return
            path = urlparse(self.path).path
            body = self._read_json()
            with state.lock:
                state.calls.append(("POST", path, body))

            if path == "/api/v1/users":
                ext = (body or {}).get("extRelationId")
                with state.lock:
                    uid = state.next_user_id
                    state.next_user_id += 1
                    user = {"id": uid, "extRelationId": ext}
                    state.users[str(ext)] = user
                self._send(201, {"data": user})
                return
            if path == "/api/v1/ssh_keys":
                with state.lock:
                    kid = state.next_key_id
                    state.next_key_id += 1
                    rec = {
                        "id": kid,
                        "publicKey": (body or {}).get("publicKey"),
                        "userId": (body or {}).get("userId"),
                    }
                    state.ssh_keys.setdefault(int(body["userId"]), []).append(rec)
                self._send(201, {"data": rec})
                return
            if path == "/api/v1/servers":
                try:
                    ipv4_count = int((body or {}).get("ipv4", 1))
                except (TypeError, ValueError):
                    ipv4_count = 1
                if ipv4_count < 0:
                    self._send(422, {"message": "ipv4 count must be >= 0"})
                    return
                with state.lock:
                    sid = state.next_server_id
                    state.next_server_id += 1
                    ipv4_objs = [
                        {
                            "address": f"203.0.113.{10 + i}",
                            "gateway": "203.0.113.1",
                            "dns1": "1.1.1.1",
                            "dns2": "8.8.8.8",
                        }
                        for i in range(ipv4_count)
                    ]
                    new_srv: dict[str, Any] = {
                        "id": sid,
                        "uuid": f"vf-uuid-{sid}",
                        "name": (body or {}).get("name") or f"server-{sid}",
                        "hypervisorId": (body or {}).get("hypervisorId"),
                        "commissioned": 3,
                        "suspended": False,
                        "status": "running",
                        "ipv4": ipv4_objs,
                        "hypervisor": {"id": 2},
                        "network": {"interfaces": []},
                    }
                    state.servers[sid] = new_srv
                self._send(201, {"data": {**new_srv, "queueId": 1}})
                return
            if path.endswith("/ipv4Qty") and "/servers/" in path:
                iface = (body or {}).get("interface")
                if iface not in ("primary", "secondary"):
                    self._send(
                        422,
                        {
                            "message": "interface not specified or malformed. It must be primary or secondary"
                        },
                    )
                    return
                try:
                    qty = int((body or {}).get("quantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                if qty < 1:
                    self._send(422, {"message": "quantity must be a positive integer"})
                    return
                sid = int(path.split("/")[4])
                assigned: list[str] = []
                with state.lock:
                    srv = state.servers.setdefault(sid, {"id": sid, "ipv4": []})
                    existing = {
                        e["address"] if isinstance(e, dict) else e for e in srv.get("ipv4", [])
                    }
                    pool = [f"192.168.1.{i}" for i in range(120, 131)]
                    for ip in pool:
                        if ip in existing:
                            continue
                        srv.setdefault("ipv4", []).append({"address": ip})
                        assigned.append(ip)
                        existing.add(ip)
                        if len(assigned) >= qty:
                            break
                    n = 50
                    while len(assigned) < qty:
                        ip = f"203.0.113.{n}"
                        n += 1
                        if ip in existing:
                            continue
                        srv.setdefault("ipv4", []).append({"address": ip})
                        assigned.append(ip)
                        existing.add(ip)
                self._send(200, {"data": assigned})
                return
            if path.endswith("/ipv4") and "/servers/" in path:
                iface = (body or {}).get("interface")
                if iface not in ("primary", "secondary"):
                    self._send(
                        422,
                        {
                            "message": "interface not specified or malformed. It must be primary or secondary"
                        },
                    )
                    return
                sid = int(path.split("/")[4])
                ips = (body or {}).get("ip") or []
                with state.lock:
                    srv = state.servers.setdefault(sid, {"id": sid, "ipv4": []})
                    existing = {
                        e["address"] if isinstance(e, dict) else e for e in srv.get("ipv4", [])
                    }
                    for ip in ips:
                        if ip not in existing:
                            srv.setdefault("ipv4", []).append({"address": ip})
                self._send(204)
                return
            # User API: POST /api/server/{uuid}/build with cloud-init userData
            if path.startswith("/api/server/") and path.endswith("/build"):
                parts = path.strip("/").split("/")
                uuid = parts[2] if len(parts) >= 4 else ""
                ud = (body or {}).get("userData")
                if not isinstance(ud, str) or not ud.strip().startswith("#cloud-config"):
                    self._send(422, {"message": "userData must be #cloud-config"})
                    return
                if (body or {}).get("method") != "template":
                    self._send(422, {"message": "method must be template"})
                    return
                if not (body or {}).get("templateId"):
                    self._send(422, {"message": "templateId required"})
                    return
                with state.lock:
                    found = None
                    for srv in state.servers.values():
                        if srv.get("uuid") == uuid:
                            found = srv
                            break
                    if found is None:
                        self._send(404, {"message": "server not found"})
                        return
                    already_built = bool(found.get("built"))
                    if already_built and not (body or {}).get("reinstall"):
                        self._send(
                            409,
                            {"message": "server already built; pass reinstall=true"},
                        )
                        return
                    found["built"] = (
                        "2026-09-06T12:00:00Z" if already_built else "2026-01-01T00:00:00Z"
                    )
                    found["state"] = "complete"
                    found["userData"] = ud
                    tid = state.next_user_task_id
                    state.next_user_task_id += 1
                    state.user_tasks[tid] = {
                        "id": tid,
                        "status": "complete",
                        "completed": True,
                    }
                self._send(
                    200,
                    {"data": {"task": {"id": tid, "status": "complete", "completed": True}}},
                )
                return
            if path.endswith("/build"):
                # Global Admin /api/v1/servers/{id}/build — not User API /api/server/{uuid}/build
                if "/api/v1/servers/" not in path:
                    self._send(404, {"message": f"unhandled POST {path}"})
                    return
                if (body or {}).get("email") is not False:
                    self._send(422, {"message": "email must be false"})
                    return
                if "password" in (body or {}):
                    self._send(422, {"message": "must not set password on build"})
                    return
                if (body or {}).get("vnc") is not True:
                    self._send(422, {"message": "vnc must be true"})
                    return
                try:
                    sid = int(path.split("/")[4])
                except (IndexError, ValueError):
                    sid = 0
                with state.lock:
                    existing_srv = state.servers.get(sid)
                    if existing_srv is not None:
                        existing_srv["built"] = "2026-01-01T00:00:00Z"
                        existing_srv["state"] = "complete"
                        if (body or {}).get("ipv6"):
                            existing_srv["ipv6"] = [
                                {"address": "2001:db8::10", "gateway": "2001:db8::1"}
                            ]
                self._send(200, {"data": {"queueId": 2}})
                return
            if "/servers/" in path and path.endswith("/resetPassword"):
                if (body or {}).get("sendMail") is not False:
                    self._send(422, {"message": "sendMail must be false"})
                    return
                self._send(200, {"data": {"queueId": 3, "expectedPassword": "generated-pass"}})
                return
            if path.endswith("/byExtRelation/resetPassword"):
                self._send(
                    201, {"data": {"email": "user@example.invalid", "password": "panel-pass"}}
                )
                return
            if "/servers/" in path and "/firewall/" in path:
                parts = path.strip("/").split("/")
                try:
                    sid = int(parts[3])
                    action = parts[-1]
                except (IndexError, ValueError):
                    self._send(404, {"message": f"unhandled POST {path}"})
                    return
                with state.lock:
                    fw = state.server_firewall.setdefault(
                        sid, {"enabled": False, "ruleset_ids": []}
                    )
                    if action == "enable":
                        fw["enabled"] = True
                        self._send(200, [])
                        return
                    if action == "disable":
                        fw["enabled"] = False
                        self._send(200, [])
                        return
                    if action == "rules":
                        raw_ids = (body or {}).get("rulesets")
                        if not isinstance(raw_ids, list):
                            self._send(422, {"errors": ["rulesets required"]})
                            return
                        ids: list[int] = []
                        for item in raw_ids:
                            try:
                                rid = int(item)
                            except (TypeError, ValueError):
                                self._send(422, {"errors": ["invalid firewall ruleset"]})
                                return
                            if rid not in state.firewall_rulesets:
                                self._send(422, {"errors": [f"invalid firewall ruleset {rid}"]})
                                return
                            ids.append(rid)
                        fw["ruleset_ids"] = ids
                        fw["enabled"] = True if ids else fw.get("enabled", False)
                        self._send(201, [])
                        return
                self._send(404, {"message": f"unhandled POST {path}"})
                return
            if "/power/" in path:
                self._send(200, {"data": {"queueId": 4}})
                return
            if path.endswith("/vnc"):
                self._send(200, {"data": {"ok": True}})
                return
            if path.endswith("/suspend") or path.endswith("/unsuspend"):
                self._send(200, {"data": {"ok": True}})
                return
            self._send(404, {"message": f"unhandled POST {path}"})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._send(401, {"message": "unauthorized"})
                return
            path = urlparse(self.path).path
            body = self._read_json()
            with state.lock:
                state.calls.append(("DELETE", path, body))
            if path.endswith("/ipv4") and "/servers/" in path:
                sid = int(path.split("/")[4])
                remove = set((body or {}).get("ip") or [])
                with state.lock:
                    srv = state.servers.get(sid)
                    if srv and "ipv4" in srv:
                        srv["ipv4"] = [
                            e
                            for e in srv["ipv4"]
                            if (e["address"] if isinstance(e, dict) else e) not in remove
                        ]
                self._send(204)
                return
            if path.startswith("/api/v1/servers/"):
                sid = int(path.rstrip("/").rsplit("/", 1)[-1].split("?")[0])
                with state.lock:
                    if sid not in state.servers:
                        self._send(404, {"msg": "server not found"})
                        return
                    state.servers.pop(sid, None)
                self._send(204)
                return
            self._send(404, {"message": f"unhandled DELETE {path}"})

        def do_PUT(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self._send(401, {"message": "unauthorized"})
                return
            path = urlparse(self.path).path
            body = self._read_json()
            with state.lock:
                state.calls.append(("PUT", path, body))
            self._send(200, {"data": {"ok": True}})

    return Handler


class FakeVirtFusionServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = FakeVirtFusionState()
        self._bind_host = host
        self._httpd = HTTPServer((host, port), make_handler(self.state))
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host_raw, port = self._httpd.server_address[:2]
        if isinstance(host_raw, bytes):
            host = host_raw.decode()
        else:
            host = str(host_raw)
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def paths(self, method: str) -> list[str]:
        with self.state.lock:
            return [p for m, p, _ in self.state.calls if m == method]

    def bodies(self, method: str, suffix: str) -> list[Any]:
        with self.state.lock:
            return [b for m, p, b in self.state.calls if m == method and p.endswith(suffix)]

    def user_api_build_bodies(self) -> list[Any]:
        with self.state.lock:
            return [
                b
                for m, p, b in self.state.calls
                if m == "POST" and p.startswith("/api/server/") and p.endswith("/build")
            ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fake VirtFusion Admin API for operator simulation"
    )
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default="sim-token")
    args = parser.parse_args()
    srv = FakeVirtFusionServer(host=args.bind, port=args.port)
    srv.state.token = args.token
    seed_exporter_inventory(srv.state)
    srv.start()
    print(f"fake virtfusion listening on {args.bind}:{srv.port} token={args.token}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
