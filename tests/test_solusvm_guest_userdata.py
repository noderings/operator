"""SolusVM user_data is Instant App files plus an sshd drop-in for passwords."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from solusvm_operator.guest_userdata import prepare_solusvm_user_data


def test_password_emits_sshd_dropin_not_chpasswd():
    doc = prepare_solusvm_user_data(password="s3cret!")
    assert doc.startswith("#cloud-config\n")
    assert "99-noderings.conf" in doc
    assert "PermitRootLogin yes" in doc
    assert "PasswordAuthentication yes" in doc
    assert "s3cret!" not in doc
    assert "/etc/noderings/nr-chpasswd" not in doc
    assert "chpasswd -e" not in doc
    assert "ssh_pwauth" not in doc
    assert "chpasswd:" not in doc
    assert "hostname:" not in doc


def test_instant_app_keeps_files_and_drops_auth_keys():
    extra = """#cloud-config
chpasswd:
    expire: false
    users:
        - name: ubuntu
          password: "1234"
          type: text
hostname: my-svmlab01
manage_etc_hosts: true
ssh_pwauth: true
users:
  - name: root
    lock_passwd: false
write_files:
  - path: /opt/noderings/app/docker-compose.yml
    permissions: "0644"
    content: |
      services:
        app:
          image: portainer/portainer-ce
runcmd:
  - systemctl enable --now docker
packages:
  - curl
"""
    doc = prepare_solusvm_user_data(extra=extra)
    assert "/opt/noderings/app/docker-compose.yml" in doc
    assert "portainer/portainer-ce" in doc
    assert "systemctl enable --now docker" in doc
    assert "curl" in doc
    assert "1234" not in doc
    assert "ssh_pwauth" not in doc
    assert "chpasswd:" not in doc
    assert "hostname:" not in doc
    assert "users:" not in doc
    assert "/etc/noderings/nr-chpasswd" not in doc


def test_password_plus_instant_app_prefixes_sshd_dropin():
    extra = """#cloud-config
write_files:
  - path: /etc/nr-app-ok
    content: app-ok
runcmd:
  - echo app-ok
"""
    doc = prepare_solusvm_user_data(password="api-pass", extra=extra)
    assert "99-noderings.conf" in doc
    assert "/etc/nr-app-ok" in doc
    assert "echo app-ok" in doc
    assert "api-pass" not in doc
    assert doc.find("99-noderings.conf") < doc.find("/etc/nr-app-ok")


def test_ssh_only_without_instant_app_sends_nothing():
    assert prepare_solusvm_user_data() is None
    assert prepare_solusvm_user_data(password="", extra=None) is None
