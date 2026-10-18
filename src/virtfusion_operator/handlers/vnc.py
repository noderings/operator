"""Kopf handlers for VirtFusionVNCRequest resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf
import kubernetes

from ..client import VirtFusionClient
from ..config import load_credentials
from ..constants import GROUP, TRANSIENT_RETRY_DELAY_SECONDS, VERSION
from ..errors import TransientError, VirtFusionHTTPError
from ..servers import get_vnc_details
from ..types import CONTROL_PLURAL, NODE_PLURAL, VM_PLURAL, VNC_PLURAL, VirtFusionVNCRequestSpec

logger = logging.getLogger(__name__)


def _resolve_server_id(vm_name: str, namespace: str) -> tuple[int, str]:
    """Look up VirtFusionVM → status.serverId and control endpoint."""
    api = kubernetes.client.CustomObjectsApi()
    vm = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=VM_PLURAL,
        name=vm_name,
    )
    server_id = vm.get("status", {}).get("serverId")
    if not server_id:
        raise VirtFusionHTTPError(f"VirtFusionVM {vm_name} has no serverId")

    node_name = vm.get("spec", {}).get("virtfusionNodeRef", {}).get("name", "")
    node = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=NODE_PLURAL,
        name=node_name,
    )
    control_name = node.get("spec", {}).get("controlRef", {}).get("name", "")
    ctrl = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=CONTROL_PLURAL,
        name=control_name,
    )
    endpoint = ctrl.get("spec", {}).get("endpoint", "")
    return int(server_id), endpoint


@kopf.on.create(GROUP, VERSION, VNC_PLURAL)
@kopf.on.update(GROUP, VERSION, VNC_PLURAL)
def on_vnc_request(
    spec: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    parsed = VirtFusionVNCRequestSpec.model_validate(spec)
    namespace = meta.get("namespace", "default")

    try:
        server_id, endpoint = _resolve_server_id(parsed.vmRef.name, namespace)
        creds = load_credentials(endpoint)
        client = VirtFusionClient(creds.url, creds.token)

        vnc = get_vnc_details(client, server_id)

        patch.status["ready"] = True
        patch.status["hypervisorIp"] = vnc["ip"]
        patch.status["port"] = vnc["port"]
        patch.status["password"] = vnc["password"]
        logger.info("VNC ready for server %s at %s:%s", server_id, vnc["ip"], vnc["port"])

    except VirtFusionHTTPError as e:
        patch.status["ready"] = False
        patch.status["error"] = str(e)
        if isinstance(e, TransientError):
            raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
        raise kopf.PermanentError(str(e)) from e
    except Exception as e:
        patch.status["ready"] = False
        patch.status["error"] = str(e)
        raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
