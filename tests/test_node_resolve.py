"""Tests for the ProxmoxNode resolution helpers in ``node_handler``.

These cover the two functions that other modules use to look up a Proxmox
endpoint via the owning ProxmoxNode CR:

- ``get_proxmox_node_spec`` — fetches the ProxmoxNode CR's ``spec`` dict.
- ``resolve_proxmox_url``  — extracts ``spec.proxmoxUrl`` from that spec.

Both must raise ``kopf.PermanentError`` for misconfiguration (missing ref,
404, missing URL, malformed spec) and ``kopf.TemporaryError`` for transient
API errors. Callers that want best-effort semantics wrap them locally.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import kopf
import pytest
from kubernetes import client as k8s_client

from proxmox_operator import node_handler


@pytest.fixture(autouse=True)
def _patch_k8s_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``utils._get_k8s_client`` (as imported into node_handler)
    with a fresh MagicMock for each test."""
    custom_api = MagicMock()
    monkeypatch.setattr(node_handler, "_get_k8s_client", lambda: (MagicMock(), custom_api))
    return custom_api


@pytest.fixture()
def logger() -> logging.Logger:
    return logging.getLogger("node-resolve-test")


def _make_api_exception(status: int, reason: str = "boom") -> k8s_client.ApiException:
    return k8s_client.ApiException(status=status, reason=reason)


def _node_cr(spec: Any) -> dict[str, Any]:
    return {"apiVersion": "vm.proxmox.com/v1alpha1", "kind": "ProxmoxNode", "spec": spec}


# ---------------------------------------------------------------------------
# get_proxmox_node_spec
# ---------------------------------------------------------------------------


class TestGetProxmoxNodeSpec:
    def test_returns_spec_dict_on_success(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.return_value = _node_cr(
            {"name": "pve-1", "proxmoxUrl": "https://10.0.0.10:8006"}
        )
        out = node_handler.get_proxmox_node_spec("node-pve-1", "default", logger)
        assert out == {"name": "pve-1", "proxmoxUrl": "https://10.0.0.10:8006"}
        _patch_k8s_client.get_namespaced_custom_object.assert_called_once_with(
            group="vm.proxmox.com",
            version="v1alpha1",
            namespace="default",
            plural="proxmoxnodes",
            name="node-pve-1",
        )

    def test_missing_node_ref_name_is_permanent(self, logger: logging.Logger) -> None:
        with pytest.raises(kopf.PermanentError, match="proxmoxNodeRef.name is required"):
            node_handler.get_proxmox_node_spec(None, "default", logger)
        with pytest.raises(kopf.PermanentError):
            node_handler.get_proxmox_node_spec("", "default", logger)

    def test_404_is_permanent(self, _patch_k8s_client: MagicMock, logger: logging.Logger) -> None:
        _patch_k8s_client.get_namespaced_custom_object.side_effect = _make_api_exception(
            404, "Not Found"
        )
        with pytest.raises(kopf.PermanentError, match="not found"):
            node_handler.get_proxmox_node_spec("node-missing", "default", logger)

    def test_5xx_is_temporary(self, _patch_k8s_client: MagicMock, logger: logging.Logger) -> None:
        _patch_k8s_client.get_namespaced_custom_object.side_effect = _make_api_exception(
            503, "Service Unavailable"
        )
        with pytest.raises(kopf.TemporaryError, match="failed to fetch ProxmoxNode"):
            node_handler.get_proxmox_node_spec("node-pve-1", "default", logger)

    def test_non_dict_spec_is_permanent(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.return_value = _node_cr("not-a-dict")
        with pytest.raises(kopf.PermanentError, match="invalid spec"):
            node_handler.get_proxmox_node_spec("node-pve-1", "default", logger)

    def test_missing_spec_returns_empty_dict_treated_as_valid(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.return_value = {"kind": "ProxmoxNode"}
        out = node_handler.get_proxmox_node_spec("node-pve-1", "default", logger)
        assert out == {}


# ---------------------------------------------------------------------------
# resolve_proxmox_url
# ---------------------------------------------------------------------------


class TestResolveProxmoxUrl:
    def test_returns_stripped_url(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.return_value = _node_cr(
            {"name": "pve-1", "proxmoxUrl": "  https://10.0.0.10:8006  "}
        )
        assert (
            node_handler.resolve_proxmox_url("node-pve-1", "default", logger)
            == "https://10.0.0.10:8006"
        )

    def test_missing_url_is_permanent(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.return_value = _node_cr({"name": "pve-1"})
        with pytest.raises(kopf.PermanentError, match="has no spec.proxmoxUrl"):
            node_handler.resolve_proxmox_url("node-pve-1", "default", logger)

    def test_empty_url_is_permanent(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.return_value = _node_cr(
            {"name": "pve-1", "proxmoxUrl": "   "}
        )
        with pytest.raises(kopf.PermanentError, match="has no spec.proxmoxUrl"):
            node_handler.resolve_proxmox_url("node-pve-1", "default", logger)

    def test_non_string_url_is_permanent(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.return_value = _node_cr(
            {"name": "pve-1", "proxmoxUrl": 12345}
        )
        with pytest.raises(kopf.PermanentError, match="has no spec.proxmoxUrl"):
            node_handler.resolve_proxmox_url("node-pve-1", "default", logger)

    def test_propagates_permanent_from_get_spec(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.side_effect = _make_api_exception(
            404, "Not Found"
        )
        with pytest.raises(kopf.PermanentError, match="not found"):
            node_handler.resolve_proxmox_url("node-missing", "default", logger)

    def test_propagates_temporary_from_get_spec(
        self, _patch_k8s_client: MagicMock, logger: logging.Logger
    ) -> None:
        _patch_k8s_client.get_namespaced_custom_object.side_effect = _make_api_exception(
            500, "Server Error"
        )
        with pytest.raises(kopf.TemporaryError):
            node_handler.resolve_proxmox_url("node-pve-1", "default", logger)

    def test_missing_ref_name_is_permanent(self, logger: logging.Logger) -> None:
        with pytest.raises(kopf.PermanentError, match="proxmoxNodeRef.name is required"):
            node_handler.resolve_proxmox_url(None, "default", logger)
