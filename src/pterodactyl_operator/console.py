"""Wings console websocket. The browser never sees the socket URL or token."""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
from typing import Any
from urllib.parse import urlparse

from .client import PanelClient


def websocket_credentials(client: PanelClient, identifier: str) -> dict[str, str]:
    _, payload = client.client_api("GET", f"/servers/{identifier}/websocket")
    data = payload.get("data") or payload.get("attributes") or payload
    token = data.get("token")
    socket_url = data.get("socket")
    if not token or not socket_url:
        raise RuntimeError("websocket response missing token or socket")
    return {"token": token, "socket": socket_url}


def authenticate(
    socket_url: str, token: str, timeout: float = 15.0, origin: str = ""
) -> list[dict[str, Any]]:
    """Open the Wings socket, send the auth token, and return the first JSON events."""
    parsed = urlparse(socket_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    raw = socket.create_connection((host, port), timeout=timeout)
    raw.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    if not origin:
        origin_scheme = "https" if parsed.scheme == "wss" else "http"
        origin = f"{origin_scheme}://{host}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        "\r\n"
    )
    raw.sendall(request.encode())
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = raw.recv(4096)
        if not chunk:
            break
        header += chunk
    status = header.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raw.close()
        raise RuntimeError(f"websocket upgrade failed: {status.decode('utf-8', 'replace')}")
    extra = header.split(b"\r\n\r\n", 1)[1]
    _send_text(raw, json.dumps({"event": "auth", "args": [token]}))
    events: list[dict[str, Any]] = []
    buffer = extra
    deadline_reads = 8
    while len(events) < deadline_reads:
        while True:
            frame = _try_read_text(buffer)
            if frame is None:
                break
            text, buffer = frame
            if not text:
                continue
            try:
                events.append(json.loads(text))
            except json.JSONDecodeError:
                continue
            if events[-1].get("event") in {"auth success", "jwt error", "daemon error"}:
                raw.close()
                return events
        chunk = raw.recv(65536)
        if not chunk:
            break
        buffer += chunk
    raw.close()
    return events


def panel_origin(base_url: str) -> str:
    """Origin Wings allows. It is the panel URL, not the Wings host."""
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    origin = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        origin += f":{parsed.port}"
    return origin


def open_live_console(
    socket_url: str, token: str, timeout: float = 15.0, origin: str = ""
) -> tuple[socket.socket, bytes]:
    """Dial Wings and authenticate. The caller owns the socket. The token is not returned."""
    parsed = urlparse(socket_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    raw = socket.create_connection((host, port), timeout=timeout)
    if parsed.scheme == "wss":
        raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    raw.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    if not origin:
        origin_scheme = "https" if parsed.scheme == "wss" else "http"
        origin = f"{origin_scheme}://{host}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        "\r\n"
    )
    raw.sendall(request.encode())
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = raw.recv(4096)
        if not chunk:
            break
        header += chunk
    status = header.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raw.close()
        raise RuntimeError(f"websocket upgrade failed: {status.decode('utf-8', 'replace')}")
    extra = header.split(b"\r\n\r\n", 1)[1]
    _send_text(raw, json.dumps({"event": "auth", "args": [token]}))
    buffer = extra
    while True:
        frame = _try_read_text(buffer)
        if frame is None:
            try:
                chunk = raw.recv(65536)
            except TimeoutError:
                raw.close()
                raise RuntimeError("console authentication timed out") from None
            if not chunk:
                raw.close()
                raise RuntimeError("console closed before authentication")
            buffer += chunk
            continue
        text, buffer = frame
        if not text:
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        kind = event.get("event")
        if kind == "auth success":
            raw.settimeout(1.0)
            return raw, buffer
        if kind in {"jwt error", "daemon error"}:
            raw.close()
            raise RuntimeError("console authentication failed")


def _send_frame(sock: socket.socket, opcode: int, payload: bytes, *, mask: bool = True) -> None:
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(mask_bit | 127)
        header.extend(length.to_bytes(8, "big"))
    if mask:
        key = os.urandom(4)
        header.extend(key)
        payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    sock.sendall(bytes(header) + payload)


def _send_text(sock: socket.socket, text: str, *, mask: bool = True) -> None:
    _send_frame(sock, 0x1, text.encode(), mask=mask)


def _try_read_frame(buffer: bytes) -> tuple[int, bytes, bytes] | None:
    """Return opcode, payload, and the unread tail. None means the frame is incomplete."""
    if len(buffer) < 2:
        return None
    length = buffer[1] & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < 4:
            return None
        length = int.from_bytes(buffer[2:4], "big")
        offset = 4
    elif length == 127:
        if len(buffer) < 10:
            return None
        length = int.from_bytes(buffer[2:10], "big")
        offset = 10
    masked = bool(buffer[1] & 0x80)
    if masked:
        if len(buffer) < offset + 4 + length:
            return None
        mask = buffer[offset : offset + 4]
        offset += 4
        data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(buffer[offset : offset + length]))
    else:
        if len(buffer) < offset + length:
            return None
        data = buffer[offset : offset + length]
    opcode = buffer[0] & 0x0F
    rest = buffer[offset + length :]
    return opcode, data, rest


def _try_read_text(buffer: bytes) -> tuple[str, bytes] | None:
    frame = _try_read_frame(buffer)
    if frame is None:
        return None
    opcode, data, rest = frame
    if opcode != 0x1:
        return "", rest
    return data.decode("utf-8", "replace"), rest
