"""VirtFusion Admin API HTTP client.

Pure HTTP — no Kopf, no kubernetes imports.
Base: {url}/api/v1
Authorization: Bearer {token}
"""

import logging
import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

from .constants import (
    CONNECT_TIMEOUT,
    MAX_RETRIES,
    PAGINATION_PAGE_SIZE,
    READ_TIMEOUT,
    RETRY_STATUS_CODES,
    SUCCESS_CODES,
)
from .errors import TransientError, VirtFusionHTTPError

logger = logging.getLogger(__name__)


def as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _verify_ssl_from_env() -> bool:
    raw = os.getenv("VIRTFUSION_VERIFY_SSL", "false").strip().lower()
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
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _check_response(resp: requests.Response, context: str = "") -> dict[str, Any] | None:
    if resp.status_code in SUCCESS_CODES:
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    status = resp.status_code
    try:
        body = resp.json()
        errors = body.get("errors", body.get("message", resp.text))
    except Exception:
        errors = resp.text

    msg = f"VirtFusion API error {status} {context}: {errors}"

    if status in RETRY_STATUS_CODES:
        raise TransientError(msg, status_code=status)
    raise VirtFusionHTTPError(msg, status_code=status)


class VirtFusionClient:
    """HTTP client for VirtFusion Admin API."""

    def __init__(self, url: str, token: str, verify_ssl: bool | None = None) -> None:
        self.url = url.rstrip("/")
        self._base = f"{self.url}/api/v1"
        if verify_ssl is None:
            verify_ssl = _verify_ssl_from_env()
        self._session = _build_session(token, verify_ssl)
        self._timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any] | None:
        resp = self._session.get(self._url(path), params=params, timeout=self._timeout)
        return _check_response(resp, f"GET {path}")

    def _post(
        self, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any] | None:
        resp = self._session.post(self._url(path), json=json, params=params, timeout=self._timeout)
        return _check_response(resp, f"POST {path}")

    def _put(
        self, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any] | None:
        resp = self._session.put(self._url(path), json=json, params=params, timeout=self._timeout)
        return _check_response(resp, f"PUT {path}")

    def _delete(
        self, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any] | None:
        resp = self._session.delete(
            self._url(path), json=json, params=params, timeout=self._timeout
        )
        return _check_response(resp, f"DELETE {path}")

    def _paginate(self, path: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Follow next_page_url pagination, collecting all items."""
        all_items: list[dict[str, Any]] = []
        p = dict(params or {})
        p.setdefault("results", PAGINATION_PAGE_SIZE)
        url: str | None = self._url(path)
        while url:
            resp = self._session.get(url, params=p, timeout=self._timeout)
            body = _check_response(resp, f"GET {path}")
            if not body:
                break
            data = body.get("data", [])
            if isinstance(data, list):
                all_items.extend(data)
            next_url = body.get("next_page_url")
            url = next_url if isinstance(next_url, str) and next_url else None
            p = {}  # subsequent pages already have params in URL
        return all_items

    # ---- Connect / hypervisors ----

    def connect(self) -> dict[str, Any] | None:
        return self._get("/connect")

    def list_hypervisors(self) -> list[dict[str, Any]]:
        return self._paginate("/compute/hypervisors")

    def get_hypervisor(self, hypervisor_id: int) -> dict[str, Any] | None:
        return self._get(f"/compute/hypervisors/{hypervisor_id}")

    def get_group_resources(self, group_id: int) -> dict[str, Any] | None:
        return self._get(f"/compute/hypervisors/groups/{group_id}/resources")

    # ---- Users ----

    def get_user_by_ext(self, ext_relation_id: str | int) -> dict[str, Any] | None:
        try:
            return self._get(f"/users/{ext_relation_id}/byExtRelation")
        except VirtFusionHTTPError as e:
            if e.status_code == 404:
                return None
            raise

    def create_user(
        self, name: str, email: str, ext_relation_id: str | int
    ) -> dict[str, Any] | None:
        return self._post(
            "/users",
            json={
                "name": name,
                "email": email,
                "extRelationId": ext_relation_id,
                "sendMail": False,
                "selfService": 0,
            },
        )

    # ---- SSH Keys ----

    def create_ssh_key(self, user_id: int, name: str, public_key: str) -> dict[str, Any] | None:
        return self._post(
            "/ssh_keys",
            json={"userId": user_id, "name": name, "publicKey": public_key},
        )

    def list_ssh_keys(self, user_id: int) -> list[dict[str, Any]]:
        result = self._get(f"/ssh_keys/user/{user_id}")
        if result and "data" in result:
            data = result["data"]
            return data if isinstance(data, list) else []
        return []

    # ---- Servers ----

    def create_server(self, body: dict[str, Any], dry_run: bool = False) -> dict[str, Any] | None:
        params = {"dryRun": "true"} if dry_run else None
        return self._post("/servers", json=body, params=params)

    def get_server(self, server_id: int) -> dict[str, Any] | None:
        return self._get(f"/servers/{server_id}")

    def add_ipv4(self, server_id: int, addresses: list[str]) -> dict[str, Any] | None:
        return self._post(
            f"/servers/{server_id}/ipv4",
            json={"interface": "primary", "ip": addresses},
        )

    def add_ipv4_qty(
        self, server_id: int, quantity: int, interface: str = "primary"
    ) -> dict[str, Any] | None:
        """POST /servers/{id}/ipv4Qty — VF assigns from its IP block."""
        return self._post(
            f"/servers/{server_id}/ipv4Qty",
            json={"interface": interface, "quantity": quantity},
        )

    def remove_ipv4(self, server_id: int, addresses: list[str]) -> dict[str, Any] | None:
        return self._delete(
            f"/servers/{server_id}/ipv4",
            json={"interface": "primary", "ip": addresses},
        )

    def build(self, server_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        return self._post(f"/servers/{server_id}/build", json=body)

    def power(self, server_id: int, action: str) -> dict[str, Any] | None:
        return self._post(f"/servers/{server_id}/power/{action}")

    def suspend(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"/servers/{server_id}/suspend")

    def unsuspend(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"/servers/{server_id}/unsuspend")

    def delete_server(self, server_id: int, delay: int = 0) -> dict[str, Any] | None:
        return self._delete(f"/servers/{server_id}", params={"delay": delay})

    def reset_password(self, server_id: int, user: str = "root") -> dict[str, Any] | None:
        return self._post(
            f"/servers/{server_id}/resetPassword",
            json={"user": user, "sendMail": False},
        )

    def reset_user_panel_password(self, ext_relation_id: int) -> dict[str, Any] | None:
        """POST /users/{ext}/byExtRelation/resetPassword {sendMail:false}.

        Returns data.email and data.password (panel login, not guest). Never log.
        """
        return self._post(
            f"/users/{ext_relation_id}/byExtRelation/resetPassword",
            json={"sendMail": False},
        )

    def enable_vnc(self, server_id: int) -> dict[str, Any] | None:
        return self._post(f"/servers/{server_id}/vnc", json={"action": "enable"})

    def get_vnc(self, server_id: int) -> dict[str, Any] | None:
        return self._get(f"/servers/{server_id}/vnc")

    def change_package(self, server_id: int, package_id: int) -> dict[str, Any] | None:
        return self._put(f"/servers/{server_id}/package/{package_id}")

    def modify_cpu(self, server_id: int, cores: int) -> dict[str, Any] | None:
        return self._put(f"/servers/{server_id}/modify/cpuCores", json={"cores": cores})

    def modify_memory(self, server_id: int, mb: int) -> dict[str, Any] | None:
        return self._put(f"/servers/{server_id}/modify/memory", json={"memory": mb})

    def modify_cpu_throttle(self, server_id: int, percent: int) -> dict[str, Any] | None:
        return self._put(
            f"/servers/{server_id}/modify/cpuThrottle",
            json={"cpuThrottle": percent},
        )

    # ---- Queue ----

    def get_queue(self, queue_id: int) -> dict[str, Any] | None:
        return self._get(f"/queue/{queue_id}")

    # ---- OS Templates ----

    def list_os_templates(self, package_id: int) -> dict[str, Any] | None:
        return self._get(f"/media/templates/fromServerPackageSpec/{package_id}")

    # ---- Firewall (apply existing panel rulesets by id; no create-ruleset API) ----

    def list_firewall_rulesets(self) -> list[dict[str, Any]]:
        """Return panel firewall rulesets `{id, name, ...}` when the Global API exposes them.

        Official VirtFusion docs have no list endpoint. GET /firewall/rulesets 404s on
        current control planes; the fake API implements it so operators can match by name.
        """
        try:
            return self._paginate("/firewall/rulesets")
        except VirtFusionHTTPError as e:
            if e.status_code == 404:
                return []
            raise

    def get_firewall(
        self, server_id: int, interface: str = "primary", *, sync: bool = False
    ) -> dict[str, Any]:
        params = {"sync": "true" if sync else "false"}
        body = self._get(f"/servers/{server_id}/firewall/{interface}", params=params)
        if isinstance(body, dict):
            return body
        return {"data": {"enabled": False, "rules": []}}

    def enable_firewall(self, server_id: int, interface: str = "primary") -> dict[str, Any] | None:
        return self._post(
            f"/servers/{server_id}/firewall/{interface}/enable",
            params={"sync": "true"},
        )

    def disable_firewall(self, server_id: int, interface: str = "primary") -> dict[str, Any] | None:
        return self._post(
            f"/servers/{server_id}/firewall/{interface}/disable",
            params={"sync": "true"},
        )

    def apply_firewall_rulesets(
        self,
        server_id: int,
        ruleset_ids: list[int],
        interface: str = "primary",
    ) -> dict[str, Any] | None:
        """POST replace-all rulesets. Empty list flushes rules."""
        return self._post(
            f"/servers/{server_id}/firewall/{interface}/rules",
            json={"rulesets": [int(i) for i in ruleset_ids]},
            params={"sync": "true"},
        )
