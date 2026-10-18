"""Tests for Pydantic CR models: CPUConfig affinity/numa validation."""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from proxmox_operator.types import CPUConfig


class TestCPUConfigAffinity:
    @pytest.mark.parametrize("affinity", ["0-3,64-67", "0", "0,4", "0-3", "1,2-5,8"])
    def test_accepts_valid_cpusets(self, affinity):
        cfg = CPUConfig(cores=4, affinity=affinity)
        assert cfg.affinity == affinity

    @pytest.mark.parametrize("affinity", ["abc", "0-", ",3", "0-3,", "-1", "0--3", "0,,3"])
    def test_rejects_invalid_cpusets(self, affinity):
        with pytest.raises(ValidationError):
            CPUConfig(cores=4, affinity=affinity)

    def test_defaults_shared_vm(self):
        cfg = CPUConfig(cores=2)
        assert cfg.affinity is None
        assert cfg.numa is True

    def test_numa_flag(self):
        cfg = CPUConfig(cores=8, affinity="0-3,64-67", numa=True)
        assert cfg.numa is True

    def test_numa_false_coerced_to_true(self):
        """numa is always enabled for shared and dedicated VMs."""
        cfg = CPUConfig(cores=4, affinity="0-3,64-67")
        assert cfg.numa is True
        assert CPUConfig(cores=4, affinity="8-15", numa=False).numa is True
        assert CPUConfig(cores=2, numa=False).numa is True
