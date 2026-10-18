"""Kopf handlers for VirtFusionVM resources — VM state machine (README §26)."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import kopf
import kubernetes

from ..client import VirtFusionClient
from ..config import VirtFusionCredentials, load_credentials
from ..constants import (
    GROUP,
    OP_CREATED,
    OP_DELETED,
    OP_EXTRA_IPV4,
    OP_PACKAGE_CHANGE,
    OP_PASSWORD,
    OP_REINSTALL,
    OP_REINSTALLED,
    OP_REMOVE_IPV4,
    OP_RESTARTED,
    OP_STARTED,
    OP_STOPPED,
    OP_SUSPENDED,
    OP_UNSUSPEND,
    PHASE_ERROR,
    PHASE_PENDING,
    PHASE_READY,
    TRANSIENT_RETRY_DELAY_SECONDS,
    VERSION,
)
from ..errors import TransientError, VirtFusionHTTPError
from ..queue import is_noop_power_failure, poll_queue
from ..servers import (
    build_server,
    build_server_with_userdata,
    change_package,
    create_server,
    delete_server,
    modify_resources,
    network_status_from_server,
    power_action,
    wait_for_assigned_ipv4,
    reset_password,
    server_is_built,
    suspend_server,
    unsuspend_server,
    add_ipv4_from_pool,
    sync_remove_ipv4,
)
from ..sshkeys import ensure_ssh_keys
from ..smtp_outbound import reconcile_vm_smtp_outbound_block
from ..status import set_status
from ..types import CONTROL_PLURAL, NODE_PLURAL, VM_KIND, VM_PLURAL, VirtFusionVMSpec
from ..user_api import VirtFusionUserClient
from ..user_tokens import ensure_user_api_token
from noderings_hooks.runner import dispatch_hook

logger = logging.getLogger(__name__)

VM_AUTH_SECRET_FMT = "vm-{uuid}-auth"
VM_CR_NAME_PREFIX = "virtfusionvm-"
# Liqo reflection re-fires kopf on.create; never call VF resetPassword on that path
# more often than this, or a missing secret plus guest-agent lag will hammer the API.
_AUTH_SECRET_RETRY_INTERVAL_SECONDS = 60.0
# Liqo can apply the VM CR before vm-{uuid}-auth. Wait so Instant App / password
# userData is not skipped for a Global /build.
AUTH_SECRET_WAIT_SECONDS = 20.0
AUTH_SECRET_WAIT_INTERVAL_SECONDS = 1.0
_auth_secret_last_attempt: dict[str, float] = {}

# Create already skips when phase is Ready (see _handle_created). Action ops
# (restart/start/stop/…) must run once per spec generation, then skip: Liqo
# strips kopf progress annotations so every reflection looks like on.create.
# Liqo re-fires on.create; skip once per spec generation like the PVE operator.
_GENERATION_SCOPED_OPS = frozenset(
    {
        OP_STARTED,
        OP_STOPPED,
        OP_RESTARTED,
        OP_PASSWORD,
        OP_SUSPENDED,
        OP_UNSUSPEND,
        OP_REINSTALL,
        OP_REINSTALLED,
        OP_PACKAGE_CHANGE,
        OP_EXTRA_IPV4,
        OP_REMOVE_IPV4,
    }
)


def should_skip_generation_scoped_op(
    op_type: str,
    status: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    requested_at: str | None = None,
) -> bool:
    """True when this restart/start/stop was already handled (Liqo/kopf re-create).

    Restart is an action, not a desired state. Re-applying type=restarted does
    not bump metadata.generation, so skip by spec.operation.requestedAt when
    present. Fall back to observedGeneration for older CRs.
    """
    if op_type not in _GENERATION_SCOPED_OPS or not status:
        return False
    if status.get("phase") not in (PHASE_READY, PHASE_PENDING):
        return False
    if requested_at:
        observed_at = status.get("observedRequestedAt")
        return bool(observed_at) and observed_at == requested_at
    if not meta:
        return False
    current_gen = meta.get("generation")
    observed_gen = status.get("observedGeneration")
    if current_gen is None or observed_gen is None or observed_gen != current_gen:
        return False
    return True


def _vm_uuid_from_meta(meta: dict[str, Any]) -> str:
    name = str(meta.get("name") or "")
    if name.startswith(VM_CR_NAME_PREFIX):
        return name[len(VM_CR_NAME_PREFIX) :]
    return name


def _guest_admin_user(spec: VirtFusionVMSpec) -> str:
    user = (spec.guestAdminUser or "").strip()
    return user or "root"


def _linux_password_userdata(spec: VirtFusionVMSpec, password: str | None) -> bool:
    """True when the create-form password can be set via User API userData.

    All Linux guests (Ubuntu, CentOS, Alma, Debian, Rocky, …) use this path.
    Windows still uses Global API + resetPassword.
    """
    if not password:
        return False
    return _guest_admin_user(spec).lower() != "administrator"


def _decode_secret_key(data: dict[str, str] | None, key: str) -> str | None:
    if not isinstance(data, dict) or not data.get(key):
        return None
    try:
        return base64.b64decode(data[key]).decode("utf-8")
    except Exception:
        return None


def _read_auth_secret(namespace: str, vm_uuid: str) -> tuple[str | None, str | None]:
    """Read (password, userdata) from vm-{uuid}-auth. Never log the values."""
    secret_name = VM_AUTH_SECRET_FMT.format(uuid=vm_uuid)
    core_api = kubernetes.client.CoreV1Api()
    try:
        sec = core_api.read_namespaced_secret(secret_name, namespace)
    except kubernetes.client.ApiException as e:
        if e.status == 404:
            return None, None
        raise
    data = getattr(sec, "data", None)
    return _decode_secret_key(data, "password"), _decode_secret_key(data, "userdata")


def _wait_for_auth_secret(
    namespace: str,
    vm_uuid: str,
    log: kopf.Logger,
) -> tuple[str | None, str | None]:
    """Read vm-{uuid}-auth, retrying briefly so Liqo can catch up with the Secret."""
    password, userdata = _read_auth_secret(namespace, vm_uuid)
    if password or userdata:
        return password, userdata
    if AUTH_SECRET_WAIT_SECONDS <= 0:
        return password, userdata
    deadline = time.monotonic() + AUTH_SECRET_WAIT_SECONDS
    log.info("waiting up to %.0fs for VM auth secret %s", AUTH_SECRET_WAIT_SECONDS, vm_uuid)
    while time.monotonic() < deadline:
        time.sleep(max(0.05, AUTH_SECRET_WAIT_INTERVAL_SECONDS))
        password, userdata = _read_auth_secret(namespace, vm_uuid)
        if password or userdata:
            log.info("VM auth secret became available for %s", vm_uuid)
            return password, userdata
    log.warning("VM auth secret not found after wait; building without User API userData")
    return None, None


def _auth_secret_present(namespace: str, vm_uuid: str) -> bool:
    """True when vm-{uuid}-auth exists (password and/or userdata). Never log values."""
    secret_name = VM_AUTH_SECRET_FMT.format(uuid=vm_uuid)
    core_api = kubernetes.client.CoreV1Api()
    try:
        core_api.read_namespaced_secret(secret_name, namespace)
    except kubernetes.client.ApiException as e:
        if e.status == 404:
            return False
        raise
    return True


def _ensure_auth_secret_if_missing(
    client: VirtFusionClient,
    server_id: int,
    spec: VirtFusionVMSpec,
    namespace: str,
    vm_uuid: str,
    log: kopf.Logger,
) -> None:
    """Store VF expectedPassword when create skipped Ready before qemu-guest-agent worked."""
    if _auth_secret_present(namespace, vm_uuid):
        return
    now = time.monotonic()
    last = _auth_secret_last_attempt.get(vm_uuid, 0.0)
    if now - last < _AUTH_SECRET_RETRY_INTERVAL_SECONDS:
        return
    _auth_secret_last_attempt[vm_uuid] = now
    try:
        password = reset_password(client, server_id, user=_guest_admin_user(spec))
        _store_password_secret(namespace, vm_uuid, password)
        log.info("stored guest auth secret for VM %s", vm_uuid)
    except VirtFusionHTTPError as e:
        log.warning("resetPassword for missing auth secret failed, will retry: %s", e)


def _publish_vm_status(
    namespace: str,
    name: str,
    phase: str,
    message: str,
    server_id: int | None = None,
) -> None:
    """Write status immediately so control-plane watchers see progress during a long handler."""
    if not name:
        return
    body: dict[str, Any] = {"status": {"phase": phase, "message": message}}
    if server_id is not None:
        body["status"]["serverId"] = server_id
    api = kubernetes.client.CustomObjectsApi()
    try:
        api.patch_namespaced_custom_object_status(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=VM_PLURAL,
            name=name,
            body=body,
        )
    except Exception:
        api.patch_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=VM_PLURAL,
            name=name,
            body=body,
        )


def _store_password_secret(namespace: str, vm_uuid: str, password: str) -> None:
    """Write expectedPassword into vm-{uuid}-auth (NodeRings template). Never log it."""
    secret_name = VM_AUTH_SECRET_FMT.format(uuid=vm_uuid)
    core_api = kubernetes.client.CoreV1Api()
    patch = {"stringData": {"password": password}}
    try:
        core_api.patch_namespaced_secret(secret_name, namespace, patch)
        return
    except kubernetes.client.ApiException as e:
        if e.status != 404:
            raise
    body = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(name=secret_name, namespace=namespace),
        string_data={"password": password},
        type="Opaque",
    )
    core_api.create_namespaced_secret(namespace, body)


def _resolve_node(node_name: str, namespace: str) -> dict[str, Any]:
    """Read the VirtFusionNode CR and return its spec."""
    api = kubernetes.client.CustomObjectsApi()
    node = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=NODE_PLURAL,
        name=node_name,
    )
    return node.get("spec", {})


def _resolve_endpoint(control_name: str, namespace: str) -> str:
    api = kubernetes.client.CustomObjectsApi()
    ctrl = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=CONTROL_PLURAL,
        name=control_name,
    )
    return ctrl.get("spec", {}).get("endpoint", "")


def _build_client(
    spec: VirtFusionVMSpec, namespace: str
) -> tuple[VirtFusionClient, int, VirtFusionCredentials]:
    """Build a VirtFusion client and return (client, groupId, credentials)."""
    node_spec = _resolve_node(spec.virtfusionNodeRef.name, namespace)
    group_id = node_spec.get("hypervisorGroupId")
    if group_id is None:
        raise ValueError("VirtFusionNode hypervisorGroupId is required")
    control_ref = node_spec.get("controlRef") or {}
    control_name = control_ref.get("name", "") if isinstance(control_ref, dict) else ""
    endpoint = _resolve_endpoint(control_name, namespace)
    creds = load_credentials(endpoint)
    return VirtFusionClient(creds.url, creds.token), int(group_id), creds


def _configured_user_id(creds: VirtFusionCredentials) -> int:
    if creds.user_id < 1:
        raise VirtFusionHTTPError(
            "VIRTFUSION_USER_ID is not set. Create a VirtFusion client user and "
            "pass its numeric id on the agent (VIRTFUSION_USER_ID).",
            status_code=400,
        )
    return creds.user_id


def _needs_user_api_build(
    spec: VirtFusionVMSpec,
    password: str | None,
    extra_user_data: str | None,
) -> bool:
    """True when build must go through User API userData (password and/or Instant App)."""
    if extra_user_data and extra_user_data.strip():
        return True
    return _linux_password_userdata(spec, password)


def _server_uuid_from(server: dict[str, Any] | None, fallback: str | None = None) -> str:
    if not server:
        return fallback or ""
    data = server.get("data", server)
    if isinstance(data, dict):
        return str(data.get("uuid") or fallback or "")
    return fallback or ""


def _apply_user_api_build(
    client: VirtFusionClient,
    spec: VirtFusionVMSpec,
    creds: VirtFusionCredentials,
    *,
    server_id: int,
    server_uuid: str,
    ssh_key_ids: list[int],
    password: str,
    extra_user_data: str | None,
    log: kopf.Logger,
    reinstall: bool = False,
) -> None:
    """Build (or rebuild) via User API userData (chosen password and/or Instant App)."""
    token = ensure_user_api_token(creds)
    user_client = VirtFusionUserClient(client.url, token)
    log.info(
        "building VF server %s with User API userData reinstall=%s",
        server_id,
        reinstall,
    )
    build_server_with_userdata(
        client,
        user_client,
        server_id,
        server_uuid,
        os_id=spec.operatingSystemId,
        password=password,
        guest_user=_guest_admin_user(spec),
        hostname=spec.hostname,
        name=spec.name,
        ssh_key_ids=ssh_key_ids or None,
        ipv6=bool(spec.ipv6),
        extra_user_data=extra_user_data,
        reinstall=reinstall,
    )


def _reconcile_smtp_safe(
    client: VirtFusionClient,
    spec: VirtFusionVMSpec,
    server_id: int | None,
    log: kopf.Logger,
) -> None:
    """Apply or remove the SMTP-named VF firewall ruleset. Never fail the VM op."""
    if not server_id:
        return
    try:
        reconcile_vm_smtp_outbound_block(
            client,
            int(server_id),
            bool(spec.blockOutboundSmtp),
            log,
        )
    except Exception as e:
        log.warning("[SMTP] reconcile failed for server %s: %s", server_id, e)


def _handle_created(
    client: VirtFusionClient,
    spec: VirtFusionVMSpec,
    group_id: int,
    creds: VirtFusionCredentials,
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    vm_uuid: str,
    meta: dict[str, Any],
    log: kopf.Logger,
) -> bool:
    """Full create flow: server (VF assigns IPv4) → ssh → build → poll → password → GET."""

    # Idempotent: already Ready with serverId. Liqo re-fires create; still backfill
    # vm-{uuid}-auth when qemu-guest-agent was too slow for the first resetPassword.
    if status.get("phase") == PHASE_READY and status.get("serverId"):
        _ensure_auth_secret_if_missing(
            client,
            int(status["serverId"]),
            spec,
            namespace,
            vm_uuid,
            log,
        )
        _reconcile_smtp_safe(client, spec, status.get("serverId"), log)
        log.debug("VM already Ready (serverId=%s), skipping create", status["serverId"])
        return False

    user_id = _configured_user_id(creds)

    # Step 4: Create server (if not yet created)
    server_id = status.get("serverId")
    server_uuid = status.get("uuid")
    if not server_id:
        server_data = create_server(
            client,
            package_id=spec.packageId,
            user_id=user_id,
            hypervisor_id=group_id,
            cpu_cores=spec.cpuCores,
            memory_mb=spec.memoryMb,
            disk_gb=spec.diskGb,
            network_speed_in=spec.networkSpeedInKBs,
            network_speed_out=spec.networkSpeedOutKBs,
        )
        server_id = int(server_data["id"])
        server_uuid = server_data.get("uuid", "")
        set_status(
            patch,
            PHASE_PENDING,
            server_id=server_id,
            uuid=server_uuid,
            observed_generation=meta.get("generation"),
        )

    # Extra IPv4 is OP_EXTRA_IPV4 via ipv4Qty (VF IP block). Do not exact-POST
    # spec.ipv4 — NodeRings-picked addresses are not in the hypervisor pool.

    # Step 6: SSH keys belong to the provider's service user
    ssh_key_ids = ensure_ssh_keys(client, user_id, spec.sshPublicKeys or [])

    desired_password, extra_user_data = _wait_for_auth_secret(namespace, vm_uuid, log)
    use_userdata = _needs_user_api_build(spec, desired_password, extra_user_data)

    existing = client.get_server(int(server_id))
    if not server_uuid:
        server_uuid = _server_uuid_from(existing, server_uuid)

    if server_is_built(existing):
        log.info("VF server %s already built, skipping build", server_id)
    elif use_userdata:
        if not server_uuid:
            raise VirtFusionHTTPError(f"No server UUID for User API build of {server_id}")
        _apply_user_api_build(
            client,
            spec,
            creds,
            server_id=int(server_id),
            server_uuid=server_uuid,
            ssh_key_ids=ssh_key_ids,
            password=desired_password or "",
            extra_user_data=extra_user_data,
            log=log,
        )
    else:
        # Step 7: Build (Global API — no chosen password / no client user-data)
        queue_id = build_server(
            client,
            server_id,
            os_id=spec.operatingSystemId,
            hostname=spec.hostname,
            name=spec.name,
            ssh_key_ids=ssh_key_ids or None,
            ipv6=bool(spec.ipv6),
        )

        # Step 8: Poll queue
        if queue_id:
            set_status(
                patch,
                PHASE_PENDING,
                queue_id=queue_id,
                server_id=server_id,
                uuid=server_uuid,
                observed_generation=meta.get("generation"),
            )
            poll_queue(client, queue_id)

    # Step 9: Persist guest password. UserData already set the chosen one — do not
    # call resetPassword (VF would replace it with expectedPassword).
    if use_userdata and desired_password:
        _store_password_secret(namespace, vm_uuid, desired_password)
    elif not use_userdata:
        try:
            password = reset_password(client, server_id, user=_guest_admin_user(spec))
            _store_password_secret(namespace, vm_uuid, password)
        except VirtFusionHTTPError as e:
            # Nested qemu-guest-agent often fails password set; SSH keys are already on the guest.
            log.warning("resetPassword failed after build, continuing: %s", e)

    if spec.cpuThrottlePercent is not None:
        try:
            modify_resources(
                client,
                int(server_id),
                cpu_throttle_percent=spec.cpuThrottlePercent,
            )
        except VirtFusionHTTPError as e:
            log.warning("cpuThrottlePercent apply failed, continuing: %s", e)

    # Step 10: GET server for hypervisor, IPs, gateway, DNS (wait so created hooks see IPv4)
    server_info = wait_for_assigned_ipv4(client, int(server_id)) or client.get_server(server_id)
    hyp_id = None
    if server_info:
        data = server_info.get("data", server_info)
        hyp = data.get("hypervisor", {})
        hyp_id = hyp.get("id") if isinstance(hyp, dict) else None
        if not server_uuid:
            server_uuid = data.get("uuid", "")

    set_status(
        patch,
        PHASE_READY,
        message="VM created and built",
        observed_generation=meta.get("generation"),
        server_id=server_id,
        uuid=server_uuid,
        hypervisor_id=hyp_id,
        **network_status_from_server(server_info),
    )
    _reconcile_smtp_safe(client, spec, server_id, log)
    return True


def _run_power(client: VirtFusionClient, server_id: int, action: str, log: kopf.Logger) -> None:
    qid = power_action(client, server_id, action)
    if not qid:
        return
    try:
        poll_queue(client, qid)
    except VirtFusionHTTPError as e:
        if is_noop_power_failure(e, action):
            log.info("VF power %s on server %s already in desired state", action, server_id)
            return
        raise


def _handle_operation(
    spec: VirtFusionVMSpec,
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    log: kopf.Logger,
    op_type: str,
) -> None:
    namespace = meta.get("namespace", "default")
    vm_uuid = _vm_uuid_from_meta(meta)
    generation = meta.get("generation")
    requested_at = spec.operation.requestedAt if spec.operation else None

    def stamp(phase: str, **kwargs: Any) -> None:
        set_status(
            patch,
            phase,
            observed_generation=generation,
            observed_requested_at=requested_at,
            **kwargs,
        )

    try:
        client, group_id, creds = _build_client(spec, namespace)
        server_id = status.get("serverId")

        if op_type == OP_CREATED:
            _handle_created(
                client,
                spec,
                group_id,
                creds,
                status,
                patch,
                namespace,
                vm_uuid,
                meta,
                log,
            )
            dispatch_hook(
                hooks=spec.hooks,
                op_type=op_type,
                meta=meta,
                status=status,
                logger=log,
                group=GROUP,
                version=VERSION,
                plural=VM_PLURAL,
                kind=VM_KIND,
                patch=patch,
            )
            return

        # Restart is an action, not a steady state. Skip by requestedAt when
        # present (repeat Restart keeps type=restarted). Else once per generation
        # (PVE operator reconcile). Liqo re-fires on.create with the same spec.
        if should_skip_generation_scoped_op(op_type, status, meta, requested_at):
            log.info(
                "Skipping %s — already handled requestedAt=%s generation %s (phase=%s, serverId=%s)",
                op_type,
                requested_at,
                generation,
                status.get("phase"),
                status.get("serverId"),
            )
            _reconcile_smtp_safe(client, spec, server_id, log)
            dispatch_hook(
                hooks=spec.hooks,
                op_type=op_type,
                meta=meta,
                status=status,
                logger=log,
                group=GROUP,
                version=VERSION,
                plural=VM_PLURAL,
                kind=VM_KIND,
                vmid=str(status.get("serverId") or server_id or ""),
                patch=patch,
            )
            return

        if not server_id:
            if op_type == OP_DELETED:
                stamp(PHASE_READY, message="VM deleted")
                return
            raise VirtFusionHTTPError(f"No serverId in status for operation {op_type}")

        if op_type in (OP_STARTED, OP_STOPPED, OP_RESTARTED):
            node_spec = _resolve_node(spec.virtfusionNodeRef.name, namespace)
            if node_spec.get("maintenanceMode"):
                log.info("Node maintenanceMode; skip power probe")
                return

        if op_type == OP_STARTED:
            _run_power(client, server_id, "boot", log)
            stamp(
                PHASE_READY,
                message="VM started",
                server_id=server_id,
                **network_status_from_server(client.get_server(server_id)),
            )

        elif op_type == OP_STOPPED:
            _run_power(client, server_id, "shutdown", log)
            stamp(
                PHASE_READY,
                message="VM stopped",
                server_id=server_id,
                **network_status_from_server(client.get_server(server_id)),
            )

        elif op_type == OP_RESTARTED:
            _run_power(client, server_id, "restart", log)
            stamp(
                PHASE_READY,
                message="VM restarted",
                server_id=server_id,
                **network_status_from_server(client.get_server(server_id)),
            )

        elif op_type in (OP_REINSTALL, OP_REINSTALLED):
            try:
                _publish_vm_status(
                    namespace,
                    meta.get("name", ""),
                    PHASE_PENDING,
                    "VM reinstalling",
                    server_id=int(server_id),
                )
            except Exception as e:
                log.warning("failed to publish reinstalling status: %s", e)
            user_id = _configured_user_id(creds)
            ssh_key_ids = ensure_ssh_keys(client, user_id, spec.sshPublicKeys or [])
            desired_password, extra_user_data = _wait_for_auth_secret(namespace, vm_uuid, log)
            use_userdata = _needs_user_api_build(spec, desired_password, extra_user_data)
            if use_userdata:
                existing = client.get_server(int(server_id))
                server_uuid = _server_uuid_from(existing, status.get("uuid"))
                if not server_uuid:
                    raise VirtFusionHTTPError(
                        f"No server UUID for User API reinstall of {server_id}"
                    )
                _apply_user_api_build(
                    client,
                    spec,
                    creds,
                    server_id=int(server_id),
                    server_uuid=server_uuid,
                    ssh_key_ids=ssh_key_ids,
                    password=desired_password or "",
                    extra_user_data=extra_user_data,
                    log=log,
                    reinstall=True,
                )
                if desired_password:
                    _store_password_secret(namespace, vm_uuid, desired_password)
            else:
                queue_id = build_server(
                    client,
                    server_id,
                    os_id=spec.operatingSystemId,
                    hostname=spec.hostname,
                    name=spec.name,
                    ssh_key_ids=ssh_key_ids or None,
                    reinstall=True,
                    ipv6=bool(spec.ipv6),
                )
                if queue_id:
                    poll_queue(client, queue_id)
                try:
                    password = reset_password(client, server_id, user=_guest_admin_user(spec))
                    _store_password_secret(namespace, vm_uuid, password)
                except VirtFusionHTTPError as e:
                    # Nested qemu-guest-agent often fails password set after rebuild; SSH keys
                    # were already posted on /build. Same continue-on-fail as create.
                    log.warning("resetPassword failed after reinstall, continuing: %s", e)
            stamp(
                PHASE_READY,
                message="VM reinstalled",
                server_id=server_id,
                **network_status_from_server(client.get_server(server_id)),
            )

        elif op_type == OP_PASSWORD:
            password = reset_password(client, server_id, user=_guest_admin_user(spec))
            _store_password_secret(namespace, vm_uuid, password)
            stamp(PHASE_READY, message="Password reset", server_id=server_id)

        elif op_type == OP_SUSPENDED:
            suspend_server(client, server_id)
            stamp(PHASE_READY, message="VM suspended", server_id=server_id)

        elif op_type == OP_UNSUSPEND:
            unsuspend_server(client, server_id)
            stamp(PHASE_READY, message="VM unsuspended", server_id=server_id)

        elif op_type == OP_DELETED:
            delete_server(client, server_id)
            stamp(PHASE_READY, message="VM deleted")

        elif op_type == OP_EXTRA_IPV4:
            add_ipv4_from_pool(client, server_id, quantity=1)
            stamp(
                PHASE_READY,
                message="IPv4 added",
                server_id=server_id,
                **network_status_from_server(client.get_server(server_id)),
            )

        elif op_type == OP_REMOVE_IPV4:
            sync_remove_ipv4(client, server_id, spec.ipv4)
            stamp(
                PHASE_READY,
                message="IPv4 removed",
                server_id=server_id,
                **network_status_from_server(client.get_server(server_id)),
            )

        elif op_type == OP_PACKAGE_CHANGE:
            change_package(client, server_id, spec.packageId)
            modify_resources(
                client,
                server_id,
                cpu_cores=spec.cpuCores,
                memory_mb=spec.memoryMb,
                cpu_throttle_percent=spec.cpuThrottlePercent,
            )
            stamp(PHASE_READY, message="Package changed", server_id=server_id)

        else:
            raise VirtFusionHTTPError(f"Unknown operation type: {op_type}")

        dispatch_hook(
            hooks=spec.hooks,
            op_type=op_type,
            meta=meta,
            status=status,
            logger=log,
            group=GROUP,
            version=VERSION,
            plural=VM_PLURAL,
            kind=VM_KIND,
            vmid=str(status.get("serverId") or server_id or ""),
            patch=patch,
        )

        if op_type != OP_DELETED:
            _reconcile_smtp_safe(client, spec, server_id, log)

    except VirtFusionHTTPError as e:
        retryable = isinstance(e, TransientError) or not (
            e.status_code and 400 <= e.status_code < 500 and e.status_code not in (429, 423)
        )
        if retryable:
            set_status(patch, PHASE_PENDING, message=str(e))
            raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
        set_status(patch, PHASE_ERROR, message=str(e))
        raise kopf.PermanentError(str(e)) from e
    except Exception as e:
        set_status(patch, PHASE_PENDING, message=str(e))
        raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e


@kopf.on.create(GROUP, VERSION, VM_PLURAL)
def on_vm_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    desired = VirtFusionVMSpec.model_validate(spec)
    op_type = desired.operation.type if desired.operation else OP_CREATED
    _handle_operation(desired, meta, status, patch, logger, op_type)


@kopf.on.update(GROUP, VERSION, VM_PLURAL)
def on_vm_update(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    desired = VirtFusionVMSpec.model_validate(spec)
    if not desired.operation:
        logger.info("No operation specified, skipping update")
        return
    _handle_operation(desired, meta, status, patch, logger, desired.operation.type)


@kopf.on.delete(GROUP, VERSION, VM_PLURAL)
def on_vm_delete(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    server_id = status.get("serverId")
    if not server_id:
        logger.info("No serverId in status, nothing to delete from VirtFusion")
        return
    desired = VirtFusionVMSpec.model_validate(spec)
    namespace = meta.get("namespace", "default")
    try:
        vf_client, _group_id, _creds = _build_client(desired, namespace)
        delete_server(vf_client, int(server_id))
    except Exception as e:
        logger.warning("Failed to delete VF server %s: %s", server_id, e)
        return
    dispatch_hook(
        hooks=desired.hooks,
        op_type=OP_DELETED,
        meta=meta,
        status=status,
        logger=logger,
        group=GROUP,
        version=VERSION,
        plural=VM_PLURAL,
        kind=VM_KIND,
        vmid=str(server_id),
        patch=patch,
    )
