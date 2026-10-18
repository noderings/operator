"""Utility functions for the Proxmox operator."""

import base64
import re
from typing import TYPE_CHECKING, Any, Optional

import kopf
from kubernetes import client as k8s_client

from .constants import MEMORY_UNITS

if TYPE_CHECKING:
    from .client import ProxmoxClient
    from .types import ProxmoxVMSpec

# Module-level cache for Kubernetes API client
# This avoids creating new clients on every maintenance mode check
_k8s_api_client: Optional[k8s_client.ApiClient] = None
_k8s_custom_api: Optional[k8s_client.CustomObjectsApi] = None
_k8s_core_api: Optional[k8s_client.CoreV1Api] = None


def _get_k8s_client() -> tuple[k8s_client.ApiClient, k8s_client.CustomObjectsApi]:
    """Get or create cached Kubernetes API client.

    Returns:
        Tuple of (ApiClient, CustomObjectsApi)
    """
    global _k8s_api_client, _k8s_custom_api

    if _k8s_api_client is None or _k8s_custom_api is None:
        _k8s_api_client = k8s_client.ApiClient()
        _k8s_custom_api = k8s_client.CustomObjectsApi(_k8s_api_client)

    return _k8s_api_client, _k8s_custom_api


def _get_k8s_core_api() -> k8s_client.CoreV1Api:
    """Get or create cached Kubernetes CoreV1Api client.

    Returns:
        CoreV1Api instance
    """
    global _k8s_api_client, _k8s_core_api

    if _k8s_api_client is None:
        _k8s_api_client = k8s_client.ApiClient()

    if _k8s_core_api is None:
        _k8s_core_api = k8s_client.CoreV1Api(_k8s_api_client)

    return _k8s_core_api


def resolve_vmid_str_from_vm_cr(vm_cr: dict[str, Any]) -> str | None:
    """Resolve Proxmox VMID string from a ProxmoxVM CR (status first, then spec)."""
    status = vm_cr.get("status") or {}
    raw = status.get("vmId") or status.get("vmid")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    vm_config = (vm_cr.get("spec") or {}).get("vmConfig") or {}
    raw = vm_config.get("vmId")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return None


def parse_memory_to_mb(memory: str) -> int:
    """
    Parse memory string to megabytes (MB).

    Supports formats:
    - Plain integers: "4096" (assumed to be MB)
    - With units: "2G", "2048M", "2048MB", "2Gi", "2048Mi"

    Args:
        memory: Memory value as string

    Returns:
        Memory in megabytes as integer

    Raises:
        ValueError: If memory format is invalid
    """
    memory = memory.strip()

    # Try plain integer first (assumed to be MB)
    if memory.isdigit():
        return int(memory)

    # Parse with units
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]i?B?)$", memory, re.IGNORECASE)
    if not match:
        raise ValueError(
            f"Invalid memory format: '{memory}'. "
            f"Expected format: '4096' (MB) or '2G', '2048M', '2Gi', etc."
        )

    value = float(match.group(1))
    unit = match.group(2).upper()

    if unit not in MEMORY_UNITS:
        raise ValueError(f"Unknown memory unit: {unit}")

    # Convert to MB using the conversion factor
    conversion_factor = MEMORY_UNITS[unit]
    return int(value * conversion_factor)


def should_skip_operation(
    client: "ProxmoxClient",
    desired: "ProxmoxVMSpec",
    op_type: str,
    logger,
    status: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """
    Check if operation should be skipped based on actual Proxmox VM status (idempotency check).

    Args:
        client: Proxmox client instance
        desired: Desired VM specification
        op_type: Operation type to check
        logger: Logger instance

    Returns:
        True if the operation should be skipped because the VM is already in the
        desired state according to Proxmox.
    """
    from .constants import (
        OP_CREATED,
        OP_DELETED,
        OP_REINSTALLED,
        OP_RESTARTED,
        OP_STARTED,
        OP_STOPPED,
        PHASE_DELETING,
        PHASE_PROVISIONING,
        PHASE_READY,
        VM_STATUS_RUNNING,
        VM_STATUS_STOPPED,
    )

    logger.info("Checking idempotency for operation: %s", op_type)
    node = desired.vmConfig.nodeName
    expected_name = desired.vmConfig.name

    if op_type == OP_REINSTALLED and status is not None and meta is not None:
        current_gen = meta.get("generation", 0)
        observed_gen = status.get("observedGeneration")
        phase = status.get("phase")
        if phase in (PHASE_DELETING, PHASE_PROVISIONING) and observed_gen == current_gen:
            logger.info(
                "Reinstall in progress (phase=%s) for %s, skipping duplicate handler",
                phase,
                expected_name,
            )
            return True
        if observed_gen == current_gen and phase == PHASE_READY:
            vmid = client.find_vm_by_name(node, expected_name)
            if vmid is not None:
                logger.info(
                    "Reinstall already complete for %s (VMID %s), skipping",
                    expected_name,
                    vmid,
                )
                return True

    # For create operations, check if VM already exists by name
    if op_type == OP_CREATED:
        vmid = client.find_vm_by_name(node, expected_name)
        if vmid is not None:
            logger.info(
                "VM with name '%s' (VMID: %s) already exists on node %s, skipping creation",
                expected_name,
                vmid,
                node,
            )
            return True
        # VM doesn't exist, proceed with creation
        return False

    # For other operations, find VM by name
    vmid = client.find_vm_by_name(node, expected_name)
    if vmid is None:
        logger.debug(
            "VM with name '%s' not found on node %s for %s operation, cannot check status",
            expected_name,
            node,
            op_type,
        )
        return False

    # For delete operations, check if VM exists by name
    if op_type == OP_DELETED:
        vmid = client.find_vm_by_name(node, expected_name)
        if vmid is None:
            logger.info(
                "VM with name '%s' does not exist on node %s, skipping deletion",
                expected_name,
                node,
            )
            return True
        # VM exists - proceed with deletion
        return False

    # Get actual VM status from Proxmox
    try:
        vm_status = client.get_vm_status(node, vmid)
        logger.info(
            "VM %s (name: %s) current status in Proxmox: %s (checking for operation: %s)",
            vmid,
            expected_name,
            vm_status,
            op_type,
        )
    except Exception as e:
        logger.warning(
            "Failed to get VM %s (name: %s) status from Proxmox: %s, proceeding with operation",
            vmid,
            expected_name,
            e,
        )
        return False

    # Check if VM is already in the desired state
    if op_type == OP_STARTED:
        if vm_status == VM_STATUS_RUNNING:
            logger.info(
                "VM %s (name: %s) is already running on node %s, skipping start operation",
                vmid,
                expected_name,
                node,
            )
            return True

    elif op_type == OP_STOPPED:
        if vm_status == VM_STATUS_STOPPED:
            logger.info(
                "VM %s (name: %s) is already stopped on node %s (status: %s), skipping stop operation",
                vmid,
                expected_name,
                node,
                vm_status,
            )
            return True

    elif op_type == OP_RESTARTED:
        # For restart, we typically want to execute it if VM is running
        # But if VM is already stopped or paused, we might want to skip
        # However, restart is an action that should always execute if requested
        # So we don't skip based on status - let it proceed
        logger.debug("Restart operation requested, proceeding regardless of current status")
        return False

    elif op_type == OP_DELETED:
        # Already handled above - if VM doesn't exist with matching name, skip deletion
        # If we reach here, VM exists with matching name, so proceed with deletion
        return False

    # If we get here, the VM is not in the desired state, so proceed with operation
    return False


def check_maintenance_mode(
    spec: "ProxmoxVMSpec",
    meta: dict[str, Any],
    logger: kopf.Logger,
) -> bool:
    """Check whether the owning ProxmoxNode is in maintenance mode.

    The authoritative source is the ProxmoxNode CR referenced by
    ``spec.proxmoxNodeRef.name``. If that lookup fails (404, transient API
    error, malformed CR) we conservatively fall back to the legacy
    ``vm.proxmox.com/node-maintenance-mode`` annotation if present, so a
    detached Proxmox/control-plane outage cannot silently force reconciliation.

    Args:
        spec: Parsed ProxmoxVMSpec for the VM being reconciled.
        meta: Kubernetes metadata dictionary.
        logger: Kopf logger instance.

    Returns:
        True if maintenance mode is enabled, False otherwise.
    """
    annotations = meta.get("annotations", {}) or {}

    def _annotation_fallback() -> bool:
        return annotations.get("vm.proxmox.com/node-maintenance-mode", "false").lower() == "true"

    node_name = spec.proxmoxNodeRef.name
    namespace = meta.get("namespace", "default")

    try:
        _, custom_api = _get_k8s_client()
        node_cr = custom_api.get_namespaced_custom_object(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace=namespace,
            plural="proxmoxnodes",
            name=node_name,
        )
        node_spec = node_cr.get("spec", {})
        if not isinstance(node_spec, dict):
            raise ValueError(f"ProxmoxNode spec is not a dict: {type(node_spec)}")
        maintenance_mode = bool(node_spec.get("maintenanceMode", False))
        logger.debug("Maintenance mode from ProxmoxNode %s: %s", node_name, maintenance_mode)
        return maintenance_mode
    except k8s_client.ApiException as e:
        if e.status == 404:
            logger.debug(
                "ProxmoxNode %s not found (404), falling back to maintenance annotation",
                node_name,
            )
        else:
            logger.warning(
                "Failed to get ProxmoxNode %s for maintenance check (status %s): %s. "
                "Falling back to annotation.",
                node_name,
                e.status,
                e.reason,
            )
        return _annotation_fallback()
    except (ValueError, KeyError) as e:
        logger.warning(
            "Invalid ProxmoxNode %s structure: %s. Falling back to annotation.",
            node_name,
            e,
        )
        return _annotation_fallback()
    except Exception as e:
        logger.warning(
            "Unexpected error getting ProxmoxNode %s: %s. Falling back to annotation.",
            node_name,
            e,
        )
        return _annotation_fallback()


def read_secret(namespace: str, name: str, logger: kopf.Logger) -> dict[str, str] | None:
    """
    Read a Kubernetes Secret and return its data as a dictionary of keys and values.

    The function reads all keys in the Secret's data field and returns
    a dictionary mapping each key to its base64-decoded value.

    Args:
        namespace: Kubernetes namespace where the Secret exists
        name: Name of the Secret
        logger: Logger instance for error logging

    Returns:
        A dictionary mapping secret keys to their decoded string values, or None if the Secret
        doesn't exist or an error occurred
    """
    try:
        core_api = _get_k8s_core_api()
        secret = core_api.read_namespaced_secret(name=name, namespace=namespace)

        if not secret.data or len(secret.data) == 0:
            logger.warning("Secret %s/%s has no data", namespace, name)
            return None

        # Decode all keys and values
        decoded_data = {}
        for key, encoded_value in secret.data.items():
            try:
                decoded_value = base64.b64decode(encoded_value).decode("utf-8")
                decoded_data[key] = decoded_value
            except Exception as e:
                logger.error(
                    "Failed to decode Secret %s/%s key '%s': %s",
                    namespace,
                    name,
                    key,
                    e,
                )
                # Continue with other keys even if one fails
                continue

        if not decoded_data:
            logger.error("Failed to decode any keys from Secret %s/%s", namespace, name)
            return None

        logger.debug(
            "Successfully read Secret %s/%s with %d key(s): %s",
            namespace,
            name,
            len(decoded_data),
            list(decoded_data.keys()),
        )
        return decoded_data

    except k8s_client.ApiException as e:
        if e.status == 404:
            logger.warning("Secret %s/%s not found", namespace, name)
        else:
            logger.error(
                "Failed to read Secret %s/%s (status %s): %s",
                namespace,
                name,
                e.status,
                e.reason,
            )
        return None
    except Exception as e:
        logger.error("Unexpected error reading Secret %s/%s: %s", namespace, name, e)
        return None


def read_auth_secret(
    namespace: str, name: str, logger: kopf.Logger
) -> tuple[str, str | list[str]] | None:
    """
    Read a Kubernetes Secret containing authentication data.

    The Secret must contain exactly one of the following auth keys:
    - 'userdata': CloudInit user data as a string
    - 'sshkeys': SSH public keys as a newline-separated string
    - 'hashed_passwd': SHA-512 crypt hash ($6$...) for cloud-init hashed_passwd / vm.hashed_passwd
    - 'password': legacy alias for hashed_passwd (accepted for older secrets)

    Returns:
        Tuple of (auth_type, auth_data) where:
        - auth_type is one of: 'userdata', 'sshkeys', 'hashed_passwd'
        - auth_data is the decoded data (string for userdata/hashed_passwd, list[str] for sshkeys)
        Returns None if the Secret doesn't exist or an error occurred

    Raises:
        ValueError: If the Secret contains multiple auth keys or no valid keys
    """
    # Use read_secret to get all keys and values
    secret_data = read_secret(namespace, name, logger)
    if secret_data is None:
        return None

    # Check which auth keys are present (other Secret keys are ignored)
    has_userdata = "userdata" in secret_data
    has_sshkeys = "sshkeys" in secret_data
    has_hashed_passwd = "hashed_passwd" in secret_data
    has_legacy_password = "password" in secret_data
    if has_hashed_passwd and has_legacy_password:
        logger.error(
            "Auth Secret %s/%s contains both hashed_passwd and legacy password keys",
            namespace,
            name,
        )
        raise ValueError(
            f"Auth Secret {namespace}/{name} contains both hashed_passwd and legacy password keys"
        )
    has_passwd_auth = has_hashed_passwd or has_legacy_password

    key_count = sum([has_userdata, has_sshkeys, has_passwd_auth])

    if key_count == 0:
        logger.error(
            "Auth Secret %s/%s does not contain userdata, sshkeys, or hashed_passwd",
            namespace,
            name,
        )
        return None
    elif key_count > 1:
        logger.error(
            "Auth Secret %s/%s contains multiple keys - only one of userdata, sshkeys, or hashed_passwd is allowed",
            namespace,
            name,
        )
        raise ValueError(
            f"Auth Secret {namespace}/{name} contains multiple keys - only one of userdata, sshkeys, or hashed_passwd is allowed"
        )

    # Extract and return the appropriate data
    if has_userdata:
        userdata = secret_data["userdata"]
        if not userdata.strip():
            logger.error("Auth Secret %s/%s contains empty userdata", namespace, name)
            return None
        logger.debug("Successfully read userdata from Auth Secret %s/%s", namespace, name)
        return ("userdata", userdata)

    elif has_sshkeys:
        ssh_keys_str = secret_data["sshkeys"].strip()
        if not ssh_keys_str:
            logger.error("Auth Secret %s/%s contains empty sshkeys", namespace, name)
            return None
        # Split by newlines and filter out empty strings
        ssh_keys = [key.strip() for key in ssh_keys_str.split("\n") if key.strip()]
        if len(ssh_keys) == 0:
            logger.error("Auth Secret %s/%s contains no valid SSH keys", namespace, name)
            return None
        logger.debug(
            "Successfully read %d SSH keys from Auth Secret %s/%s", len(ssh_keys), namespace, name
        )
        return ("sshkeys", ssh_keys)

    elif has_passwd_auth:
        # SHA-512 crypt hash from backend; exposed to Jinja as vm.hashed_passwd.
        key_name = "hashed_passwd" if has_hashed_passwd else "password"
        password = secret_data[key_name].strip()
        if not password:
            logger.error("Auth Secret %s/%s contains empty %s", namespace, name, key_name)
            return None
        logger.debug(
            "Successfully read hashed_passwd from Auth Secret %s/%s (key=%s)",
            namespace,
            name,
            key_name,
        )
        return ("hashed_passwd", password)

    # This should never be reached due to the key_count check above, but included for completeness
    return None


def read_configmap(namespace: str, name: str, logger: kopf.Logger) -> str | None:
    """
    Read a Kubernetes ConfigMap and return its data as a string.

    The function reads the first key in the ConfigMap's data field and returns
    its value. If the ConfigMap has multiple keys, only the first one is returned.

    Args:
        namespace: Kubernetes namespace where the ConfigMap exists
        name: Name of the ConfigMap
        logger: Logger instance for error logging

    Returns:
        The content of the ConfigMap as a string, or None if the ConfigMap
        doesn't exist or an error occurred
    """
    try:
        core_api = _get_k8s_core_api()
        configmap = core_api.read_namespaced_config_map(name=name, namespace=namespace)

        if not configmap.data or len(configmap.data) == 0:
            logger.warning("ConfigMap %s/%s has no data", namespace, name)
            return None

        # Get the first key's value (ConfigMaps typically have one key for cloudInit data)
        first_key = list(configmap.data.keys())[0]
        data = configmap.data[first_key]

        logger.debug("Successfully read ConfigMap %s/%s (key: %s)", namespace, name, first_key)
        return data

    except k8s_client.ApiException as e:
        if e.status == 404:
            logger.warning("ConfigMap %s/%s not found", namespace, name)
        else:
            logger.error(
                "Failed to read ConfigMap %s/%s (status %s): %s",
                namespace,
                name,
                e.status,
                e.reason,
            )
        return None
    except Exception as e:
        logger.error("Unexpected error reading ConfigMap %s/%s: %s", namespace, name, e)
        return None
