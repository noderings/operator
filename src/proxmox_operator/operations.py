"""Operation handlers for different VM operations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Type

import kopf

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
    VM_CREATE_MAX_RETRIES,
)
from .status import set_status
from .template_compiler import TemplateCompiler, TemplateCompilationError
from .utils import parse_memory_to_mb, read_configmap, read_secret

if TYPE_CHECKING:
    from .client import ProxmoxClient
    from .types import ProxmoxVMSpec

from .client import ProxmoxError, compute_resize_delta


def _parse_vmid_raw(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _vmid_from_status_or_spec(
    spec: "ProxmoxVMSpec", status: dict[str, Any] | None = None
) -> int | None:
    """Prefer status.vmId (operator-allocated); fall back to spec.vmConfig.vmId."""
    if status:
        parsed = _parse_vmid_raw(status.get("vmId") or status.get("vmid"))
        if parsed is not None:
            return parsed
    return _parse_vmid_raw(spec.vmConfig.vmId)


def cleanup_cloudinit_iso(
    client: "ProxmoxClient",
    node: str,
    vm_name: str,
    logger: kopf.Logger,
) -> None:
    """Remove per-VM cloudInit ISO files from Proxmox ISO storage."""
    if not vm_name:
        return
    try:
        n = client.delete_cloudinit_iso_for_vm(node, vm_name)
        if n:
            logger.info(
                "Removed %s cloudInit ISO file(s) for VM %s",
                n,
                vm_name,
            )
    except Exception as e:
        logger.warning(
            "cloudInit ISO cleanup for VM %s failed: %s",
            vm_name,
            e,
        )


class OperationHandler(ABC):
    """Base class for VM operation handlers."""

    def __init__(
        self,
        client: "ProxmoxClient",
        spec: "ProxmoxVMSpec",
        patch: kopf.Patch,
        meta: dict,
        logger: kopf.Logger,
        status: dict[str, Any] | None = None,
    ):
        """Initialize operation handler.

        Args:
            client: Proxmox client instance
            spec: VM specification
            patch: Kopf patch object for status updates
            meta: Kubernetes metadata
            logger: Logger instance
            status: Current CR status (for status.vmId fallback reads)
        """
        self.client = client
        self.spec = spec
        self.patch = patch
        self.meta = meta
        self.logger = logger
        self.status = status or {}
        self.node = spec.vmConfig.nodeName

    @abstractmethod
    def execute(self) -> tuple[str, int]:
        """
        Execute the operation.

        Returns:
            Tuple of (message, vmid)
        """
        pass

    def _get_vmid(self) -> int:
        """Get VMID by finding VM by name, raising error if not found.

        Uses the VM name (UUID) from spec to find the VM on Proxmox,
        then returns its VMID. This allows operations to work without
        requiring vmId in the spec.
        """
        expected_name = self.spec.vmConfig.name
        if not expected_name:
            raise kopf.PermanentError(f"VM name is required for {self.operation_type} operation")

        # Find VM by name
        vmid = self.client.find_vm_by_name(self.node, expected_name)
        if vmid is None:
            raise kopf.PermanentError(
                f"VM with name '{expected_name}' not found on node {self.node} for {self.operation_type} operation"
            )

        return vmid

    @property
    @abstractmethod
    def operation_type(self) -> str:
        """Return the operation type string."""
        pass


class CreateOperationHandler(OperationHandler):
    """Handler for VM creation operations."""

    @dataclass
    class SecretData:
        """Holds authentication data extracted from secrets."""

        auth_user_data: str | None = None
        auth_ssh_keys: list[str] | None = None
        auth_password: str | None = None

    @property
    def operation_type(self) -> str:
        return OP_CREATED

    def _parse_auth_secret_data(
        self, namespace: str, secret_name: str, vmid: int
    ) -> "CreateOperationHandler.SecretData":
        """
        Read a Secret once and extract auth data.

        Returns:
            SecretData with one of auth_user_data/auth_ssh_keys/auth_password populated.
        """
        secret_data = read_secret(namespace, secret_name, self.logger)
        if not secret_data:
            self.logger.warning(
                "Failed to read Secret %s/%s for VM %s", namespace, secret_name, vmid
            )
            return self.SecretData()

        secret_keys = list(secret_data.keys())
        self.logger.info(
            "Secret %s/%s contains keys: %s for VM %s", namespace, secret_name, secret_keys, vmid
        )

        result = self.SecretData()

        # Auth data: allow exactly one of userdata, sshkeys, hashed_passwd
        # (legacy Secret key "password" is accepted as an alias for hashed_passwd).
        has_userdata = "userdata" in secret_data
        has_sshkeys = "sshkeys" in secret_data
        has_hashed_passwd = "hashed_passwd" in secret_data
        has_legacy_password = "password" in secret_data
        if has_hashed_passwd and has_legacy_password:
            raise ValueError(
                f"Auth Secret {namespace}/{secret_name} contains both hashed_passwd and legacy password keys"
            )
        has_passwd_auth = has_hashed_passwd or has_legacy_password
        key_count = sum([has_userdata, has_sshkeys, has_passwd_auth])

        if key_count > 1:
            # Align with read_auth_secret behavior (only one allowed)
            raise ValueError(
                f"Auth Secret {namespace}/{secret_name} contains multiple keys - only one of userdata, sshkeys, or hashed_passwd is allowed"
            )

        if has_userdata:
            result.auth_user_data = secret_data["userdata"]
            if not result.auth_user_data.strip():
                raise ValueError(f"Auth Secret {namespace}/{secret_name} contains empty userdata")
        elif has_sshkeys:
            ssh_keys_str = secret_data["sshkeys"].strip()
            ssh_keys = [key.strip() for key in ssh_keys_str.split("\n") if key.strip()]
            if not ssh_keys:
                raise ValueError(
                    f"Auth Secret {namespace}/{secret_name} contains no valid SSH keys"
                )
            result.auth_ssh_keys = ssh_keys
        elif has_passwd_auth:
            key_name = "hashed_passwd" if has_hashed_passwd else "password"
            result.auth_password = secret_data[key_name].strip()
            if not result.auth_password:
                raise ValueError(f"Auth Secret {namespace}/{secret_name} contains empty {key_name}")

        return result

    def _collect_cloud_init_inputs(
        self, vmid: int, namespace: str
    ) -> tuple[str | None, str | None, list[str] | None, str | None]:
        """
        Collect hostname and auth inputs from cloudInit and its auth secret (if present).

        Returns:
            (hostname, auth_user_data, auth_ssh_keys, auth_password)
        """
        hostname = None
        auth_user_data = None
        auth_ssh_keys = None
        auth_password = None

        if not self.spec.vmConfig.cloudInit:
            return hostname, auth_user_data, auth_ssh_keys, auth_password

        cloud_init = self.spec.vmConfig.cloudInit
        hostname = cloud_init.hostname

        if cloud_init.authSecretRef:
            try:
                secret_info = self._parse_auth_secret_data(
                    namespace, cloud_init.authSecretRef.name, vmid
                )
                auth_user_data = secret_info.auth_user_data
                auth_ssh_keys = secret_info.auth_ssh_keys
                auth_password = secret_info.auth_password
            except (ValueError, kopf.PermanentError) as e:
                self.logger.warning(
                    "Failed to parse auth secret %s/%s for VM %s: %s",
                    namespace,
                    cloud_init.authSecretRef.name,
                    vmid,
                    str(e),
                )

        return hostname, auth_user_data, auth_ssh_keys, auth_password

    def _build_template_context(
        self,
        hostname: str | None,
        ssh_keys: list[str] | None,
        password: str | None,
    ) -> dict[str, Any]:
        """Build template context for Jinja compilation.

        Returns:
            Template context dictionary with vm.name, vm.hostname, vm.ssh_keys,
            vm.hashed_passwd, vm_network.*.

            password is already a SHA-512 crypt hash ($6$...) from the backend auth Secret;
            it is exposed as vm.hashed_passwd for cloud-init hashed_passwd.
        """
        context: dict[str, Any] = {"vm": {"name": self.spec.vmConfig.name}}
        if hostname is not None:
            context["vm"]["hostname"] = hostname
        if ssh_keys is not None:
            context["vm"]["ssh_keys"] = ssh_keys
        if password is not None:
            context["vm"]["hashed_passwd"] = password
        # Always create vm_network dict (even if empty) to avoid template errors when staticIpConfig is not provided
        # This ensures templates can safely check vm_network.* variables without errors
        context["vm_network"] = {}
        # Allocated primary NIC MAC — lets network-data match interfaces by MAC
        # (netplan match.macaddress) instead of hardcoding ens18/enp0s* names
        if self.spec.vmConfig.networks:
            mac = self.spec.vmConfig.networks[0].macAddress
            if mac:
                context["vm_network"]["mac_address"] = mac.lower()
        # Add static IP configuration if available (when static IP is used, not DHCP)
        if self.spec.vmConfig.cloudInit and self.spec.vmConfig.cloudInit.staticIpConfig:
            net_config = self.spec.vmConfig.cloudInit.staticIpConfig
            if net_config.ipv4Address:
                context["vm_network"]["ipv4_address"] = net_config.ipv4Address
            if net_config.ipv4Prefix:
                context["vm_network"]["ipv4_prefix"] = net_config.ipv4Prefix
            if net_config.ipv6Address:
                context["vm_network"]["ipv6_address"] = net_config.ipv6Address
            if net_config.ipv6Prefix:
                context["vm_network"]["ipv6_prefix"] = net_config.ipv6Prefix
            if net_config.ipv4Gateway:
                context["vm_network"]["ipv4_gateway"] = net_config.ipv4Gateway
            if net_config.ipv6Gateway:
                context["vm_network"]["ipv6_gateway"] = net_config.ipv6Gateway
            if net_config.dnsServers:
                context["vm_network"]["dns_servers"] = net_config.dnsServers
        return context

    def _read_and_compile_configmap(
        self, namespace: str, configmap_name: str, vmid: int, template_context: dict, data_type: str
    ) -> str | None:
        """Read and compile ConfigMap data (vendorData or networkData).

        Returns:
            Compiled data string or None if ConfigMap not found
        """
        self.logger.info(
            "Reading %s from ConfigMap %s/%s for VM %s", data_type, namespace, configmap_name, vmid
        )
        data = read_configmap(namespace, configmap_name, self.logger)
        if data is None:
            self.logger.warning(
                "Failed to read %s from ConfigMap %s/%s, continuing without %s",
                data_type,
                namespace,
                configmap_name,
                data_type,
            )
            return None
        self.logger.info(
            "Read %s from ConfigMap %s/%s (length: %d chars), compiling",
            data_type,
            namespace,
            configmap_name,
            len(data),
        )
        return self._compile_cloudinit_data(
            data, template_context, f"{data_type}-{configmap_name}", data_type
        )

    def _compile_cloudinit_data(
        self, data: str, template_context: dict, template_name: str, data_type: str
    ) -> str:
        """Compile CloudInit data with Jinja template.

        Returns:
            Compiled data string
        """
        template_compiler = TemplateCompiler()
        try:
            compiled = template_compiler.compile_if_template(
                data, template_context, template_name=template_name
            )
            self.logger.info("Successfully compiled %s template", template_name)
            return compiled
        except TemplateCompilationError as e:
            self.logger.error("Failed to compile %s template: %s", template_name, e)
            raise kopf.PermanentError(f"Failed to compile {data_type} template: {e}") from e

    def _resize_root_disk_if_needed(self, vmid: int) -> None:
        """Grow the cloned VM's first block disk to disks[0].size (plan target).

        Proxmox cannot shrink disks: when the template disk is already at or
        above the target, resize is skipped.
        """
        disks = self.spec.vmConfig.disks
        if not disks or not disks[0].size:
            return
        target = disks[0].size.strip()

        config = self.client.get_vm_config(self.node, vmid)
        disk_key = self.client.first_block_disk_key(self.node, vmid, config=config)
        current_gib = self.client.get_disk_size_gib(self.node, vmid, disk_key, config=config)
        try:
            delta = compute_resize_delta(current_gib, target)
        except ValueError as e:
            raise kopf.PermanentError(f"Invalid disks[0].size on CR: {e}") from e
        if delta is None:
            self.logger.info(
                "VM %s disk %s already %.1fG >= target %s, skipping resize",
                vmid,
                disk_key,
                current_gib,
                target,
            )
            return
        self.logger.info(
            "Resizing VM %s disk %s by %s (current %.1fG, target %s)",
            vmid,
            disk_key,
            delta,
            current_gib,
            target,
        )
        self.client.resize_disk(self.node, vmid, disk_key, delta)

    def _apply_cpu_settings(self, vmid: int) -> None:
        """Post cpu=/cpulimit=/cpuunits=/affinity= from the CR and always enable numa=."""
        cpu = self.spec.vmConfig.cpu
        self.client.configure_cpu(
            self.node,
            vmid,
            cpu_type=cpu.type,
            cpu_limit=cpu.limit,
            cpu_units=cpu.units,
            affinity=cpu.affinity,
            numa=cpu.numa,
        )

    def _apply_network_settings(self, vmid: int) -> None:
        """Overwrite the primary NIC (networks[0]) inherited from the template.

        Sends the full net device string in one config post: model + allocated
        MAC, bridge, optional VLAN tag, optional rate limit.
        """
        networks = self.spec.vmConfig.networks
        if not networks:
            return
        net = networks[0]
        self.client.configure_net(
            self.node,
            vmid,
            net_name=net.name,
            model=net.model,
            bridge=net.bridge,
            mac_address=net.macAddress,
            vlan_id=net.vlanId,
            rate_limit_mbs=net.rateLimitMBps,
        )

    def execute(self) -> tuple[str, int]:
        """Create and start a new VM."""
        expected_name = self.spec.vmConfig.name

        # Identity is VM name: skip clone if that name already exists (VMID may change).
        existing_vmid = self.client.find_vm_by_name(self.node, expected_name)
        if existing_vmid is not None:
            self.logger.info(
                "VM with name '%s' (VMID: %s) already exists on node %s, skipping creation",
                expected_name,
                existing_vmid,
                self.node,
            )
            return "VM already exists", existing_vmid

        return self._clone_and_configure_vm(provisioning_message="Creating VM")

    def _clone_and_configure_vm(
        self,
        *,
        skip_if_exists: bool = False,
        provisioning_message: str = "Creating VM",
    ) -> tuple[str, int]:
        """Clone template, configure cloud-init, start VM, and write vmId into status."""
        expected_name = self.spec.vmConfig.name

        if skip_if_exists:
            existing_vmid = self.client.find_vm_by_name(self.node, expected_name)
            if existing_vmid is not None:
                self.logger.info(
                    "VM with name '%s' (VMID: %s) already exists on node %s, skipping clone",
                    expected_name,
                    existing_vmid,
                    self.node,
                )
                set_status(
                    self.patch,
                    phase=PHASE_PROVISIONING,
                    message="VM already exists",
                    observed_generation=self.meta.get("generation"),
                    vm_id=str(existing_vmid),
                )
                return "VM already exists", existing_vmid

        # Get storage from first disk configuration (required)
        if not self.spec.vmConfig.disks or len(self.spec.vmConfig.disks) == 0:
            raise kopf.PermanentError(
                "At least one disk configuration with storage is required for VM creation"
            )
        storage = self.spec.vmConfig.disks[0].storage
        if not storage:
            raise kopf.PermanentError(
                "Storage must be specified in disk configuration for clone operation"
            )

        set_status(
            self.patch,
            phase=PHASE_PROVISIONING,
            message=provisioning_message,
            observed_generation=self.meta.get("generation"),
        )

        # Clone template with retry logic for VMID conflicts
        template_id = int(self.spec.vmConfig.templateId)
        vmid_seed = self.spec.vmidSeed
        vmid = None

        for attempt in range(VM_CREATE_MAX_RETRIES):
            try:
                # Get next available VMID for create operations
                vmid = self.client.get_next_vmid(seed=vmid_seed, node=self.node)
                self.logger.info(
                    "Allocated VMID: %s%s (attempt %d/%d)",
                    vmid,
                    f" (from seed {vmid_seed})" if vmid_seed else "",
                    attempt + 1,
                    VM_CREATE_MAX_RETRIES,
                )

                self.logger.info(
                    "Using storage '%s' from disk configuration for clone operation", storage
                )
                self.client.clone_template(
                    node=self.node,
                    template_id=template_id,
                    name=self.spec.vmConfig.name,
                    newid=vmid,
                    storage=storage,
                )
                # Clone succeeded, break out of retry loop
                break

            except ProxmoxError as e:
                error_msg = str(e).lower()
                # Check if error is due to VMID conflict or lock timeout
                # - "config file already exists" or "already exists": VMID already taken
                # - "lock" and "timeout": VMID is locked by another operation (concurrent clone)
                is_vmid_conflict = (
                    "config file already exists" in error_msg
                    or "already exists" in error_msg
                    or ("lock" in error_msg and "timeout" in error_msg)
                )

                if is_vmid_conflict:
                    self.logger.warning(
                        "VMID %s conflict detected (attempt %d/%d): %s. Retrying with new VMID...",
                        vmid,
                        attempt + 1,
                        VM_CREATE_MAX_RETRIES,
                        str(e),
                    )
                    # If this was the last attempt, raise the error
                    if attempt == VM_CREATE_MAX_RETRIES - 1:
                        raise kopf.PermanentError(
                            f"Failed to allocate unique VMID after {VM_CREATE_MAX_RETRIES} attempts. "
                            f"Last error: {e}"
                        ) from e
                    # Continue to next iteration to try with a new VMID
                    continue
                else:
                    # For other errors, raise immediately
                    raise

        # Ensure vmid was successfully allocated
        if vmid is None:
            raise kopf.PermanentError(
                f"Failed to allocate VMID after {VM_CREATE_MAX_RETRIES} attempts"
            )

        # Grow root disk to the plan target (disks[0].size) before any other config
        self._resize_root_disk_if_needed(vmid)

        # Read cloudInit data from Kubernetes resources if specified
        user_data = None
        vendor_data = None
        network_data = None

        # Read auth data from authSecretRef if provided
        namespace = self.meta.get("namespace", "default")
        auth_user_data = None
        auth_ssh_keys = None
        auth_password = None

        # Get hostname and authSecretRef from cloudInit if available
        # All are optional - operator handles missing values gracefully
        (
            hostname,
            auth_user_data,
            auth_ssh_keys,
            auth_password,
        ) = self._collect_cloud_init_inputs(vmid, namespace)

        # Build template context for Jinja compilation
        template_context = self._build_template_context(hostname, auth_ssh_keys, auth_password)
        # Log staticIpConfig explicitly for debugging

        if self.spec.vmConfig.cloudInit:
            cloud_init = self.spec.vmConfig.cloudInit

            # Read and compile userData from Secret if specified
            # If authSecretRef contains userdata, it replaces cloudInit userData
            if cloud_init.userDataSecretRef and not auth_user_data:
                secret_name = cloud_init.userDataSecretRef.name
                secret_data = read_secret(namespace, secret_name, self.logger)
                if secret_data:
                    # Get the first key's value (for backward compatibility with single-key secrets)
                    first_key = list(secret_data.keys())[0]
                    user_data = secret_data[first_key]
                    self.logger.info(
                        "Read userData from Secret %s/%s (key: %s, length: %d chars), compiling",
                        namespace,
                        secret_name,
                        first_key,
                        len(user_data),
                    )
                    user_data = self._compile_cloudinit_data(
                        user_data, template_context, f"userData-{secret_name}", "userData"
                    )
                else:
                    self.logger.warning(
                        "Failed to read userData from Secret %s/%s for VM %s, continuing without userData",
                        namespace,
                        secret_name,
                        vmid,
                    )
            elif auth_user_data:
                self.logger.info(
                    "authSecretRef contains userdata, which replaces cloudInit userDataSecretRef"
                )

            # Read and compile vendorData and networkData if specified
            if cloud_init.vendorDataConfigMapRef:
                vendor_data = self._read_and_compile_configmap(
                    namespace,
                    cloud_init.vendorDataConfigMapRef.name,
                    vmid,
                    template_context,
                    "vendorData",
                )
                if not vendor_data:
                    self.logger.warning(
                        "vendorData is None for VM %s, continuing without vendorData", vmid
                    )
            if cloud_init.networkDataConfigMapRef:
                network_data = self._read_and_compile_configmap(
                    namespace,
                    cloud_init.networkDataConfigMapRef.name,
                    vmid,
                    template_context,
                    "networkData",
                )
                if not network_data:
                    self.logger.warning(
                        "networkData is None for VM %s, continuing without networkData", vmid
                    )

        # Use userData from authSecretRef if provided (replaces cloudInit userData, no template compilation)
        if not user_data and auth_user_data:
            self.logger.info(
                "Using userData from authSecretRef for VM %s (passing as-is without template compilation)",
                vmid,
            )
            user_data = auth_user_data

        # Warn if userData is None when cloudInit is configured
        if self.spec.vmConfig.cloudInit and not user_data:
            self.logger.warning("userData is None for VM %s, continuing without userData", vmid)

        self.client.configure_vm(
            node=self.node,
            vmid=vmid,
            cores=self.spec.vmConfig.cpu.cores,
            memory=parse_memory_to_mb(self.spec.vmConfig.memory),
            description=self.spec.vmConfig.description,
            user_data=user_data,
            vendor_data=vendor_data,
            network_data=network_data,
            cloud_init_config=self.spec.vmConfig.cloudInit,
            vm_config=self.spec.vmConfig,
        )

        # Guest CPU model (node) + cpulimit/cpuunits (plan) from CR
        self._apply_cpu_settings(vmid)

        # Overwrite the primary NIC inherited from the template: bridge, model,
        # VLAN, allocated MAC, and rate limit from CR
        self._apply_network_settings(vmid)

        # Start VM
        self.client.start_vm(self.node, vmid)
        message = "VM created and started"

        # Enable guest firewall options + ensure NIC firewall=1 (needed for ipfilter).
        try:
            self.client.configure_vm_firewall_options(self.node, vmid)
            self.client.set_nic_firewall(self.node, vmid, enabled=True)
        except Exception as e:
            self.logger.warning(
                "Failed to configure firewall options for VM %s: %s. Continuing.", vmid, e
            )

        # Create ipfilter- IPSets
        try:
            self.client.reconcile_ipfilter_ipsets(self.node, vmid, self.spec.vmConfig)
        except Exception as e:
            self.logger.warning(
                "Failed to reconcile ipfilter- IPSets for VM %s: %s. Continuing.", vmid, e
            )

        # Allocated VMID belongs in status (Liqo mirrors status remote→local only).
        set_status(
            self.patch,
            phase=PHASE_PROVISIONING,
            message=message,
            observed_generation=self.meta.get("generation"),
            vm_id=str(vmid),
        )

        return message, vmid


class StartOperationHandler(OperationHandler):
    """Handler for VM start operations."""

    @property
    def operation_type(self) -> str:
        return OP_STARTED

    def execute(self) -> tuple[str, int]:
        """Start a VM."""
        expected_name = self.spec.vmConfig.name
        vmid = self._get_vmid()
        self.logger.info(
            "Starting VM %s (name: %s) for operation: %s", vmid, expected_name, self.operation_type
        )

        set_status(
            self.patch,
            phase=PHASE_READY,
            message=f"Executing {self.operation_type} operation",
            observed_generation=self.meta.get("generation"),
        )

        self.client.start_vm(self.node, vmid)

        try:
            self.client.reconcile_ipfilter_ipsets(self.node, vmid, self.spec.vmConfig)
        except Exception as e:
            self.logger.warning(
                "Failed to reconcile ipfilter- IPSets for VM %s: %s. Continuing.", vmid, e
            )

        return "VM started", vmid


class StopOperationHandler(OperationHandler):
    """Handler for VM stop operations."""

    @property
    def operation_type(self) -> str:
        return OP_STOPPED

    def execute(self) -> tuple[str, int]:
        """Stop a VM."""
        expected_name = self.spec.vmConfig.name
        vmid = self._get_vmid()
        self.logger.info(
            "Stopping VM %s (name: %s) for operation: %s", vmid, expected_name, self.operation_type
        )

        set_status(
            self.patch,
            phase=PHASE_READY,
            message=f"Executing {self.operation_type} operation",
            observed_generation=self.meta.get("generation"),
        )

        self.client.stop_vm(self.node, vmid)
        return "VM stopped", vmid


class RestartOperationHandler(OperationHandler):
    """Handler for VM restart operations."""

    @property
    def operation_type(self) -> str:
        return OP_RESTARTED

    def execute(self) -> tuple[str, int]:
        """Restart a VM."""
        expected_name = self.spec.vmConfig.name
        vmid = self._get_vmid()
        self.logger.info(
            "Restarting VM %s (name: %s) for operation: %s",
            vmid,
            expected_name,
            self.operation_type,
        )

        set_status(
            self.patch,
            phase=PHASE_READY,
            message=f"Executing {self.operation_type} operation",
            observed_generation=self.meta.get("generation"),
        )

        self.client.reset_vm(self.node, vmid)
        return "VM restarted", vmid


class DeleteOperationHandler(OperationHandler):
    """Handler for VM deletion operations."""

    @property
    def operation_type(self) -> str:
        return OP_DELETED

    def execute(self) -> tuple[str, int]:
        """Delete a VM."""
        expected_name = self.spec.vmConfig.name
        force = self.spec.operation.force if self.spec.operation else False

        # Check if VM exists by name first
        vmid = self.client.find_vm_by_name(self.node, expected_name)
        if vmid is None:
            # VM doesn't exist - already deleted, treat as success
            # TODO: Temporarily set to DEBUG - change back to INFO later
            self.logger.debug(
                "VM with name '%s' does not exist on node %s, already deleted",
                expected_name,
                self.node,
            )
            # Still remove orphaned vzdump files if status/spec has the old vmid
            vmid_cleanup = _vmid_from_status_or_spec(self.spec, self.status)
            if vmid_cleanup:
                try:
                    n = self.client.delete_all_backups_for_vmid(self.node, vmid_cleanup)
                    if n:
                        self.logger.info(
                            "Removed %s Proxmox backup file(s) for vmid %s (guest already gone)",
                            n,
                            vmid_cleanup,
                        )
                except Exception as e:
                    self.logger.warning("Proxmox backup cleanup (guest already gone) failed: %s", e)
            cleanup_cloudinit_iso(self.client, self.node, expected_name, self.logger)
            # Return a dummy vmid (0) since VM doesn't exist
            return "VM already deleted", 0
        self.logger.info("Deleting VM %s (name: %s) with force=%s", vmid, expected_name, force)

        set_status(
            self.patch,
            phase=PHASE_DELETING,
            message="Deleting VM",
            observed_generation=self.meta.get("generation"),
        )

        # Proceed with deletion
        self.client.delete_vm(self.node, vmid, force=force)
        try:
            n = self.client.delete_all_backups_for_vmid(self.node, vmid)
            if n:
                self.logger.info("Removed %s Proxmox backup file(s) for deleted vmid %s", n, vmid)
        except Exception as e:
            self.logger.warning("Proxmox backup cleanup after VM delete failed: %s", e)
        cleanup_cloudinit_iso(self.client, self.node, expected_name, self.logger)
        message = f"VM deleted {'(force)' if force else ''}"
        return message, vmid


class ReinstallOperationHandler(CreateOperationHandler):
    """Handler for VM reinstall — delete existing guest then clone fresh template."""

    @property
    def operation_type(self) -> str:
        return OP_REINSTALLED

    def execute(self) -> tuple[str, int]:
        expected_name = self.spec.vmConfig.name
        force = self.spec.operation.force if self.spec.operation else False

        set_status(
            self.patch,
            phase=PHASE_DELETING,
            message="Removing existing VM",
            observed_generation=self.meta.get("generation"),
        )

        vmid = self.client.find_vm_by_name(self.node, expected_name)
        if vmid is not None:
            self.logger.info(
                "Reinstall: deleting existing VM %s (name: %s) with force=%s",
                vmid,
                expected_name,
                force,
            )
            self.client.delete_vm(self.node, vmid, force=force)
            try:
                n = self.client.delete_all_backups_for_vmid(self.node, vmid)
                if n:
                    self.logger.info(
                        "Reinstall: removed %s Proxmox backup file(s) for vmid %s",
                        n,
                        vmid,
                    )
            except Exception as e:
                self.logger.warning("Reinstall: Proxmox backup cleanup failed: %s", e)
            cleanup_cloudinit_iso(self.client, self.node, expected_name, self.logger)
        else:
            self.logger.info(
                "Reinstall: guest %s not found on node %s, cleaning orphans",
                expected_name,
                self.node,
            )
            cleanup_cloudinit_iso(self.client, self.node, expected_name, self.logger)
            stale_vmid = _vmid_from_status_or_spec(self.spec, self.status)
            if stale_vmid:
                try:
                    n = self.client.delete_all_backups_for_vmid(self.node, stale_vmid)
                    if n:
                        self.logger.info(
                            "Reinstall: removed %s orphan backup file(s) for stale vmid %s",
                            n,
                            stale_vmid,
                        )
                except Exception as e:
                    self.logger.warning("Reinstall: stale backup cleanup failed: %s", e)

        message, new_vmid = self._clone_and_configure_vm(
            skip_if_exists=False,
            provisioning_message="Cloning new template",
        )
        return f"VM reinstalled: {message}", new_vmid


def get_operation_handler(
    op_type: str,
    client: "ProxmoxClient",
    spec: "ProxmoxVMSpec",
    patch: kopf.Patch,
    meta: dict,
    logger: kopf.Logger,
    status: dict[str, Any] | None = None,
) -> OperationHandler:
    """
    Factory function to get the appropriate operation handler.

    Args:
        op_type: Operation type
        client: Proxmox client instance
        spec: VM specification
        patch: Kopf patch object
        meta: Kubernetes metadata
        logger: Logger instance
        status: Current CR status (for status.vmId reads)

    Returns:
        Operation handler instance

    Raises:
        kopf.PermanentError: If operation type is unknown
    """
    handlers: dict[str, Type[OperationHandler]] = {
        OP_CREATED: CreateOperationHandler,
        OP_STARTED: StartOperationHandler,
        OP_STOPPED: StopOperationHandler,
        OP_RESTARTED: RestartOperationHandler,
        OP_DELETED: DeleteOperationHandler,
        OP_REINSTALLED: ReinstallOperationHandler,
    }

    handler_class = handlers.get(op_type)
    if handler_class is None:
        raise kopf.PermanentError(f"Unknown operation type: {op_type}")

    # Type narrowing: mypy knows handler_class is not None here
    return handler_class(client, spec, patch, meta, logger, status=status)
