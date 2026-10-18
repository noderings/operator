"""Dispatch provider lifecycle scripts as isolated Kubernetes Jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import kopf
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from .config import HookSettings, load_hook_settings
from .constants import (
    ANN_HOOK,
    ANN_VM_GROUP,
    ANN_VM_KIND,
    ANN_VM_NAME,
    ANN_VM_NAMESPACE,
    ANN_VM_PLURAL,
    ANN_VM_VERSION,
    HOOK_ALIAS,
    HOOK_CONCURRENCY_RETRY_SECONDS,
    HOOK_FS_GROUP,
    HOOK_RUN_AS_GROUP,
    HOOK_RUN_AS_USER,
    LABEL_RUNNER,
    PROVIDER_SECRET_MOUNT_PATH,
    PROVIDER_SECRET_VOLUME,
    PYTHON_BINARY,
    SCRIPT_COMMAND,
    SCRIPT_FILENAME,
    SCRIPT_MOUNT_PATH,
    SCRIPT_VOLUME,
    TMP_MOUNT_PATH,
    TMP_VOLUME,
)
from .net import netmask_from_prefix, normalize_prefix
from .types import HookAddresses, HookConcurrencyError, VMHooks


def _hooks_map(hooks: Mapping[str, str] | VMHooks | None) -> dict[str, str]:
    if hooks is None:
        return {}
    if isinstance(hooks, VMHooks):
        return {key: value for key, value in hooks.model_dump().items() if value}
    return {key: value for key, value in hooks.items() if value}


def _script_for(hooks: Mapping[str, str] | VMHooks | None, op_type: str) -> str | None:
    mapping = _hooks_map(hooks)
    if not mapping:
        return None
    key = HOOK_ALIAS.get(op_type, op_type)
    script = mapping.get(key) or mapping.get(op_type)
    if script is None:
        return None
    text = str(script).strip()
    return text or None


def job_name(namespace: str, vm_name: str, op_type: str, generation: int) -> str:
    digest = hashlib.sha1(f"{namespace}/{vm_name}/{op_type}/{generation}".encode()).hexdigest()[:16]
    return f"hook-{digest}"


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    text = str(value).strip()
    return [text] if text else []


def addresses_from_status(status: dict[str, Any] | None) -> HookAddresses:
    """Collect guest addresses already published on the VM CR status."""
    status = status or {}
    ipv6 = _as_str_list(status.get("ipv6"))
    return {
        "ipv4": _as_str_list(status.get("ipv4")),
        "ipv6": ipv6,
        "ipv4Gateway": str(status.get("ipv4Gateway") or status.get("ipv4_gateway") or "").strip(),
        "ipv6Gateway": str(status.get("ipv6Gateway") or status.get("ipv6_gateway") or "").strip(),
        "ipv4Prefix": normalize_prefix(
            status.get("ipv4Prefix") or status.get("ipv4_prefix"), version=4
        ),
        "ipv6Prefix": normalize_prefix(
            status.get("ipv6Prefix") or status.get("ipv6_prefix"), version=6
        ),
    }


def merge_addresses(*parts: Mapping[str, Any] | None) -> HookAddresses:
    merged: HookAddresses = {
        "ipv4": [],
        "ipv6": [],
        "ipv4Gateway": "",
        "ipv6Gateway": "",
        "ipv4Prefix": "",
        "ipv6Prefix": "",
    }
    for part in parts:
        if not part:
            continue
        ipv4 = _as_str_list(part.get("ipv4"))
        if ipv4:
            merged["ipv4"] = ipv4
        ipv6 = _as_str_list(part.get("ipv6"))
        if ipv6:
            merged["ipv6"] = ipv6
        gw4 = str(part.get("ipv4Gateway") or part.get("ipv4_gateway") or "").strip()
        if gw4:
            merged["ipv4Gateway"] = gw4
        gw6 = str(part.get("ipv6Gateway") or part.get("ipv6_gateway") or "").strip()
        if gw6:
            merged["ipv6Gateway"] = gw6
        p4 = normalize_prefix(part.get("ipv4Prefix") or part.get("ipv4_prefix"), version=4)
        if p4:
            merged["ipv4Prefix"] = p4
        p6 = normalize_prefix(part.get("ipv6Prefix") or part.get("ipv6_prefix"), version=6)
        if p6:
            merged["ipv6Prefix"] = p6
    return merged


def _event_json(
    op_type: str,
    vm_name: str,
    namespace: str,
    vmid: str | None,
    addresses: Mapping[str, Any] | None = None,
) -> str:
    addrs = merge_addresses(addresses)
    payload = {
        "hook": op_type,
        "vmName": vm_name,
        "namespace": namespace,
        "vmId": vmid or "",
        "operation": op_type,
        "ipv4": addrs["ipv4"],
        "ipv6": addrs["ipv6"],
        "ipv4Gateway": addrs["ipv4Gateway"],
        "ipv6Gateway": addrs["ipv6Gateway"],
        "ipv4Prefix": addrs["ipv4Prefix"],
        "ipv6Prefix": addrs["ipv6Prefix"],
        "ipv4Netmask": netmask_from_prefix(addrs["ipv4Prefix"]),
    }
    return json.dumps(payload, separators=(",", ":"))


def _labels(op_type: str, generation: int) -> dict[str, str]:
    return {
        LABEL_RUNNER: "true",
        "noderings.com/hook": op_type[:63],
        "noderings.com/generation": str(generation)[:63],
    }


def _annotations(
    *,
    vm_name: str,
    namespace: str,
    group: str,
    version: str,
    plural: str,
    kind: str,
    op_type: str,
) -> dict[str, str]:
    return {
        ANN_VM_NAME: vm_name,
        ANN_VM_NAMESPACE: namespace,
        ANN_VM_GROUP: group,
        ANN_VM_VERSION: version,
        ANN_VM_PLURAL: plural,
        ANN_VM_KIND: kind,
        ANN_HOOK: op_type,
    }


def _count_active_jobs(batch: k8s_client.BatchV1Api, namespace: str, settings: HookSettings) -> int:
    """Best-effort active count (capped list; TOCTOU with create)."""
    jobs = batch.list_namespaced_job(
        namespace,
        label_selector=f"{LABEL_RUNNER}=true",
        limit=settings.job_list_limit,
    )
    active = 0
    for job in jobs.items or []:
        status = job.status
        if status is None:
            continue
        if (status.active or 0) > 0:
            active += 1
    return active


@dataclass(frozen=True)
class _ProviderSecretSources:
    env_from: list[k8s_client.V1EnvFromSource] | None
    volume: k8s_client.V1Volume | None
    mount: k8s_client.V1VolumeMount | None


def _provider_secret_sources(settings: HookSettings) -> _ProviderSecretSources:
    """Optional provider-owned Secret → envFrom + file mount (optional: true)."""
    name = settings.provider_secret_name
    if not name:
        return _ProviderSecretSources(env_from=None, volume=None, mount=None)
    env_from = [
        k8s_client.V1EnvFromSource(
            secret_ref=k8s_client.V1SecretEnvSource(name=name, optional=True),
        )
    ]
    volume = k8s_client.V1Volume(
        name=PROVIDER_SECRET_VOLUME,
        secret=k8s_client.V1SecretVolumeSource(secret_name=name, optional=True),
    )
    mount = k8s_client.V1VolumeMount(
        name=PROVIDER_SECRET_VOLUME,
        mount_path=PROVIDER_SECRET_MOUNT_PATH,
        read_only=True,
    )
    return _ProviderSecretSources(env_from=env_from, volume=volume, mount=mount)


def _job_spec(
    *,
    name: str,
    namespace: str,
    vm_namespace: str,
    script_secret: str,
    op_type: str,
    vm_name: str,
    vmid: str | None,
    labels: dict[str, str],
    annotations: dict[str, str],
    settings: HookSettings,
    addresses: Mapping[str, Any] | None = None,
    env: list[k8s_client.V1EnvVar] | None = None,
) -> k8s_client.V1Job:
    addrs = merge_addresses(addresses)
    primary_v4 = addrs["ipv4"][0] if addrs["ipv4"] else ""
    primary_v6 = addrs["ipv6"][0] if addrs["ipv6"] else ""
    prefix_v4 = addrs["ipv4Prefix"]
    prefix_v6 = addrs["ipv6Prefix"]
    provider = _provider_secret_sources(settings)
    volume_mounts = [
        k8s_client.V1VolumeMount(
            name=SCRIPT_VOLUME,
            mount_path=SCRIPT_MOUNT_PATH,
            read_only=True,
        ),
        k8s_client.V1VolumeMount(name=TMP_VOLUME, mount_path=TMP_MOUNT_PATH),
    ]
    volumes = [
        k8s_client.V1Volume(
            name=SCRIPT_VOLUME,
            secret=k8s_client.V1SecretVolumeSource(secret_name=script_secret),
        ),
        k8s_client.V1Volume(name=TMP_VOLUME, empty_dir=k8s_client.V1EmptyDirVolumeSource()),
    ]
    if provider.volume is not None and provider.mount is not None:
        volume_mounts.append(provider.mount)
        volumes.append(provider.volume)
    # envFrom first, then explicit env so NODERINGS_* always wins over Secret keys.
    return k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
            annotations=annotations,
        ),
        spec=k8s_client.V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=settings.timeout_seconds,
            ttl_seconds_after_finished=settings.ttl_seconds,
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(
                    labels=labels,
                    annotations=annotations,
                ),
                spec=k8s_client.V1PodSpec(
                    restart_policy="Never",
                    automount_service_account_token=False,
                    security_context=k8s_client.V1PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=HOOK_RUN_AS_USER,
                        run_as_group=HOOK_RUN_AS_GROUP,
                        fs_group=HOOK_FS_GROUP,
                        seccomp_profile=k8s_client.V1SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s_client.V1Container(
                            name="hook",
                            image=settings.job_image,
                            image_pull_policy="IfNotPresent",
                            command=[PYTHON_BINARY, SCRIPT_COMMAND],
                            env_from=provider.env_from,
                            env=env
                            if env is not None
                            else [
                                k8s_client.V1EnvVar(name="NODERINGS_HOOK", value=op_type),
                                k8s_client.V1EnvVar(name="NODERINGS_VM_NAME", value=vm_name),
                                k8s_client.V1EnvVar(name="NODERINGS_VM_ID", value=vmid or ""),
                                k8s_client.V1EnvVar(name="NODERINGS_IPV4", value=primary_v4),
                                k8s_client.V1EnvVar(name="NODERINGS_IPV6", value=primary_v6),
                                k8s_client.V1EnvVar(name="NODERINGS_IPV4_PREFIX", value=prefix_v4),
                                k8s_client.V1EnvVar(
                                    name="NODERINGS_IPV4_NETMASK",
                                    value=netmask_from_prefix(prefix_v4),
                                ),
                                k8s_client.V1EnvVar(name="NODERINGS_IPV6_PREFIX", value=prefix_v6),
                                k8s_client.V1EnvVar(
                                    name="NODERINGS_EVENT_JSON",
                                    value=_event_json(op_type, vm_name, vm_namespace, vmid, addrs),
                                ),
                            ],
                            resources=k8s_client.V1ResourceRequirements(
                                limits={
                                    "cpu": settings.cpu_limit,
                                    "memory": settings.mem_limit,
                                },
                                requests={
                                    "cpu": settings.cpu_request,
                                    "memory": settings.mem_request,
                                },
                            ),
                            security_context=k8s_client.V1SecurityContext(
                                allow_privilege_escalation=False,
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                capabilities=k8s_client.V1Capabilities(drop=["ALL"]),
                            ),
                            volume_mounts=volume_mounts,
                        )
                    ],
                    volumes=volumes,
                ),
            ),
        ),
    )


def run_hook_for_operation(
    *,
    hooks: Mapping[str, str] | VMHooks | None,
    op_type: str,
    vm_name: str,
    namespace: str,
    generation: int,
    group: str,
    version: str,
    plural: str,
    kind: str,
    vmid: str | None,
    logger: kopf.Logger,
    addresses: Mapping[str, Any] | None = None,
    core_api: k8s_client.CoreV1Api | None = None,
    batch_api: k8s_client.BatchV1Api | None = None,
    settings: HookSettings | None = None,
    env: list[k8s_client.V1EnvVar] | None = None,
) -> str | None:
    """Create an at-most-once Job for this VM operation. Returns the job name or None."""
    cfg = settings or load_hook_settings()
    if not cfg.enabled:
        return None
    script = _script_for(hooks, op_type)
    if script is None:
        return None

    core = core_api or k8s_client.CoreV1Api()
    batch = batch_api or k8s_client.BatchV1Api()
    job_ns = cfg.namespace_for(namespace)
    name = job_name(namespace, vm_name, op_type, generation)
    labels = _labels(op_type, generation)
    annotations = _annotations(
        vm_name=vm_name,
        namespace=namespace,
        group=group,
        version=version,
        plural=plural,
        kind=kind,
        op_type=op_type,
    )

    try:
        batch.read_namespaced_job(name, job_ns)
        logger.info("hook job %s already exists; skipping", name)
        return name
    except ApiException as exc:
        if exc.status != 404:
            raise

    try:
        if _count_active_jobs(batch, job_ns, cfg) >= cfg.max_concurrent:
            raise HookConcurrencyError(f"hook concurrency cap ({cfg.max_concurrent}) reached")
    except HookConcurrencyError:
        raise
    except ApiException as exc:
        logger.warning("failed to count hook jobs: %s", exc)

    secret = k8s_client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=k8s_client.V1ObjectMeta(
            name=name,
            namespace=job_ns,
            labels=labels,
            annotations=annotations,
        ),
        type="Opaque",
        string_data={SCRIPT_FILENAME: script},
    )
    try:
        core.create_namespaced_secret(job_ns, secret)
    except ApiException as exc:
        if exc.status != 409:
            raise

    job = _job_spec(
        name=name,
        namespace=job_ns,
        vm_namespace=namespace,
        script_secret=name,
        op_type=op_type,
        vm_name=vm_name,
        vmid=vmid,
        labels=labels,
        annotations=annotations,
        settings=cfg,
        addresses=addresses,
        env=env,
    )
    created = None
    try:
        created = batch.create_namespaced_job(job_ns, job)
    except ApiException as exc:
        if exc.status == 409:
            logger.info("hook job %s already exists; skipping", name)
            return name
        try:
            core.delete_namespaced_secret(name, job_ns)
        except ApiException as cleanup_exc:
            logger.warning(
                "failed to delete hook secret %s after job create error: %s", name, cleanup_exc
            )
        raise

    job_uid = created.metadata.uid if created.metadata else None
    if job_uid:
        try:
            current = core.read_namespaced_secret(name, job_ns)
            current.metadata.owner_references = [
                k8s_client.V1OwnerReference(
                    api_version="batch/v1",
                    kind="Job",
                    name=name,
                    uid=job_uid,
                    controller=False,
                    block_owner_deletion=False,
                )
            ]
            core.patch_namespaced_secret(name, job_ns, current)
        except ApiException as exc:
            logger.warning("failed to set secret ownerReference for %s: %s", name, exc)

    logger.info("dispatched hook job %s for %s", name, op_type)
    return name


def safe_run_hook(
    *,
    hooks: Mapping[str, str] | VMHooks | None,
    op_type: str,
    meta: dict[str, Any],
    status: dict[str, Any] | None,
    logger: kopf.Logger,
    group: str,
    version: str,
    plural: str,
    kind: str,
    vmid: str | None = None,
    addresses: Mapping[str, Any] | None = None,
    patch: kopf.Patch | None = None,
) -> None:
    """Fire a hook without failing the VM operation.

    ``HookConcurrencyError`` is re-raised so the handler can retry via
    ``kopf.TemporaryError``. Other failures are logged and swallowed.
    """
    try:
        status = dict(status or {})
        if patch is not None:
            status.update(dict(patch.status or {}))
        addrs = merge_addresses(addresses_from_status(status), addresses)
        run_hook_for_operation(
            hooks=hooks,
            op_type=op_type,
            vm_name=str(meta.get("name") or ""),
            namespace=str(meta.get("namespace") or "default"),
            generation=int(meta.get("generation") or 0),
            group=group,
            version=version,
            plural=plural,
            kind=kind,
            vmid=vmid
            if vmid is not None
            else str(status.get("vmId") or status.get("serverId") or ""),
            logger=logger,
            addresses=addrs,
        )
    except HookConcurrencyError:
        raise
    except Exception as exc:  # noqa: BLE001 — never fail the VM op
        logger.warning("lifecycle hook dispatch failed for %s: %s", op_type, exc)


def dispatch_hook(
    *,
    hooks: Mapping[str, str] | VMHooks | None,
    op_type: str,
    meta: dict[str, Any],
    status: dict[str, Any] | None,
    logger: kopf.Logger,
    group: str,
    version: str,
    plural: str,
    kind: str,
    vmid: str | None = None,
    addresses: Mapping[str, Any] | None = None,
    patch: kopf.Patch | None = None,
) -> None:
    """Dispatch a hook; retry the handler when the concurrency cap is hit."""
    try:
        safe_run_hook(
            hooks=hooks,
            op_type=op_type,
            meta=meta,
            status=status,
            logger=logger,
            group=group,
            version=version,
            plural=plural,
            kind=kind,
            vmid=vmid,
            addresses=addresses,
            patch=patch,
        )
    except HookConcurrencyError as exc:
        raise kopf.TemporaryError(str(exc), delay=HOOK_CONCURRENCY_RETRY_SECONDS) from exc


def gameserver_hook_env(
    op_type: str,
    name: str,
    gameserver_id: str,
    game_ip: str,
    game_port: str,
) -> list[k8s_client.V1EnvVar]:
    """Environment for a game server hook. VM variables are not set."""
    payload = json.dumps(
        {
            "hook": op_type,
            "name": name,
            "id": gameserver_id,
            "ip": game_ip,
            "port": game_port,
        }
    )
    pairs = {
        "NODERINGS_HOOK": op_type,
        "NODERINGS_EVENT_JSON": payload,
        "NODERINGS_GAMESERVER_NAME": name,
        "NODERINGS_GAMESERVER_ID": gameserver_id,
        "NODERINGS_GAME_IP": game_ip,
        "NODERINGS_GAME_PORT": game_port,
    }
    return [k8s_client.V1EnvVar(name=key, value=value) for key, value in pairs.items()]


def dispatch_gameserver_hook(
    *,
    hooks: Mapping[str, str] | None,
    op_type: str,
    meta: dict[str, Any],
    status: dict[str, Any] | None,
    logger: kopf.Logger,
    name: str,
    gameserver_id: str,
    game_ip: str,
    game_port: str,
) -> None:
    """Start a game server hook Job. A missing script is a no-op. Failures are logged."""
    try:
        run_hook_for_operation(
            hooks=hooks,
            op_type=op_type,
            vm_name=name,
            namespace=str(meta.get("namespace") or "default"),
            generation=int(meta.get("generation") or 0),
            group="gs.pterodactyl.com",
            version="v1alpha1",
            plural="pterodactylservers",
            kind="PterodactylServer",
            vmid=gameserver_id,
            logger=logger,
            env=gameserver_hook_env(op_type, name, gameserver_id, game_ip, game_port),
        )
    except HookConcurrencyError as exc:
        raise kopf.TemporaryError(str(exc), delay=HOOK_CONCURRENCY_RETRY_SECONDS) from exc
    except Exception as exc:  # noqa: BLE001 — a hook must not fail the game server operation
        logger.warning("gameserver hook dispatch failed for %s: %s", op_type, exc)
    _ = status


def last_hook_patch(op_type: str, state: str, job_name_value: str, message: str) -> dict[str, Any]:
    return {
        "lastHook": {
            "operation": op_type,
            "state": state,
            "ranAt": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "jobName": job_name_value,
        }
    }
