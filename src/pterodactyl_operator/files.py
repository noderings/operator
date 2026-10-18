"""Client API file manager. Paths are inside the server root. `..` is rejected here."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote

from .client import PanelClient, PanelError


class UnsafePath(ValueError):
    pass


def reject_unsafe(path: str) -> str:
    if path is None:
        raise UnsafePath(path)
    decoded = unquote(unquote(path))
    if "\x00" in decoded or "\\" in decoded or ".." in decoded.replace("\\", "/").split("/"):
        raise UnsafePath(path)
    return path


def _server(identifier: str) -> str:
    return f"/servers/{identifier}"


def empty_recycle_bin(client: PanelClient, identifier: str) -> None:
    """Drop every file the panel is still holding after a delete. Retention is 24 hours."""
    client.client_api("POST", _server(identifier) + "/files/trash/empty")


def list_files(client: PanelClient, identifier: str, directory: str = "/") -> list[dict[str, Any]]:
    reject_unsafe(directory)
    _, payload = client.client_api(
        "GET",
        _server(identifier) + "/files/list",
        params={"directory": directory},
    )
    return [item["attributes"] for item in payload.get("data") or []]


def create_folder(client: PanelClient, identifier: str, root: str, name: str) -> None:
    reject_unsafe(root)
    reject_unsafe(name)
    client.client_api(
        "POST",
        _server(identifier) + "/files/create-folder",
        json_body={"root": root, "name": name},
    )


def write_file(client: PanelClient, identifier: str, path: str, content: bytes) -> None:
    reject_unsafe(path)
    client.client_api(
        "POST",
        _server(identifier) + "/files/write",
        params={"file": path},
        raw_body=content,
        content_type="text/plain",
    )


def read_file(client: PanelClient, identifier: str, path: str) -> bytes:
    reject_unsafe(path)
    try:
        _, body = client.client_api(
            "GET",
            _server(identifier) + "/files/contents",
            params={"file": path},
        )
    except PanelError as exc:
        # The panel editor refuses contents above its own cap. The signed download
        # URL still returns the file, up to the same ceiling as upload.
        if "too large" not in (exc.detail or "").lower():
            raise
        return download_file(client, identifier, path)
    if isinstance(body, bytes):
        return body
    return str(body).encode()


def copy_file(client: PanelClient, identifier: str, location: str) -> None:
    reject_unsafe(location)
    client.client_api(
        "POST",
        _server(identifier) + "/files/copy",
        json_body={"location": location},
    )


def wipe_server_files(client: PanelClient, identifier: str) -> None:
    """Delete every name in the server root. The root directory itself stays.

    An empty root is success. One delete call removes directories.
    """
    names = [
        str(row.get("name") or "")
        for row in list_files(client, identifier, "/")
        if str(row.get("name") or "") not in {"", ".", ".."}
    ]
    if not names:
        return
    delete_files(client, identifier, "/", names)


def delete_files(client: PanelClient, identifier: str, root: str, names: list[str]) -> None:
    reject_unsafe(root)
    for name in names:
        reject_unsafe(name)
    client.client_api(
        "POST",
        _server(identifier) + "/files/delete",
        json_body={"root": root, "files": names},
    )


def upload_url(client: PanelClient, identifier: str) -> str:
    _, payload = client.client_api("GET", _server(identifier) + "/files/upload")
    return payload["attributes"]["url"]


def rename_file(client: PanelClient, identifier: str, root: str, source: str, dest: str) -> None:
    reject_unsafe(root)
    reject_unsafe(source)
    reject_unsafe(dest)
    client.client_api(
        "PUT",
        _server(identifier) + "/files/rename",
        json_body={"root": root, "files": [{"from": source, "to": dest}]},
    )


def chmod_file(client: PanelClient, identifier: str, root: str, name: str, mode: str) -> None:
    reject_unsafe(root)
    reject_unsafe(name)
    client.client_api(
        "POST",
        _server(identifier) + "/files/chmod",
        json_body={"root": root, "files": [{"file": name, "mode": mode}]},
    )


def compress_files(
    client: PanelClient, identifier: str, root: str, names: list[str]
) -> dict[str, Any]:
    reject_unsafe(root)
    for name in names:
        reject_unsafe(name)
    _, payload = client.client_api(
        "POST",
        _server(identifier) + "/files/compress",
        json_body={"root": root, "files": names},
        timeout=240,
    )
    return payload["attributes"]


def decompress_file(client: PanelClient, identifier: str, root: str, name: str) -> None:
    reject_unsafe(root)
    reject_unsafe(name)
    client.client_api(
        "POST",
        _server(identifier) + "/files/decompress",
        json_body={"root": root, "file": name},
    )


def download_file(client: PanelClient, identifier: str, path: str) -> bytes:
    response = open_download(client, identifier, path)
    try:
        if response.status_code >= 400:
            raise RuntimeError(f"download {response.status_code}")
        return response.content
    finally:
        response.close()


def open_download(client: PanelClient, identifier: str, path: str) -> Any:
    """Signed panel URL, streamed. The caller closes the response."""
    reject_unsafe(path)
    _, payload = client.client_api(
        "GET",
        _server(identifier) + "/files/download",
        params={"file": path},
    )
    url = payload["attributes"]["url"]
    response = client.session.get(url, timeout=(30, 300), stream=True)
    if response.status_code >= 400:
        response.close()
        raise RuntimeError(f"download {response.status_code}")
    return response


def upload_file(
    client: PanelClient, identifier: str, directory: str, filename: str, content: bytes
) -> None:
    reject_unsafe(directory)
    reject_unsafe(filename)
    signed = upload_url(client, identifier)
    url = signed + ("&" if "?" in signed else "?") + "directory=" + quote(directory, safe="/")
    response = client.session.post(
        url,
        files={"files": (filename, content, "application/octet-stream")},
        timeout=client.timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"upload {response.status_code}: {response.text[:200]}")
