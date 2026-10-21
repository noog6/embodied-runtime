"""Secret-free connection identity and bounded provider failure categories."""

from json import JSONDecodeError
import re
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


def cognition_setup_guidance(backend, error):
    """Bounded startup guidance, never exception messages, keys or response bodies."""
    hints = {
        "missing_credentials": "The credential environment variable is missing or empty.",
        "authentication": "The provider rejected authentication or access; check the key and project/model permissions.",
        "missing_dependency": "Install the provider SDK in this venv: python -m pip install -e '.[openai]'",
        "client_initialization": "Local SDK initialization failed; check SDK dependencies, proxy and endpoint settings.",
        "connection": "Could not connect to the provider; check network, DNS, proxy and endpoint settings.",
        "timeout": "The provider request timed out; check connectivity and retry.",
        "rate_limit": "The provider rate or quota limit was reached; check usage/billing and retry later.",
        "payment_required": "The provider requires billing or credits; check the provider account.",
        "model_unavailable": "The configured model or endpoint is unavailable; check model name and access.",
        "malformed_response": "The provider returned an unexpected response; retry and inspect safe diagnostics.",
        "invalid_request": "The provider rejected the request; check the model, endpoint and supported options.",
        "provider_unavailable": "The provider service is unavailable; retry later.",
        "cancelled": "Provider preparation was cancelled.",
        "unavailable": "The configured provider client is unavailable; check its local SDK configuration.",
    }
    category = failure_category(error)
    if category not in hints:
        return "provider_error", "Provider preparation failed; inspect the safe provider diagnostics and retry."
    hint = hints[category]
    if category in {"missing_credentials", "authentication"}:
        variable = "OPENAI_API_KEY"
        if getattr(backend, "identifier", None) == "openai-compatible":
            candidate = getattr(getattr(backend, "config", None), "api_key_env", "")
            variable = candidate if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate) else "configured credential"
        hint += f" Credential: {variable}."
        if variable == "OPENAI_API_KEY":
            replace_option = " --replace-openai-key" if category == "authentication" else ""
            hint += (f" Run python scripts/setup_environment.py --config YOUR_CONFIG --credentials-only{replace_option}; "
                     "source ~/.config/embodied-runtime/openai.env and restart. "
                     "For systemd, update the service environment and restart the service.")
    return category, hint
