"""Secret-free connection identity and bounded provider failure categories."""

from json import JSONDecodeError
from urllib.parse import urlsplit


def endpoint_identity(base_url: str) -> str:
    """Only origin is diagnostic: paths, userinfo, queries, fragments are omitted."""
    try:
        url = urlsplit(base_url)
        host = url.hostname
        if url.scheme not in {"http", "https"} or not host:
            return "unknown"
        if ":" in host:
            host = f"[{host}]"
        port = f":{url.port}" if url.port is not None else ""
        return f"{url.scheme}://{host}{port}"
    except ValueError:
        return "unknown"


def failure_category(error: BaseException) -> str:
    category = getattr(error, "category", None)
    if isinstance(category, str):
        return category
    name = type(error).__name__
    if name == "CancelledError":
        return "cancelled"
    if isinstance(error, TimeoutError) or name == "APITimeoutError":
        return "timeout"
    if isinstance(error, JSONDecodeError) or name == "APIResponseValidationError":
        return "malformed_response"
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return "authentication"
    if status == 402:
        return "payment_required"
    if status in (404, 410):
        return "model_unavailable"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if isinstance(status, int) and status >= 500:
        return "provider_unavailable"
    if isinstance(status, int) and status >= 400:
        return "invalid_request"
    if isinstance(error, ConnectionError) or name == "APIConnectionError":
        return "connection"
    return "provider_error"


def provider_http_status(error: BaseException | None) -> int | None:
    """Only retain a safe HTTP error status, never a body or header."""
    status = getattr(error, "status_code", None)
    return status if type(status) is int and 400 <= status <= 599 else None
