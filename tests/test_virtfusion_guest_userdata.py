"""VirtFusion User API userData cloud-config for chosen guest passwords."""

from virtfusion_operator.guest_userdata import (
    cloud_config_for_password,
    merge_cloud_config,
    prepare_virtfusion_user_data,
)


def test_cloud_config_enables_password_ssh():
    doc = cloud_config_for_password("s3cret!", "root")
    assert doc.startswith("#cloud-config\n")
    assert "ssh_pwauth: true" in doc
    assert "disable_root: false" in doc
    assert "PermitRootLogin yes" in doc
    assert "00-noderings.conf" in doc
    assert "mkdir -p /etc/ssh/sshd_config.d" in doc
    assert "chpasswd" in doc
    assert "$6$" in doc
    assert "type: hash" in doc
    assert "/etc/noderings/nr-chpasswd" in doc
    assert "chpasswd -e" in doc
    assert "bootcmd:" in doc
    assert "noderings-passwd" in doc
    assert "while [ -f /etc/noderings/nr-chpasswd ]" in doc
    assert "s3cret!" not in doc
    assert "nr-apply-guest-password" not in doc
    assert "usermod" not in doc


def test_cloud_config_rejects_newlines():
    try:
        cloud_config_for_password("a\nb")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_merge_keeps_password_and_appends_client_runcmd():
    extra = "#cloud-config\npackages:\n- nginx\nruncmd:\n- echo extra\n"
    doc = merge_cloud_config(password="s3cret!", guest_user="root", extra=extra)
    assert doc.startswith("#cloud-config\n")
    assert "ssh_pwauth: true" in doc
    assert "nginx" in doc
    assert "echo extra" in doc
    assert "chpasswd" in doc
    pwd_pos = doc.find("chpasswd -e")
    extra_pos = doc.find("echo extra")
    assert pwd_pos != -1 and extra_pos != -1
    assert pwd_pos < extra_pos
    assert "noderings-passwd" in doc
    assert "while [ -f /etc/noderings/nr-chpasswd ]" in doc


def test_merge_instant_app_shape_keeps_compose_files():
    extra = """#cloud-config
hostname: wp.example.test
users:
  - name: root
    lock_passwd: false
packages:
  - curl
write_files:
  - path: /opt/noderings/wordpress/docker-compose.yml
    content: |
      services:
        wordpress:
          image: wordpress:6.8
runcmd:
  - systemctl enable --now docker
"""
    doc = merge_cloud_config(password="s3cret!", guest_user="root", extra=extra)
    assert "wordpress:6.8" in doc
    assert "/opt/noderings/wordpress/docker-compose.yml" in doc
    assert "chpasswd" in doc
    assert "ssh_pwauth: true" in doc
    assert "systemctl enable --now docker" in doc
    assert "users:" in doc
    assert "00-noderings.conf" in doc


def test_merge_rejects_uncompiled_instant_app_jinja():
    extra = """#cloud-config
{% if vm.hashed_passwd is defined and vm.hashed_passwd %}
ssh_pwauth: true
{% endif %}
"""
    try:
        merge_cloud_config(extra=extra)
    except Exception:
        return
    raise AssertionError("uncompiled Instant App Jinja must not be sent as VF userData")


def test_instant_app_without_password_is_passed_through():
    extra = """#cloud-config
hostname: my-server123
manage_etc_hosts: true
write_files:
  - path: /opt/noderings/app/ok
    content: |
      ok
"""
    ud, hostname = prepare_virtfusion_user_data(
        extra=extra,
        hostname_fallback="tidalhawk-sc-gc-1c-1g-20-01.noderings.internal",
    )
    assert hostname is None
    assert "hostname: my-server123" in ud
    assert "manage_etc_hosts: true" in ud
    assert "/opt/noderings/app/ok" in ud
    assert "/etc/noderings/nr-chpasswd" not in ud
    assert "00-noderings.conf" not in ud


def test_prepare_userdata_without_login_is_pass_through():
    extra = "#cloud-config\npackages:\n- nginx\nwrite_files:\n- path: /etc/nr-ok\n  content: ok\n"
    ud, hostname = prepare_virtfusion_user_data(
        extra=extra,
        hostname_fallback="guest.noderings.internal",
    )
    assert hostname == "guest.noderings.internal"
    assert "nginx" in ud
    assert "/etc/nr-ok" in ud
    assert "/etc/noderings/nr-chpasswd" not in ud


def test_prepare_os_password_for_root_guest():
    ud, hostname = prepare_virtfusion_user_data(
        password="AlmaPass1!",
        guest_user="root",
        hostname_fallback="alma.noderings.internal",
    )
    assert hostname == "alma.noderings.internal"
    assert "/etc/noderings/nr-chpasswd" in ud
    assert "AlmaPass1!" not in ud
    assert "$6$" in ud
    extra = """#cloud-config
hostname: wp.example.test
packages:
  - curl
write_files:
  - path: /opt/noderings/wordpress/docker-compose.yml
    content: |
      services:
        wordpress:
          image: wordpress:6.8
"""
    ud, hostname = prepare_virtfusion_user_data(
        password="s3cret!",
        extra=extra,
        hostname_fallback="guest.noderings.internal",
    )
    assert hostname == "wp.example.test"
    assert "hostname:" not in ud
    assert "wordpress:6.8" in ud
    assert "/etc/noderings/nr-chpasswd" in ud
    assert "s3cret!" not in ud


def test_prepare_uses_form_hostname_when_userdata_has_none():
    extra = "#cloud-config\npackages:\n- nginx\n"
    ud, hostname = prepare_virtfusion_user_data(
        extra=extra,
        hostname_fallback="guest.noderings.internal",
    )
    assert hostname == "guest.noderings.internal"
    assert "nginx" in ud
    assert "/etc/noderings/nr-chpasswd" not in ud


def test_prepare_form_password_without_userdata_keeps_fallback_hostname():
    ud, hostname = prepare_virtfusion_user_data(
        password="s3cret!",
        guest_user="root",
        hostname_fallback="guest.noderings.internal",
    )
    assert hostname == "guest.noderings.internal"
    assert "ssh_pwauth: true" in ud
    assert "$6$" in ud
    assert "s3cret!" not in ud
