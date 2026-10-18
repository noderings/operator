"""HMAC console/transfer grants must verify across processes."""

from __future__ import annotations

import pytest

from pterodactyl_operator import bridge
from pterodactyl_operator.constants import (
    CONSOLE_CLIENT_KEY_ENV,
    CONSOLE_GATEWAY_REPLICAS_ENV,
    CONSOLE_SESSION_TTL_SECONDS,
)


@pytest.fixture(autouse=True)
def _client_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONSOLE_CLIENT_KEY_ENV, "ptlc_test_signing_key")
    bridge._used.clear()


def test_session_round_trip() -> None:
    token = bridge.register_session("abc12def")
    assert bridge.take_session(token) == "abc12def"
    assert bridge.take_session(token) == ""


def test_session_rejects_tamper() -> None:
    token = bridge.register_session("abc12def")
    bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert bridge.take_session(bad) == ""


def test_transfer_round_trip() -> None:
    token = bridge.register_transfer(
        "abc12def",
        "read",
        {"path": "/server.properties"},
    )
    grant = bridge.take_transfer(token)
    assert grant is not None
    assert grant["identifier"] == "abc12def"
    assert grant["op"] == "read"
    assert grant["meta"]["path"] == "/server.properties"
    assert bridge.take_transfer(token) is None


def test_missing_key_cannot_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONSOLE_CLIENT_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError):
        bridge.register_session("abc12def")


def test_expired_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "CONSOLE_SESSION_TTL_SECONDS", -1)
    token = bridge.register_session("abc12def")
    assert bridge.take_session(token) == ""
    assert CONSOLE_SESSION_TTL_SECONDS == 90


def test_wrong_kind_rejected() -> None:
    token = bridge.register_transfer("abc12def", "read", {"path": "/x"})
    assert bridge.take_session(token) == ""


def test_empty_identifier_rejected() -> None:
    with pytest.raises(ValueError):
        bridge.register_session("  ")


def test_unsupported_transfer_op_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported file operation"):
        bridge.register_transfer("abc12def", "delete", {"path": "/x"})


def test_require_single_replica_rejects_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONSOLE_GATEWAY_REPLICAS_ENV, "2")
    with pytest.raises(SystemExit):
        bridge._require_single_replica()
