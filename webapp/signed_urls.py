from __future__ import annotations

import hashlib
import hmac
from urllib.parse import quote

MIN_FILE_URL_SECRET_LENGTH = 32
INSECURE_FILE_URL_SECRET_PREFIXES = (
    "CHANGE_ME",
    "PASTE_",
    "TODO",
    "EXAMPLE",
    "REPLACE_ME",
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
