"""Local console bridge. The browser talks to NodeRings; this process dials Wings.

Session and transfer grants are HMAC tokens signed with the panel client key so
the operator (mint) and a separate console Deployment (consume) can share them
without in-memory state. That lets pterodactyl-console be offloaded like
vnc-gateway while pterodactyl-system stays local with the exporter and Alloy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .client import PanelError
from .console import (
    _send_frame,
    _send_text,
    _try_read_frame,
    open_live_console,
    panel_origin,
    websocket_credentials,
)
from .constants import (
    CONSOLE_BRIDGE_ADDR_DEFAULT,
    CONSOLE_BRIDGE_ADDR_ENV,
    CONSOLE_BRIDGE_ADDR_LISTEN_ALL,
    CONSOLE_CLIENT_KEY_ENV,
    CONSOLE_GATEWAY_REPLICAS_ENV,
    CONSOLE_GATEWAY_REQUIRED_REPLICAS,
    CONSOLE_SESSION_TTL_SECONDS,
    CONSOLE_TRANSFER_OPS,
    CONSOLE_TRANSFER_TTL_SECONDS,
)
from .panel import panel_client as _client
from .limits import MAX_FILE_BYTES, within_file_limit
from . import files

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_used: dict[str, float] = {}
_lock = threading.Lock()
_started = False


def _signing_key() -> bytes:
    return os.environ.get(CONSOLE_CLIENT_KEY_ENV, "").strip().encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    key = _signing_key()
    if not key:
        raise RuntimeError(f"{CONSOLE_CLIENT_KEY_ENV} is required to mint console grants")
    sig = hmac.new(key, body, hashlib.sha256).digest()
    # Encode payload and signature separately so dots inside JSON (file paths)
    # cannot break the token. Same shape as a compact JWT.
    return _b64url(body) + "." + _b64url(sig)


def _unsign(token: str) -> dict[str, Any] | None:
    key = _signing_key()
    if not key or not token or token.count(".") != 1:
        return None
    try:
        left, _, right = token.partition(".")
        body = _b64url_decode(left)
        sig = _b64url_decode(right)
        expect = hmac.new(key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(body.decode("utf-8"))
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError, TypeError):
        return None


def _consume(token: str, retain_for: float) -> bool:
    """Mark a grant one-time on this process. True if this call wins."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with _lock:
        for key, exp in list(_used.items()):
            if exp < now:
                del _used[key]
        if digest in _used:
            return False
        _used[digest] = now + retain_for
        return True


def register_session(identifier: str) -> str:
    identifier = str(identifier).strip()
    if not identifier:
        raise ValueError("console session requires a server identifier")
    return _sign(
        {
            "kind": "session",
            "identifier": identifier,
            "exp": int(time.time()) + CONSOLE_SESSION_TTL_SECONDS,
            "nonce": secrets.token_urlsafe(12),
        }
    )


def take_session(session_id: str) -> str:
    payload = _unsign(session_id)
    if not payload or payload.get("kind") != "session":
        return ""
    if not _consume(session_id, CONSOLE_SESSION_TTL_SECONDS):
        return ""
    return str(payload.get("identifier") or "")


def register_transfer(identifier: str, op: str, meta: dict[str, Any]) -> str:
    identifier = str(identifier).strip()
    if not identifier:
        raise ValueError("file transfer requires a server identifier")
    if op not in CONSOLE_TRANSFER_OPS:
        raise ValueError(f"unsupported file operation: {op}")
    return _sign(
        {
            "kind": "transfer",
            "identifier": identifier,
            "op": op,
            "meta": meta,
            "exp": int(time.time()) + CONSOLE_TRANSFER_TTL_SECONDS,
            "nonce": secrets.token_urlsafe(12),
        }
    )


def take_transfer(transfer_id: str) -> dict[str, Any] | None:
    payload = _unsign(transfer_id)
    if not payload or payload.get("kind") != "transfer":
        return None
    if not _consume(transfer_id, CONSOLE_TRANSFER_TTL_SECONDS):
        return None
    meta = payload.get("meta")
    return {
        "identifier": str(payload.get("identifier") or ""),
        "op": str(payload.get("op") or ""),
        "meta": meta if isinstance(meta, dict) else {},
        "exp": float(payload.get("exp") or 0),
    }


def _require_single_replica() -> None:
    """HMAC anti-replay is process-local; refuse to run with replicas != 1."""
    raw = os.environ.get(CONSOLE_GATEWAY_REPLICAS_ENV, str(CONSOLE_GATEWAY_REQUIRED_REPLICAS))
    try:
        replicas = int(raw.strip())
    except ValueError as exc:
        raise SystemExit(f"{CONSOLE_GATEWAY_REPLICAS_ENV}={raw!r} is not an integer") from exc
    if replicas != CONSOLE_GATEWAY_REQUIRED_REPLICAS:
        raise SystemExit(
            f"console gateway one-time grants require "
            f"{CONSOLE_GATEWAY_REPLICAS_ENV}={CONSOLE_GATEWAY_REQUIRED_REPLICAS} "
            f"(got {replicas})"
        )


def start_bridge() -> None:
    global _started
    if _started:
        return
    raw = os.environ.get(CONSOLE_BRIDGE_ADDR_ENV, CONSOLE_BRIDGE_ADDR_DEFAULT)
    host, port_text = raw.rsplit(":", 1)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((host, int(port_text)))
    except OSError as exc:
        logging.getLogger(__name__).error("console bridge not started: %s", exc)
        return
    server.listen(16)
    _started = True

    def accept_loop() -> None:
        while True:
            conn, _addr = server.accept()
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    logging.getLogger(__name__).info("console bridge listening on %s", raw)


def main() -> None:
    """Console-only process for the pterodactyl-console Deployment."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    _require_single_replica()
    os.environ.setdefault(CONSOLE_BRIDGE_ADDR_ENV, CONSOLE_BRIDGE_ADDR_LISTEN_ALL)
    start_bridge()
    if not _started:
        raise SystemExit("console bridge failed to bind")
    threading.Event().wait()


def _handle(conn: socket.socket) -> None:
    try:
        _bridge(conn)
    except Exception:
        try:
            conn.close()
        except OSError:
            return
    finally:
        try:
            conn.close()
        except OSError:
            return


def _read_http(conn: socket.socket) -> tuple[str, dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 16384:
            break
    head, _, extra = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").split("\r\n")
    request = lines[0] if lines else ""
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return request, headers, extra


def _relay(
    src: socket.socket,
    dst: socket.socket,
    buffer: bytes,
    stop: threading.Event,
    *,
    reply_mask: bool,
    send_mask: bool,
    drop: set[str],
) -> None:
    """Copy text frames from src to dst. Answer pings so Wings does not drop the socket."""
    src.settimeout(1.0)
    while not stop.is_set():
        frame = _try_read_frame(buffer)
        if frame is None:
            try:
                chunk = src.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            continue
        opcode, payload, buffer = frame
        if opcode == 0x9:
            try:
                _send_frame(src, 0xA, payload, mask=reply_mask)
            except OSError:
                break
            continue
        if opcode != 0x1 or not payload:
            continue
        text = payload.decode("utf-8", "replace")
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        if event.get("event") in drop:
            continue
        try:
            _send_text(dst, text, mask=send_mask)
        except OSError:
            break
    stop.set()


def _bridge(conn: socket.socket) -> None:
    request, headers, extra = _read_http(conn)
    parts = request.split(" ")
    if len(parts) < 2:
        _send_http(conn, 400, b"")
        return
    parsed = urlparse(parts[1])
    if parsed.path == "/file":
        _file_transfer(conn, parts[0], parsed, headers, extra)
        return
    if parts[0] != "GET":
        _send_http(conn, 400, b"")
        return
    session_id = (parse_qs(parsed.query).get("session") or [""])[0]
    identifier = take_session(session_id)
    key = headers.get("sec-websocket-key", "")
    if parsed.path != "/bridge" or not identifier or not key:
        conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
        return
    accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode()
    )
    try:
        client = _client()
        creds = websocket_credentials(client, identifier)
        wings, wings_buffer = open_live_console(
            creds["socket"], creds["token"], origin=panel_origin(client.base_url)
        )
    except (PanelError, OSError, RuntimeError) as exc:
        _send_text(conn, json.dumps({"event": "error", "args": [str(exc)]}), mask=False)
        return
    _send_text(conn, json.dumps({"event": "auth success", "args": []}), mask=False)
    # Wings keeps the scrollback until asked. Without this the page stays on
    # "Waiting for console output..." for a quiet server.
    _send_text(wings, json.dumps({"event": "send logs", "args": [None]}))
    stop = threading.Event()

    def wings_to_browser() -> None:
        _relay(
            wings,
            conn,
            wings_buffer,
            stop,
            reply_mask=True,
            send_mask=False,
            drop={"auth", "stats"},
        )

    def browser_to_wings() -> None:
        _relay(
            conn, wings, extra, stop, reply_mask=False, send_mask=True, drop={"auth", "set token"}
        )

    left = threading.Thread(target=wings_to_browser, daemon=True)
    right = threading.Thread(target=browser_to_wings, daemon=True)
    left.start()
    right.start()
    left.join(timeout=600)
    stop.set()
    right.join(timeout=2)
    try:
        wings.close()
    except OSError:
        return


def _send_chunked(conn: socket.socket, chunks: Any) -> None:
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Connection: close\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )
    for chunk in chunks:
        if not chunk:
            continue
        conn.sendall(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
    conn.sendall(b"0\r\n\r\n")


def _send_http(
    conn: socket.socket, status: int, body: bytes, content_type: str = "text/plain"
) -> None:
    reason = {
        200: "OK",
        204: "No Content",
        400: "Bad Request",
        401: "Unauthorized",
        413: "Payload Too Large",
        502: "Bad Gateway",
    }.get(status, "Error")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Connection: close\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode()
    conn.sendall(head + body)


def _read_exact(conn: socket.socket, extra: bytes, length: int) -> bytes:
    body = bytearray(extra)
    conn.settimeout(300)
    while len(body) < length:
        chunk = conn.recv(min(1024 * 1024, length - len(body)))
        if not chunk:
            break
        body += chunk
    return bytes(body)


def _safe_error(exc: BaseException) -> bytes:
    message = exc.detail if isinstance(exc, PanelError) else str(exc)
    if not message or "token=" in message or "://" in message:
        message = "The panel rejected that request."
    return message[:300].encode()


def _file_transfer(
    conn: socket.socket,
    method: str,
    parsed: Any,
    headers: dict[str, str],
    extra: bytes,
) -> None:
    if method not in {"GET", "POST"}:
        _send_http(conn, 400, b"")
        return
    transfer_id = (parse_qs(parsed.query).get("transfer") or [""])[0]
    grant = take_transfer(transfer_id)
    if not grant:
        _send_http(conn, 401, b"unauthorized")
        return
    op = str(grant["op"])
    meta = grant["meta"] if isinstance(grant["meta"], dict) else {}
    identifier = str(grant["identifier"])
    try:
        if method == "POST":
            if op not in {"write", "upload"}:
                _send_http(conn, 400, b"wrong operation")
                return
            length = int(headers.get("content-length") or "-1")
            if not within_file_limit(length):
                _send_http(conn, 413, b"file is too large")
                return
            body = _read_exact(conn, extra, length)
            if len(body) != length:
                _send_http(conn, 400, b"short body")
                return
            client = _client()
            client.timeout = 300
            if op == "write":
                files.write_file(client, identifier, str(meta.get("path") or ""), body)
            else:
                files.upload_file(
                    client,
                    identifier,
                    str(meta.get("directory") or "/"),
                    str(meta.get("filename") or ""),
                    body,
                )
            _send_http(conn, 204, b"")
            return
        if op == "stream":
            client = _client()
            response = files.open_download(client, identifier, str(meta.get("path") or ""))
            try:
                _send_chunked(conn, response.iter_content(chunk_size=64 * 1024))
            except Exception:
                return
            finally:
                response.close()
            return
        if op not in {"read", "download"}:
            _send_http(conn, 400, b"wrong operation")
            return
        client = _client()
        client.timeout = 300
        if op == "read":
            data = files.read_file(client, identifier, str(meta.get("path") or ""))
        else:
            data = files.download_file(client, identifier, str(meta.get("path") or ""))
        if len(data) > MAX_FILE_BYTES:
            _send_http(conn, 413, b"file is too large")
            return
        _send_http(conn, 200, data, "application/octet-stream")
    except Exception as exc:
        _send_http(conn, 502, _safe_error(exc))


if __name__ == "__main__":
    main()
