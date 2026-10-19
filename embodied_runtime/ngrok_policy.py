"""Small, secret-free Traffic Policy for the managed SMS agent endpoint."""

from __future__ import annotations

import json
import re

from embodied_runtime.sms import OUTBOUND_MEDIA_PATH

SMS_REQUESTS_PER_MINUTE = 30
MEDIA_REQUESTS_PER_MINUTE = 60
MAX_DECLARED_BODY_BYTES = 64 * 1024
# Match the token alphabet/length accepted by SmsService._serve_media, and only
# the two extensions registered by SmsService.start. Never bucket by bearer URL.
MEDIA_PATH_PATTERN = rf"^{OUTBOUND_MEDIA_PATH}[A-Za-z0-9_-]{{40,64}}[.](jpg|png)$"


def validate_policy_settings(webhook_path: str, vault: str | None, secret: str | None) -> None:
    """Accept static, unambiguous routes and names, never raw CEL or credentials."""
    if (not re.fullmatch(r"/[A-Za-z0-9_~./-]*", webhook_path)
            or "//" in webhook_path
            or any(part in {".", ".."} for part in webhook_path.split("/"))
            or webhook_path.startswith(OUTBOUND_MEDIA_PATH.rstrip("/"))):
        raise ValueError(
            "managed ngrok sms.webhook_path must be a static absolute URL path "
            "without encoding, query, fragment, templates, dot segments, or the outbound-media prefix"
        )
    for flag, value in (("--ngrok-twilio-vault", vault), ("--ngrok-twilio-secret", secret)):
        if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError(
                f"{flag} requires an ngrok cloud resource name (letters, digits, '_' or '-'); "
                "create the vault/secret first; supply its name, never the Twilio auth token"
            )


def build_traffic_policy(webhook_path: str, vault: str | None, secret: str | None) -> dict:
    validate_policy_settings(webhook_path, vault, secret)
    sms = f"req.url.path == {json.dumps(webhook_path)} && req.method == 'POST'"
    media = f"req.url.path.matches({json.dumps(MEDIA_PATH_PATTERN)}) && req.method in ['GET', 'HEAD']"

    def rate_limit(name: str, capacity: int) -> dict:
        return {"type": "rate-limit", "config": {
            "name": name, "algorithm": "sliding_window", "capacity": capacity,
            "rate": "60s", "enforce": True,
            # Constant keys aggregate all clients within each separate action.
            "bucket_key": [json.dumps(name)],
        }}

    return {"on_http_request": [
        {"expressions": [f"!(({sms}) || ({media}))"],
         "actions": [{"type": "custom-response", "config": {"status_code": 403}}]},
        # Content-Length is optional for streaming requests. This is a declared
        # size check, not a claim of a streamed-body or concurrency ceiling.
        {"expressions": [f"has(req.content_length) && req.content_length > {MAX_DECLARED_BODY_BYTES}"],
         "actions": [{"type": "custom-response", "config": {"status_code": 413}}]},
        {"expressions": [sms], "actions": [
            rate_limit("sms", SMS_REQUESTS_PER_MINUTE),
            {"type": "verify-webhook", "config": {
                "provider": "twilio", "enforce": True,
                "secret": "${secrets.get('" + vault + "', '" + secret + "')}",
            }},
        ]},
        {"expressions": [media], "actions": [rate_limit("outbound-media", MEDIA_REQUESTS_PER_MINUTE)]},
    ]}
