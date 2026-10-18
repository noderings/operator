"""VirtFusion SSH key management."""

import hashlib
import logging

from .client import VirtFusionClient

logger = logging.getLogger(__name__)


def ensure_ssh_keys(client: VirtFusionClient, user_id: int, public_keys: list[str]) -> list[int]:
    """Ensure SSH keys exist for the VF user, returning a list of key ids.

    Deduplicates by checking existing keys before creating.
    """
    if not public_keys:
        return []

    existing = client.list_ssh_keys(user_id)
    existing_fps: dict[str, int] = {}
    for key in existing:
        pk = key.get("publicKey", "")
        key_id = key.get("id")
        if pk and key_id:
            fp = hashlib.sha256(pk.strip().encode()).hexdigest()[:16]
            existing_fps[fp] = int(key_id)

    key_ids: list[int] = []
    for pub_key in public_keys:
        fp = hashlib.sha256(pub_key.strip().encode()).hexdigest()[:16]
        if fp in existing_fps:
            key_ids.append(existing_fps[fp])
            logger.debug("SSH key already exists (id=%s)", existing_fps[fp])
            continue

        name = f"nr-{fp}"
        result = client.create_ssh_key(user_id, name, pub_key.strip())
        if result:
            data = result.get("data", result)
            kid = int(data.get("id"))
            key_ids.append(kid)
            existing_fps[fp] = kid
            logger.info("Created SSH key %s (id=%s)", name, kid)

    return key_ids
