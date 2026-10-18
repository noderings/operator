"""Proxmox credential discovery from the environment."""

from proxmox_operator.config import load_settings_from_env


def test_load_settings_from_default_proxmox_url_env(monkeypatch):
    monkeypatch.setenv("PROXMOX_URL", "https://192.0.2.10:8006")
    monkeypatch.setenv("PROXMOX_USERNAME", "kopfoperator@pve")
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "dev-token")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "dev-secret")

    settings = load_settings_from_env("https://192.0.2.10:8006/")

    assert settings.host == "192.0.2.10:8006"
    assert settings.username == "kopfoperator@pve"
    assert settings.token_name == "dev-token"
    assert settings.token_value == "dev-secret"


def test_named_instance_env_wins_over_default_url(monkeypatch):
    monkeypatch.setenv("PROXMOX_URL", "https://192.0.2.10:8006")
    monkeypatch.setenv("PROXMOX_USERNAME", "default@pve")
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "default-token")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "default-secret")
    monkeypatch.setenv("PROXMOX_LAB_URL", "https://192.0.2.10:8006/")
    monkeypatch.setenv("PROXMOX_LAB_USERNAME", "lab@pve")
    monkeypatch.setenv("PROXMOX_LAB_TOKEN_ID", "lab-token")
    monkeypatch.setenv("PROXMOX_LAB_TOKEN_SECRET", "lab-secret")

    settings = load_settings_from_env("https://192.0.2.10:8006/")

    assert settings.username == "lab@pve"
    assert settings.token_name == "lab-token"
