"""Cloud-init user_data for SolusVM 2 POST /servers and /reinstall.

Clients no longer send cloud-init. This module only emits operator-owned
snippets plus Instant App YAML.

SolusVM YAML-merges Admin API ``user_data`` into its cidata template. Keys both
sides set (``ssh_pwauth``, ``chpasswd``, ``users``, ``hostname``) become lists
or collide. The template also pins ``cloud_config_modules`` to ``write-files``
+ ``runcmd``. Ubuntu images ship ``60-cloudimg-settings.conf`` with
``PasswordAuthentication no``.

Therefore:

- Guest password and SSH keys go through the SolusVM API fields, not cloud-init.
- Password creates add an sshd drop-in (``write_files`` + ``runcmd``) so Ubuntu
  accepts the API password.
- Instant App YAML is kept only for merge-safe keys (files, runcmd, packages).
  Auth keys in that YAML are dropped; Instant App passwords use the API field.
"""

from __future__ import annotations

import yaml

# Keys SolusVM's cidata template already sets, or that YAML-merge corrupts.
_DROP_KEYS = frozenset(
    {
        "ssh_pwauth",
        "chpasswd",
        "users",
        "hostname",
        "fqdn",
        "manage_etc_hosts",
        "disable_root",
        "cloud_config_modules",
        "ssh_authorized_keys",
        "ssh_deletekeys",
        "ssh_genkeytypes",
    }
)
_LIST_KEYS = ("write_files", "runcmd", "bootcmd", "packages")

_SSHD_DROPIN = "PermitRootLogin yes\nPasswordAuthentication yes\n"
_RELOAD_SSH = "systemctl try-reload-or-restart sshd || systemctl try-reload-or-restart ssh || true"


def _dump(doc: dict) -> str:
    return "#cloud-config\n" + yaml.safe_dump(doc, sort_keys=False, width=10000)


def _as_mapping(raw: str | None) -> dict:
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


def instant_app_for_solusvm(extra: str | None) -> dict:
    """Keep Instant App keys that survive SolusVM cidata merge.

    Drops auth/hostname keys. Password and SSH keys are applied via the
    SolusVM API, not this document.
    """
    doc = _as_mapping(extra)
    if not doc:
        return {}
    return {key: value for key, value in doc.items() if key not in _DROP_KEYS}


def _sshd_password_login() -> dict:
    return {
        "write_files": [
            {
                "path": "/etc/ssh/sshd_config.d/99-noderings.conf",
                "permissions": "0644",
                "content": _SSHD_DROPIN,
            }
        ],
        "runcmd": [_RELOAD_SSH],
    }


def prepare_solusvm_user_data(
    *,
    password: str = "",
    extra: str | None = None,
) -> str | None:
    """Build merge-safe cloud-config for SolusVM cidata.

    ``password`` is the create-form secret (truthy → enable password SSH).
    The plaintext is not written into user_data; SolusVM's API ``password``
    field sets the hash. ``extra`` is compiled Instant App YAML only.

    Returns None when there is nothing to send (SSH-only, no Instant App).
    Never log the password or the YAML.
    """
    app = instant_app_for_solusvm(extra)
    out: dict = dict(app)
    if password:
        sshd = _sshd_password_login()
        for key in _LIST_KEYS:
            extra_list = _as_list(sshd.get(key))
            if extra_list:
                out[key] = extra_list + _as_list(out.get(key))
    if not out:
        return None
    return _dump(out)
