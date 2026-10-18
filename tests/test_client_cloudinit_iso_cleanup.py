"""Tests for per-VM cloudInit ISO upload, cleanup, and storage delete."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from proxmox_operator.client import ProxmoxClient, ProxmoxError
from proxmox_operator.config import ProxmoxSettings
from proxmox_operator.constants import cloudinit_iso_filename


@pytest.fixture
def client():
    config = ProxmoxSettings(
        host="test.example.com:8006",
        username="testuser",
        token_name="test-token",
        token_value="test-value",
        verify_ssl=True,
        timeout=30,
    )
    return ProxmoxClient(config)


class TestCloudinitIsoFilename:
    def test_cloudinit_iso_filename(self):
        vm_name = "NR-38f0eedd-b5f6-419a-8a27-ec6d35b3dfb3"
        assert (
            cloudinit_iso_filename(vm_name)
            == "cloudinit-nr-38f0eedd-b5f6-419a-8a27-ec6d35b3dfb3.iso"
        )


class TestConfigureVmCloudinitIso:
    def test_configure_vm_uploads_per_vm_cloudinit_iso_filename(self, client):
        vm_name = "NR-38f0eedd-b5f6-419a-8a27-ec6d35b3dfb3"
        expected_filename = cloudinit_iso_filename(vm_name)
        vm_config = SimpleNamespace(name=vm_name)
        mock_proxmox = MagicMock()

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with patch.object(client, "_remove_cloudinit_drives"):
                with patch.object(
                    client, "_generate_cidata_iso", return_value=b"iso-bytes"
                ) as generate_iso:
                    with patch.object(
                        client,
                        "upload_iso_to_storage",
                        return_value=f"local:iso/{expected_filename}",
                    ) as upload_iso:
                        with patch.object(client, "_attach_iso_cdrom") as attach_iso:
                            client.configure_vm(
                                node="pve01",
                                vmid=1200,
                                cores=2,
                                memory=4096,
                                description=None,
                                user_data="#cloud-config\n",
                                vm_config=vm_config,
                            )

        generate_iso.assert_called_once_with(
            user_data="#cloud-config\n",
            vendor_data=None,
            network_data=None,
            vm_name=vm_name,
            instance_id=vm_name,
        )
        upload_iso.assert_called_once_with(
            node="pve01",
            storage="local",
            filename=expected_filename,
            iso_bytes=b"iso-bytes",
        )
        attach_iso.assert_called_once_with(
            node="pve01",
            vmid=1200,
            iso_path=f"local:iso/{expected_filename}",
            device="ide2",
        )

    @pytest.mark.parametrize(
        "vm_config",
        [None, SimpleNamespace(name=None), SimpleNamespace(name="")],
    )
    def test_configure_vm_requires_vm_config_name(self, client, vm_config):
        mock_proxmox = MagicMock()

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with patch.object(client, "_remove_cloudinit_drives"):
                with pytest.raises(ProxmoxError, match="vmConfig.name"):
                    client.configure_vm(
                        node="pve01",
                        vmid=1200,
                        cores=2,
                        memory=4096,
                        description=None,
                        user_data="#cloud-config\n",
                        vm_config=vm_config,
                    )


class TestDeleteStorageContent:
    def test_delete_storage_content_treats_missing_volume_as_success(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.storage.return_value.content.return_value.delete.side_effect = Exception(
            "volume does not exist"
        )

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            client.delete_storage_content("pve01", "local:iso/missing.iso")

    def test_delete_storage_content_raises_on_other_errors(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.storage.return_value.content.return_value.delete.side_effect = Exception(
            "permission denied"
        )

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with pytest.raises(ProxmoxError, match="Failed to delete storage content"):
                client.delete_storage_content("pve01", "local:iso/test.iso")

    def test_delete_storage_content_rejects_invalid_volid(self, client):
        with pytest.raises(ProxmoxError, match="Invalid storage volid format"):
            client.delete_storage_content("pve01", "invalid-volid")


class TestUploadIsoToStorage:
    def test_upload_iso_waits_for_volume_not_task(self, client):
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.storage.return_value.upload.post.return_value = "UPID:proxmox01-lab01-dev:000507CE:00C66C5D:6A42DAD9:imgcopy::kopfoperator@pve!kopf-operator-token:"

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with patch.object(client, "_wait_for_task") as wait_for_task:
                with patch.object(client, "_wait_for_iso_volume") as wait_for_volume:
                    path = client.upload_iso_to_storage(
                        node="proxmox02-lab01-dev",
                        storage="local",
                        filename="cloudinit-test.iso",
                        iso_bytes=b"iso",
                    )

        assert path == "local:iso/cloudinit-test.iso"
        wait_for_task.assert_not_called()
        wait_for_volume.assert_called_once_with(
            "proxmox02-lab01-dev", "local", "cloudinit-test.iso"
        )


class TestCloudinitIsoCleanup:
    def test_delete_cloudinit_iso_for_vm_scans_iso_storages(self, client):
        vm_name = "NR-38f0eedd-b5f6-419a-8a27-ec6d35b3dfb3"
        iso_name = cloudinit_iso_filename(vm_name)

        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.storage.get.return_value = [
            {"storage": "local", "content": ["iso", "vztmpl"]},
            {"storage": "backups-lvm", "content": ["backup"]},
        ]
        mock_proxmox.nodes.return_value.storage.return_value.content.get.return_value = [
            {"content": "iso", "volid": f"local:iso/{iso_name}"},
            {"content": "iso", "volid": "local:iso/other.iso"},
        ]

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with patch.object(client, "delete_storage_content") as delete_content:
                deleted = client.delete_cloudinit_iso_for_vm("pve01", vm_name)

        assert deleted == 1
        delete_content.assert_called_once_with("pve01", f"local:iso/{iso_name}")

    def test_delete_cloudinit_iso_for_vm_skips_non_iso_storages(self, client):
        vm_name = "NR-38f0eedd-b5f6-419a-8a27-ec6d35b3dfb3"
        mock_proxmox = MagicMock()
        mock_proxmox.nodes.return_value.storage.get.return_value = [
            {"storage": "backups-lvm", "content": ["backup"]},
        ]

        with patch.object(client, "_get_connection", return_value=mock_proxmox):
            with patch.object(client, "delete_storage_content") as delete_content:
                deleted = client.delete_cloudinit_iso_for_vm("pve01", vm_name)

        assert deleted == 0
        delete_content.assert_not_called()
