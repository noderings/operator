"""VirtFusion user management (ensure user by extRelationId)."""

from __future__ import annotations

import hashlib
import logging
import uuid

from .client import VirtFusionClient

logger = logging.getLogger(__name__)

# VF OpenAPI types POST /users extRelationId as integer in 1..2^64-1.
# NodeRings stores the client org UUID on the CR; map it at the API boundary.
_EXT_RELATION_MAX = (1 << 64) - 1
_EXT_RELATION_MASK = (1 << 63) - 1


def ext_relation_id_for_api(raw: str) -> int:
    """Map a CR userExtRelationId (UUID or digits) to a VF integer extRelationId.

    Numeric strings in range are passed through. UUIDs use the low 63 bits of
    uuid.int (stable, never 0). Any other string is a SHA-256 digest truncated
    the same way so tests and non-UUID labels still POST a valid integer.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("userExtRelationId is required")
    if s.isdigit():
        n = int(s)
        if 1 <= n <= _EXT_RELATION_MAX:
            return n
        raise ValueError(f"extRelationId {n} is outside 1..{_EXT_RELATION_MAX}")
    try:
        n = uuid.UUID(s).int & _EXT_RELATION_MASK
    except ValueError:
        digest = hashlib.sha256(s.encode("utf-8")).digest()
        n = int.from_bytes(digest[:8], "big") & _EXT_RELATION_MASK
    return n or 1


def ensure_user(client: VirtFusionClient, ext_relation_id: str) -> int:
    """Ensure a VirtFusion user exists for the given extRelationId.

    Returns the VF user id.
    """
    api_ext = ext_relation_id_for_api(ext_relation_id)
    existing = client.get_user_by_ext(str(api_ext))
    if existing:
        data = existing.get("data", existing)
        user_id = data.get("id")
        logger.info(
            "Found existing VF user %s for extRelationId %s (cr=%s)",
            user_id,
            api_ext,
            ext_relation_id,
        )
        return int(user_id)

    local = ext_relation_id.replace("@", "+")
    result = client.create_user(
        name=local[:64] or str(api_ext),
        email=f"noreply+{api_ext}@noderings.invalid",
        ext_relation_id=api_ext,
    )
    if not result:
        raise RuntimeError(f"Failed to create VF user for {ext_relation_id}")

    data = result.get("data", result)
    user_id = data.get("id")
    logger.info(
        "Created VF user %s for extRelationId %s (cr=%s)",
        user_id,
        api_ext,
        ext_relation_id,
    )
    return int(user_id)
