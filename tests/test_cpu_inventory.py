"""Tests for ProxmoxNode CPU inventory parsing (node_handler)."""

from proxmox_operator.node_handler import (
    CpuInventoryBrand,
    build_inventory_from_cpuinfo,
    extract_cpuinfo,
    infer_brand,
    parse_mhz,
)


def test_extract_cpuinfo_nested_data() -> None:
    raw = {"data": {"cpuinfo": {"cpus": 8, "model": "Intel", "sockets": 1, "mhz": "2800"}}}
    assert extract_cpuinfo(raw) == {"cpus": 8, "model": "Intel", "sockets": 1, "mhz": "2800"}


def test_infer_brand() -> None:
    assert infer_brand("11th Gen Intel(R) Core(TM) i7") is CpuInventoryBrand.INTEL
    assert infer_brand("AMD Ryzen 9") is CpuInventoryBrand.AMD
    assert infer_brand("unknown chip") is CpuInventoryBrand.UNKNOWN
    # StrEnum members compare equal to their string value
    assert infer_brand("AMD Ryzen 9") == "amd"


def test_parse_mhz() -> None:
    assert parse_mhz("2808.000") == 2808.0
    assert parse_mhz(None) is None


def test_build_inventory_from_cpuinfo() -> None:
    inv = build_inventory_from_cpuinfo(
        {
            "cpus": 8,
            "sockets": 1,
            "mhz": "2800",
            "model": "Intel Fake",
        }
    )
    assert inv["phase"] == "Ready"
    assert inv["cores"] == 8
    assert inv["sockets"] == 1
    assert inv["brand"] == "intel"
    assert inv["mhz"] == 2800.0
    assert "collectedAt" in inv
