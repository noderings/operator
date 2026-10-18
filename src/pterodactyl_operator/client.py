"""HTTP client for the Pterodactyl panel. Application key and client key stay apart.

Server calls are limited to panel external ids that start with NR-. The panel
name is the customer hostname. Eggs, nodes, and users are unchanged. A server
that fails the check is not returned and is not modified.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

import requests

from .constants import ACCEPT

# Same rule as the metrics exporter: only NodeRings servers are in scope.
NR_PREFIX = "NR-"
FOREIGN_SERVER = "operator only manages servers whose external id starts with NR-"


def is_noderings_server(external_id: object) -> bool:
    return str(external_id or "").strip().upper().startswith(NR_PREFIX)


class PanelError(Exception):
    def __init__(self, status: int, detail: str, body: bytes = b"") -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body


class PanelClient:
    def __init__(
        self,
        base_url: str,
        application_key: str,
        client_key: str,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.application_key = application_key
        self.client_key = client_key
        self.timeout = timeout
        self.session = requests.Session()

    def application(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        return self._call(
            method, "/api/application" + path, self.application_key, json_body, params
        )

    def client_api(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        raw_body: bytes | str | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        return self._call(
            method,
            "/api/client" + path,
            self.client_key,
            json_body,
            params,
            raw_body=raw_body,
            content_type=content_type,
            timeout=timeout,
        )

    def _call(
        self,
        method: str,
        path: str,
        key: str,
        json_body: Any,
        params: dict[str, Any] | None,
        raw_body: bytes | str | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        target = _server_target(path)
        if target is not None:
            self._require_noderings(method, path, key, json_body, target)
        status, payload = self._request(
            method, path, key, json_body, params, raw_body, content_type, timeout=timeout
        )
        if target is None or status >= 400:
            return status, payload
        if target["kind"] == "collection" and method.upper() == "GET" and isinstance(payload, dict):
            return status, _only_noderings(payload)
        if target["exact"] and method.upper() == "GET":
            if target["api"] == "client":
                # The client API omits external_id. The application record has NR-….
                external_id = self._external_id_from_client_payload(payload)
                if not is_noderings_server(external_id):
                    raise PanelError(403, FOREIGN_SERVER)
            else:
                _require_record(payload)
        return status, payload

    def _require_noderings(
        self,
        method: str,
        path: str,
        key: str,
        json_body: Any,
        target: dict[str, Any],
    ) -> None:
        verb = method.upper()
        if target["kind"] == "collection":
            if verb == "POST" and not is_noderings_server(_body_external_id(json_body)):
                raise PanelError(403, FOREIGN_SERVER)
            if verb not in {"GET", "POST"}:
                raise PanelError(403, FOREIGN_SERVER)
            return
        if target["exact"] and verb == "GET":
            return
        external_id = self._server_external_id(key, target)
        if external_id is None:
            raise PanelError(404, "not found")
        if not is_noderings_server(external_id):
            raise PanelError(403, FOREIGN_SERVER)

    def _server_external_id(self, key: str, target: dict[str, Any]) -> str | None:
        if target["kind"] == "external":
            lookup = f"/api/application/servers/external/{target['ident']}"
        elif target["api"] == "client":
            lookup = f"/api/client/servers/{target['ident']}"
        else:
            lookup = f"/api/application/servers/{target['ident']}"
        try:
            _, payload = self._request("GET", lookup, key, None, None, None, None)
        except PanelError as exc:
            if exc.status == 404:
                return None
            raise
        if target["api"] == "client":
            return self._external_id_from_client_payload(payload)
        attrs = payload.get("attributes") if isinstance(payload, dict) else None
        if not isinstance(attrs, dict):
            return ""
        return str(attrs.get("external_id") or "")

    def _external_id_from_client_payload(self, payload: Any) -> str:
        """Client server objects leave external_id off. Read it from the application API."""
        attrs = payload.get("attributes") if isinstance(payload, dict) else None
        if not isinstance(attrs, dict):
            return ""
        external_id = str(attrs.get("external_id") or "").strip()
        if external_id:
            return external_id
        internal_id = attrs.get("internal_id")
        if internal_id in (None, ""):
            return ""
        return self._application_external_id(internal_id)

    def _application_external_id(self, server_id: object) -> str:
        try:
            _, payload = self._request(
                "GET",
                f"/api/application/servers/{server_id}",
                self.application_key,
                None,
                None,
                None,
                None,
            )
        except PanelError as exc:
            if exc.status == 404:
                return ""
            raise
        attrs = payload.get("attributes") if isinstance(payload, dict) else None
        if not isinstance(attrs, dict):
            return ""
        return str(attrs.get("external_id") or "")

    def _request(
        self,
        method: str,
        path: str,
        key: str,
        json_body: Any,
        params: dict[str, Any] | None,
        raw_body: bytes | str | None = None,
        content_type: str | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {"Authorization": f"Bearer {key}", "Accept": ACCEPT}
        data = None
        if raw_body is not None:
            data = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
            headers["Content-Type"] = content_type or "text/plain"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        response = self.session.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            timeout=self.timeout if timeout is None else timeout,
        )
        body = response.content
        if response.status_code >= 400:
            detail = _detail(body) or response.reason
            raise PanelError(response.status_code, detail, body)
        if not body:
            return response.status_code, None
        ctype = response.headers.get("Content-Type", "")
        if "json" in ctype:
            return response.status_code, response.json()
        return response.status_code, body


def _server_target(path: str) -> dict[str, Any] | None:
    path = path.split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "api" or parts[2] != "servers":
        return None
    if parts[1] not in {"application", "client"}:
        return None
    rest = parts[3:]
    if not rest:
        return {"api": parts[1], "kind": "collection", "ident": "", "exact": False}
    if parts[1] == "application" and rest[0] == "external":
        if len(rest) < 2 or not rest[1]:
            return None
        return {"api": parts[1], "kind": "external", "ident": rest[1], "exact": len(rest) == 2}
    return {"api": parts[1], "kind": "one", "ident": rest[0], "exact": len(rest) == 1}


def _body_external_id(json_body: Any) -> str:
    if not isinstance(json_body, dict):
        return ""
    return str(json_body.get("external_id") or "")


def _only_noderings(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return payload
    kept = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes")
        if isinstance(attrs, dict) and is_noderings_server(attrs.get("external_id")):
            kept.append(item)
    filtered = dict(payload)
    filtered["data"] = kept
    return filtered


def _require_record(payload: Any) -> None:
    attrs = payload.get("attributes") if isinstance(payload, dict) else None
    if not isinstance(attrs, dict) or not is_noderings_server(attrs.get("external_id")):
        raise PanelError(403, FOREIGN_SERVER)


def _detail(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", "replace")[:300]
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("detail") or first.get("code") or first)
    return body.decode("utf-8", "replace")[:300]
