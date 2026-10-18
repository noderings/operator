"""Error types for the SolusVM 2 operator."""


class SolusVMHTTPError(Exception):
    """Raised for SolusVM 2 API HTTP errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TransientError(SolusVMHTTPError):
    """Raised for retryable HTTP errors (429, 502, 503, transport)."""
