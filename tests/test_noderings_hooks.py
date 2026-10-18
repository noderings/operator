"""Unit tests for noderings_hooks runner and watcher (mocked Kubernetes client)."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import kopf
import pytest

from noderings_hooks.config import load_hook_settings
from noderings_hooks.constants import (
    ANN_HOOK_REPORTED,
    DEFAULT_PROVIDER_SECRET_NAME,
    ENV_PROVIDER_SECRET_NAME,
    PROVIDER_SECRET_MOUNT_PATH,
    PROVIDER_SECRET_VOLUME,
    PYTHON_BINARY,
    SCRIPT_COMMAND,
    SCRIPT_FILENAME,
)
from noderings_hooks.net import netmask_from_prefix, normalize_prefix
from noderings_hooks.runner import (
    dispatch_hook,
    job_name,
    last_hook_patch,
    run_hook_for_operation,
    safe_run_hook,
)
from noderings_hooks.types import HookConcurrencyError
from noderings_hooks.watcher import on_hook_job_event


def _api_error(status: int) -> ApiException:
    return ApiException(status=status)


class TestJobNamespace:
    def test_defaults_to_vm_namespace(self, monkeypatch):
        monkeypatch.delenv("HOOKS_JOB_NAMESPACE", raising=False)
        assert load_hook_settings().namespace_for("agent") == "agent"

    def test_dedicated_env_wins(self, monkeypatch):
        monkeypatch.setenv("HOOKS_JOB_NAMESPACE", "agent-hooks")
        assert load_hook_settings().namespace_for("agent") == "agent-hooks"


class TestProviderSecretsSettings:
    def test_default_secret_name(self, monkeypatch):
        monkeypatch.delenv(ENV_PROVIDER_SECRET_NAME, raising=False)
        assert load_hook_settings().provider_secret_name == DEFAULT_PROVIDER_SECRET_NAME

    def test_empty_disables(self, monkeypatch):
        monkeypatch.setenv(ENV_PROVIDER_SECRET_NAME, "")
        assert load_hook_settings().provider_secret_name == ""

    def test_whitespace_only_disables(self, monkeypatch):
        monkeypatch.setenv(ENV_PROVIDER_SECRET_NAME, "  ")
        assert load_hook_settings().provider_secret_name == ""

    def test_invalid_dns_name_disables(self, monkeypatch):
        monkeypatch.setenv(ENV_PROVIDER_SECRET_NAME, "My_Secret")
        assert load_hook_settings().provider_secret_name == ""

    def test_custom_name(self, monkeypatch):
        monkeypatch.setenv(ENV_PROVIDER_SECRET_NAME, "my-hooks-creds")
        assert load_hook_settings().provider_secret_name == "my-hooks-creds"


class TestJobName:
    def test_deterministic_and_short(self):
        a = job_name("ns", "proxmoxvm-very-long-name-that-would-exceed-dns", "created", 7)
        b = job_name("ns", "proxmoxvm-very-long-name-that-would-exceed-dns", "created", 7)
        assert a == b
        assert a.startswith("hook-")
        assert len(a) <= 63

    def test_changes_with_generation(self):
        assert job_name("ns", "vm", "started", 1) != job_name("ns", "vm", "started", 2)


class TestRunHook:
    def test_no_script_is_noop(self):
        core = MagicMock()
        batch = MagicMock()
        name = run_hook_for_operation(
            hooks=None,
            op_type="created",
            vm_name="vm-1",
            namespace="agent",
            generation=1,
            group="vm.proxmox.com",
            version="v1alpha1",
            plural="proxmoxvms",
            kind="ProxmoxVM",
            vmid="100",
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )
        assert name is None
        core.create_namespaced_secret.assert_not_called()
        batch.create_namespaced_job.assert_not_called()

    def test_creates_hardened_job_and_secret(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "true")
        monkeypatch.setenv("HOOKS_JOB_IMAGE", "python:3.12-slim-bookworm")
        monkeypatch.delenv(ENV_PROVIDER_SECRET_NAME, raising=False)
        core = MagicMock()
        batch = MagicMock()
        batch.read_namespaced_job.side_effect = _api_error(404)
        batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
        created_job = SimpleNamespace(metadata=SimpleNamespace(uid="job-uid-1"))
        batch.create_namespaced_job.return_value = created_job
        core.read_namespaced_secret.return_value = SimpleNamespace(metadata=SimpleNamespace())

        name = run_hook_for_operation(
            hooks={"created": "print('hi')"},
            op_type="created",
            vm_name="vm-1",
            namespace="agent",
            generation=3,
            group="vm.proxmox.com",
            version="v1alpha1",
            plural="proxmoxvms",
            kind="ProxmoxVM",
            vmid="101",
            addresses={
                "ipv4": ["203.0.113.10"],
                "ipv6": ["2001:db8::1"],
                "ipv4Prefix": "24",
                "ipv6Prefix": "64",
            },
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )

        assert name == job_name("agent", "vm-1", "created", 3)
        core.create_namespaced_secret.assert_called_once()
        batch.create_namespaced_job.assert_called_once()
        core.patch_namespaced_secret.assert_called_once()
        assert core.create_namespaced_secret.call_args.args[0] == "agent"
        assert batch.create_namespaced_job.call_args.args[0] == "agent"

        secret = core.create_namespaced_secret.call_args.args[1]
        assert secret.string_data[SCRIPT_FILENAME] == "print('hi')"

        job = batch.create_namespaced_job.call_args.args[1]
        spec = job.spec.template.spec
        assert spec.automount_service_account_token is False
        container = spec.containers[0]
        assert container.command == [PYTHON_BINARY, SCRIPT_COMMAND]
        assert container.security_context.read_only_root_filesystem is True
        assert container.security_context.allow_privilege_escalation is False
        assert container.security_context.capabilities.drop == ["ALL"]
        env = {item.name: item.value for item in container.env}
        assert env["NODERINGS_HOOK"] == "created"
        assert env["NODERINGS_VM_NAME"] == "vm-1"
        assert env["NODERINGS_VM_ID"] == "101"
        assert env["NODERINGS_IPV4"] == "203.0.113.10"
        assert env["NODERINGS_IPV6"] == "2001:db8::1"
        assert env["NODERINGS_IPV4_PREFIX"] == "24"
        assert env["NODERINGS_IPV4_NETMASK"] == "255.255.255.0"
        assert env["NODERINGS_IPV6_PREFIX"] == "64"
        payload = json.loads(env["NODERINGS_EVENT_JSON"])
        assert payload["vmName"] == "vm-1"
        assert payload["namespace"] == "agent"
        assert payload["ipv4"] == ["203.0.113.10"]
        assert payload["ipv6"] == ["2001:db8::1"]
        assert payload["ipv4Prefix"] == "24"
        assert payload["ipv4Netmask"] == "255.255.255.0"
        assert payload["ipv6Prefix"] == "64"
        assert "hooks" not in payload
        assert container.env_from is not None
        assert len(container.env_from) == 1
        assert container.env_from[0].secret_ref.name == DEFAULT_PROVIDER_SECRET_NAME
        assert container.env_from[0].secret_ref.optional is True
        mounts = {m.name: m for m in container.volume_mounts}
        assert PROVIDER_SECRET_VOLUME in mounts
        assert mounts[PROVIDER_SECRET_VOLUME].mount_path == PROVIDER_SECRET_MOUNT_PATH
        assert mounts[PROVIDER_SECRET_VOLUME].read_only is True
        vols = {v.name: v for v in spec.volumes}
        assert vols[PROVIDER_SECRET_VOLUME].secret.secret_name == DEFAULT_PROVIDER_SECRET_NAME
        assert vols[PROVIDER_SECRET_VOLUME].secret.optional is True
        owner = core.patch_namespaced_secret.call_args.args[2].metadata.owner_references[0]
        assert owner.kind == "Job"
        assert owner.uid == "job-uid-1"

    def test_custom_provider_secret_name_is_wired(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "true")
        monkeypatch.setenv(ENV_PROVIDER_SECRET_NAME, "my-hooks-creds")
        core = MagicMock()
        batch = MagicMock()
        batch.read_namespaced_job.side_effect = _api_error(404)
        batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
        batch.create_namespaced_job.return_value = SimpleNamespace(
            metadata=SimpleNamespace(uid="u")
        )
        core.read_namespaced_secret.return_value = SimpleNamespace(metadata=SimpleNamespace())

        run_hook_for_operation(
            hooks={"created": "print(1)"},
            op_type="created",
            vm_name="vm-1",
            namespace="agent",
            generation=1,
            group="vm.proxmox.com",
            version="v1alpha1",
            plural="proxmoxvms",
            kind="ProxmoxVM",
            vmid="1",
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )
        job = batch.create_namespaced_job.call_args.args[1]
        container = job.spec.template.spec.containers[0]
        assert container.env_from is not None
        assert container.env_from[0].secret_ref.name == "my-hooks-creds"
        vols = {v.name: v for v in job.spec.template.spec.volumes}
        assert vols[PROVIDER_SECRET_VOLUME].secret.secret_name == "my-hooks-creds"

    def test_provider_secrets_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "true")
        monkeypatch.setenv(ENV_PROVIDER_SECRET_NAME, "")
        core = MagicMock()
        batch = MagicMock()
        batch.read_namespaced_job.side_effect = _api_error(404)
        batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
        batch.create_namespaced_job.return_value = SimpleNamespace(
            metadata=SimpleNamespace(uid="u")
        )
        core.read_namespaced_secret.return_value = SimpleNamespace(metadata=SimpleNamespace())

        run_hook_for_operation(
            hooks={"created": "print(1)"},
            op_type="created",
            vm_name="vm-1",
            namespace="agent",
            generation=1,
            group="vm.proxmox.com",
            version="v1alpha1",
            plural="proxmoxvms",
            kind="ProxmoxVM",
            vmid="1",
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )
        job = batch.create_namespaced_job.call_args.args[1]
        container = job.spec.template.spec.containers[0]
        assert container.env_from is None
        assert all(m.name != PROVIDER_SECRET_VOLUME for m in container.volume_mounts)
        assert all(v.name != PROVIDER_SECRET_VOLUME for v in job.spec.template.spec.volumes)

    def test_jobs_land_in_dedicated_namespace(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "true")
        monkeypatch.setenv("HOOKS_JOB_NAMESPACE", "agent-hooks")
        core = MagicMock()
        batch = MagicMock()
        batch.read_namespaced_job.side_effect = _api_error(404)
        batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
        batch.create_namespaced_job.return_value = SimpleNamespace(
            metadata=SimpleNamespace(uid="u")
        )
        core.read_namespaced_secret.return_value = SimpleNamespace(metadata=SimpleNamespace())

        name = run_hook_for_operation(
            hooks={"created": "print(1)"},
            op_type="created",
            vm_name="vm-1",
            namespace="agent",
            generation=1,
            group="vm.proxmox.com",
            version="v1alpha1",
            plural="proxmoxvms",
            kind="ProxmoxVM",
            vmid="1",
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )
        assert name == job_name("agent", "vm-1", "created", 1)
        assert batch.read_namespaced_job.call_args.args[1] == "agent-hooks"
        assert batch.list_namespaced_job.call_args.args[0] == "agent-hooks"
        assert core.create_namespaced_secret.call_args.args[0] == "agent-hooks"
        assert batch.create_namespaced_job.call_args.args[0] == "agent-hooks"
        job = batch.create_namespaced_job.call_args.args[1]
        env = {item.name: item.value for item in job.spec.template.spec.containers[0].env}
        payload = json.loads(env["NODERINGS_EVENT_JSON"])
        assert payload["namespace"] == "agent"
        assert job.metadata.annotations["noderings.com/vm-namespace"] == "agent"

    def test_reinstall_alias(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "true")
        core = MagicMock()
        batch = MagicMock()
        batch.read_namespaced_job.side_effect = _api_error(404)
        batch.list_namespaced_job.return_value = SimpleNamespace(items=[])
        batch.create_namespaced_job.return_value = SimpleNamespace(
            metadata=SimpleNamespace(uid="u")
        )
        core.read_namespaced_secret.return_value = SimpleNamespace(metadata=SimpleNamespace())
        name = run_hook_for_operation(
            hooks={"reinstalled": "print(1)"},
            op_type="reinstall",
            vm_name="vm-1",
            namespace="agent",
            generation=1,
            group="vm.solusvm.com",
            version="v1alpha1",
            plural="solusvmvms",
            kind="SolusVMVM",
            vmid="9",
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )
        assert name is not None
        batch.create_namespaced_job.assert_called_once()

    def test_already_exists_skips(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "true")
        core = MagicMock()
        batch = MagicMock()
        batch.read_namespaced_job.return_value = SimpleNamespace()
        name = run_hook_for_operation(
            hooks={"started": "print(1)"},
            op_type="started",
            vm_name="vm-1",
            namespace="agent",
            generation=2,
            group="vm.proxmox.com",
            version="v1alpha1",
            plural="proxmoxvms",
            kind="ProxmoxVM",
            vmid="1",
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )
        assert name == job_name("agent", "vm-1", "started", 2)
        batch.create_namespaced_job.assert_not_called()

    def test_concurrency_cap(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "true")
        monkeypatch.setenv("HOOKS_MAX_CONCURRENT", "1")
        core = MagicMock()
        batch = MagicMock()
        batch.read_namespaced_job.side_effect = _api_error(404)
        batch.list_namespaced_job.return_value = SimpleNamespace(
            items=[SimpleNamespace(status=SimpleNamespace(active=1, succeeded=0, failed=0))]
        )
        with pytest.raises(HookConcurrencyError, match="concurrency cap"):
            run_hook_for_operation(
                hooks={"stopped": "print(1)"},
                op_type="stopped",
                vm_name="vm-1",
                namespace="agent",
                generation=4,
                group="vm.proxmox.com",
                version="v1alpha1",
                plural="proxmoxvms",
                kind="ProxmoxVM",
                vmid="1",
                logger=MagicMock(),
                core_api=core,
                batch_api=batch,
            )
        batch.create_namespaced_job.assert_not_called()
        assert batch.list_namespaced_job.call_args.kwargs.get("limit") == 50

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("HOOKS_ENABLED", "false")
        core = MagicMock()
        batch = MagicMock()
        name = run_hook_for_operation(
            hooks={"created": "print(1)"},
            op_type="created",
            vm_name="vm-1",
            namespace="agent",
            generation=1,
            group="vm.proxmox.com",
            version="v1alpha1",
            plural="proxmoxvms",
            kind="ProxmoxVM",
            vmid="1",
            logger=MagicMock(),
            core_api=core,
            batch_api=batch,
        )
        assert name is None
        assert load_hook_settings().enabled is False


class TestSafeRunHook:
    def test_swallows_errors(self):
        logger = MagicMock()
        with patch(
            "noderings_hooks.runner.run_hook_for_operation", side_effect=RuntimeError("boom")
        ):
            safe_run_hook(
                hooks={"created": "x"},
                op_type="created",
                meta={"name": "vm-1", "namespace": "ns", "generation": 1},
                status={"vmId": "10"},
                logger=logger,
                group="vm.proxmox.com",
                version="v1alpha1",
                plural="proxmoxvms",
                kind="ProxmoxVM",
            )
        logger.warning.assert_called()

    def test_reraises_concurrency(self):
        with patch(
            "noderings_hooks.runner.run_hook_for_operation",
            side_effect=HookConcurrencyError("cap"),
        ):
            with pytest.raises(HookConcurrencyError):
                safe_run_hook(
                    hooks={"created": "x"},
                    op_type="created",
                    meta={"name": "vm-1", "namespace": "ns", "generation": 1},
                    status={},
                    logger=MagicMock(),
                    group="vm.proxmox.com",
                    version="v1alpha1",
                    plural="proxmoxvms",
                    kind="ProxmoxVM",
                )

    def test_dispatch_hook_retries_via_temporary_error(self):
        with patch(
            "noderings_hooks.runner.run_hook_for_operation",
            side_effect=HookConcurrencyError("cap"),
        ):
            with pytest.raises(kopf.TemporaryError, match="cap"):
                dispatch_hook(
                    hooks={"created": "x"},
                    op_type="created",
                    meta={"name": "vm-1", "namespace": "ns", "generation": 1},
                    status={},
                    logger=MagicMock(),
                    group="vm.proxmox.com",
                    version="v1alpha1",
                    plural="proxmoxvms",
                    kind="ProxmoxVM",
                )


class TestWatcher:
    def test_patches_status_on_success(self):
        api = MagicMock()
        batch = MagicMock()
        logger = MagicMock()
        with (
            patch("noderings_hooks.watcher.k8s_client.CustomObjectsApi", return_value=api),
            patch("noderings_hooks.watcher.k8s_client.BatchV1Api", return_value=batch),
            patch("noderings_hooks.watcher.kopf.event") as posted,
        ):
            on_hook_job_event(
                body={"metadata": {"name": "hook-abc"}},
                status={"succeeded": 1, "failed": 0},
                meta={
                    "name": "hook-abc",
                    "annotations": {
                        "noderings.com/vm-name": "vm-1",
                        "noderings.com/vm-namespace": "agent",
                        "noderings.com/vm-group": "vm.proxmox.com",
                        "noderings.com/vm-version": "v1alpha1",
                        "noderings.com/vm-plural": "proxmoxvms",
                        "noderings.com/vm-kind": "ProxmoxVM",
                        "noderings.com/hook": "created",
                    },
                },
                namespace="agent-hooks",
                logger=logger,
            )
        api.patch_namespaced_custom_object_status.assert_called_once()
        body = api.patch_namespaced_custom_object_status.call_args.kwargs["body"]
        assert body["status"]["lastHook"]["state"] == "Succeeded"
        assert body["status"]["lastHook"]["operation"] == "created"
        batch.patch_namespaced_job.assert_called_once()
        assert batch.patch_namespaced_job.call_args.args[0] == "hook-abc"
        assert batch.patch_namespaced_job.call_args.args[1] == "agent-hooks"
        assert (
            batch.patch_namespaced_job.call_args.args[2]["metadata"]["annotations"][
                ANN_HOOK_REPORTED
            ]
            == "true"
        )
        posted.assert_called_once()
        assert posted.call_args.kwargs["reason"] == "LifecycleHookSucceeded"

    def test_already_reported_is_noop(self):
        api = MagicMock()
        with patch("noderings_hooks.watcher.k8s_client.CustomObjectsApi", return_value=api):
            on_hook_job_event(
                body={},
                status={"succeeded": 1},
                meta={
                    "name": "hook-abc",
                    "annotations": {ANN_HOOK_REPORTED: "true"},
                },
                namespace="agent-hooks",
                logger=MagicMock(),
            )
        api.patch_namespaced_custom_object_status.assert_not_called()

    def test_missing_cr_is_tolerated(self):
        api = MagicMock()
        api.patch_namespaced_custom_object_status.side_effect = _api_error(404)
        logger = MagicMock()
        with patch("noderings_hooks.watcher.k8s_client.CustomObjectsApi", return_value=api):
            on_hook_job_event(
                body={},
                status={"failed": 1},
                meta={
                    "name": "hook-abc",
                    "annotations": {
                        "noderings.com/vm-name": "vm-1",
                        "noderings.com/vm-namespace": "agent",
                        "noderings.com/vm-group": "vm.proxmox.com",
                        "noderings.com/vm-version": "v1alpha1",
                        "noderings.com/vm-plural": "proxmoxvms",
                        "noderings.com/hook": "deleted",
                    },
                },
                namespace="agent",
                logger=logger,
            )
        logger.info.assert_called()

    def test_pending_job_ignored(self):
        api = MagicMock()
        with patch("noderings_hooks.watcher.k8s_client.CustomObjectsApi", return_value=api):
            on_hook_job_event(
                body={},
                status={"active": 1, "succeeded": 0, "failed": 0},
                meta={"name": "hook-abc", "annotations": {}},
                namespace="agent",
                logger=MagicMock(),
            )
        api.patch_namespaced_custom_object_status.assert_not_called()


def test_last_hook_patch_shape():
    patch = last_hook_patch("started", "Failed", "hook-xyz", "hook started failed")
    assert set(patch["lastHook"]) == {"operation", "state", "ranAt", "message", "jobName"}
    assert patch["lastHook"]["jobName"] == "hook-xyz"


def test_addresses_from_status_and_merge():
    from noderings_hooks.runner import addresses_from_status, merge_addresses

    got = addresses_from_status(
        {"ipv4": ["10.0.0.2"], "ipv4Gateway": "10.0.0.1", "ipv4Prefix": "24"}
    )
    assert got["ipv4"] == ["10.0.0.2"]
    assert got["ipv4Prefix"] == "24"
    merged = merge_addresses(got, {"ipv4": ["203.0.113.8"], "ipv4Prefix": "255.255.255.0"})
    assert merged["ipv4"] == ["203.0.113.8"]
    assert merged["ipv4Gateway"] == "10.0.0.1"
    assert merged["ipv4Prefix"] == "24"
    assert netmask_from_prefix("24") == "255.255.255.0"

    assert normalize_prefix("255.255.255.0", version=4) == "24"
    assert normalize_prefix("/29", version=4) == "29"
    assert normalize_prefix("203.0.113.10/24", version=4) == "24"
    assert normalize_prefix("64", version=6) == "64"


def test_sample_hook_script_is_valid_python3():
    src = """#!/usr/bin/env python3
import json
import os
import urllib.request

payload = {
    "event": os.environ.get("NODERINGS_EVENT_JSON", "{}"),
    "hook": os.environ.get("NODERINGS_HOOK", ""),
    "vm_name": os.environ.get("NODERINGS_VM_NAME", ""),
    "vm_id": os.environ.get("NODERINGS_VM_ID", ""),
    "ipv4": os.environ.get("NODERINGS_IPV4", ""),
    "ipv6": os.environ.get("NODERINGS_IPV6", ""),
    "ipv4_prefix": os.environ.get("NODERINGS_IPV4_PREFIX", ""),
    "ipv4_netmask": os.environ.get("NODERINGS_IPV4_NETMASK", ""),
    "ipv6_prefix": os.environ.get("NODERINGS_IPV6_PREFIX", ""),
}
req = urllib.request.Request(
    "https://hooks.example.com/noderings",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as resp:
    resp.read()
"""
    tree = ast.parse(src, filename="hook-sample.py")
    assert isinstance(tree, ast.Module)
