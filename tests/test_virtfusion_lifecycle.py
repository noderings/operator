"""VirtFusion operator lifecycle against a fake Admin API (no real VF panel).

Proves the operator talks to /api/v1 (not Proxmox), lets VirtFusion assign IPv4
on create (ipv4: 1), build without password, resetPassword sendMail=false, and that
control-plane operation 'reinstalled' is accepted.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from vf_fake_api import FakeVirtFusionServer
from virtfusion_operator.client import VirtFusionClient
from virtfusion_operator.constants import GROUP as VF_GROUP
from virtfusion_operator.handlers.control import _reconcile_control
from virtfusion_operator.handlers.node import group_resources_status
from virtfusion_operator.handlers.vm import _handle_operation, should_skip_generation_scoped_op
from virtfusion_operator.handlers.vnc import on_vnc_request
from virtfusion_operator.queue import _queue_fail_message, is_noop_power_failure
from virtfusion_operator.servers import build_server, create_server, ipv4_addresses_from_server
from virtfusion_operator.types import VirtFusionVMSpec
from proxmox_operator.types import GROUP as PVE_GROUP

VM_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VM_CR = f"virtfusionvm-{VM_UUID}"


@pytest.fixture
def vf_api():
    srv = FakeVirtFusionServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def vf_env(vf_api, monkeypatch):
    for key in list(os.environ):
        if key.startswith("VIRTFUSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VIRTFUSION_URL", vf_api.url)
    monkeypatch.setenv("VIRTFUSION_TOKEN", vf_api.state.token)
    monkeypatch.setenv("VIRTFUSION_USER_API_TOKEN", vf_api.state.token)
    monkeypatch.setenv("VIRTFUSION_USER_ID", "10")
    monkeypatch.setenv("VIRTFUSION_USER_NAME", "noderings")
    return vf_api


@pytest.fixture
def client(vf_api):
    return VirtFusionClient(vf_api.url, vf_api.state.token)


class _KopfPatch:
    def __init__(self) -> None:
        self.status: dict = {}


def _spec(**overrides):
    base = {
        "virtfusionNodeRef": {"name": "node-1"},
        "packageId": 42,
        "operatingSystemId": 8,
        "userExtRelationId": "org-uuid",
        "hostname": "guest.noderings.internal",
        "name": "NR-vm-uuid",
        "cpuCores": 2,
        "memoryMb": 2048,
        "diskGb": 40,
        "ipv4": [],
        "sshPublicKeys": ["ssh-ed25519 AAAA"],
        "ipv6": True,
        "operation": {"type": "created"},
    }
    base.update(overrides)
    return VirtFusionVMSpec.model_validate(base)


def _k8s_node_control(endpoint: str, server_id: int | None = 100):
    node = {"spec": {"hypervisorGroupId": 7, "controlRef": {"name": "control-1"}}}
    ctrl = {"spec": {"endpoint": endpoint}}
    vm = {
        "spec": {"virtfusionNodeRef": {"name": "node-1"}},
        "status": {"serverId": server_id},
    }

    def get_obj(**kwargs):
        plural = kwargs.get("plural")
        if plural == "virtfusionnodes":
            return node
        if plural == "virtfusioncontrols":
            return ctrl
        if plural == "virtfusionvms":
            return vm
        raise AssertionError(f"unexpected plural {plural}")

    api = MagicMock()
    api.get_namespaced_custom_object.side_effect = lambda **kw: get_obj(**kw)
    return api


def _core_missing_secret():
    core = MagicMock()
    core.patch_namespaced_secret.side_effect = ApiException(status=404)
    return core


def _run(spec, vf_api, status, op, core=None, generation=1, auth_secret_wait=0):
    status_patch = _KopfPatch()
    meta = {"name": VM_CR, "namespace": "vf-sim", "generation": generation}
    k8s_api = _k8s_node_control(vf_api.url, status.get("serverId"))
    if core is None:
        core = _core_missing_secret()
    with (
        patch(
            "virtfusion_operator.handlers.vm.kubernetes.client.CustomObjectsApi",
            return_value=k8s_api,
        ),
        patch("virtfusion_operator.handlers.vm.kubernetes.client.CoreV1Api", return_value=core),
        patch("virtfusion_operator.queue.QUEUE_POLL_INTERVAL", 0),
        patch("virtfusion_operator.handlers.vm.AUTH_SECRET_WAIT_SECONDS", auth_secret_wait),
    ):
        _handle_operation(spec, meta, status, status_patch, MagicMock(), op)
    return status_patch, core


def test_create_server_requests_one_auto_ipv4(client, vf_api):
    data = create_server(client, package_id=42, user_id=10, hypervisor_id=7)
    assert data["id"] == 100
    bodies = vf_api.bodies("POST", "/servers")
    assert bodies[0]["ipv4"] == 1
    assert bodies[0]["hypervisorId"] == 7
    assert bodies[0]["packageId"] == 42
    assert data["ipv4"][0]["address"] == "203.0.113.10"


def test_build_never_sends_password_or_mail(client, vf_api):
    create_server(client, package_id=1, user_id=1, hypervisor_id=7)
    build_server(client, 100, os_id=8, hostname="nr.test", name="NR-x", ssh_key_ids=[50], ipv6=True)
    body = vf_api.bodies("POST", "/build")[0]
    assert body["vnc"] is True
    assert body["email"] is False
    assert "password" not in body
    assert body["ipv6"] is True
    assert body["sshKeys"] == [50]


def test_operator_does_not_call_proxmox_paths(client, vf_api):
    client.connect()
    for _m, path, _b in vf_api.state.calls:
        assert "/pve" not in path
        assert "proxmox" not in path.lower()
        assert "8006" not in path


def test_driver_groups_are_not_mixed():
    assert VF_GROUP == "vm.virtfusion.com"
    assert PVE_GROUP == "vm.proxmox.com"
    assert VF_GROUP != PVE_GROUP


def test_created_flow_uses_group_id_and_stores_server_id(vf_env):
    patch, core = _run(_spec(), vf_env, {}, "created")

    assert patch.status.get("phase") == "Ready", patch.status
    assert patch.status.get("serverId") == 100
    posts = vf_env.paths("POST")
    assert "/api/v1/users" not in posts
    assert "/api/v1/servers" in posts
    assert not any(p.endswith("/ipv4") for p in posts)
    assert any(p.endswith("/build") for p in posts)
    assert any(p.endswith("/resetPassword") for p in posts)
    create_body = vf_env.bodies("POST", "/servers")[0]
    assert create_body["hypervisorId"] == 7  # group id, not host id
    assert create_body["userId"] == 10
    assert create_body["ipv4"] == 1
    assert patch.status.get("ipv4") == ["203.0.113.10"]
    assert patch.status.get("ipv4Gateway") == "203.0.113.1"
    assert patch.status.get("ipv6") == "2001:db8::10"
    assert patch.status.get("ipv6Gateway") == "2001:db8::1"
    assert patch.status.get("dnsServers") == ["1.1.1.1", "8.8.8.8"]
    rp = vf_env.bodies("POST", "/resetPassword")[0]
    assert rp["sendMail"] is False
    core.create_namespaced_secret.assert_called()
    secret = core.create_namespaced_secret.call_args[0][1]
    assert secret.metadata.name == f"vm-{VM_UUID}-auth"
    for _m, path, _b in vf_env.state.calls:
        assert "/pve" not in path
        assert "proxmox" not in path.lower()


def test_created_uses_user_api_userdata_when_password_secret(vf_env):
    """Create-form password: User API build with userData, never Global resetPassword."""
    core = MagicMock()
    core.read_namespaced_secret.return_value = MagicMock(
        data={"password": base64.b64encode(b"chosen-pass").decode()}
    )

    def _mark_built(*_a, **_k):
        vf_env.state.servers[100]["built"] = "2026-01-01T00:00:00Z"
        vf_env.state.servers[100]["state"] = "complete"

    with (
        patch(
            "virtfusion_operator.handlers.vm.ensure_user_api_token", return_value="user-token"
        ) as mint,
        patch(
            "virtfusion_operator.handlers.vm.build_server_with_userdata",
            side_effect=_mark_built,
        ) as ud_build,
    ):
        status_patch, _core = _run(_spec(sshPublicKeys=[]), vf_env, {}, "created", core=core)

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    mint.assert_called_once()
    ud_build.assert_called_once()
    assert ud_build.call_args.kwargs["password"] == "chosen-pass"
    assert vf_env.bodies("POST", "/build") == []
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_created_uses_user_api_userdata_when_inline_userdata(vf_env):
    """Instant App YAML (no form password): User API build, never Global resetPassword."""
    core = MagicMock()
    yaml_ud = b"#cloud-config\npackages:\n- nginx\n"
    core.read_namespaced_secret.return_value = MagicMock(
        data={"userdata": base64.b64encode(yaml_ud).decode()}
    )

    def _mark_built(*_a, **_k):
        vf_env.state.servers[100]["built"] = "2026-01-01T00:00:00Z"
        vf_env.state.servers[100]["state"] = "complete"

    with (
        patch(
            "virtfusion_operator.handlers.vm.ensure_user_api_token", return_value="user-token"
        ) as mint,
        patch(
            "virtfusion_operator.handlers.vm.build_server_with_userdata",
            side_effect=_mark_built,
        ) as ud_build,
    ):
        status_patch, _core = _run(_spec(sshPublicKeys=[]), vf_env, {}, "created", core=core)

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    mint.assert_called_once()
    ud_build.assert_called_once()
    assert ud_build.call_args.kwargs["password"] == ""
    assert ud_build.call_args.kwargs["extra_user_data"] == yaml_ud.decode()
    assert vf_env.bodies("POST", "/build") == []
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_created_uses_user_api_for_instant_app_userdata(vf_env):
    """Compiled Instant App YAML + password: User API userData, never cidata/ISO or resetPassword."""
    core = MagicMock()
    yaml_ud = b"""#cloud-config
hostname: wp.example.test
packages:
- curl
write_files:
- path: /opt/noderings/wordpress/docker-compose.yml
  content: |
    services:
      wordpress:
        image: wordpress:6.8
"""
    core.read_namespaced_secret.return_value = MagicMock(
        data={
            "password": base64.b64encode(b"chosen-pass").decode(),
            "userdata": base64.b64encode(yaml_ud).decode(),
        }
    )

    def _mark_built(*_a, **_k):
        vf_env.state.servers[100]["built"] = "2026-01-01T00:00:00Z"
        vf_env.state.servers[100]["state"] = "complete"

    with (
        patch(
            "virtfusion_operator.handlers.vm.ensure_user_api_token", return_value="user-token"
        ) as mint,
        patch(
            "virtfusion_operator.handlers.vm.build_server_with_userdata",
            side_effect=_mark_built,
        ) as ud_build,
    ):
        status_patch, _core = _run(_spec(sshPublicKeys=[]), vf_env, {}, "created", core=core)

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    mint.assert_called_once()
    ud_build.assert_called_once()
    assert ud_build.call_args.kwargs["password"] == "chosen-pass"
    extra = ud_build.call_args.kwargs["extra_user_data"]
    assert "wordpress:6.8" in extra
    assert "/opt/noderings/wordpress/docker-compose.yml" in extra
    assert "{%" not in extra
    assert vf_env.bodies("POST", "/build") == []
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_created_waits_for_late_auth_secret(vf_env):
    """Liqo may apply the VM CR before vm-{uuid}-auth; wait then User API build."""
    core = MagicMock()
    yaml_ud = b"#cloud-config\npackages:\n- curl\n"
    missing = ApiException(status=404)
    present = MagicMock(
        data={
            "password": base64.b64encode(b"chosen-pass").decode(),
            "userdata": base64.b64encode(yaml_ud).decode(),
        }
    )
    core.read_namespaced_secret.side_effect = [missing, present]

    def _mark_built(*_a, **_k):
        vf_env.state.servers[100]["built"] = "2026-01-01T00:00:00Z"
        vf_env.state.servers[100]["state"] = "complete"

    with (
        patch("virtfusion_operator.handlers.vm.ensure_user_api_token", return_value="user-token"),
        patch(
            "virtfusion_operator.handlers.vm.build_server_with_userdata",
            side_effect=_mark_built,
        ) as ud_build,
        patch("virtfusion_operator.handlers.vm.time.sleep"),
    ):
        status_patch, _core = _run(
            _spec(sshPublicKeys=[]),
            vf_env,
            {},
            "created",
            core=core,
            auth_secret_wait=5,
        )

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    ud_build.assert_called_once()
    assert ud_build.call_args.kwargs["password"] == "chosen-pass"
    assert ud_build.call_args.kwargs["extra_user_data"] == yaml_ud.decode()
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_created_posts_merged_password_and_userdata_to_user_api(vf_env):
    """End-to-end operator path against the fake panel (no mock of build_server_with_userdata).

    Auth secret has a chosen password + Instant App YAML. Operator must POST that merge as
    User API /api/server/{uuid}/build userData, never Global /build or resetPassword.
    """
    core = MagicMock()
    extra = (
        b"#cloud-config\n"
        b"packages:\n"
        b"- nginx\n"
        b"write_files:\n"
        b"- path: /etc/noderings-e2e-userdata\n"
        b"  content: |\n"
        b"    noderings-e2e-ok\n"
    )
    core.read_namespaced_secret.return_value = MagicMock(
        data={
            "password": base64.b64encode(b"chosen-pass").decode(),
            "userdata": base64.b64encode(extra).decode(),
        }
    )

    with (
        patch("virtfusion_operator.servers.QUEUE_POLL_INTERVAL", 0),
        patch("virtfusion_operator.servers.QUEUE_POLL_TIMEOUT", 5),
    ):
        status_patch, _core = _run(_spec(sshPublicKeys=[]), vf_env, {}, "created", core=core)

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    bodies = vf_env.user_api_build_bodies()
    assert len(bodies) == 1, vf_env.paths("POST")
    body = bodies[0]
    assert body["method"] == "template"
    assert body["templateId"] == 8
    ud = body["userData"]
    assert ud.startswith("#cloud-config")
    assert "nginx" in ud
    assert "/etc/noderings-e2e-userdata" in ud
    assert "noderings-e2e-ok" in ud
    assert "ssh_pwauth: true" in ud
    assert "chpasswd" in ud
    assert "$6$" in ud
    assert "{%" not in ud
    global_builds = [
        p
        for m, p, _b in vf_env.state.calls
        if m == "POST" and "/api/v1/servers/" in p and p.endswith("/build")
    ]
    assert global_builds == []
    assert vf_env.bodies("POST", "/resetPassword") == []
    user_paths = [
        p for m, p, _b in vf_env.state.calls if m == "POST" and p.startswith("/api/server/")
    ]
    assert any(p.endswith("/build") for p in user_paths)


def test_created_posts_instant_app_userdata_hostname_to_user_api(vf_env):
    """Instant App YAML hostname stays in userData; VF API hostname is not the CR FQDN."""
    core = MagicMock()
    extra = (
        b"#cloud-config\n"
        b"hostname: my-server123\n"
        b"manage_etc_hosts: true\n"
        b"write_files:\n"
        b"- path: /opt/noderings/app/ok\n"
        b"  content: |\n"
        b"    ok\n"
    )
    core.read_namespaced_secret.return_value = MagicMock(
        data={"userdata": base64.b64encode(extra).decode()}
    )

    with (
        patch("virtfusion_operator.servers.QUEUE_POLL_INTERVAL", 0),
        patch("virtfusion_operator.servers.QUEUE_POLL_TIMEOUT", 5),
    ):
        status_patch, _core = _run(_spec(sshPublicKeys=[]), vf_env, {}, "created", core=core)

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    bodies = vf_env.user_api_build_bodies()
    assert len(bodies) == 1, vf_env.paths("POST")
    body = bodies[0]
    assert "hostname" not in body
    ud = body["userData"]
    assert ud.startswith("#cloud-config")
    assert "hostname: my-server123" in ud
    assert "manage_etc_hosts: true" in ud
    assert "/opt/noderings/app/ok" in ud
    assert "/etc/noderings/nr-chpasswd" not in ud
    assert vf_env.bodies("POST", "/resetPassword") == []
    global_builds = [
        p
        for m, p, _b in vf_env.state.calls
        if m == "POST" and "/api/v1/servers/" in p and p.endswith("/build")
    ]
    assert global_builds == []


def test_created_posts_os_password_userdata_to_user_api(vf_env):
    """Ubuntu/CentOS/Alma form password: User API userData, never Global resetPassword."""
    core = MagicMock()
    core.read_namespaced_secret.return_value = MagicMock(
        data={"password": base64.b64encode(b"OsPass1!").decode()}
    )

    with (
        patch("virtfusion_operator.servers.QUEUE_POLL_INTERVAL", 0),
        patch("virtfusion_operator.servers.QUEUE_POLL_TIMEOUT", 5),
    ):
        status_patch, _core = _run(
            _spec(sshPublicKeys=[], guestAdminUser="root"),
            vf_env,
            {},
            "created",
            core=core,
        )

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    bodies = vf_env.user_api_build_bodies()
    assert len(bodies) == 1, vf_env.paths("POST")
    ud = bodies[0]["userData"]
    assert bodies[0]["hostname"] == "guest.noderings.internal"
    assert "/etc/noderings/nr-chpasswd" in ud
    assert "OsPass1!" not in ud
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_created_alma_guest_user_still_uses_user_api_password(vf_env):
    core = MagicMock()
    core.read_namespaced_secret.return_value = MagicMock(
        data={"password": base64.b64encode(b"AlmaPass1!").decode()}
    )

    with (
        patch("virtfusion_operator.servers.QUEUE_POLL_INTERVAL", 0),
        patch("virtfusion_operator.servers.QUEUE_POLL_TIMEOUT", 5),
    ):
        status_patch, _core = _run(
            _spec(sshPublicKeys=[], guestAdminUser="almalinux"),
            vf_env,
            {},
            "created",
            core=core,
        )

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    bodies = vf_env.user_api_build_bodies()
    assert len(bodies) == 1, vf_env.paths("POST")
    assert "almalinux" in bodies[0]["userData"]
    assert "AlmaPass1!" not in bodies[0]["userData"]
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_created_windows_password_uses_global_reset(vf_env):
    core = MagicMock()
    core.read_namespaced_secret.return_value = MagicMock(
        data={"password": base64.b64encode(b"WinPass1!").decode()}
    )
    status_patch, _core = _run(
        _spec(sshPublicKeys=[], guestAdminUser="Administrator"),
        vf_env,
        {},
        "created",
        core=core,
    )
    assert status_patch.status.get("phase") == "Ready", status_patch.status
    assert vf_env.user_api_build_bodies() == []
    assert vf_env.bodies("POST", "/resetPassword")


def test_created_instant_app_ssh_sends_userdata_and_ssh_keys(vf_env):
    """Instant App + SSH: compiled YAML as userData, keys on the build, no password merge."""
    core = MagicMock()
    yaml_ud = b"""#cloud-config
hostname: wp.example.test
packages:
- curl
write_files:
- path: /opt/noderings/wordpress/docker-compose.yml
  content: |
    services:
      wordpress:
        image: wordpress:6.8
"""
    core.read_namespaced_secret.return_value = MagicMock(
        data={"userdata": base64.b64encode(yaml_ud).decode()}
    )

    with (
        patch("virtfusion_operator.servers.QUEUE_POLL_INTERVAL", 0),
        patch("virtfusion_operator.servers.QUEUE_POLL_TIMEOUT", 5),
    ):
        status_patch, _core = _run(_spec(), vf_env, {}, "created", core=core)

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    bodies = vf_env.user_api_build_bodies()
    assert len(bodies) == 1, vf_env.paths("POST")
    body = bodies[0]
    assert body["hostname"] == "wp.example.test"
    assert "wordpress:6.8" in body["userData"]
    assert "/etc/noderings/nr-chpasswd" not in body["userData"]
    assert body.get("sshKeys")
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_created_is_idempotent_when_server_already_built(vf_env):
    vf_env.state.servers[100] = {
        "id": 100,
        "uuid": "vf-uuid-100",
        "ipv4": [{"address": "203.0.113.10"}],
        "hypervisor": {"id": 2},
        "state": "complete",
        "built": "2026-01-01T00:00:00+00:00",
        "network": {"interfaces": []},
    }
    patch, core = _run(_spec(), vf_env, {"serverId": 100}, "created")
    assert patch.status.get("phase") == "Ready", patch.status
    assert patch.status.get("serverId") == 100
    assert vf_env.bodies("POST", "/servers") == []
    assert vf_env.bodies("POST", "/ipv4") == []
    assert vf_env.bodies("POST", "/build") == []
    assert vf_env.bodies("POST", "/resetPassword")
    core.create_namespaced_secret.assert_called()


def test_ready_backfills_auth_secret_when_missing(vf_env):
    """Liqo re-create with phase Ready must still persist expectedPassword."""
    from virtfusion_operator.handlers import vm as vm_mod

    vm_mod._auth_secret_last_attempt.clear()
    vf_env.state.servers[100] = {
        "id": 100,
        "uuid": "vf-uuid-100",
        "ipv4": [{"address": "203.0.113.10"}],
        "hypervisor": {"id": 2},
        "state": "complete",
        "built": "2026-01-01T00:00:00+00:00",
        "network": {"interfaces": []},
    }
    core = _core_missing_secret()
    core.read_namespaced_secret.side_effect = ApiException(status=404)
    patch, core = _run(
        _spec(),
        vf_env,
        {"phase": "Ready", "serverId": 100},
        "created",
        core=core,
    )
    assert patch.status.get("phase") in (None, "Ready") or patch.status == {}
    assert vf_env.bodies("POST", "/servers") == []
    assert vf_env.bodies("POST", "/build") == []
    assert vf_env.bodies("POST", "/resetPassword")
    core.create_namespaced_secret.assert_called()
    secret = core.create_namespaced_secret.call_args[0][1]
    assert secret.metadata.name == f"vm-{VM_UUID}-auth"


def test_ready_skip_does_not_reset_when_auth_secret_exists(vf_env):
    from virtfusion_operator.handlers import vm as vm_mod

    vm_mod._auth_secret_last_attempt.clear()
    core = MagicMock()
    core.read_namespaced_secret.return_value = MagicMock(data={"password": "c2VjcmV0"})
    _run(_spec(), vf_env, {"phase": "Ready", "serverId": 100}, "created", core=core)
    assert vf_env.bodies("POST", "/resetPassword") == []
    core.create_namespaced_secret.assert_not_called()
    core.patch_namespaced_secret.assert_not_called()


def test_ready_missing_auth_secret_respects_retry_cooldown(vf_env):
    from virtfusion_operator.handlers import vm as vm_mod

    vm_mod._auth_secret_last_attempt.clear()
    vf_env.state.servers[100] = {
        "id": 100,
        "ipv4": [{"address": "203.0.113.10"}],
        "hypervisor": {"id": 2},
        "state": "complete",
        "built": "2026-01-01T00:00:00+00:00",
        "network": {"interfaces": []},
    }
    core = _core_missing_secret()
    core.read_namespaced_secret.side_effect = ApiException(status=404)
    status = {"phase": "Ready", "serverId": 100}
    _run(_spec(), vf_env, status, "created", core=core)
    _run(_spec(), vf_env, status, "created", core=core)
    assert len(vf_env.bodies("POST", "/resetPassword")) == 1


def test_reinstall_with_password_posts_user_api_reinstall(vf_env):
    """Password reinstall must User API rebuild with reinstall=true, not a no-op /build."""
    vf_env.state.servers[100] = {
        "id": 100,
        "uuid": "vf-uuid-100",
        "ipv4": [{"address": "203.0.113.10"}],
        "hypervisor": {"id": 2},
        "state": "complete",
        "built": "2026-01-01T00:00:00+00:00",
        "network": {"interfaces": []},
    }
    core = MagicMock()
    core.read_namespaced_secret.return_value = MagicMock(
        data={"password": base64.b64encode(b"chosen-pass").decode()}
    )

    with (
        patch("virtfusion_operator.servers.QUEUE_POLL_INTERVAL", 0),
        patch("virtfusion_operator.servers.QUEUE_POLL_TIMEOUT", 5),
    ):
        status_patch, _core = _run(
            _spec(operation={"type": "reinstalled"}, sshPublicKeys=[]),
            vf_env,
            {"serverId": 100, "uuid": "vf-uuid-100"},
            "reinstalled",
            core=core,
        )

    assert status_patch.status.get("phase") == "Ready", status_patch.status
    assert status_patch.status.get("message") == "VM reinstalled"
    bodies = vf_env.user_api_build_bodies()
    assert len(bodies) == 1, vf_env.paths("POST")
    assert bodies[0].get("reinstall") is True
    assert bodies[0]["templateId"] == 8
    assert "$6$" in bodies[0]["userData"]
    global_builds = [
        p
        for m, p, _b in vf_env.state.calls
        if m == "POST" and "/api/v1/servers/" in p and p.endswith("/build")
    ]
    assert global_builds == []
    assert vf_env.bodies("POST", "/resetPassword") == []


def test_reinstalled_operation_is_accepted(vf_env):
    vf_env.state.servers[100] = {
        "id": 100,
        "ipv4": [{"address": "203.0.113.10"}],
        "hypervisor": {"id": 2},
    }
    patch, _core = _run(
        _spec(operation={"type": "reinstalled"}), vf_env, {"serverId": 100}, "reinstalled"
    )
    assert patch.status.get("phase") == "Ready", patch.status
    assert patch.status.get("message") == "VM reinstalled"
    builds = vf_env.bodies("POST", "/build")
    assert builds and builds[0].get("reinstall") is True
    assert builds[0].get("sshKeys")
    assert vf_env.bodies("POST", "/servers") == []


def test_reinstall_continues_when_reset_password_queue_fails(vf_env):
    vf_env.state.servers[100] = {
        "id": 100,
        "ipv4": [{"address": "203.0.113.10"}],
        "hypervisor": {"id": 2},
    }
    vf_env.state.fail_queue_ids.add(3)
    vf_env.state.fail_queue_job = "App\\Jobs\\Server\\KVM\\ResetPassword"
    patch, _core = _run(
        _spec(operation={"type": "reinstalled"}), vf_env, {"serverId": 100}, "reinstalled"
    )
    assert patch.status.get("phase") == "Ready", patch.status
    assert patch.status.get("message") == "VM reinstalled"


def test_pydantic_accepts_reinstalled():
    spec = _spec(operation={"type": "reinstalled"})
    assert spec.operation.type == "reinstalled"


def test_started_treats_empty_boot_queue_fail_as_ready(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    vf_env.state.fail_queue_ids.add(4)
    vf_env.state.fail_queue_job = "App\\Jobs\\Server\\KVM\\Boot"
    patch, _ = _run(_spec(operation={"type": "started"}), vf_env, {"serverId": 100}, "started")
    assert patch.status.get("phase") == "Ready", patch.status
    assert patch.status.get("message") == "VM started"


def test_create_continues_when_reset_password_queue_fails(vf_env):
    vf_env.state.fail_queue_ids.add(3)
    vf_env.state.fail_queue_job = "App\\Jobs\\Server\\KVM\\ResetPassword"
    patch, _core = _run(_spec(), vf_env, {}, "created")
    assert patch.status.get("phase") == "Ready", patch.status
    assert patch.status.get("serverId")


def test_started_does_not_create_server(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    patch, _ = _run(_spec(operation={"type": "started"}), vf_env, {"serverId": 100}, "started")
    assert patch.status.get("phase") == "Ready", patch.status
    assert vf_env.bodies("POST", "/servers") == []
    assert any("/power/boot" in p for p in vf_env.paths("POST"))


def test_stopped_and_restarted_power_paths(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    _run(_spec(operation={"type": "stopped"}), vf_env, {"serverId": 100}, "stopped")
    _run(_spec(operation={"type": "restarted"}), vf_env, {"serverId": 100}, "restarted")
    posts = vf_env.paths("POST")
    assert any(p.endswith("/power/shutdown") for p in posts)
    assert any(p.endswith("/power/restart") for p in posts)


def test_restarted_runs_when_generation_differs(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    patch, _ = _run(
        _spec(operation={"type": "restarted"}),
        vf_env,
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM created and built",
            "observedGeneration": 1,
        },
        "restarted",
        generation=2,
    )
    assert any(p.endswith("/power/restart") for p in vf_env.paths("POST"))
    assert patch.status.get("observedGeneration") == 2
    assert patch.status.get("message") == "VM restarted"


def test_restarted_skipped_when_same_generation_ready(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    _run(
        _spec(operation={"type": "restarted"}),
        vf_env,
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM restarted",
            "observedGeneration": 4,
        },
        "restarted",
        generation=4,
    )
    assert not any(p.endswith("/power/restart") for p in vf_env.paths("POST"))


def test_restarted_runs_when_requested_at_differs_same_generation(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    patch, _ = _run(
        _spec(operation={"type": "restarted", "requestedAt": "2026-08-29T16:00:00Z"}),
        vf_env,
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
    assert any(p.endswith("/power/restart") for p in vf_env.paths("POST"))
    assert patch.status.get("observedRequestedAt") == "2026-08-29T16:00:00Z"


def test_restarted_runs_when_requested_at_set_and_unobserved(vf_env):
    """UI Restart while spec.type is already restarted: generation does not bump."""
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    patch, _ = _run(
        _spec(operation={"type": "restarted", "requestedAt": "2026-08-29T16:10:00Z"}),
        vf_env,
        {
            "serverId": 100,
            "phase": "Ready",
            "message": "VM restarted",
            "observedGeneration": 8,
        },
        "restarted",
        generation=8,
    )
    assert any(p.endswith("/power/restart") for p in vf_env.paths("POST"))
    assert patch.status.get("observedRequestedAt") == "2026-08-29T16:10:00Z"


def test_restarted_skipped_when_same_requested_at(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    _run(
        _spec(operation={"type": "restarted", "requestedAt": "2026-08-29T16:00:00Z"}),
        vf_env,
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
    assert not any(p.endswith("/power/restart") for p in vf_env.paths("POST"))


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


def test_started_skipped_when_same_generation_ready(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    _run(
        _spec(operation={"type": "started"}),
        vf_env,
        {"serverId": 100, "phase": "Ready", "message": "VM started", "observedGeneration": 3},
        "started",
        generation=3,
    )
    assert not any("/power/boot" in p for p in vf_env.paths("POST"))


def test_extra_ipv4_posts_qty_not_exact_ip(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": [{"address": "203.0.113.10"}]}
    patch, _ = _run(
        _spec(ipv4=["203.0.113.10", "203.0.113.11"], operation={"type": "extra_ipv4"}),
        vf_env,
        {"serverId": 100},
        "extra_ipv4",
    )
    assert patch.status.get("phase") == "Ready", patch.status
    assert vf_env.bodies("POST", "/ipv4Qty") == [{"interface": "primary", "quantity": 1}]
    assert vf_env.bodies("POST", "/ipv4") == []
    assert "192.168.1.120" in (patch.status.get("ipv4") or [])
    assert "203.0.113.10" in (patch.status.get("ipv4") or [])


def test_remove_ipv4_deletes_delta(vf_env):
    vf_env.state.servers[100] = {
        "id": 100,
        "ipv4": [{"address": "203.0.113.10"}, {"address": "203.0.113.11"}],
    }
    patch, _ = _run(
        _spec(ipv4=["203.0.113.10"], operation={"type": "remove_ipv4"}),
        vf_env,
        {"serverId": 100},
        "remove_ipv4",
    )
    assert patch.status.get("phase") == "Ready", patch.status
    deletes = vf_env.bodies("DELETE", "/ipv4")
    assert deletes and deletes[0]["ip"] == ["203.0.113.11"]


def test_deleted_removes_server(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    patch, _ = _run(_spec(operation={"type": "deleted"}), vf_env, {"serverId": 100}, "deleted")
    assert patch.status.get("phase") == "Ready", patch.status
    assert 100 not in vf_env.state.servers


def test_deleted_is_idempotent_when_server_already_gone(vf_env):
    patch, _ = _run(_spec(operation={"type": "deleted"}), vf_env, {"serverId": 100}, "deleted")
    assert patch.status.get("phase") == "Ready", patch.status


def test_deleted_without_server_id_is_success(vf_env):
    patch, _ = _run(_spec(operation={"type": "deleted"}), vf_env, {}, "deleted")
    assert patch.status.get("phase") == "Ready", patch.status
    assert vf_env.paths("DELETE") == []


def test_package_change_uses_put(vf_env):
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    patch, _ = _run(
        _spec(operation={"type": "package_change"}), vf_env, {"serverId": 100}, "package_change"
    )
    assert patch.status.get("phase") == "Ready", patch.status
    puts = vf_env.paths("PUT")
    assert any("/package/42" in p for p in puts)
    assert any(p.endswith("/modify/cpuCores") for p in puts)
    assert any(p.endswith("/modify/memory") for p in puts)


def test_control_connects_and_lists_hypervisors(vf_env):
    status_patch = _KopfPatch()
    _reconcile_control({"endpoint": vf_env.url}, status_patch, MagicMock())
    assert status_patch.status.get("phase") == "Ready"
    assert status_patch.status["hypervisors"][0]["id"] == 2
    assert "encryptedToken" not in status_patch.status["hypervisors"][0]


def test_vnc_enable_then_get_rfb(vf_env):
    status_patch = _KopfPatch()
    k8s_api = _k8s_node_control(vf_env.url, 100)
    vf_env.state.servers[100] = {"id": 100}
    with (
        patch(
            "virtfusion_operator.handlers.vnc.kubernetes.client.CustomObjectsApi",
            return_value=k8s_api,
        ),
    ):
        on_vnc_request(
            {"vmRef": {"name": VM_CR}, "ttl": 30},
            {"namespace": "vf-sim"},
            status_patch,
            MagicMock(),
        )
    assert status_patch.status["ready"] is True
    assert status_patch.status["hypervisorIp"] == "10.0.0.9"
    assert status_patch.status["port"] == 5903
    assert status_patch.status["password"] == "rfb-secret"
    posts = vf_env.paths("POST")
    gets = vf_env.paths("GET")
    assert any(p.endswith("/vnc") for p in posts)
    assert any(p.endswith("/vnc") for p in gets)


def test_ipv4_parse_from_server_payload():
    assert ipv4_addresses_from_server({"data": {"ipv4": [{"address": "1.2.3.4"}]}}) == ["1.2.3.4"]


def test_is_noop_power_failure():
    from virtfusion_operator.errors import VirtFusionHTTPError

    empty = VirtFusionHTTPError("Queue 22 failed (App\\Jobs\\Server\\KVM\\Boot)")
    real = VirtFusionHTTPError("Queue 22 failed (App\\Jobs\\Server\\KVM\\Boot): qemu died")
    assert is_noop_power_failure(empty, "boot") is True
    assert is_noop_power_failure(real, "boot") is False


def test_queue_fail_message_omits_hypervisor_trace():
    msg = _queue_fail_message(
        10,
        {
            "job": "App\\Jobs\\Server\\KVM\\Boot",
            "action": "boot_server",
            "errors": {"exception": {"message": "[]", "trace": "x" * 5000}},
        },
    )
    assert "Queue 10 failed" in msg
    assert "Boot" in msg
    assert "x" * 100 not in msg


def test_group_resources_status_wraps_array_for_crd():
    payload = {
        "data": [
            {
                "hypervisor": {"id": 2, "name": "hv-1"},
                "resources": {"cpuCores": {"allocated": 10, "max": 32}},
            }
        ]
    }
    shaped = group_resources_status(payload, 2)
    assert shaped is not None
    assert "items" in shaped
    assert shaped["thisHypervisor"]["hypervisor"]["id"] == 2
    # CRD rejects a bare list on status.groupResources
    assert isinstance(shaped, dict)


def test_group_resources_status_zeros_none_percent():
    payload = {
        "data": [
            {
                "hypervisor": {"id": 1},
                "resources": {"servers": {"percent": None, "max": 0, "allocated": 0, "free": 0}},
            }
        ]
    }
    shaped = group_resources_status(payload, 1)
    assert shaped is not None
    assert shaped["thisHypervisor"]["resources"]["servers"]["percent"] == 0


def test_created_applies_smtp_ruleset_when_blocked(vf_env):
    from virtfusion_operator.smtp_outbound import clear_smtp_ruleset_cache

    clear_smtp_ruleset_cache()
    patch, _ = _run(_spec(blockOutboundSmtp=True), vf_env, {}, "created")
    assert patch.status.get("phase") == "Ready", patch.status
    fw = vf_env.state.server_firewall.get(100) or {}
    assert fw.get("ruleset_ids") == [2]
    bodies = vf_env.bodies("POST", "/rules")
    assert bodies
    assert bodies[-1]["rulesets"] == [2]


def test_started_removes_smtp_ruleset_when_allowed(vf_env):
    from virtfusion_operator.smtp_outbound import clear_smtp_ruleset_cache

    clear_smtp_ruleset_cache()
    vf_env.state.servers[100] = {"id": 100, "ipv4": []}
    vf_env.state.server_firewall[100] = {"enabled": True, "ruleset_ids": [2]}
    patch, _ = _run(
        _spec(blockOutboundSmtp=False, operation={"type": "started"}),
        vf_env,
        {"serverId": 100},
        "started",
    )
    assert patch.status.get("phase") == "Ready", patch.status
    fw = vf_env.state.server_firewall.get(100) or {}
    assert fw.get("ruleset_ids") == []
    assert fw.get("enabled") is False
