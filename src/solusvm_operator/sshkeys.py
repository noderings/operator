"""SolusVM SSH key management. Field name is ``body``, not VF ``publicKey``."""

from __future__ import annotations

import hashlib
import logging

from .client import SolusVMClient, unwrap_data
from .errors import SolusVMHTTPError

logger = logging.getLogger(__name__)


def _fingerprint(material: str) -> str:
    return hashlib.sha256(material.strip().encode()).hexdigest()[:16]


def _key_body(key: dict) -> str:
    return str(key.get("body") or key.get("public_key") or key.get("publicKey") or "")


def _key_name(key: dict) -> str:
    return str(key.get("name") or "")


def _index_keys(keys: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    """Map fingerprint and name to SolusVM key id."""
    by_fp: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for key in keys:
        key_id = key.get("id")
        if not key_id:
            continue
        kid = int(key_id)
        body = _key_body(key)
        if body:
            by_fp[_fingerprint(body)] = kid
        name = _key_name(key)
        if name:
            by_name[name] = kid
    return by_fp, by_name


def _load_key_index(client: SolusVMClient, user_id: int) -> tuple[dict[str, int], dict[str, int]]:
    """User-scoped list plus unfiltered list.

    Create-VM omits ``user`` when the SVM account has no project, so keys
    land on the admin user. Reinstall then looks up the org mailbox user and
    misses them; POST /ssh_keys fails 422 "name has already been taken".
    """
    by_fp, by_name = _index_keys(client.list_ssh_keys(user_id))
    all_fp, all_name = _index_keys(client.list_ssh_keys(None))
    by_fp.update(all_fp)
    by_name.update(all_name)
    return by_fp, by_name


def ensure_ssh_keys(client: SolusVMClient, user_id: int, public_keys: list[str]) -> list[int]:
    """Ensure SSH keys exist for the SolusVM user, returning key ids.

    POST /ssh_keys {name, body, user_id}. Dedupes via GET /ssh_keys.
    """
    if not public_keys:
        return []

    existing_fps, existing_names = _load_key_index(client, user_id)

    key_ids: list[int] = []
    for pub_key in public_keys:
        material = pub_key.strip()
        if not material:
            continue
        fp = _fingerprint(material)
        if fp in existing_fps:
            key_ids.append(existing_fps[fp])
            logger.debug("SSH key already exists (id=%s)", existing_fps[fp])
            continue

        name = f"nr-{fp}"
        if name in existing_names:
            kid = existing_names[name]
            key_ids.append(kid)
            existing_fps[fp] = kid
            logger.info("Reusing SSH key %s (id=%s) by name", name, kid)
            continue

        try:
            result = client.create_ssh_key(name, material, user_id)
        except SolusVMHTTPError as e:
            taken = e.status_code == 422 and "already been taken" in (e.message or str(e)).lower()
            if not taken:
                raise
            existing_fps, existing_names = _load_key_index(client, user_id)
            reused_id = existing_fps.get(fp) or existing_names.get(name)
            if reused_id is None:
                raise
            key_ids.append(reused_id)
            existing_fps[fp] = reused_id
            existing_names[name] = reused_id
            logger.info("Reusing SSH key %s (id=%s) after name conflict", name, reused_id)
            continue

        data = unwrap_data(result)
        created_id = data.get("id")
        if created_id:
            kid_int = int(created_id)
            key_ids.append(kid_int)
            existing_fps[fp] = kid_int
            existing_names[name] = kid_int
            logger.info("Created SSH key %s (id=%s)", name, kid_int)

    return key_ids
