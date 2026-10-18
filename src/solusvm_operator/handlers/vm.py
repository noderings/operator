"""Kopf handlers for SolusVMVM resources — VM state machine (README §26)."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import kopf
import kubernetes
from kubernetes.client.rest import ApiException

from ..client import SolusVMClient, unwrap_data
from ..config import load_credentials
from ..constants import (
    GROUP,
    OP_CREATED,
    OP_DELETED,
    OP_EXTRA_IPV4,
    OP_PASSWORD,
    OP_PLAN_CHANGE,
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
from ..errors import SolusVMHTTPError, TransientError
from ..guest_userdata import prepare_solusvm_user_data
from ..projects import resolve_project
from ..servers import (
    add_ips,
    change_hostname,
    compute_resource_id_from_server,
    create_server,
    custom_plan_from_spec,
    delete_ips,
    delete_server,
    parse_dns_servers,
    parse_ipv4,
    parse_ipv4_gateway,
    parse_ipv4_ordered,
    parse_ipv4_prefix,
    parse_ipv4_set,
    parse_ipv6_gateway,
    parse_ipv6_prefix,
    parse_ipv6_primary,
    parse_mac,
    reinstall_server,
    require_not_processing,
    reset_password,
    resize_server,
    restart_server,
    resume_server,
    start_server,
    stop_server,
    suspend_server,
)
from ..sshkeys import ensure_ssh_keys
from ..status import set_status
from ..types import CONTROL_PLURAL, NODE_PLURAL, VM_KIND, VM_PLURAL, SolusVMVMSpec
from ..users import ensure_user, generate_panel_password
from noderings_hooks.runner import dispatch_hook

logger = logging.getLogger(__name__)

VM_AUTH_SECRET_FMT = "vm-{uuid}-auth"
VM_CR_NAME_PREFIX = "solusvmvm-"
AUTH_SECRET_WAIT_SECONDS = 20.0
AUTH_SECRET_WAIT_INTERVAL_SECONDS = 1.0
# Create already skips when phase is Ready (see _handle_created). Action ops
# (restart/start/stop/…) must run once per spec generation, then skip: Liqo
# strips kopf progress annotations so every reflection looks like on.create.
# Skip once per spec generation (same generation-scoped restart rule as VF/PVE).
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
        OP_PLAN_CHANGE,
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


def _expected_ipv4(spec: SolusVMVMSpec) -> set[str]:
    ips: set[str] = set()
    if spec.primaryIP:
        ips.add(spec.primaryIP.strip())
    for ip in spec.additionalIPv4 or []:
        if ip and ip.strip():
            ips.add(ip.strip())
    return ips


def _assert_ipv4_assignment(got: set[str], spec: SolusVMVMSpec) -> None:
    extra = {ip.strip() for ip in (spec.additionalIPv4 or []) if ip and ip.strip()}
    if spec.primaryIP:
        expected = _expected_ipv4(spec)
        if expected and got != expected:
            _fail_ipv4_mismatch(got, expected)
        return
    if extra and not extra.issubset(got):
        _fail_ipv4_mismatch(got, extra)


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
    except ApiException as e:
        if e.status == 404:
            return None, None
        raise
    data = getattr(sec, "data", None)
    return _decode_secret_key(data, "password"), _decode_secret_key(data, "userdata")


def _read_password_secret(namespace: str, vm_uuid: str) -> str | None:
    """Read plaintext password from vm-{uuid}-auth. Never log the value."""
    password, _userdata = _read_auth_secret(namespace, vm_uuid)
    return password


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
    log.warning("VM auth secret not found after wait; continuing with spec/generated password")
    return None, None


def _store_password_secret(namespace: str, vm_uuid: str, password: str) -> None:
    """Write password into vm-{uuid}-auth. Never log it."""
    secret_name = VM_AUTH_SECRET_FMT.format(uuid=vm_uuid)
    core_api = kubernetes.client.CoreV1Api()
    patch = {"stringData": {"password": password}}
    try:
        core_api.patch_namespaced_secret(secret_name, namespace, patch)
        return
    except ApiException as e:
        if e.status != 404:
            raise
    body = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(name=secret_name, namespace=namespace),
        string_data={"password": password},
        type="Opaque",
    )
    core_api.create_namespaced_secret(namespace, body)


def _guest_password(namespace: str, vm_uuid: str) -> str:
    existing = _read_password_secret(namespace, vm_uuid)
    if existing:
        return existing
    password = generate_panel_password()
    _store_password_secret(namespace, vm_uuid, password)
    return password


def _publish_vm_status(
    namespace: str,
    name: str,
    phase: str,
    message: str,
    server_id: int | None = None,
) -> None:
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


def _resolve_node(node_name: str, namespace: str) -> dict[str, Any]:
    api = kubernetes.client.CustomObjectsApi()
    node = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=NODE_PLURAL,
        name=node_name,
    )
    return node


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


def _build_client(spec: SolusVMVMSpec, namespace: str) -> tuple[SolusVMClient, dict[str, Any]]:
    node = _resolve_node(spec.solusvmNodeRef.name, namespace)
    node_spec = node.get("spec", {})
    control_name = node_spec.get("controlRef", {}).get("name", "")
    endpoint = _resolve_endpoint(control_name, namespace)
    creds = load_credentials(endpoint)
    return SolusVMClient(creds.url, creds.token), node


def _os_version(client: SolusVMClient, os_id: int) -> dict[str, Any]:
    result = client.get_os_image_version(os_id)
    data = unwrap_data(result)
    vtype = str(data.get("virtualization_type") or "kvm").lower()
    if vtype != "kvm":
        raise SolusVMHTTPError(
            f"KVM only; OS image version {os_id} is {vtype}",
            status_code=422,
        )
    return data


def _apply_server_status(
    patch: kopf.Patch,
    phase: str,
    message: str,
    server_data: dict[str, Any],
    *,
    server_id: int | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
    task_id: int | None = None,
    observed_generation: int | None = None,
    observed_requested_at: str | None = None,
) -> None:
    sid = server_id or int(server_data.get("id") or 0)
    cr_id = compute_resource_id_from_server(server_data)
    set_status(
        patch,
        phase,
        message=message,
        observed_generation=observed_generation,
        observed_requested_at=observed_requested_at,
        server_id=sid or None,
        uuid=server_data.get("uuid") or "",
        task_id=task_id,
        compute_resource_id=cr_id,
        project_id=project_id,
        user_id=user_id,
        mac_address=parse_mac(server_data),
        ipv4=parse_ipv4_ordered(server_data),
        ipv6=parse_ipv6_primary(server_data) or None,
        ipv4_gateway=parse_ipv4_gateway(server_data) or None,
        ipv6_gateway=parse_ipv6_gateway(server_data) or None,
        ipv4_prefix=parse_ipv4_prefix(server_data) or None,
        ipv6_prefix=parse_ipv6_prefix(server_data) or None,
        dns_servers=parse_dns_servers(server_data) or None,
    )


def _fail_ipv4_mismatch(got: set[str], expected: set[str]) -> None:
    raise SolusVMHTTPError(
        f"IPv4 set {sorted(got)} does not match spec {sorted(expected)}",
        status_code=422,
    )


def _handle_created(
    client: SolusVMClient,
    spec: SolusVMVMSpec,
    node: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    vm_uuid: str,
    log: kopf.Logger,
    *,
    generation: int | None = None,
    requested_at: str | None = None,
) -> bool:
    if status.get("phase") == PHASE_READY and status.get("serverId"):
        log.debug("VM already Ready (serverId=%s), skipping create", status["serverId"])
        return False

    node_spec = node.get("spec", {})
    compute_resource_id = int(node_spec.get("computeResourceId") or spec.computeResourceId)
    location_id = int(node_spec.get("locationId") or spec.locationId)

    os_data = _os_version(client, spec.osImageVersionId)
    ssh_supported = os_data.get("is_ssh_keys_supported")
    if ssh_supported is None:
        ssh_supported = True

    user_id = ensure_user(client, spec.userEmail)
    project_id, send_user = resolve_project(client, user_id)

    ssh_key_ids: list[int] = []
    if ssh_supported and spec.sshPublicKeys:
        ssh_key_ids = ensure_ssh_keys(client, user_id, spec.sshPublicKeys)

    server_id = status.get("serverId")
    if not server_id:
        secret_password, secret_userdata = _wait_for_auth_secret(namespace, vm_uuid, log)
        extra = (spec.userData or secret_userdata or "").strip()
        secret = (secret_password or "").strip()
        # SolusVM POST /servers requires a panel password (min 6). SSH-only
        # creates get a generated value the guest never uses.
        password = secret or _guest_password(namespace, vm_uuid)
        prepared = prepare_solusvm_user_data(password=secret, extra=extra or None)
        hostname = (spec.hostname or "").strip()
        display = (spec.name or hostname or f"NR-{vm_uuid}").strip()
        body: dict[str, Any] = {
            "name": display,
            "fqdns": [hostname] if hostname else [display],
            "password": password,
            "plan": spec.planId,
            "location": location_id,
            "compute_resource": compute_resource_id,
            "project": project_id,
            "os": spec.osImageVersionId,
            "boot_mode": "disk",
        }
        if send_user:
            body["user"] = user_id
        if spec.primaryIP:
            body["primary_ip"] = spec.primaryIP
        if spec.ipv6:
            body["additional_ipv6_count"] = 1
        if ssh_key_ids:
            body["ssh_keys"] = ssh_key_ids
        if prepared:
            # Instant App files/runcmd + sshd drop-in when the client set a password.
            body["user_data"] = prepared
            log.info("POST /servers user_data_bytes=%s", len(prepared))
        custom = custom_plan_from_spec(spec.cpuCores, spec.memoryMb, spec.diskGb)
        if custom:
            body["custom_plan"] = custom

        server_data = create_server(client, body)
        server_id = int(server_data["id"])
        set_status(
            patch,
            PHASE_PENDING,
            message="Server created",
            observed_generation=generation,
            observed_requested_at=requested_at,
            server_id=server_id,
            uuid=server_data.get("uuid") or "",
            user_id=user_id,
            project_id=project_id,
            compute_resource_id=compute_resource_id,
        )

    extra = [ip for ip in (spec.additionalIPv4 or []) if ip]
    if extra:
        current = unwrap_data(client.get_server(int(server_id)))
        have = parse_ipv4_set(current)
        missing = [ip for ip in extra if ip not in have]
        if missing:
            add_ips(client, int(server_id), count=len(missing))
        after = unwrap_data(client.get_server(int(server_id)))
        got = parse_ipv4_set(after)
        _assert_ipv4_assignment(got, spec)

    server_info = unwrap_data(client.get_server(int(server_id)))
    if server_info.get("is_processing"):
        raise TransientError(f"Server {server_id} is_processing", status_code=409)
    if spec.ipv6 and not parse_ipv6_primary(server_info):
        raise TransientError(f"Server {server_id} IPv6 not assigned yet", status_code=409)
    _apply_server_status(
        patch,
        PHASE_READY,
        "VM created",
        server_info,
        server_id=int(server_id),
        user_id=user_id,
        project_id=project_id,
        observed_generation=generation,
        observed_requested_at=requested_at,
    )
    return True


def _sync_extra_ipv4(
    client: SolusVMClient,
    server_id: int,
    spec: SolusVMVMSpec,
    cr_ipv4: list[str] | None = None,
) -> dict[str, Any]:
    """POST one extra IPv4 from the SolusVM block (panel assigns the address).

    NodeRings must not exact-match spec.additionalIPv4 — those addresses come
    from NodeRings IPAM and are not in the compute-resource pool.

    If GET /servers/{id} already has IPv4s missing from CR status (previous
    extra_ipv4 timed out on a pending task), publish those and do not POST
    another address.
    """
    _ = spec
    live = require_not_processing(client, server_id)
    live_ips = parse_ipv4_set(live)
    cr_ips = {str(ip).strip() for ip in (cr_ipv4 or []) if ip}
    already = live_ips - cr_ips
    if already:
        logger.info(
            "Server %s already has IPv4 not on CR %s; skip POST /ips",
            server_id,
            sorted(already),
        )
        return live
    return add_ips(client, server_id, count=1)


def _sync_remove_ipv4(client: SolusVMClient, server_id: int, spec: SolusVMVMSpec) -> None:
    data = require_not_processing(client, server_id)
    keep = _expected_ipv4(spec)
    for item in parse_ipv4(data):
        if item.get("is_primary") and item.get("ip"):
            keep.add(item["ip"])
    to_delete: list[int] = []
    for item in parse_ipv4(data):
        ip = item.get("ip")
        ip_id = item.get("id")
        if ip and ip not in keep and ip_id is not None and not item.get("is_primary"):
            to_delete.append(int(ip_id))
    delete_ips(client, server_id, to_delete)


def _raise_mapped(patch: kopf.Patch, err: Exception) -> None:
    if isinstance(err, SolusVMHTTPError):
        retryable = isinstance(err, TransientError) or err.status_code in (
            409,
            429,
            502,
            503,
        )
        if not retryable and err.status_code in (401, 403):
            set_status(patch, PHASE_ERROR, message="token rejected")
            raise kopf.PermanentError("token rejected") from err
        if not retryable and (
            err.status_code == 422 or (err.status_code and 400 <= err.status_code < 500)
        ):
            set_status(patch, PHASE_ERROR, message=str(err))
            raise kopf.PermanentError(str(err)) from err
        # Retryable: keep Pending so control-plane watchers do not abort on "Error".
        set_status(patch, PHASE_PENDING, message=str(err))
        raise kopf.TemporaryError(str(err), delay=TRANSIENT_RETRY_DELAY_SECONDS) from err
    set_status(patch, PHASE_PENDING, message=str(err))
    raise kopf.TemporaryError(str(err), delay=TRANSIENT_RETRY_DELAY_SECONDS) from err


def _handle_operation(
    spec: SolusVMVMSpec,
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
        client, node = _build_client(spec, namespace)
        node_spec = node.get("spec", {})

        if op_type == OP_CREATED:
            _handle_created(
                client,
                spec,
                node,
                status,
                patch,
                namespace,
                vm_uuid,
                log,
                generation=generation,
                requested_at=requested_at,
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
        # present (repeat Restart keeps type=restarted). Else once per generation.
        # Liqo re-fires on.create with the same spec.
        if should_skip_generation_scoped_op(op_type, status, meta, requested_at):
            log.info(
                "Skipping %s — already handled requestedAt=%s generation %s (phase=%s, serverId=%s)",
                op_type,
                requested_at,
                generation,
                status.get("phase"),
                status.get("serverId"),
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
                vmid=str(status.get("serverId") or ""),
                patch=patch,
            )
            return

        if op_type in (OP_STARTED, OP_STOPPED, OP_RESTARTED) and node_spec.get("maintenanceMode"):
            log.info("Node maintenanceMode; skip power probe")
            return

        server_id = status.get("serverId")

        if not server_id:
            if op_type == OP_DELETED:
                stamp(PHASE_READY, message="VM deleted")
                return
            raise SolusVMHTTPError(f"No serverId in status for operation {op_type}")

        server_id = int(server_id)

        if op_type == OP_STARTED:
            start_server(client, server_id)
            stamp(PHASE_READY, message="VM started", server_id=server_id)

        elif op_type == OP_STOPPED:
            stop_server(client, server_id)
            stamp(PHASE_READY, message="VM stopped", server_id=server_id)

        elif op_type == OP_RESTARTED:
            restart_server(client, server_id)
            stamp(PHASE_READY, message="VM restarted", server_id=server_id)

        elif op_type in (OP_REINSTALL, OP_REINSTALLED):
            try:
                _publish_vm_status(
                    namespace,
                    meta.get("name", ""),
                    PHASE_PENDING,
                    "VM reinstalling",
                    server_id=server_id,
                )
            except Exception as e:
                log.warning("failed to publish reinstalling status: %s", e)
            os_data = _os_version(client, spec.osImageVersionId)
            ssh_supported = os_data.get("is_ssh_keys_supported")
            if ssh_supported is None:
                ssh_supported = True
            user_id = int(status.get("userId") or ensure_user(client, spec.userEmail))
            ssh_key_ids: list[int] = []
            if ssh_supported and spec.sshPublicKeys:
                ssh_key_ids = ensure_ssh_keys(client, user_id, spec.sshPublicKeys)
            secret_password, secret_userdata = _wait_for_auth_secret(namespace, vm_uuid, log)
            extra = (spec.userData or secret_userdata or "").strip()
            secret = (secret_password or "").strip() or _read_password_secret(namespace, vm_uuid)
            password = secret or ""
            prepared = prepare_solusvm_user_data(
                password=secret or "",
                extra=extra or None,
            )
            if prepared:
                log.info("POST /reinstall user_data_bytes=%s", len(prepared))
            reinstall_server(
                client,
                server_id,
                os=spec.osImageVersionId,
                ssh_keys=ssh_key_ids or None,
                user_data=prepared,
                password=password,
            )
            stamp(PHASE_READY, message="VM reinstalled", server_id=server_id)

        elif op_type == OP_PASSWORD:
            raw_user_id = status.get("userId")
            password = reset_password(
                client, server_id, int(raw_user_id) if raw_user_id is not None else None
            )
            _store_password_secret(namespace, vm_uuid, password)
            stamp(PHASE_READY, message="Password reset", server_id=server_id)

        elif op_type == OP_SUSPENDED:
            suspend_server(client, server_id)
            stamp(PHASE_READY, message="VM suspended", server_id=server_id)

        elif op_type == OP_UNSUSPEND:
            resume_server(client, server_id)
            stamp(PHASE_READY, message="VM unsuspended", server_id=server_id)

        elif op_type == OP_DELETED:
            delete_server(client, server_id)
            stamp(PHASE_READY, message="VM deleted")

        elif op_type == OP_EXTRA_IPV4:
            after = _sync_extra_ipv4(
                client,
                server_id,
                spec,
                cr_ipv4=status.get("ipv4"),
            )
            _apply_server_status(
                patch,
                PHASE_READY,
                "IPv4 added",
                after,
                server_id=server_id,
                user_id=status.get("userId"),
                project_id=status.get("projectId"),
                observed_generation=generation,
                observed_requested_at=requested_at,
            )

        elif op_type == OP_REMOVE_IPV4:
            _sync_remove_ipv4(client, server_id, spec)
            after = require_not_processing(client, server_id)
            _apply_server_status(
                patch,
                PHASE_READY,
                "IPv4 removed",
                after,
                server_id=server_id,
                user_id=status.get("userId"),
                project_id=status.get("projectId"),
                observed_generation=generation,
                observed_requested_at=requested_at,
            )

        elif op_type == OP_PLAN_CHANGE:
            custom = custom_plan_from_spec(spec.cpuCores, spec.memoryMb, spec.diskGb)
            resize_server(client, server_id, spec.planId, custom_plan=custom, preserve_disk=True)
            if spec.hostname:
                change_hostname(client, server_id, spec.hostname)
            stamp(PHASE_READY, message="Plan changed", server_id=server_id)

        else:
            raise SolusVMHTTPError(f"Unknown operation type: {op_type}")

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

    except Exception as e:
        _raise_mapped(patch, e)


@kopf.on.create(GROUP, VERSION, VM_PLURAL)
def on_vm_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    desired = SolusVMVMSpec.model_validate(spec)
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
    desired = SolusVMVMSpec.model_validate(spec)
    if not desired.operation:
        logger.info("No operation specified, skipping update")
        return
    _handle_operation(desired, meta, status, patch, logger, desired.operation.type)


@kopf.on.resume(GROUP, VERSION, VM_PLURAL)
def on_vm_resume(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """Publish hypervisor IPv4 that never made it onto CR status (pending add_ips task)."""
    desired = SolusVMVMSpec.model_validate(spec)
    server_id = status.get("serverId")
    if not server_id:
        return
    try:
        client, _node = _build_client(desired, meta.get("namespace", "default"))
        live = unwrap_data(client.get_server(int(server_id)))
        live_ips = parse_ipv4_set(live)
        cr_ips = {str(ip).strip() for ip in (status.get("ipv4") or []) if ip}
        missing = live_ips - cr_ips
        if not missing:
            return
        logger.info("resume: publishing hypervisor IPv4 %s", sorted(missing))
        _apply_server_status(
            patch,
            PHASE_READY,
            status.get("message") or "IPv4 synced",
            live,
            server_id=int(server_id),
            user_id=status.get("userId"),
            project_id=status.get("projectId"),
        )
    except Exception as e:
        logger.warning("resume ipv4 sync skipped: %s", e)


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
        logger.info("No serverId in status, nothing to delete from SolusVM")
        return
    desired = SolusVMVMSpec.model_validate(spec)
    namespace = meta.get("namespace", "default")
    try:
        client, _node = _build_client(desired, namespace)
        delete_server(client, int(server_id))
    except Exception as e:
        logger.warning("Failed to delete SolusVM server %s: %s", server_id, e)
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
