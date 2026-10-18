"""SolusVM 2 REST API HTTP client.

Pure HTTP — no Kopf, no kubernetes imports.
Base: {url}/api/v1/  (trailing slash like WHMCS Connector)
Authorization: Bearer {token}

Pagination is Laravel ``?page=N`` until ``meta.last_page``.
Never retries 422. Retries 429 / 502 / 503 and transport errors.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

from .constants import (
    CONNECT_TIMEOUT,
    MAX_RETRIES,
    READ_TIMEOUT,
    RETRY_STATUS_CODES,
    SUCCESS_CODES,
)
from .errors import SolusVMHTTPError, TransientError

logger = logging.getLogger(__name__)

_RETRY_METHODS = ["GET", "HEAD", "PUT", "PATCH", "DELETE"]


def as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _verify_ssl_from_env() -> bool:
    raw = os.getenv("SOLUSVM_VERIFY_SSL", "false").strip().lower()
    return raw in ("1", "true", "yes")


def _build_session(token: str, verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    session.verify = verify_ssl
    if not verify_ssl:
        disable_warnings(InsecureRequestWarning)
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=list(RETRY_STATUS_CODES),
        allowed_methods=_RETRY_METHODS,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _format_error_body(body: Any, text: str) -> str:
    """Surface 422 ``errors`` JSON (field → messages) without guessing."""
    if not isinstance(body, dict):
        return text
    errors = body.get("errors")
    message = body.get("message")
    parts: list[str] = []
    if message:
        parts.append(str(message))
    if isinstance(errors, dict):
        for field, msgs in errors.items():
            if isinstance(msgs, list):
                parts.append(f"{field}: {'; '.join(str(m) for m in msgs)}")
            else:
                parts.append(f"{field}: {msgs}")
    elif errors is not None and errors != message:
        parts.append(str(errors))
    return "; ".join(parts) if parts else text


def _check_response(resp: requests.Response, context: str = "") -> dict[str, Any] | None:
    if resp.status_code in SUCCESS_CODES:
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else {"data": payload}

    status = resp.status_code
    try:
        body = resp.json()
    except Exception:
        body = None
    errors = _format_error_body(body, resp.text or "")

    if status in (401, 403):
        msg = f"token rejected ({status} {context})"
        raise SolusVMHTTPError(msg, status_code=status)

    if status == 422:
        msg = f"SolusVM API error 422 {context}: {errors}"
        raise SolusVMHTTPError(msg, status_code=status)

    msg = f"SolusVM API error {status} {context}: {errors}"
    if status in RETRY_STATUS_CODES:
        raise TransientError(msg, status_code=status)
    if status == 409:
        raise TransientError(msg, status_code=status)
    raise SolusVMHTTPError(msg, status_code=status)


def unwrap_data(result: dict[str, Any] | None) -> dict[str, Any]:
    """Return the ``data`` object or the body itself. Accepts wrapped and top-level."""
    if not result:
        return {}
    data = result.get("data", result)
    return data if isinstance(data, dict) else {}


def management_host(url: str) -> str:
    """Hostname (no scheme/path) from a management-node URL."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname or url.split("/")[0]


def _ssh_key_owner_id(key: dict[str, Any]) -> int | None:
    owner = key.get("user") or key.get("owner")
    if isinstance(owner, dict) and owner.get("id") is not None:
        return int(owner["id"])
    if key.get("user_id") is not None:
        return int(key["user_id"])
    if key.get("userId") is not None:
        return int(key["userId"])
    return None


class SolusVMClient:
    """HTTP client for the SolusVM 2 management-node API."""

    def __init__(self, url: str, token: str, verify_ssl: bool | None = None) -> None:
        # Trailing slash like WHMCS Connector: {origin}/api/v1/
        self._base = f"{url.rstrip('/')}/api/v1/"
        self._origin = url.rstrip("/")
        if verify_ssl is None:
            verify_ssl = _verify_ssl_from_env()
        self._session = _build_session(token, verify_ssl)
        self._timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)

    def _url(self, path: str) -> str:
        return f"{self._base}{path.lstrip('/')}"

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any] | None:
        resp = self._session.get(self._url(path), params=params, timeout=self._timeout)
        return _check_response(resp, f"GET {path}")

    def _post(
        self, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any] | None:
        resp = self._session.post(self._url(path), json=json, params=params, timeout=self._timeout)
        return _check_response(resp, f"POST {path}")

    def _patch(
        self, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any] | None:
        resp = self._session.patch(self._url(path), json=json, params=params, timeout=self._timeout)
        return _check_response(resp, f"PATCH {path}")

    def _delete(
        self, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any] | None:
        resp = self._session.delete(
            self._url(path), json=json, params=params, timeout=self._timeout
        )
        return _check_response(resp, f"DELETE {path}")

    def _paginate(self, path: str, params: dict | None = None) -> list[dict[str, Any]]:
        """WHMCS ListRequest: GET {entity}?page=N until meta.last_page.

        Do not follow VirtFusion ``next_page_url``.
        """
        all_items: list[dict[str, Any]] = []
        page = 1
        p = dict(params or {})
        while True:
            p["page"] = page
            body = self._get(path, params=p)
            if not body:
                break
            data = body.get("data", [])
            if isinstance(data, list):
                all_items.extend(item for item in data if isinstance(item, dict))
            elif isinstance(data, dict):
                all_items.append(data)
            meta = as_mapping(body.get("meta"))
            last_page = int(meta.get("last_page") or page)
            if page >= last_page:
                break
            page += 1
        return all_items

    # ---- Account / inventory ----

    def account(self) -> dict[str, Any] | None:
        return self._get("account")

    def list_compute_resources(self) -> list[dict[str, Any]]:
        return self._paginate("compute_resources")

    def get_compute_resource(self, compute_resource_id: int) -> dict[str, Any] | None:
        return self._get(f"compute_resources/{compute_resource_id}")

    def list_locations(self) -> list[dict[str, Any]]:
        return self._paginate("locations")

    def list_plans(self) -> list[dict[str, Any]]:
        return self._paginate("plans")

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        return self._get(f"plans/{plan_id}")

    def list_os_images(self) -> list[dict[str, Any]]:
        return self._paginate("os_images")

    def get_os_image_version(self, version_id: int) -> dict[str, Any] | None:
        return self._get(f"os_image_versions/{version_id}")

    # ---- Users ----

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        users = self._paginate("users", params={"filter[search]": email})
        needle = (email or "").strip().lower()
        for user in users:
            if str(user.get("email") or "").strip().lower() == needle:
                return user
        return None

    def create_user(self, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._post("users", json=body)

    def create_one_time_user_token(self, user_id: int) -> str:
        """POST /users/{id}/tokens {is_one_time: true}. Return access_token; never store it."""
        result = self._post(f"users/{user_id}/tokens", json={"is_one_time": True})
        data = unwrap_data(result)
        token = data.get("access_token") or data.get("token") or (result or {}).get("access_token")
        if not token:
            raise SolusVMHTTPError("One-time user token response missing access_token")
        return str(token)

    # ---- Projects ----

    def list_projects(self, params: dict | None = None) -> list[dict[str, Any]]:
        return self._paginate("projects", params=params)

    # ---- SSH keys ----

    def create_ssh_key(self, name: str, body: str, user_id: int) -> dict[str, Any] | None:
        return self._post("ssh_keys", json={"name": name, "body": body, "user_id": user_id})

    def list_ssh_keys(self, user_id: int | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            return self._paginate("ssh_keys")
        try:
            keys = self._paginate("ssh_keys", params={"filter[user_id]": user_id})
            if keys:
                return keys
        except SolusVMHTTPError as e:
            if e.status_code != 400:
                raise
            logger.warning(
                "GET /ssh_keys filter[user_id] not allowed (%s); matching by owner",
                e,
            )
        return [k for k in self._paginate("ssh_keys") if _ssh_key_owner_id(k) == user_id]

    # ---- Servers ----

    def create_server(self, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._post("servers", json=body)

    def get_server(self, server_id: int) -> dict[str, Any] | None:
        return self._get(f"servers/{server_id}")

    def add_ips(self, server_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/ips", json=body)

    def delete_ips(self, server_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._delete(f"servers/{server_id}/ips", json=body)

    def start(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/start")

    def stop(self, server_id: int, force: bool = False) -> dict[str, Any] | None:
        json_body = {"force": True} if force else None
        return self._post(f"servers/{server_id}/stop", json=json_body)

    def restart(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/restart")

    def suspend(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/suspend")

    def resume(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/resume")

    def reinstall(self, server_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/reinstall", json=body)

    def reset_password(self, server_id: int, *, bearer: str | None = None) -> dict[str, Any] | None:
        """POST /servers/{id}/reset_password with empty body.

        Optional one-time Bearer is used only for this call and never stored.
        """
        headers = None
        if bearer:
            headers = {
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        resp = self._session.post(
            self._url(f"servers/{server_id}/reset_password"),
            json={},
            timeout=self._timeout,
            headers=headers,
        )
        return _check_response(resp, f"POST /servers/{server_id}/reset_password")

    def vnc_up(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/vnc_up")

    def patch_server(self, server_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._patch(f"servers/{server_id}", json=body)

    def resize(self, server_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._post(f"servers/{server_id}/resize", json=body)

    def delete_server(self, server_id: int) -> dict[str, Any] | None:
        return self._delete(f"servers/{server_id}")

    # ---- Tasks ----

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return self._get(f"tasks/{task_id}")
