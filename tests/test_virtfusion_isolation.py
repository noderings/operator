"""VirtFusion and Proxmox operators must not share GVRs, clients, or imports."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent / "src"


def _read_tree(pkg: str) -> str:
    parts = []
    for path in (ROOT / pkg).rglob("*.py"):
        parts.append(path.read_text())
    return "\n".join(parts)


def test_proxmox_operator_never_mentions_virtfusion():
    src = _read_tree("proxmox_operator").lower()
    assert "virtfusion" not in src
    assert "vm.virtfusion.com" not in src


def test_virtfusion_operator_never_imports_proxmox():
    src = _read_tree("virtfusion_operator")
    lower = src.lower()
    assert "proxmox_operator" not in src
    assert "proxmoxer" not in lower
    assert "vm.proxmox.com" not in lower
    assert "/pve/" not in lower
    assert "pvesh" not in lower
