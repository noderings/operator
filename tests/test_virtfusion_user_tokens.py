"""Provider-supplied VirtFusion User API token (no panel mint)."""

from virtfusion_operator.config import VirtFusionCredentials
from virtfusion_operator.errors import VirtFusionHTTPError
from virtfusion_operator.user_tokens import ensure_user_api_token


def test_ensure_user_api_token_from_credentials():
    creds = VirtFusionCredentials(
        instance_id="default",
        url="https://cp.example.com",
        token="admin",
        user_api_token="user-token",
        user_id=5,
        user_name="noderings",
    )
    assert ensure_user_api_token(creds) == "user-token"


def test_ensure_user_api_token_missing():
    creds = VirtFusionCredentials(
        instance_id="default",
        url="https://cp.example.com",
        token="admin",
    )
    try:
        ensure_user_api_token(creds)
    except VirtFusionHTTPError as e:
        assert e.status_code == 400
        return
    raise AssertionError("expected VirtFusionHTTPError")
