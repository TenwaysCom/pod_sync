from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from urllib.parse import quote

MIN_FILE_URL_SECRET_LENGTH = 32
INSECURE_FILE_URL_SECRET_PREFIXES = (
    "CHANGE_ME",
    "PASTE_",
    "TODO",
    "EXAMPLE",
    "REPLACE_ME",
)

REDACTED_VALUE = "[REDACTED]"
SENSITIVE_VALUE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authkey",
        "authorization",
        "client_secret",
        "cookie",
        "file_url_secret",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "sig",
        "signature",
        "tempauth",
        "token",
    }
)
_SENSITIVE_QUERY_VALUE_RE = re.compile(
    r"(?i)([?&](?:access_token|authkey|client_secret|id_token|refresh_token|sig|signature|tempauth|token)=)([^&#\s\"']*)"
)
_SENSITIVE_TEXT_FIELD_RE = re.compile(
    r"(?i)((?:\"|')?(?:access_token|api_key|authkey|authorization|client_secret|cookie|file_url_secret|id_token|password|refresh_token|secret|sig|signature|tempauth|token)(?:\"|')?\s*[:=]\s*(?:\"|')?)([^,}\[\]\s\"']+)"
)


def normalize_storage_relative_path(relative_path: str) -> str:
    return str(relative_path or "").replace("\\", "/").strip("/")


def file_url_secret_error(secret: str | None) -> str | None:
    value = str(secret or "").strip()
    if not value:
        return "missing"
    upper_value = value.upper()
    if any(upper_value.startswith(prefix) for prefix in INSECURE_FILE_URL_SECRET_PREFIXES):
        return "placeholder"
    if len(value) < MIN_FILE_URL_SECRET_LENGTH:
        return "too_short"
    return None


def is_valid_file_url_secret(secret: str | None) -> bool:
    return file_url_secret_error(secret) is None


def select_file_url_secret(
    environment_secret: str | None,
    configured_secret: str | None,
) -> str:
    """Select a valid environment override without masking valid config."""

    if is_valid_file_url_secret(environment_secret):
        return str(environment_secret).strip()
    if is_valid_file_url_secret(configured_secret):
        return str(configured_secret).strip()
    return str(environment_secret or configured_secret or "").strip()


def sign_storage_path(relative_path: str, secret: str) -> str:
    normalized = normalize_storage_relative_path(relative_path)
    return hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_storage_path_token(relative_path: str, token: str | None, secret: str | None) -> bool:
    if not token or not secret:
        return False
    expected = sign_storage_path(relative_path, secret)
    return hmac.compare_digest(str(token), expected)


def build_signed_storage_url(base_url: str, relative_path: str, secret: str) -> str:
    normalized = normalize_storage_relative_path(relative_path)
    encoded = "/".join(quote(part, safe="") for part in normalized.split("/") if part)
    token = sign_storage_path(normalized, secret)
    clean_base_url = base_url.rstrip().rstrip("/")
    return f"{clean_base_url}/storage/files/{encoded}?token={token}"


def redact_sensitive_text(value: str) -> str:
    """Redact credentials embedded in URLs or serialized log text."""

    text = str(value)
    text = _SENSITIVE_QUERY_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{REDACTED_VALUE}",
        text,
    )
    return _SENSITIVE_TEXT_FIELD_RE.sub(
        lambda match: f"{match.group(1)}{REDACTED_VALUE}",
        text,
    )


def redact_sensitive_data(value, *, _key: str | None = None):
    """Return a recursively redacted copy suitable for logs and persistence.

    Callers should keep using the original value for the actual provider/Odoo
    request.  This helper is only the public-output boundary.
    """

    normalized_key = str(_key or "").strip().casefold()
    if normalized_key in SENSITIVE_VALUE_KEYS:
        return REDACTED_VALUE
    if isinstance(value, Mapping):
        return {
            key: redact_sensitive_data(item, _key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, set):
        return {redact_sensitive_data(item) for item in value}
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value
