"""Cloud-init userData for VirtFusion User API POST /api/server/{uuid}/build.

Clients no longer send cloud-init. This module applies the create-form
password and compiled Instant App YAML only.
"""

from __future__ import annotations

import re

import yaml

_LIST_KEYS = ("runcmd", "write_files", "packages", "bootcmd")
_SAFE_USER = re.compile(r"^[A-Za-z0-9_.-]+$")

# VirtFusion merges vendor cloud-config after ours: it drops `chpasswd` /
# `disable_root`, injects `users:` with its own root hash, then prepends runcmd
# (`dnf upgrade` on RHEL). Our runcmd therefore runs ~5 minutes later. Apply
# the hash from bootcmd (before VF `users:`) and keep re-applying until runcmd
# deletes the file. Pipe so SELinux passwd_t never reads /root.
#
# VF also YAML-merges API `hostname` with userData `hostname`. Two strings
# become a list, which cloud-init rejects. Lift hostname out of userData onto
# the API field so the guest gets one string.
_SET_PASSWORD = (
    "cat /etc/noderings/nr-chpasswd | /usr/sbin/chpasswd -e "
    "|| cat /etc/noderings/nr-chpasswd | chpasswd -e || true"
)
_RELOAD_SSH = "systemctl try-reload-or-restart sshd || systemctl try-reload-or-restart ssh || true"
_PASSWORD_LOOP = (
    "while [ -f /etc/noderings/nr-chpasswd ]; do "
    "cat /etc/noderings/nr-chpasswd | /usr/sbin/chpasswd -e "
    "|| cat /etc/noderings/nr-chpasswd | chpasswd -e || true; "
    "sleep 3; done"
)
_START_PASSWORD_LOOP = (
    "systemd-run --unit=noderings-passwd --collect /bin/bash -c "
    f"'{_PASSWORD_LOOP}' "
    "|| nohup bash -c "
    f"'{_PASSWORD_LOOP}' "
    "</dev/null >/dev/null 2>&1 &"
)


def _sha512_crypt(password: str) -> str:
    """SHA-512 crypt hash for chpasswd -e (same on Debian and RHEL)."""
    import crypt

    hashed = crypt.crypt(password, crypt.METHOD_SHA512)
    if not hashed or not hashed.startswith("$6$"):
        raise ValueError("guest password hash failed")
    return hashed


def _dump(doc: dict) -> str:
    """Dump cloud-config. Wide width so shell commands are not folded mid-token."""
    return "#cloud-config\n" + yaml.safe_dump(doc, sort_keys=False, width=10000)


def _safe_user(user: str) -> str:
    name = (user or "root").strip() or "root"
    if not _SAFE_USER.fullmatch(name):
        raise ValueError("guest user name is invalid")
    return name


def _ensure_user_cmd(user: str) -> str:
    if user == "root":
        return "true"
    return (
        f"id {user} >/dev/null 2>&1 || "
        f"useradd -m -s /bin/bash {user} || "
        f"adduser --disabled-password --gecos '' {user} || true"
    )


def _password_doc(pairs: list[tuple[str, str]]) -> dict:
    """Cloud-config that sets hashed passwords via bootcmd (survives VF vendor merge)."""
    if not pairs:
        raise ValueError("guest password users are empty")
    quoted = " ".join(f"'{user}:{hashed}'" for user, hashed in pairs)
    lines = "".join(f"{user}:{hashed}\n" for user, hashed in pairs)
    write_hash = (
        "mkdir -p /etc/noderings && "
        f"printf '%s\\n' {quoted} > /etc/noderings/nr-chpasswd && "
        "chmod 600 /etc/noderings/nr-chpasswd"
    )
    ensure_cmds = [_ensure_user_cmd(user) for user, _ in pairs]
    return {
        "ssh_pwauth": True,
        "disable_root": False,
        "chpasswd": {
            "expire": False,
            "users": [{"name": user, "password": hashed, "type": "hash"} for user, hashed in pairs],
        },
        "bootcmd": [*ensure_cmds, write_hash, _SET_PASSWORD, _START_PASSWORD_LOOP],
        "write_files": [
            {
                "path": "/etc/ssh/sshd_config.d/00-noderings.conf",
                "permissions": "0644",
                "content": "PermitRootLogin yes\nPasswordAuthentication yes\n",
            },
            {
                "path": "/etc/noderings/nr-chpasswd",
                "permissions": "0600",
                "content": lines,
            },
        ],
        "runcmd": [
            "mkdir -p /etc/ssh/sshd_config.d /etc/noderings",
            _SET_PASSWORD,
            "rm -f /etc/noderings/nr-chpasswd",
            (
                "sed -i -E "
                "'s/^#?PermitRootLogin.*/PermitRootLogin yes/; "
                "s/^#?PasswordAuthentication.*/PasswordAuthentication yes/' "
                "/etc/ssh/sshd_config || true"
            ),
            _RELOAD_SSH,
        ],
    }


def cloud_config_for_password(password: str, user: str = "root") -> str:
    """Build cloud-config that sets *user*'s password and enables SSH password login.

    Standard cloud-init keys plus a hashed chpasswd pipe (not /root) so the same
    document works on Debian/Ubuntu and SELinux RHEL-family images. Never log
    the password. Reject newlines so YAML/shell cannot split the secret.
    """
    if not password or "\n" in password or "\r" in password:
        raise ValueError("guest password must be a single non-empty line")
    user = _safe_user(user)
    hashed = _sha512_crypt(password)
    return _dump(_password_doc([(user, hashed)]))


def _as_mapping(raw: str | None) -> dict:
    """Parse cloud-config YAML into a dict. Empty input → {}."""
    if not raw or not str(raw).strip():
        return {}
    text = str(raw).strip()
    if text.startswith("#cloud-config"):
        parts = text.split("\n", 1)
        text = parts[1] if len(parts) > 1 else ""
        if not text.strip():
            return {}
    loaded = yaml.safe_load(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("cloud-config user-data must be a YAML mapping")
    return loaded


def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def merge_cloud_config(
    *,
    password: str = "",
    guest_user: str = "root",
    extra: str | None = None,
) -> str:
    """Merge NodeRings password cloud-config with Instant App YAML.

    Instant Apps include a create-form password (or SSH keys). Password keys
    (ssh_pwauth, chpasswd, disable_root) win. List keys are concatenated with
    password entries first so sshd reload still runs.

    ``extra`` is compiled Instant App YAML only. Clients no longer send
    cloud-init.
    """
    extra_doc = _as_mapping(extra)
    pwd_doc: dict = {}
    if password:
        dumped = cloud_config_for_password(password, guest_user)
        body = dumped.split("\n", 1)[1] if "\n" in dumped else ""
        parsed = yaml.safe_load(body) if body.strip() else {}
        if isinstance(parsed, dict):
            pwd_doc = parsed
    if not extra_doc and not pwd_doc:
        raise ValueError("user-data is empty")
    out = dict(extra_doc)
    for key, value in pwd_doc.items():
        if key in _LIST_KEYS:
            out[key] = _as_list(value) + _as_list(out.get(key))
        else:
            out[key] = value
    return _dump(out)


def _hostname_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def lift_hostname_for_vf_api(user_data: str, fallback: str | None = None) -> tuple[str, str | None]:
    """Move hostname/fqdn out of userData onto the VF build hostname field.

    VirtFusion concatenates API hostname with userData hostname into a YAML list,
    which cloud-init rejects. One string on the API field is honored.

    VF also 422s non-FQDN API hostnames (no dot). Prefer a dotted name: YAML FQDN,
    else the CR fallback, else YAML label + .noderings.internal.
    """
    doc = _as_mapping(user_data)
    host = _hostname_string(doc.pop("hostname", None))
    fqdn = _hostname_string(doc.pop("fqdn", None))
    yaml_host = host or fqdn
    api_hostname = yaml_host if yaml_host and "." in yaml_host else None
    if not api_hostname and fallback and "." in fallback.strip():
        api_hostname = fallback.strip()
    if not api_hostname and yaml_host:
        api_hostname = f"{yaml_host}.noderings.internal"
    if not api_hostname and fallback and fallback.strip():
        api_hostname = fallback.strip()
    dumped = _dump(doc) if doc else "#cloud-config\n{}\n"
    return dumped, api_hostname


def prepare_virtfusion_user_data(
    *,
    password: str = "",
    guest_user: str = "root",
    extra: str | None = None,
    hostname_fallback: str | None = None,
) -> tuple[str, str | None]:
    """Return (userData YAML, VF API hostname) for User API POST /build.

    VirtFusion has no chosen-password field and deletes ``chpasswd`` from every
    document. The create-form password is applied with an operator-owned
    cloud-config (hashed file + bootcmd). Instant App YAML is merged when
    present. Hostname is lifted onto the API field so VF cannot turn two
    hostname strings into a list.

    Clients no longer send cloud-init. ``extra`` is Instant App YAML only.
    """
    extra_text = (extra or "").strip()
    if extra_text and not password:
        doc = _as_mapping(extra_text)
        yaml_host = _hostname_string(doc.get("hostname")) or _hostname_string(doc.get("fqdn"))
        if yaml_host and "." in yaml_host:
            return lift_hostname_for_vf_api(extra_text, None)
        if yaml_host:
            # VF 422s non-FQDN API hostnames. Keep Instant App hostname in YAML.
            return extra_text, None
        return extra_text, (
            hostname_fallback.strip() if hostname_fallback and hostname_fallback.strip() else None
        )
    merged = merge_cloud_config(password=password, guest_user=guest_user, extra=extra)
    return lift_hostname_for_vf_api(merged, hostname_fallback)
