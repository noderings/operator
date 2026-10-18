"""Unit tests for provider outbound SMTP block helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from proxmox_operator.smtp_outbound import (
    SMTP_OUTBOUND_PORTS,
    is_smtp_outbound_rule,
    reconcile_vm_smtp_outbound_block,
    smtp_outbound_comment,
)


def test_smtp_outbound_comment():
    assert smtp_outbound_comment(25) == "noderings:smtp-outbound-block:25"
    assert smtp_outbound_comment(587) == "noderings:smtp-outbound-block:587"


def test_is_smtp_outbound_rule():
    assert is_smtp_outbound_rule({"comment": "noderings:smtp-outbound-block:25"})
    assert not is_smtp_outbound_rule({"comment": "customer-rule"})
    assert not is_smtp_outbound_rule({})
    assert not is_smtp_outbound_rule(None)


def test_reconcile_enabled_creates_missing_rules():
    proxmox = MagicMock()
    rules_api = proxmox.nodes.return_value.qemu.return_value.firewall.rules
    rules_api.get.return_value = []
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(proxmox, "node1", 100, True, logger)

    assert rules_api.post.call_count == len(SMTP_OUTBOUND_PORTS)
    posted_ports = {call.kwargs.get("dport") for call in rules_api.post.call_args_list}
    assert posted_ports == {"25", "465", "587"}
    for call in rules_api.post.call_args_list:
        assert call.kwargs["action"] == "DROP"
        assert call.kwargs["type"] == "out"
        assert call.kwargs["proto"] == "tcp"
        assert call.kwargs["comment"].startswith("noderings:smtp-outbound-block:")


def test_reconcile_enabled_idempotent_when_present():
    proxmox = MagicMock()
    rules_api = proxmox.nodes.return_value.qemu.return_value.firewall.rules
    existing = [
        {"pos": 0, "comment": "noderings:smtp-outbound-block:25", "action": "DROP"},
        {"pos": 1, "comment": "noderings:smtp-outbound-block:465", "action": "DROP"},
        {"pos": 2, "comment": "noderings:smtp-outbound-block:587", "action": "DROP"},
    ]
    rules_api.get.return_value = existing
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(proxmox, "node1", 100, True, logger)

    rules_api.post.assert_not_called()
    rules_api.return_value.delete.assert_not_called()


def test_reconcile_disabled_deletes_only_smtp_rules():
    proxmox = MagicMock()
    rules_api = proxmox.nodes.return_value.qemu.return_value.firewall.rules
    rules_api.get.return_value = [
        {"pos": 0, "comment": "noderings:smtp-outbound-block:25"},
        {"pos": 1, "comment": "customer-https"},
        {"pos": 2, "comment": "noderings:smtp-outbound-block:465"},
        {"pos": 3, "comment": "noderings:smtp-outbound-block:587"},
    ]
    logger = MagicMock()

    reconcile_vm_smtp_outbound_block(proxmox, "node1", 100, False, logger)

    called_pos = [c.args[0] for c in rules_api.call_args_list]
    assert set(called_pos) == {0, 2, 3}
    assert rules_api.post.call_count == 0
