"""Error types for the VirtFusion operator."""


class VirtFusionHTTPError(Exception):
    """Raised for VirtFusion Admin API HTTP errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TransientError(VirtFusionHTTPError):
    """Raised for retryable HTTP errors (429, 502, 503, 423, transport)."""
