"""SolusVM user management (ensure user by email, not extRelationId)."""

from __future__ import annotations

import logging
import secrets
import string

from .client import SolusVMClient, unwrap_data
from .constants import PASSWORD_CLASS_SIZE

logger = logging.getLogger(__name__)


def generate_panel_password() -> str:
    """15-char mixed password: 5 digits + 5 A–Z + 5 a–z, shuffled (WHMCS).

    Used only for the SolusVM *user* (panel) account. Never log or return to customers.
    """
    digits = [secrets.choice(string.digits) for _ in range(PASSWORD_CLASS_SIZE)]
    upper = [secrets.choice(string.ascii_uppercase) for _ in range(PASSWORD_CLASS_SIZE)]
    lower = [secrets.choice(string.ascii_lowercase) for _ in range(PASSWORD_CLASS_SIZE)]
    chars = digits + upper + lower
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def ensure_user(client: SolusVMClient, email: str) -> int:
    """Ensure a SolusVM user exists for the given email.

    GET /users?filter[search]=email; POST /users if missing.
    Returns the SolusVM user id. Never logs the generated panel password.
    """
    email = (email or "").strip()
    if not email:
        raise ValueError("userEmail is required")

    existing = client.get_user_by_email(email)
    if existing:
        user_id = existing.get("id")
        if user_id is None:
            raise ValueError(f"SolusVM user for {email} has no id")
        logger.info("Found existing SolusVM user %s for email %s", user_id, email)
        return int(user_id)

    result = client.create_user(
        {
            "email": email,
            "password": generate_panel_password(),
            "status": "active",
        }
    )
    data = unwrap_data(result)
    user_id = data.get("id")
    if not user_id:
        raise RuntimeError(f"Failed to create SolusVM user for {email}")
    logger.info("Created SolusVM user %s for email %s", user_id, email)
    return int(user_id)
