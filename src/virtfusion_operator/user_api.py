"""VirtFusion User API HTTP client (not Global /api/v1).

Base: {url}/api
Authorization: Bearer {user token}
Used to POST /server/{uuid}/build with cloud-init userData (chosen password
and Instant App YAML).
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

from .client import _check_response
from .constants import CONNECT_TIMEOUT, MAX_RETRIES, READ_TIMEOUT, RETRY_STATUS_CODES
from .errors import VirtFusionHTTPError

logger = logging.getLogger(__name__)


def _insecure_session() -> requests.Session:
    session = requests.Session()
    session.verify = False
    disable_warnings(InsecureRequestWarning)
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=list(RETRY_STATUS_CODES),
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class VirtFusionUserClient:
    """HTTP client for VirtFusion User API (/api, not /api/v1)."""

    def __init__(self, url: str, token: str) -> None:
        self._base = f"{url.rstrip('/')}/api"
        self._session = _insecure_session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def connect(self) -> dict[str, Any] | None:
        resp = self._session.get(self._url("/connect"), timeout=self._timeout)
        return _check_response(resp, "GET /api/connect")

    def build(self, server_uuid: str, body: dict[str, Any]) -> dict[str, Any] | None:
        resp = self._session.post(
            self._url(f"/server/{server_uuid}/build"),
            json=body,
            timeout=self._timeout,
        )
        return _check_response(resp, f"POST /api/server/{server_uuid}/build")

    def get_task(self, server_uuid: str, task_id: int) -> dict[str, Any] | None:
        resp = self._session.get(
            self._url(f"/server/{server_uuid}/task/{task_id}"),
            timeout=self._timeout,
        )
        return _check_response(resp, f"GET /api/server/{server_uuid}/task/{task_id}")


def mint_user_api_token(panel_url: str, email: str, panel_password: str) -> str:
    """Unused by the operator. Do not call from create/reinstall.

    User API tokens are provider-supplied (client user → Account → API).
    """
    origin = panel_url.rstrip("/")
    session = _insecure_session()
    session.headers.update(
        {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }
    )
    login_page = session.get(f"{origin}/login", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    if login_page.status_code >= 400:
        raise VirtFusionHTTPError(
            f"GET /login failed: {login_page.status_code}",
            status_code=login_page.status_code,
        )
    xsrf = unquote(session.cookies.get("XSRF-TOKEN") or "")
    if xsrf:
        session.headers["X-XSRF-TOKEN"] = xsrf
    login = session.post(
        f"{origin}/login",
        json={"email": email, "password": panel_password},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    if login.status_code >= 400:
        raise VirtFusionHTTPError(
            f"POST /login failed: {login.status_code}",
            status_code=login.status_code,
        )
    xsrf = unquote(session.cookies.get("XSRF-TOKEN") or xsrf)
    if xsrf:
        session.headers["X-XSRF-TOKEN"] = xsrf
    minted = session.post(
        f"{origin}/account/_api",
        json={"name": "noderings-operator"},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    body = _check_response(minted, "POST /account/_api") or {}
    token = body.get("token") or ""
    if not token:
        raise VirtFusionHTTPError("POST /account/_api did not return a token")
    return str(token)


def poll_user_task(
    client: VirtFusionUserClient,
    server_uuid: str,
    task_id: int,
    *,
    timeout: int,
    interval: int,
) -> None:
    """Poll GET /api/server/{uuid}/task/{id} until complete or failed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = client.get_task(server_uuid, task_id)
        if not result:
            time.sleep(interval)
            continue
        data = result.get("data", result)
        if not isinstance(data, dict):
            time.sleep(interval)
            continue
        status = str(data.get("status") or "").lower()
        if data.get("failed") or status in {"failed", "error"}:
            raise VirtFusionHTTPError(f"User API task {task_id} failed")
        if (
            data.get("completed")
            or data.get("success")
            or status
            in {
                "complete",
                "completed",
                "finished",
                "success",
            }
        ):
            logger.info("User API task %s finished", task_id)
            return
        time.sleep(interval)
    raise VirtFusionHTTPError(f"User API task {task_id} timed out after {timeout}s")
