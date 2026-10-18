"""VirtFusion SMTP outbound block via a panel firewall ruleset named smtp."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from vf_fake_api import FakeVirtFusionServer
from virtfusion_operator.client import VirtFusionClient
from virtfusion_operator.smtp_outbound import (
    clear_smtp_ruleset_cache,
    merge_ruleset_ids,
    pick_first_smtp_ruleset,
    reconcile_vm_smtp_outbound_block,
    rule_looks_like_smtp_deny,
    ruleset_name_is_smtp,
)


@pytest.fixture(autouse=True)
def _reset_smtp_cache():
    clear_smtp_ruleset_cache()
    yield
    clear_smtp_ruleset_cache()


@pytest.fixture
def vf_api():
    srv = FakeVirtFusionServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(vf_api):
    return VirtFusionClient(vf_api.url, vf_api.state.token)


def test_ruleset_name_is_smtp():
    assert ruleset_name_is_smtp("Block SMTP")
    assert ruleset_name_is_smtp("smtp-deny")
    assert ruleset_name_is_smtp("SMTP")
    assert not ruleset_name_is_smtp("asd")
    assert not ruleset_name_is_smtp("")


def test_pick_first_smtp_ruleset_by_name():
    rulesets = [
        {"id": 1, "name": "asd"},
        {"id": 2, "name": "Block SMTP"},
        {"id": 3, "name": "smtp-allow"},
    ]
    picked = pick_first_smtp_ruleset(rulesets)
    assert picked is not None
    assert picked["id"] == 2


def test_pick_first_smtp_ruleset_none():
    assert pick_first_smtp_ruleset([{"id": 1, "name": "asd"}]) is None
    assert pick_first_smtp_ruleset([]) is None


def test_merge_ruleset_ids_adds_and_preserves():
    assert merge_ruleset_ids([1], 2, True) == [1, 2]
    assert merge_ruleset_ids([1, 2], 2, True) == [1, 2]
    assert merge_ruleset_ids([1, 2], 2, False) == [1]
    assert merge_ruleset_ids([2], 2, False) == []
    assert merge_ruleset_ids([], 2, True) == [2]


def test_rule_looks_like_smtp_deny():
    assert rule_looks_like_smtp_deny(
        {
            "protocol": 1,
            "direction": 3,
            "action": 1,
            "port_dst_start": 25,
            "port_dst_end": 25,
        }
    )
    assert not rule_looks_like_smtp_deny(
        {
            "protocol": 1,
            "direction": 1,
            "action": 1,
            "port_dst_start": 0,
            "port_dst_end": 0,
        }
    )


def test_reconcile_block_applies_first_smtp_named_ruleset(client, vf_api):
    vf_api.state.servers[100] = {"id": 100}
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(client, 100, True, logger)

    fw = vf_api.state.server_firewall[100]
    assert fw["enabled"] is True
    assert fw["ruleset_ids"] == [2]
    bodies = vf_api.bodies("POST", "/rules")
    assert bodies[-1]["rulesets"] == [2]


def test_reconcile_block_preserves_non_smtp_rulesets(client, vf_api):
    vf_api.state.servers[100] = {"id": 100}
    vf_api.state.server_firewall[100] = {"enabled": True, "ruleset_ids": [1]}
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(client, 100, True, logger)

    assert vf_api.state.server_firewall[100]["ruleset_ids"] == [1, 2]


def test_reconcile_allow_removes_smtp_ruleset(client, vf_api):
    vf_api.state.servers[100] = {"id": 100}
    vf_api.state.server_firewall[100] = {"enabled": True, "ruleset_ids": [1, 2]}
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(client, 100, True, logger)
    reconcile_vm_smtp_outbound_block(client, 100, False, logger)

    fw = vf_api.state.server_firewall[100]
    assert fw["ruleset_ids"] == [1]
    assert fw["enabled"] is True


def test_reconcile_allow_disables_firewall_when_only_smtp(client, vf_api):
    vf_api.state.servers[100] = {"id": 100}
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(client, 100, True, logger)
    reconcile_vm_smtp_outbound_block(client, 100, False, logger)

    fw = vf_api.state.server_firewall[100]
    assert fw["ruleset_ids"] == []
    assert fw["enabled"] is False


def test_reconcile_allow_noop_when_already_open(client, vf_api):
    vf_api.state.servers[100] = {"id": 100}
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(client, 100, False, logger)

    posts = [p for p in vf_api.paths("POST") if "/firewall/" in p]
    assert posts == []
