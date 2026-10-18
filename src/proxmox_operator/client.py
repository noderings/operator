"""Proxmox API client for VM operations."""

import io
import ipaddress
import logging
import math
import os
import re
import time
import urllib.parse
from typing import Any

from proxmoxer import ProxmoxAPI

from .config import ProxmoxSettings
from .constants import (
    DEFAULT_STOP_TIMEOUT,
    DEFAULT_TASK_TIMEOUT,
    EXIT_STATUS_OK,
    EXIT_STATUS_STOPPED,
    IPFILTER_IPV4_HOST_PREFIX_LEN,
    IPFILTER_IPV6_HOST_PREFIX_LEN,
    MAX_VMID_SEARCH_ITERATIONS,
    PROXMOX_VM_ABSENT_ERROR_MARKERS,
    TASK_STATUS_CHECK_INTERVAL,
    cloudinit_iso_filename,
    VM_STOP_INITIAL_BACKOFF,
    VM_STOP_MAX_BACKOFF,
    VM_STOP_MAX_RETRIES,
    VM_STOP_SKIPLOCK_THRESHOLD,
    VM_STOP_VERIFICATION_DELAY,
    VM_STATUS_RUNNING,
    VM_STATUS_STOPPED,
    VM_STATUS_UNKNOWN,
)
from .iso_generator import generate_cidata_iso
from .types import StaticIPConfig


logger = logging.getLogger(__name__)


class ProxmoxError(Exception):
    """Exception raised for Proxmox API errors.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _exception_message_chain(exc: BaseException) -> str:
    """Concatenate str() for exc and its causes (lowercased) for robust substring checks."""
    parts: list[str] = []
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    return " ".join(parts).lower()


def _storage_content_absent_on_delete(exc: BaseException) -> bool:
    """True if Proxmox indicates the storage volume is already gone."""
    msg = _exception_message_chain(exc)
    return any(
        needle in msg
        for needle in (
            "does not exist",
            "doesn't exist",
            "not found",
            "no such file",
            "unknown volume",
        )
    )


def _snapshot_absent_on_delete(exc: BaseException) -> bool:
    """True if Proxmox indicates the snapshot is already gone (manual delete in UI, etc.)."""
    msg = _exception_message_chain(exc)
    if "snapshot" not in msg:
        return False
    return any(
        needle in msg
        for needle in (
            "does not exist",
            "doesn't exist",
            "no such snapshot",
            "unknown snapshot",
            "snapshot not found",
            "not found",  # e.g. HTTP 404 bodies mentioning snapshot
        )
    )


_BLOCK_DISK_KEY_RE = re.compile(r"^(virtio|scsi|ide|sata)\d+$")
_DISK_SIZE_RE = re.compile(r"(?:^|,)size=(\d+(?:\.\d+)?)([KMGT]?)", re.IGNORECASE)

_SIZE_UNIT_TO_GIB = {
    "K": 1.0 / (1024 * 1024),
    "M": 1.0 / 1024,
    "G": 1.0,
    "T": 1024.0,
    # Proxmox size= without unit means bytes
    "": 1.0 / (1024 * 1024 * 1024),
}


def first_block_disk_key_from_config(config: dict[str, Any]) -> str | None:
    """First virtioN/scsiN/ideN/sataN key (lexicographic) holding a block disk.

    Skips cdrom (media=cdrom), cloud-init drives, and ISO entries. Returns
    None when the config has no block disk.
    """
    for key in sorted(config):
        if not _BLOCK_DISK_KEY_RE.match(key):
            continue
        value = str(config[key] or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if "media=cdrom" in lowered or "cloudinit" in lowered or ".iso" in lowered:
            continue
        return key
    return None


def parse_disk_size_gib(config_value: str) -> float | None:
    """Parse size= from a Proxmox disk config string into GiB (None if absent)."""
    match = _DISK_SIZE_RE.search(config_value)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).upper()
    return number * _SIZE_UNIT_TO_GIB[unit]


def compute_resize_delta(current_gib: float, target_size: str) -> str | None:
    """Return a "+NG" grow spec toward target_size, or None when no grow needed.

    target_size is the CR disks[0].size value, e.g. "40G". Proxmox cannot
    shrink disks, so a target at or below the current size returns None.
    """
    target_gib = parse_disk_size_gib(f"size={target_size.strip()}")
    if target_gib is None:
        raise ValueError(f"Invalid target disk size {target_size!r}")
    delta = target_gib - current_gib
    if delta <= 0:
        return None
    # Proxmox accepts fractional G but whole numbers are safest across backends
    return f"+{math.ceil(delta)}G"


def _ipv6_filter_cidr(address: str, prefix_len: str) -> str:
    """Delegated-prefix CIDR for ipfilter (e.g. VM /64 block → 2602:f224:0:3::/64)."""
    network = ipaddress.ip_network(f"{address}/{prefix_len}", strict=False)
    return str(network)


def _ipv6_ipfilter_cidr(address: str, prefix_len: str) -> str:
    """CIDR for ipfilter IPSets: host /128 for single-IP mode, delegated prefix otherwise."""
    if str(prefix_len) == str(IPFILTER_IPV6_HOST_PREFIX_LEN):
        return str(
            ipaddress.ip_network(
                f"{address}/{IPFILTER_IPV6_HOST_PREFIX_LEN}",
                strict=False,
            )
        )
    return _ipv6_filter_cidr(address, prefix_len)


def _ipfilter_cidr_key(cidr: str) -> str:
    """Canonical comparison key for an ipfilter CIDR.

    Bare IP (no ``/``) means host prefix (``/32`` or ``/128``). Otherwise the
    written prefix is kept: ``/32`` is ``/32``, ``/31`` is ``/31``, etc.
    """
    value = (cidr or "").strip()
    if not value:
        return value
    try:
        if "/" not in value:
            addr = ipaddress.ip_address(value)
            return str(ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}"))
        return str(ipaddress.ip_network(value, strict=False))
    except (ValueError, ipaddress.AddressValueError):
        return value


class ProxmoxClient:
    """Client for interacting with Proxmox VE API."""

    def __init__(self, settings: ProxmoxSettings):
        """Initialize Proxmox client with settings."""
        self.host = settings.host
        self.username = settings.username
        self.token_name = settings.token_name
        self.token_value = settings.token_value
        self.verify_ssl = settings.verify_ssl
        self.timeout = settings.timeout

        if not all([self.host, self.username, self.token_name, self.token_value]):
            raise ProxmoxError("Missing required Proxmox connection settings")

    def _get_connection(self) -> ProxmoxAPI:
        """Get Proxmox API connection."""
        try:
            return ProxmoxAPI(
                self.host,
                user=self.username,
                token_name=self.token_name,
                token_value=self.token_value,
                verify_ssl=self.verify_ssl,
                timeout=self.timeout,
            )
        except Exception as e:
            raise ProxmoxError(f"Failed to connect to Proxmox: {e}") from e

    def get_next_vmid(self, seed: int | None = None, node: str | None = None) -> int:
        """Get the next available VM ID.

        If seed is provided, finds the next available VMID starting from that seed.
        If node is provided with seed, only checks VMIDs on that specific node.
        Otherwise, uses Proxmox's cluster.nextid API.
        """
        try:
            proxmox = self._get_connection()

            if seed is not None:
                # Get existing VMIDs on the specified node (or all nodes if node is None)
                all_vmids = set()
                if node is not None:
                    # Only check the specific node
                    try:
                        vms = proxmox.nodes(node).qemu.get()
                        for vm in vms:
                            all_vmids.add(int(vm["vmid"]))
                    except Exception as e:
                        raise ProxmoxError(f"Failed to get VMs from node {node}: {e}") from e
                else:
                    # Get all existing VMIDs across all nodes (backward compatibility)
                    nodes = proxmox.nodes.get()
                    for node_item in nodes:
                        try:
                            vms = proxmox.nodes(node_item["node"]).qemu.get()
                            for vm in vms:
                                all_vmids.add(int(vm["vmid"]))
                        except Exception as e:
                            logger.warning(
                                "Failed to get VMs from node %s: %s", node_item["node"], e
                            )

                # Find next available VMID starting from seed
                next_vmid = seed
                max_iterations = MAX_VMID_SEARCH_ITERATIONS
                iteration = 0
                while next_vmid in all_vmids and iteration < max_iterations:
                    next_vmid += 1
                    iteration += 1

                if iteration >= max_iterations:
                    raise ProxmoxError(
                        f"Unable to find available VMID starting from seed {seed} after {max_iterations} iterations"
                    )

                logger.info(
                    "Found next available VMID %s starting from seed %s%s",
                    next_vmid,
                    seed,
                    f" on node {node}" if node else "",
                )
                return next_vmid
            else:
                # Use Proxmox's default nextid API
                next_vmid = proxmox.cluster.nextid.get()
                return int(next_vmid)
        except Exception as e:
            raise ProxmoxError(f"Failed to get next VM ID: {e}") from e

    def vm_exists(self, node: str, vmid: int) -> bool:
        """Check if a VM exists on the specified node.

        Returns:
            True if the VM list includes ``vmid``, False if the list was retrieved
            and the ID is absent

        Raises:
            ProxmoxError: On transport/API failures. Callers must not treat
                these as "does not exist".
        """
        try:
            proxmox = self._get_connection()
            vms = proxmox.nodes(node).qemu.get()
        except ProxmoxError:
            raise
        except Exception as e:
            raise ProxmoxError(f"Failed to check if VM {vmid} exists on node {node}: {e}") from e
        return any(int(vm["vmid"]) == int(vmid) for vm in vms)

    def get_vm_name(self, node: str, vmid: int) -> str | None:
        """Get VM name from Proxmox API.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            VM name if found, None if VM doesn't exist or name is not set

        Raises:
            ProxmoxError: On transport/API failures (not a genuine missing VM).
        """
        try:
            proxmox = self._get_connection()
            config = proxmox.nodes(node).qemu(vmid).config.get()
            vm_name = config.get("name")
            logger.debug("VM %s name: %s", vmid, vm_name)
            return vm_name
        except ProxmoxError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if any(marker in msg for marker in PROXMOX_VM_ABSENT_ERROR_MARKERS):
                logger.debug("VM %s not found on node %s while reading name: %s", vmid, node, e)
                return None
            raise ProxmoxError(f"Failed to get VM {vmid} name on node {node}: {e}") from e

    def find_vm_by_name(self, node: str, name: str) -> int | None:
        """Find a VM by name on the specified node and return its VMID.

        Uses the VM list API which already includes the name field, avoiding
        additional API calls for each VM.

        Args:
            node: Node name
            name: VM name (UUID) to search for

        Returns:
            VMID if found, None if the VM list was retrieved and no name matched

        Raises:
            ProxmoxError: On transport/API failures. Callers must not treat
                these as "not found" — that causes duplicate clones.
        """
        try:
            proxmox = self._get_connection()
            vms = proxmox.nodes(node).qemu.get()
        except ProxmoxError:
            raise
        except Exception as e:
            raise ProxmoxError(f"Failed to find VM by name '{name}' on node {node}: {e}") from e

        for vm in vms:
            if vm.get("name") != name:
                continue
            try:
                vmid = int(vm["vmid"])
            except (KeyError, TypeError, ValueError) as e:
                raise ProxmoxError(
                    f"Invalid VM list entry while finding '{name}' on node {node}: {e}"
                ) from e
            logger.debug("Found VM with name '%s' on node %s: VMID %s", name, node, vmid)
            return vmid
        logger.debug("VM with name '%s' not found on node %s", name, node)
        return None

    def clone_template(
        self, node: str, template_id: int, name: str, newid: int, storage: str
    ) -> None:
        """Clone a template to create a new VM.

        Args:
            node: Proxmox node name
            template_id: Template VM ID to clone from
            name: Name for the new VM
            newid: VM ID for the new VM
            storage: Storage name where cloned disks should be stored (e.g., "local-lvm")
        """
        try:
            proxmox = self._get_connection()
            logger.info(
                "Cloning template %s on node %s -> VM %s (id=%s) to storage %s",
                template_id,
                node,
                name,
                newid,
                storage,
            )

            # Clone the template with storage parameter
            task = (
                proxmox.nodes(node)
                .qemu(template_id)
                .clone.post(
                    newid=newid,
                    name=name,
                    full=1,  # Full clone
                    storage=storage,
                )
            )

            # Wait for clone to complete
            self._wait_for_task(node, task)

        except Exception as e:
            raise ProxmoxError(f"Failed to clone template {template_id}: {e}") from e

    def configure_net(
        self,
        node: str,
        vmid: int,
        *,
        net_name: str,
        model: str,
        bridge: str,
        mac_address: str | None = None,
        vlan_id: int | None = None,
        rate_limit_mbs: float | None = None,
    ) -> None:
        """Set the full net device string for one NIC in a single config post.

        Proxmox expects the whole device value at once, e.g.
        ``net0=virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1,tag=100,rate=12.5``.
        Without a MAC the model is sent alone (``virtio``) and Proxmox
        auto-assigns one. tag= is omitted when untagged, rate= when unlimited.

        ``firewall=1`` is always set so guest firewall and ipfilter anti-spoofing
        apply on the NIC from create time.
        """
        model_part = f"{model}={mac_address}" if mac_address else model
        parts = [model_part, f"bridge={bridge}", "firewall=1"]
        if vlan_id is not None and vlan_id > 0:
            parts.append(f"tag={vlan_id}")
        if rate_limit_mbs is not None and rate_limit_mbs > 0:
            parts.append(f"rate={rate_limit_mbs:g}")
        net_value = ",".join(parts)
        try:
            proxmox = self._get_connection()
            logger.info("Configuring VM %s on node %s: %s=%s", vmid, node, net_name, net_value)
            proxmox.nodes(node).qemu(vmid).config.post(**{net_name: net_value})
        except Exception as e:
            raise ProxmoxError(f"Failed to configure {net_name} for VM {vmid}: {e}") from e

    def configure_cpu(
        self,
        node: str,
        vmid: int,
        *,
        cpu_type: str | None = None,
        cpu_limit: int | None = None,
        cpu_units: int | None = None,
        affinity: str | None = None,
        numa: bool | None = None,
    ) -> None:
        """Apply guest CPU model (cpu=), cpulimit=, cpuunits=, affinity= and numa= from the CR.

        Absent cpu=/cpulimit=/cpuunits=/affinity= are not posted so cloned/template
        defaults survive. Dedicated VMs omit cpulimit and cpuunits entirely (do not
        post 0). cpulimit=0 is posted explicitly when set (Proxmox: unlimited).
        numa=1 is always posted (shared and dedicated) so every VM gets NUMA
        enabled; the ``numa`` argument is accepted for call-site compatibility
        but ignored.
        """
        params: dict[str, Any] = {}
        if cpu_type:
            params["cpu"] = cpu_type
        if cpu_limit is not None:
            params["cpulimit"] = cpu_limit
        if cpu_units is not None:
            params["cpuunits"] = cpu_units
        if affinity:
            params["affinity"] = affinity
        # Always enable Proxmox numa= for shared and dedicated VMs.
        params["numa"] = 1
        try:
            proxmox = self._get_connection()
            logger.info("Configuring VM %s CPU settings on node %s: %s", vmid, node, params)
            proxmox.nodes(node).qemu(vmid).config.post(**params)
        except Exception as e:
            raise ProxmoxError(f"Failed to configure CPU settings for VM {vmid}: {e}") from e

    def get_vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        """Return the Proxmox qm config dict for a VM."""
        try:
            proxmox = self._get_connection()
            return proxmox.nodes(node).qemu(vmid).config.get()
        except Exception as e:
            raise ProxmoxError(f"Failed to read config for VM {vmid}: {e}") from e

    def first_block_disk_key(
        self, node: str, vmid: int, *, config: dict[str, Any] | None = None
    ) -> str:
        """Return the first Proxmox block disk key on a VM (e.g. scsi0, virtio0).

        Reads qm config and returns the first virtioN/scsiN/ideN/sataN key in
        lexicographic order whose value is a block disk (skips cdrom, cloudinit
        and ISO entries). Raises ProxmoxError when the VM has no block disk.
        """
        if config is None:
            config = self.get_vm_config(node, vmid)

        key = first_block_disk_key_from_config(config)
        if key is None:
            raise ProxmoxError(f"No block disk found on VM {vmid} for resize")
        return key

    def get_disk_size_gib(
        self, node: str, vmid: int, disk_key: str, *, config: dict[str, Any] | None = None
    ) -> float:
        """Current size in GiB of the disk at config[disk_key] (parsed from size=)."""
        if config is None:
            config = self.get_vm_config(node, vmid)

        value = config.get(disk_key)
        if not value:
            raise ProxmoxError(f"Disk {disk_key} not found in VM {vmid} config")
        size = parse_disk_size_gib(str(value))
        if size is None:
            raise ProxmoxError(f"Could not parse size from disk {disk_key}={value} on VM {vmid}")
        return size

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        """Grow a VM disk via the Proxmox resize API (shrink is not supported).

        Args:
            node: Proxmox node name
            vmid: VM ID
            disk: Proxmox disk key, e.g. "scsi0"
            size: "+NG" relative grow (recommended) or absolute "NG"
        """
        try:
            proxmox = self._get_connection()
            logger.info("Resizing VM %s disk %s on node %s: size=%s", vmid, disk, node, size)
            result = proxmox.nodes(node).qemu(vmid).resize.put(disk=disk, size=size)
            task = self._extract_task_upid(result)
            if task:
                self._wait_for_task(node, task)
        except Exception as e:
            raise ProxmoxError(f"Failed to resize disk {disk} for VM {vmid}: {e}") from e

    def _generate_cidata_iso(
        self,
        user_data: str | None = None,
        vendor_data: str | None = None,
        network_data: str | None = None,
        vm_name: str | None = None,
        instance_id: str | None = None,
    ) -> bytes:
        """Generate a cidata ISO file in memory containing Cloud-Init configuration.

        Args:
            user_data: CloudInit user data content
            vendor_data: CloudInit vendor data content
            network_data: CloudInit network data content
            vm_name: VM name (vmConfig.name) for local-hostname in meta-data
            instance_id: VM name (vmConfig.name) for instance-id in meta-data; same value in production

        Returns:
            ISO file as bytes

        Raises:
            ProxmoxError: If ISO generation fails
        """
        try:
            return generate_cidata_iso(
                user_data=user_data,
                vendor_data=vendor_data,
                network_data=network_data,
                vm_name=vm_name,
                instance_id=instance_id,
            )
        except Exception as e:
            raise ProxmoxError(f"Failed to generate cidata ISO: {e}") from e

    def upload_iso_to_storage(
        self, node: str, storage: str, filename: str, iso_bytes: bytes
    ) -> str:
        """Upload an ISO file to Proxmox storage using proxmoxer.

        Uses BytesIO with a name attribute to avoid disk I/O while still using proxmoxer.
        Proxmoxer supports io.IOBase (BytesIO) and uses requests.utils.guess_filename()
        which requires a name attribute.

        Args:
            node: Proxmox node name
            storage: Storage name (e.g., "local")
            filename: Name of the ISO file to create
            iso_bytes: ISO file content as bytes

        Returns:
            The path to the uploaded ISO file in format "storage:iso/filename"

        Raises:
            ProxmoxError: If upload fails
        """
        try:
            logger.info("Uploading ISO file %s to storage %s on node %s", filename, storage, node)

            file_basename = os.path.basename(filename)
            proxmox = self._get_connection()

            # Create BytesIO object and set name attribute
            # Proxmoxer's https.py checks for io.IOBase and uses requests.utils.guess_filename()
            # which needs a name attribute to work properly
            iso_file = io.BytesIO(iso_bytes)
            iso_file.name = file_basename

            # Upload completes asynchronously; poll storage on the target node instead of
            # waiting on the imgcopy task UPID (in a cluster that UPID is on the API node,
            # not necessarily `node`).
            result = (
                proxmox.nodes(node)
                .storage(storage)
                .upload.post(
                    content="iso",
                    filename=iso_file,
                )
            )
            if result:
                logger.debug("ISO upload accepted for %s on %s: %s", file_basename, node, result)

            self._wait_for_iso_volume(node, storage, file_basename)

            file_path = f"{storage}:iso/{file_basename}"
            logger.info("Successfully uploaded ISO file %s to storage %s", file_basename, storage)
            return file_path

        except Exception as e:
            error_msg = str(e)
            logger.error(
                "Failed to upload ISO file %s to storage %s: %s", filename, storage, error_msg
            )
            raise ProxmoxError(
                f"Failed to upload ISO file {filename} to storage {storage}: {e}"
            ) from e

    def _extract_task_upid(self, result: Any) -> str | None:
        """Return a Proxmox task UPID from common proxmoxer response shapes."""
        if isinstance(result, str) and result.startswith("UPID"):
            return result
        if isinstance(result, dict):
            for key in ("data", "task"):
                task = result.get(key)
                if isinstance(task, str) and task.startswith("UPID"):
                    return task
        return None

    def _iso_volume_exists(self, node: str, storage: str, filename: str) -> bool:
        """Check whether an ISO is visible to Proxmox storage on the target node."""
        proxmox = self._get_connection()
        volid = f"{storage}:iso/{filename}"
        try:
            content = proxmox.nodes(node).storage(storage).content.get()
        except Exception as exc:
            logger.debug("Failed to list storage %s on node %s: %s", storage, node, exc)
            return False
        for item in content:
            if item.get("volid") == volid:
                return True
        return False

    def _wait_for_iso_volume(
        self,
        node: str,
        storage: str,
        filename: str,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Wait until Proxmox can resolve storage:iso/<filename> on the target node."""
        deadline = time.time() + timeout
        volid = f"{storage}:iso/{filename}"
        while time.time() < deadline:
            if self._iso_volume_exists(node, storage, filename):
                return
            time.sleep(poll_interval)
        raise ProxmoxError(
            f"ISO volume {volid} not visible in storage {storage} on node {node} after upload"
        )

    def _remove_cloudinit_drives(self, node: str, vmid: int) -> None:
        """Remove existing cloudinit drives from VM to avoid conflicts with ISO approach.

        When cloning a template, Proxmox may automatically create cloudinit drives
        (typically on scsi1, ide1, or other devices). These need to be removed
        before attaching our cloudinit ISO to avoid conflicts.

        Args:
            node: Proxmox node name
            vmid: VM ID

        Raises:
            ProxmoxError: If removal fails
        """
        try:
            proxmox = self._get_connection()
            config = proxmox.nodes(node).qemu(vmid).config.get()

            # Common cloudinit drive patterns in device paths
            cloudinit_devices = []
            cloudinit_patterns = ["cloudinit", "cloud-init"]

            # Check all devices in config
            for key, value in config.items():
                if not isinstance(value, str):
                    continue

                # Skip if value is empty
                if not value.strip():
                    continue

                value_lower = value.lower()

                # Check if device path contains cloudinit patterns
                # This catches paths like "local:200/vm-200-cloudinit.qcow2"
                if any(pattern in value_lower for pattern in cloudinit_patterns):
                    cloudinit_devices.append(key)
                    logger.debug(
                        "Found cloudinit drive %s on VM %s with path: %s", key, vmid, value
                    )

            # Remove cloudinit devices
            for device in cloudinit_devices:
                device_value = config.get(device, "")
                logger.info(
                    "Removing existing cloudinit drive %s from VM %s (path: %s)",
                    device,
                    vmid,
                    device_value,
                )
                # Use delete parameter to remove the device (Proxmox API requirement)
                proxmox.nodes(node).qemu(vmid).config.post(delete=device)
                logger.info("Successfully removed cloudinit drive %s from VM %s", device, vmid)

            if cloudinit_devices:
                logger.info(
                    "Removed %d cloudinit drive(s) from VM %s to avoid conflicts with ISO approach",
                    len(cloudinit_devices),
                    vmid,
                )
            else:
                logger.debug("No existing cloudinit drives found on VM %s", vmid)

        except Exception as e:
            logger.warning(
                "Failed to remove cloudinit drives from VM %s: %s. "
                "Continuing with ISO attachment - this may cause conflicts.",
                vmid,
                e,
            )
            # Don't raise - we'll try to attach ISO anyway

    def _attach_iso_cdrom(self, node: str, vmid: int, iso_path: str, device: str = "scsi1") -> None:
        """Attach an ISO file as a CD-ROM drive to the VM.

        Args:
            node: Proxmox node name
            vmid: VM ID
            iso_path: Path to ISO file in format "storage:iso/filename"
            device: Device to attach ISO to (default: "scsi1")

        Raises:
            ProxmoxError: If attachment fails
        """
        try:
            proxmox = self._get_connection()

            # Get current VM config to check for existing CD-ROM on the device
            config = proxmox.nodes(node).qemu(vmid).config.get()

            current_device = config.get(device, "")

            # Check if device already has the same ISO attached
            if current_device == iso_path:
                logger.debug("VM %s already has ISO %s attached on %s", vmid, iso_path, device)
                return

            # Attach ISO as CD-ROM
            # Proxmox API requires explicit 'media=cdrom' parameter for ISO images
            # Format: ide2=local:iso/filename,media=cdrom
            iso_path_with_media = f"{iso_path},media=cdrom"
            logger.info("Attaching ISO %s to VM %s on %s", iso_path, vmid, device)
            result = proxmox.nodes(node).qemu(vmid).config.post(**{device: iso_path_with_media})
            task = self._extract_task_upid(result)
            if task:
                self._wait_for_task(node, task)
            logger.info("Successfully attached ISO to VM %s on %s", vmid, device)

        except Exception as e:
            raise ProxmoxError(f"Failed to attach ISO to VM {vmid} on {device}: {e}") from e

    def configure_vm(
        self,
        node: str,
        vmid: int,
        cores: int,
        memory: int,
        description: str | None,
        user_data: str | None = None,
        vendor_data: str | None = None,
        network_data: str | None = None,
        cloud_init_config: Any | None = None,
        vm_config: Any | None = None,
    ) -> None:
        """Configure VM settings including cloudInit data.

        Args:
            node: Proxmox node name
            vmid: VM ID
            cores: Number of CPU cores
            memory: Memory in MB
            description: VM description
            user_data: CloudInit user data content
            vendor_data: CloudInit vendor data content
            network_data: CloudInit network data content
            cloud_init_config: CloudInitConfig object with reference names for filenames
            vm_config: VMConfig object to access disk storage information
        """
        try:
            proxmox = self._get_connection()
            config_items = ["cores", "memory", "firewall"]
            if description:
                config_items.append("description")
            if user_data or vendor_data or network_data:
                config_items.append("cloudInit")
            logger.info("Configuring VM %s on node %s: %s", vmid, node, config_items)

            # Configure CPU cores
            proxmox.nodes(node).qemu(vmid).config.post(cores=cores)

            # Configure memory
            proxmox.nodes(node).qemu(vmid).config.post(memory=memory)

            # Configure description only if provided
            if description is not None and str(description).strip() != "":
                proxmox.nodes(node).qemu(vmid).config.post(description=description)

            # Set VGA to std for VNC/console compatibility
            proxmox.nodes(node).qemu(vmid).config.post(vga="std")

            # Configure cloudInit data if provided (using ISO approach)
            if user_data or vendor_data or network_data:
                logger.info("Applying cloudInit configuration to VM %s using ISO approach", vmid)

                # Remove any existing cloudinit drives to avoid conflicts
                self._remove_cloudinit_drives(node, vmid)

                # Get ISO storage from CR if specified, otherwise default to "local"
                iso_storage = None
                if (
                    cloud_init_config
                    and hasattr(cloud_init_config, "storage")
                    and cloud_init_config.storage
                ):
                    iso_storage = cloud_init_config.storage
                    logger.info(
                        "Using storage '%s' from CR for cloudInit ISO on VM %s",
                        iso_storage,
                        vmid,
                    )
                else:
                    # Default to "local" if not specified in CR
                    iso_storage = "local"
                    logger.info(
                        "Using default storage 'local' for cloudInit ISO on VM %s",
                        vmid,
                    )

                vm_name = vm_config.name if vm_config else None
                if not vm_name:
                    raise ProxmoxError(f"VM {vmid} requires vmConfig.name for per-VM cloudInit ISO")

                try:
                    # Build cidata ISO with this VM's compiled user/vendor/network data
                    iso_bytes = self._generate_cidata_iso(
                        user_data=user_data,
                        vendor_data=vendor_data,
                        network_data=network_data,
                        vm_name=vm_name,
                        instance_id=vm_name,
                    )
                    iso_filename = cloudinit_iso_filename(vm_name)
                    logger.info(
                        "Using per-VM cloudInit ISO %s for VM %s (name=%s)",
                        iso_filename,
                        vmid,
                        vm_name,
                    )

                    # Upload ISO to storage
                    iso_path = self.upload_iso_to_storage(
                        node=node,
                        storage=iso_storage,
                        filename=iso_filename,
                        iso_bytes=iso_bytes,
                    )

                    # Attach ISO as CD-ROM drive (ide2: <node>:iso/<filename>,media=cdrom)
                    self._attach_iso_cdrom(
                        node=node,
                        vmid=vmid,
                        iso_path=iso_path,
                        device="ide2",
                    )

                    logger.info(
                        "Successfully applied cloudInit configuration to VM %s using ISO %s",
                        vmid,
                        iso_path,
                    )

                except Exception as e:
                    logger.warning(
                        "Failed to upload and attach cloudInit ISO for VM %s: %s. "
                        "VM will be created without cloudInit configuration.",
                        vmid,
                        e,
                    )
                    # Continue without cloudInit - VM creation should still succeed

        except Exception as e:
            raise ProxmoxError(f"Failed to configure VM {vmid}: {e}") from e

    def configure_vm_firewall_options(self, node: str, vmid: int) -> None:
        """Enable guest firewall options (enable/ipfilter/policies) for a VM."""
        try:
            proxmox = self._get_connection()
            logger.info(
                "[FIREWALL OPTIONS] Configuring firewall options for VM %s on node %s", vmid, node
            )
            proxmox.nodes(node).qemu(vmid).firewall.options.put(
                enable=1, ipfilter=1, policy_in="ACCEPT", policy_out="ACCEPT"
            )
            logger.info(
                "[FIREWALL OPTIONS] Successfully configured firewall options for VM %s", vmid
            )
        except Exception as e:
            logger.warning(
                "[FIREWALL OPTIONS] Failed to configure firewall options for VM %s: %s", vmid, e
            )
            raise ProxmoxError(f"Failed to configure firewall options for VM {vmid}: {e}") from e

    def set_nic_firewall(self, node: str, vmid: int, enabled: bool) -> None:
        """Set ``firewall=1|0`` on every ``netX`` NIC (required for guest FW / ipfilter)."""
        try:
            proxmox = self._get_connection()
            want = "1" if enabled else "0"
            cfg = proxmox.nodes(node).qemu(vmid).config.get() or {}
            for key, value in cfg.items():
                if not key.startswith("net") or not isinstance(value, str):
                    continue
                parts = [p for p in value.split(",") if not p.startswith("firewall=")]
                parts.append(f"firewall={want}")
                updated = ",".join(parts)
                if updated == value:
                    continue
                logger.info(
                    "[FIREWALL OPTIONS] Setting NIC firewall=%s on VM %s: %s=%s",
                    want,
                    vmid,
                    key,
                    updated,
                )
                proxmox.nodes(node).qemu(vmid).config.post(**{key: updated})
        except Exception as e:
            raise ProxmoxError(
                f"Failed to set NIC firewall={int(enabled)} for VM {vmid}: {e}"
            ) from e

    def _static_ipfilter_cidrs(self, static_ip_config: StaticIPConfig | None) -> set[str]:
        """CIDR entries for ipfilter-* IPSets from staticIpConfig (primary, additional, IPv6 block/host)."""
        if not static_ip_config:
            return set()
        cidrs: set[str] = set()
        if getattr(static_ip_config, "ipv4Address", None):
            # ipfilter matches the VM host address, not the upstream subnet prefix.
            cidrs.add(f"{static_ip_config.ipv4Address}/{IPFILTER_IPV4_HOST_PREFIX_LEN}")
        ipv6_address = static_ip_config.ipv6Address
        if ipv6_address:
            prefix_len = static_ip_config.ipv6Prefix or str(IPFILTER_IPV6_HOST_PREFIX_LEN)
            try:
                cidrs.add(_ipv6_ipfilter_cidr(ipv6_address, prefix_len))
            except ValueError as e:
                logger.warning(
                    "[IPSET RECONCILE] Invalid IPv6 staticIpConfig address=%r prefix=%r: %s",
                    ipv6_address,
                    prefix_len,
                    e,
                )
        for addr in getattr(static_ip_config, "additionalIpv4Addresses", None) or []:
            address = getattr(addr, "address", None)
            if address:
                cidrs.add(f"{address}/{IPFILTER_IPV4_HOST_PREFIX_LEN}")
        return cidrs

    def reconcile_ipfilter_ipsets(self, node: str, vmid: int, vm_config) -> None:
        """Ensure ipfilter-* IPSets match staticIpConfig (primary, additional IPv4, and IPv6).

        Creates missing IPSets, adds new auto-* entries, and removes stale auto-* entries.
        """
        try:
            proxmox = self._get_connection()
            static_ip_config = (
                getattr(getattr(vm_config, "cloudInit", None), "staticIpConfig", None)
                if vm_config
                else None
            )
            expected_cidrs = self._static_ipfilter_cidrs(static_ip_config)
            if not expected_cidrs:
                logger.info(
                    "[IPSET RECONCILE] No staticIpConfig CIDRs, skipping VM %s",
                    vmid,
                )
                return

            vm_config_proxmox = proxmox.nodes(node).qemu(vmid).config.get()
            network_interfaces = [
                key
                for key in vm_config_proxmox.keys()
                if key.startswith("net") and isinstance(vm_config_proxmox[key], str)
            ]
            if not network_interfaces:
                logger.warning("[IPSET RECONCILE] No network interfaces found for VM %s", vmid)
                return

            existing_ipsets = {
                ipset.get("name", "")
                for ipset in (proxmox.nodes(node).qemu(vmid).firewall.ipset.get() or [])
                if ipset.get("name")
            }

            for interface_name in network_interfaces:
                ipset_name = f"ipfilter-{interface_name}"
                if ipset_name not in existing_ipsets:
                    try:
                        proxmox.nodes(node).qemu(vmid).firewall.ipset.post(name=ipset_name)
                        logger.info(
                            "[IPSET RECONCILE] Created IPSet %s for VM %s", ipset_name, vmid
                        )
                    except Exception as e:
                        logger.warning(
                            "[IPSET RECONCILE] Failed to create IPSet %s: %s", ipset_name, e
                        )
                        continue

                try:
                    entries = proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).get() or []
                except Exception as e:
                    logger.warning(
                        "[IPSET RECONCILE] Failed to list IPSet %s for VM %s: %s",
                        ipset_name,
                        vmid,
                        e,
                    )
                    continue

                # Each present entry: (raw cidr, comparison key, is auto-managed).
                present_entries: list[tuple[str, str, bool]] = []
                for entry in entries:
                    cidr = entry.get("cidr")
                    if not cidr:
                        continue
                    key = _ipfilter_cidr_key(cidr)
                    is_auto = (entry.get("comment") or "").startswith("auto-")
                    present_entries.append((cidr, key, is_auto))

                expected_by_key = {_ipfilter_cidr_key(cidr): cidr for cidr in expected_cidrs}
                expected_keys = set(expected_by_key)

                for raw_cidr, key, is_auto in present_entries:
                    if not is_auto:
                        continue
                    if key in expected_keys:
                        continue
                    try:
                        encoded = urllib.parse.quote(raw_cidr, safe="")
                        proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name)(encoded).delete()
                        logger.info(
                            "[IPSET RECONCILE] Removed CIDR %s from %s for VM %s",
                            raw_cidr,
                            ipset_name,
                            vmid,
                        )
                    except Exception as e:
                        logger.warning(
                            "[IPSET RECONCILE] Failed to remove CIDR %s from %s: %s",
                            raw_cidr,
                            ipset_name,
                            e,
                        )

                covered_keys = {key for _, key, _ in present_entries}

                for key, cidr in expected_by_key.items():
                    if key in covered_keys:
                        continue
                    try:
                        proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).post(
                            cidr=cidr, comment=f"auto-{cidr}"
                        )
                        covered_keys.add(key)
                        logger.info(
                            "[IPSET RECONCILE] Added CIDR %s to %s for VM %s",
                            cidr,
                            ipset_name,
                            vmid,
                        )
                    except Exception as e:
                        logger.warning(
                            "[IPSET RECONCILE] Failed to add CIDR %s to %s: %s",
                            cidr,
                            ipset_name,
                            e,
                        )

        except Exception as e:
            logger.error(
                "[IPSET RECONCILE] Failed for VM %s: %s",
                vmid,
                e,
                exc_info=True,
            )
            raise ProxmoxError(f"Failed to reconcile ipfilter- IPSets for VM {vmid}: {e}") from e

    def start_vm(self, node: str, vmid: int) -> None:
        """Start a VM."""
        try:
            proxmox = self._get_connection()
            logger.info("Starting VM %s on node %s", vmid, node)

            task = proxmox.nodes(node).qemu(vmid).status.start.post()
            self._wait_for_task(node, task)

        except Exception as e:
            raise ProxmoxError(f"Failed to start VM {vmid}: {e}") from e

    def stop_vm(self, node: str, vmid: int) -> None:
        """Stop a VM."""
        try:
            proxmox = self._get_connection()
            logger.info("Stopping VM %s on node %s", vmid, node)

            task = proxmox.nodes(node).qemu(vmid).status.stop.post()
            # Treat an exitstatus of 'stopped' as success for stop operations
            self._wait_for_task(node, task, allowed_exit_statuses=["OK", "stopped"])

        except Exception as e:
            raise ProxmoxError(f"Failed to stop VM {vmid}: {e}") from e

    def reset_vm(self, node: str, vmid: int) -> None:
        """Restart a VM (calls Proxmox reset API for hard reset)."""
        try:
            proxmox = self._get_connection()
            logger.info("Restarting VM %s on node %s", vmid, node)

            task = proxmox.nodes(node).qemu(vmid).status.reset.post()
            self._wait_for_task(node, task)

        except Exception as e:
            raise ProxmoxError(f"Failed to restart VM {vmid}: {e}") from e

    def get_vm_status(self, node: str, vmid: int) -> str:
        """Get VM status."""
        try:
            proxmox = self._get_connection()
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
            vm_status = status.get("status", "unknown")
            logger.debug("VM %s status: %s", vmid, vm_status)
            return vm_status
        except Exception as e:
            logger.warning("Failed to get VM %s status: %s", vmid, e)
            # If we can't get status, return unknown
            return VM_STATUS_UNKNOWN

    def get_node_status(self, node: str) -> dict[str, Any]:
        """Return Proxmox GET /nodes/{node}/status payload (includes cpuinfo)."""
        try:
            proxmox = self._get_connection()
            return proxmox.nodes(node).status.get()
        except Exception as e:
            raise ProxmoxError(f"Failed to get node status for {node}: {e}") from e

    def restore_backup(
        self,
        node: str,
        vmid: int,
        volid: str,
        storage: str | None = None,
        start: bool = False,
    ) -> str:
        """Restore a VM from a backup.

        Uses the Qemu API endpoint (POST /nodes/{node}/qemu) which works with API tokens,
        unlike the storage content restore endpoint which requires root@pam.

        Args:
            node: Proxmox node name
            vmid: VM ID to restore to
            volid: Backup volume ID (e.g., 'local:backup/vzdump-qemu-100-20260202_030000.vma.zst'
                   or 'backups-lvm:backup/vzdump-qemu-1201-2026_02_23-19_50_31.vma.zst')
            storage: Target storage for restore (optional, not used by Qemu API endpoint)
            start: If True, start VM after restore completes (default: False)

        Returns:
            Task UPID for the restore operation

        Raises:
            ProxmoxError: If restore fails
        """
        try:
            proxmox = self._get_connection()
            logger.debug(
                "Restoring VM %s from backup %s on node %s (start=%s)", vmid, volid, node, start
            )

            # Validate volid format
            if ":" not in volid:
                raise ProxmoxError(f"Invalid backup volume ID format: {volid}")

            # Use Qemu API endpoint: POST /nodes/{node}/qemu
            # This endpoint works with API tokens and doesn't require root@pam
            # Parameters:
            #   - vmid: VM ID to restore to
            #   - archive: Full volid (e.g., "backups-lvm:backup/vzdump-qemu-1201-...")
            #   - force: 1 to overwrite existing VM
            #   - start: 1 to start VM after restore (optional)
            params = {
                "vmid": vmid,
                "archive": volid,
                "force": 1,
            }
            if start:
                params["start"] = 1

            # Restore backup using Qemu API endpoint
            task = proxmox.nodes(node).qemu.post(**params)

            task_upid = None
            if isinstance(task, dict):
                task_upid = task.get("UPID") or task.get("data")
            elif isinstance(task, str):
                task_upid = task

            if not task_upid:
                raise ProxmoxError("Failed to get task UPID from restore operation")

            logger.info("Restore task started for VM %s: %s", vmid, task_upid)
            return task_upid

        except Exception as e:
            logger.error("Failed to restore VM %s from backup %s: %s", vmid, volid, e)
            raise ProxmoxError(f"Failed to restore VM {vmid} from backup {volid}: {e}") from e

    def delete_vm(self, node: str, vmid: int, force: bool = False) -> None:
        """Delete a VM. If VM is running, stop it first before deletion."""
        try:
            proxmox = self._get_connection()
            logger.info("Deleting VM %s on node %s", vmid, node)

            # Check if VM is running
            vm_status = self.get_vm_status(node, vmid)
            logger.info("VM %s status: %s", vmid, vm_status)

            if vm_status in [VM_STATUS_RUNNING, VM_STATUS_UNKNOWN]:
                logger.info("VM %s status is %s, stopping before deletion", vmid, vm_status)
                # Always use stop (not shutdown) before deletion
                self._stop_with_retries(proxmox, node, vmid, timeout=60)
                logger.info("VM %s stopped", vmid)

                # Verify VM is actually stopped before proceeding with deletion
                time.sleep(VM_STOP_VERIFICATION_DELAY)
                vm_status_after_stop = self.get_vm_status(node, vmid)
                if vm_status_after_stop not in [VM_STATUS_STOPPED, VM_STATUS_UNKNOWN]:
                    logger.warning(
                        "VM %s still in status '%s' after stop attempt, proceeding with deletion anyway",
                        vmid,
                        vm_status_after_stop,
                    )

            # Now delete the VM (HTTP DELETE). If lock issues occur, retry with skiplock.
            try:
                task = proxmox.nodes(node).qemu(vmid).delete()
                self._wait_for_task(node, task)
            except Exception as delete_error:
                msg = str(delete_error).lower()
                if "lock" in msg or "timeout" in msg:
                    logger.warning(
                        "Initial delete failed due to lock/timeout for VM %s, retrying with skiplock",
                        vmid,
                    )
                    task = proxmox.nodes(node).qemu(vmid).delete(skiplock=1)
                    self._wait_for_task(node, task)
                else:
                    raise
            logger.info("VM %s deleted successfully", vmid)

        except Exception as e:
            raise ProxmoxError(f"Failed to delete VM {vmid}: {e}") from e

    def delete_snapshot(self, node: str, vmid: int, snapname: str) -> None:
        """Delete a QEMU snapshot. Proxmox API: DELETE nodes/{node}/qemu/{vmid}/snapshot/{snapname}."""
        try:
            proxmox = self._get_connection()
            logger.debug("Deleting snapshot %s for VM %s on node %s", snapname, vmid, node)
            task = proxmox.nodes(node).qemu(vmid).snapshot(snapname).delete()
            task_upid = task.get("data") or task.get("UPID") if isinstance(task, dict) else task
            if task_upid:
                self._wait_for_task(node, task_upid)
            logger.info("Snapshot %s deleted for VM %s", snapname, vmid)
        except Exception as e:
            if _snapshot_absent_on_delete(e):
                logger.info(
                    "Snapshot %s already absent on VM %s node %s; treating delete as successful",
                    snapname,
                    vmid,
                    node,
                )
                return
            raise ProxmoxError(f"Failed to delete snapshot {snapname} for VM {vmid}: {e}") from e

    def delete_storage_content(self, node: str, volid: str) -> None:
        """Delete a volume from Proxmox storage. volid format: storage:path."""
        if ":" not in volid:
            raise ProxmoxError(f"Invalid storage volid format: {volid}")
        storage_name, _volume_path = volid.split(":", 1)
        try:
            proxmox = self._get_connection()
            logger.debug("Deleting storage content %s from node %s", volid, node)
            proxmox.nodes(node).storage(storage_name).content(volid).delete()
            logger.info("Storage content %s deleted from Proxmox", volid)
        except Exception as e:
            if _storage_content_absent_on_delete(e):
                logger.info(
                    "Storage content %s already absent on node %s; treating delete as successful",
                    volid,
                    node,
                )
                return
            raise ProxmoxError(f"Failed to delete storage content {volid}: {e}") from e

    def delete_backup(self, node: str, volid: str) -> None:
        """Delete a backup volume from Proxmox storage. Proxmox API: DELETE nodes/{node}/storage/{storage}/content/{volid}.
        volid format: storage:path (e.g. backups-lvm:backup/vzdump-qemu-1200-2026_03_17-22_20_30.vma.zst).
        """
        self.delete_storage_content(node, volid)

    def delete_cloudinit_iso_for_vm(self, node: str, vm_name: str) -> int:
        """Delete per-VM cloudInit ISO from all ISO-capable storages on the node. Best-effort."""
        if not vm_name:
            return 0
        iso_filename = cloudinit_iso_filename(vm_name)
        deleted = 0
        try:
            proxmox = self._get_connection()
            storages = proxmox.nodes(node).storage.get()
        except Exception as e:
            logger.warning("delete_cloudinit_iso_for_vm: cannot list storages on %s: %s", node, e)
            return 0
        if not isinstance(storages, list):
            return 0
        for storage in storages:
            storage_name = storage.get("storage")
            if not storage_name:
                continue
            storage_content = storage.get("content") or []
            if storage_content and "iso" not in storage_content:
                continue
            try:
                content = proxmox.nodes(node).storage(storage_name).content.get()
            except Exception:
                continue
            if not isinstance(content, list):
                continue
            for item in content:
                if item.get("content") != "iso":
                    continue
                volid = item.get("volid")
                if not volid or ":" not in str(volid):
                    continue
                if os.path.basename(str(volid)) != iso_filename:
                    continue
                try:
                    self.delete_storage_content(node, str(volid))
                    deleted += 1
                except Exception as e:
                    logger.warning("delete_cloudinit_iso_for_vm: skip %s: %s", volid, e)
        return deleted

    def delete_all_backups_for_vmid(self, node: str, vmid: int) -> int:
        """Delete every vzdump/backup object on this node tied to vmid (storage content scan). Best-effort."""
        if vmid <= 0:
            return 0
        deleted = 0
        try:
            proxmox = self._get_connection()
            storages = proxmox.nodes(node).storage.get()
        except Exception as e:
            logger.warning("delete_all_backups_for_vmid: cannot list storages on %s: %s", node, e)
            return 0
        if not isinstance(storages, list):
            return 0
        for storage in storages:
            storage_name = storage.get("storage")
            if not storage_name:
                continue
            try:
                content = proxmox.nodes(node).storage(storage_name).content.get()
            except Exception:
                continue
            if not isinstance(content, list):
                continue
            for item in content:
                if item.get("content") != "backup":
                    continue
                if str(item.get("vmid")) != str(vmid) and item.get("vmid") != vmid:
                    continue
                volid = item.get("volid")
                if not volid or ":" not in str(volid):
                    continue
                try:
                    self.delete_backup(node, str(volid))
                    deleted += 1
                except Exception as e:
                    logger.warning("delete_all_backups_for_vmid: skip %s: %s", volid, e)
        return deleted

    def restore_snapshot(self, node: str, vmid: int, snapname: str) -> str | None:
        """Rollback VM to a QEMU snapshot. Proxmox API: POST nodes/{node}/qemu/{vmid}/snapshot/{snapname}/rollback.
        Returns task UPID for status polling, or None on failure.
        """
        try:
            proxmox = self._get_connection()
            logger.debug("Restoring snapshot %s for VM %s on node %s", snapname, vmid, node)
            task = proxmox.nodes(node).qemu(vmid).snapshot(snapname).rollback.post()
            if isinstance(task, dict):
                upid = task.get("UPID") or task.get("data")
            else:
                upid = task
            if upid:
                logger.info("Snapshot restore started vmid=%s task=%s", vmid, upid)
                return upid
            logger.warning("Proxmox returned no UPID for snapshot rollback vmid=%s", vmid)
            return None
        except Exception as e:
            logger.error("Failed to restore snapshot %s for VM %s: %s", snapname, vmid, e)
            raise ProxmoxError(f"Failed to restore snapshot {snapname} for VM {vmid}: {e}") from e

    def get_task_status(self, node: str, task: str) -> dict[str, Any] | None:
        """Get current status of a Proxmox task without blocking.

        Returns task status dict with keys: status, exitstatus, etc.
        Returns None if task not found or error occurred.
        """
        try:
            proxmox = self._get_connection()
            return proxmox.nodes(node).tasks(task).status.get()
        except Exception as e:
            logger.warning("Failed to get task status for %s: %s", task, e)
            return None

    def _wait_for_task(
        self,
        node: str,
        task: str,
        timeout: int = DEFAULT_TASK_TIMEOUT,
        allowed_exit_statuses: list[str] | None = None,
    ) -> None:
        """Wait for a Proxmox task to complete.

        allowed_exit_statuses: additional acceptable exit statuses besides 'OK'.
        """
        try:
            proxmox = self._get_connection()
            start_time = time.time()
            # Infer task type from UPID to adjust acceptable exit statuses for stop/shutdown
            try:
                task_type = task.split(":")[5]
            except Exception:
                task_type = ""

            default_allowed = [EXIT_STATUS_OK]
            if task_type in ("qmstop", "qmshutdown"):
                default_allowed.append(EXIT_STATUS_STOPPED)
            if allowed_exit_statuses:
                default_allowed.extend(
                    [s for s in allowed_exit_statuses if s not in default_allowed]
                )

            # Wait for task completion
            while True:
                # Check timeout
                if time.time() - start_time > timeout:
                    raise ProxmoxError(f"Task {task} timed out after {timeout} seconds")

                status = proxmox.nodes(node).tasks(task).status.get()
                if status.get("status") == "stopped":
                    exit_status = status.get("exitstatus")
                    if exit_status in default_allowed:
                        logger.info("Task %s completed successfully (exit=%s)", task, exit_status)
                        break
                    raise ProxmoxError(f"Task {task} failed: {exit_status or status.get('status')}")
                elif status.get("status") == "running":
                    # Task is still running, wait a bit
                    time.sleep(TASK_STATUS_CHECK_INTERVAL)
                else:
                    raise ProxmoxError(f"Task {task} in unexpected status: {status.get('status')}")

        except Exception as e:
            if "Task" in str(e):
                raise  # Re-raise task-related errors
            raise ProxmoxError(f"Failed to wait for task {task}: {e}") from e

    def _stop_with_retries(
        self, proxmox: ProxmoxAPI, node: str, vmid: int, timeout: int = DEFAULT_STOP_TIMEOUT
    ) -> None:
        """Attempt to stop a VM, retrying on lock timeouts and escalating to skiplock.

        Tries up to VM_STOP_MAX_RETRIES times with exponential backoff;
        attempts >= VM_STOP_SKIPLOCK_THRESHOLD use skiplock.
        """
        attempts = 0
        backoff_seconds = VM_STOP_INITIAL_BACKOFF
        last_error: Exception | None = None
        while attempts < VM_STOP_MAX_RETRIES:
            try:
                if attempts >= VM_STOP_SKIPLOCK_THRESHOLD:
                    task = proxmox.nodes(node).qemu(vmid).status.stop.post(skiplock=1)
                else:
                    task = proxmox.nodes(node).qemu(vmid).status.stop.post()
                self._wait_for_task(
                    node,
                    task,
                    timeout=timeout,
                    allowed_exit_statuses=[EXIT_STATUS_OK, EXIT_STATUS_STOPPED],
                )
                return
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                if "lock" in msg or "timeout" in msg:
                    attempts += 1
                    if attempts < VM_STOP_MAX_RETRIES:
                        logger.warning(
                            "Stop attempt %s for VM %s failed (lock/timeout). Retrying in %ss%s",
                            attempts,
                            vmid,
                            backoff_seconds,
                            " with skiplock" if attempts >= VM_STOP_SKIPLOCK_THRESHOLD else "",
                        )
                        time.sleep(backoff_seconds)
                        backoff_seconds = min(backoff_seconds * 2, VM_STOP_MAX_BACKOFF)
                        continue
                # For other errors or after max attempts, break and raise
                break
        if last_error is not None:
            raise ProxmoxError(f"Failed to stop VM {vmid}: {last_error}") from last_error

    def get_vnc_ticket(self, node: str, vmid: int) -> dict[str, Any]:
        """Get VNC ticket and connection details from Proxmox vnproxy API.

        Calls POST /nodes/{node}/qemu/{vmid}/vncproxy with websocket and
        generate-password options for noVNC-compatible auth.

        Args:
            node: Proxmox node name.
            vmid: VM ID.

        Returns:
            Dict with required keys: ticket (str), port (int), user (str),
            password (str). Optional key: cert (str, PEM) when TLS is used.
            Port is normalized to int.

        Raises:
            ProxmoxError: If the API call fails or response is invalid.
        """
        try:
            proxmox = self._get_connection()
            logger.info("Getting VNC ticket for VM %s on node %s", vmid, node)

            # Call Proxmox VNC proxy API with websocket and generate-password for noVNC
            # POST /nodes/{node}/qemu/{vmid}/vncproxy
            # Params: websocket=1, generate-password=1 for WebSocket RFB auth
            # Response: ticket, port, user, optionally cert (PEM), and password (RFB)
            # API expects "generate-password" and "websocket"; use dict for hyphenated param
            response = (
                proxmox.nodes(node)
                .qemu(vmid)
                .vncproxy.post(
                    websocket=1,
                    **{"generate-password": 1},
                )
            )

            ticket = response.get("ticket")
            port = response.get("port")
            user = response.get("user", "root@pam")
            cert = response.get("cert")
            # RFB password for noVNC; some Proxmox versions use ticket for both
            password = response.get("password") or ticket

            if not ticket or port is None:
                raise ProxmoxError(
                    f"Invalid VNC proxy response: missing ticket or port. Response: {response}"
                )

            # Normalize port to int (API may return string e.g. "5900")
            port_int = int(port) if not isinstance(port, int) else port

            logger.info(
                "VNC ticket obtained for VM %s: port=%s, user=%s, cert=%s",
                vmid,
                port_int,
                user,
                "yes" if cert else "no",
            )

            result: dict[str, Any] = {
                "ticket": ticket,
                "port": port_int,
                "user": user,
                "password": password,
            }
            if cert:
                result["cert"] = cert
            return result

        except Exception as e:
            raise ProxmoxError(f"Failed to get VNC ticket for VM {vmid}: {e}") from e
