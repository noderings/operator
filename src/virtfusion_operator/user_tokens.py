"""Read the provider-supplied VirtFusion User API token from agent credentials.

The provider creates one normal VirtFusion client user and generates a User API
token in that user's Account → API. The agent stores it; we do not mint tokens
via panel login or resetPassword.
"""

from __future__ import annotations

from .config import VirtFusionCredentials
from .errors import VirtFusionHTTPError


def ensure_user_api_token(creds: VirtFusionCredentials) -> str:
    """Return the User API bearer token from agent credentials. Never log it."""
    token = (creds.user_api_token or "").strip()
    if not token:
        raise VirtFusionHTTPError(
            "VIRTFUSION_USER_API_TOKEN is not set. Create a VirtFusion client user, "
            "log in as that user, and generate a User API token (Account → API).",
            status_code=400,
        )
    return token
