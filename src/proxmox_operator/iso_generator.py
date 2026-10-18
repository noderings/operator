"""ISO generator for Cloud-Init cidata files."""

import io
import logging

import pycdlib

logger = logging.getLogger(__name__)


class ISOGenerationError(Exception):
    """Exception raised for ISO generation errors."""

    pass


def generate_cidata_iso(
    user_data: str | None = None,
    vendor_data: str | None = None,
    network_data: str | None = None,
    vm_name: str | None = None,
    instance_id: str | None = None,
) -> bytes:
    """Generate a cidata ISO file in memory containing Cloud-Init configuration.

    Creates an ISO 9660 filesystem with volume label "cidata" containing:
    - user-data (if provided)
    - meta-data (generated with instance-id and local-hostname)
    - vendor-data (if provided)
    - network-config (if provided, uses network_data)

    Args:
        user_data: CloudInit user data content
        vendor_data: CloudInit vendor data content
        network_data: CloudInit network data content
        vm_name: VM name (vmConfig.name) for local-hostname in meta-data
        instance_id: VM name (vmConfig.name) for instance-id in meta-data; same value in production

    Returns:
        ISO file as bytes

    Raises:
        ISOGenerationError: If ISO generation fails
    """
    try:
        # Create a new ISO with Rock Ridge extensions and Joliet support
        # genisoimage   -output <output_file> -volid cidata   -joliet -rock
        iso = pycdlib.PyCdlib()
        iso.new(vol_ident="cidata", interchange_level=3, joliet=True, rock_ridge="1.09")

        def add_content_file(content: str, filename: str) -> None:
            """Helper to write string content as a file into the ISO.

            Args:
                content: File content as string
                filename: Filename (e.g., "meta-data", "user-data")
            """
            if not content:
                return

            # Convert the string to bytes
            data = content.encode("utf-8")

            # ISO 9660 Strict Name: UPPERCASE, underscores only, ending in ;1
            # Example: user-data -> USER_DATA.;1
            iso_strict_name = filename.upper().replace("-", "_") + ";1"

            # Add the file from a memory buffer (BytesIO)
            iso.add_fp(
                io.BytesIO(data),  # The file content (as a file-like object)
                len(data),  # The size of the file
                iso_path=f"/{iso_strict_name}",  # The strict ISO 9660 path
                joliet_path=f"/{filename}",  # The path for Windows (Joliet)
                rr_name=filename,  # The path for Linux (Rock Ridge)
            )

        meta_instance_id = instance_id if instance_id else "unknown"
        local_hostname = vm_name if vm_name else f"vm-{meta_instance_id}"
        meta_data = f"instance-id: {meta_instance_id}\nlocal-hostname: {local_hostname}\n"

        # Add meta-data file (always present)
        add_content_file(meta_data, "meta-data")

        # Add user-data file if provided
        if user_data:
            add_content_file(user_data, "user-data")

        # Add vendor-data file if provided
        if vendor_data:
            add_content_file(vendor_data, "vendor-data")

        # Add network-config file if provided
        if network_data:
            add_content_file(network_data, "network-config")

        # Write ISO to bytes buffer
        out_buffer = io.BytesIO()
        iso.write_fp(out_buffer)
        iso.close()

        iso_bytes = out_buffer.getvalue()
        logger.info(
            "Generated cidata ISO: %d bytes (user-data: %s, vendor-data: %s, network-config: %s)",
            len(iso_bytes),
            "yes" if user_data else "no",
            "yes" if vendor_data else "no",
            "yes" if network_data else "no",
        )
        return iso_bytes

    except Exception as e:
        raise ISOGenerationError(f"Failed to generate cidata ISO: {e}") from e
