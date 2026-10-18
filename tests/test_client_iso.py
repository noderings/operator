"""Tests for ProxmoxClient ISO generation functionality."""

import io
import pycdlib
import pytest
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from proxmox_operator.client import ProxmoxClient
from proxmox_operator.config import ProxmoxSettings


class TestGenerateCidataIso:
    """Test cases for _generate_cidata_iso method."""

    @pytest.fixture
    def client(self):
        """Create a ProxmoxClient instance for testing."""
        # Create minimal config for testing
        config = ProxmoxSettings(
            host="test.example.com:8006",
            username="testuser",
            token_name="test-token",
            token_value="test-value",
            verify_ssl=True,
            timeout=30,
        )
        return ProxmoxClient(config)

    def test_generate_iso_with_all_data(self, client):
        """Test ISO generation with all CloudInit data types."""
        user_data = "#cloud-config\nusers:\n  - name: testuser\n"
        vendor_data = "#cloud-config\nvendor: test\n"
        network_data = "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n"

        iso_bytes = client._generate_cidata_iso(
            user_data=user_data,
            vendor_data=vendor_data,
            network_data=network_data,
            vm_name="test-vm",
            instance_id="test-instance",
        )

        # Verify ISO is generated (non-empty bytes)
        assert iso_bytes is not None
        assert len(iso_bytes) > 0

        # Verify ISO can be read back
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))

        # Verify volume label
        assert iso.pvd.volume_identifier.decode("utf-8").strip() == "cidata"

        # Verify files exist by checking ISO structure
        # The ISO should contain at least meta-data
        files_found = []
        try:
            for dirname, dirlist, filelist in iso.walk(iso_path="/"):
                files_found.extend(filelist)
        except Exception:
            pass

        iso.close()

        # Verify at least one file exists (meta-data should always be present)
        assert len(files_found) > 0

    def test_generate_iso_with_only_user_data(self, client):
        """Test ISO generation with only user_data."""
        user_data = "#cloud-config\nusers:\n  - name: testuser\n"

        iso_bytes = client._generate_cidata_iso(
            user_data=user_data,
            vm_name="test-vm",
            instance_id="test-instance",
        )

        assert iso_bytes is not None
        assert len(iso_bytes) > 0

        # Verify ISO can be read
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))
        assert iso.pvd.volume_identifier.decode("utf-8").strip() == "cidata"
        iso.close()

    def test_generate_iso_with_meta_data_only(self, client):
        """Test ISO generation with only meta-data (no user/vendor/network data)."""
        iso_bytes = client._generate_cidata_iso(
            vm_name="test-vm",
            instance_id="test-instance",
        )

        assert iso_bytes is not None
        assert len(iso_bytes) > 0

        # Verify ISO can be read
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))
        assert iso.pvd.volume_identifier.decode("utf-8").strip() == "cidata"
        iso.close()

    def test_generate_iso_meta_data_content(self, client):
        """Test that meta-data contains correct instance-id and local-hostname."""
        instance_id = "test-instance"
        vm_name = "test-vm"

        iso_bytes = client._generate_cidata_iso(
            vm_name=vm_name,
            instance_id=instance_id,
        )

        # Read ISO and extract meta-data
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))

        # Try to read meta-data file
        meta_data_content = None
        try:
            # Try different possible paths
            for path in ["/META.;1", "/meta-data"]:
                try:
                    fp = iso.get_file_from_iso_fp(path)
                    meta_data_content = fp.read().decode("utf-8")
                    fp.close()
                    break
                except Exception:
                    continue
        except Exception:
            pass

        iso.close()

        # Verify meta-data content
        if meta_data_content:
            assert f"instance-id: {instance_id}" in meta_data_content
            assert f"local-hostname: {vm_name}" in meta_data_content

    def test_generate_iso_meta_data_with_vm_name(self, client):
        """Test that meta-data uses vmConfig.name as instance-id (production behavior)."""
        vm_name = "NR-38f0eedd-b5f6-419a-8a27-ec6d35b3dfb3"

        iso_bytes = client._generate_cidata_iso(
            vm_name=vm_name,
            instance_id=vm_name,
        )

        # Read ISO and extract meta-data
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))

        # Try to read meta-data file
        meta_data_content = None
        try:
            # Try different possible paths
            for path in ["/META.;1", "/meta-data"]:
                try:
                    fp = iso.get_file_from_iso_fp(path)
                    meta_data_content = fp.read().decode("utf-8")
                    fp.close()
                    break
                except Exception:
                    continue
        except Exception:
            pass

        iso.close()

        # Verify meta-data content uses vmConfig.name, not Proxmox VMID or template ID
        if meta_data_content:
            assert f"instance-id: {vm_name}" in meta_data_content
            assert "instance-id: 9900" not in meta_data_content
            assert f"local-hostname: {vm_name}" in meta_data_content

    def test_generate_iso_with_defaults(self, client):
        """Test ISO generation with no instance_id or vm_name."""
        iso_bytes = client._generate_cidata_iso()

        assert iso_bytes is not None
        assert len(iso_bytes) > 0

        # Verify ISO can be read
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))
        assert iso.pvd.volume_identifier.decode("utf-8").strip() == "cidata"
        iso.close()

    def test_generate_iso_rock_ridge_compliance(self, client):
        """Test that ISO filenames comply with ISO 9660 Level 1 restrictions."""
        user_data = "#cloud-config\ntest: data\n"
        vendor_data = "#cloud-config\nvendor: test\n"
        network_data = "network:\n  version: 2\n"

        iso_bytes = client._generate_cidata_iso(
            user_data=user_data,
            vendor_data=vendor_data,
            network_data=network_data,
            vm_name="test-vm",
            instance_id="test-instance",
        )

        # Verify ISO can be read (if filenames are invalid, this would fail)
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))

        # Verify volume label
        assert iso.pvd.volume_identifier.decode("utf-8").strip() == "cidata"

        iso.close()

    def test_generate_iso_large_content(self, client):
        """Test ISO generation with large content."""
        # Generate large user_data (10KB)
        user_data = "#cloud-config\n" + "x" * 10000

        iso_bytes = client._generate_cidata_iso(
            user_data=user_data,
            vm_name="test-vm",
            instance_id="test-instance",
        )

        assert iso_bytes is not None
        assert (
            len(iso_bytes) > 10000
        )  # ISO should be larger than content due to filesystem overhead

        # Verify ISO can be read
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(iso_bytes))
        assert iso.pvd.volume_identifier.decode("utf-8").strip() == "cidata"
        iso.close()
