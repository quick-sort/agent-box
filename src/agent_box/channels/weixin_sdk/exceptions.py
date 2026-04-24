class WeixinError(Exception):
    """Base exception for the extracted Weixin SDK."""


class WeixinApiError(WeixinError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class MissingDependencyError(WeixinError):
    """Raised when an optional runtime dependency is missing."""
