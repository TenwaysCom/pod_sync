from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

from clients.common.sync_integrity import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    append_manifest_entry,
    build_manifest_entry,
    exclusive_file_lock,
    load_manifest_entries,
    normalize_runtime_config,
    sanitize_path_component,
    save_bytes_to_file,
)
from webapp.actions import resolve_project_path
from webapp.odoo_push import get_local_file_url, load_config


SOURCE_TYPE = "burger_api"
DEFAULT_WAREHOUSE = "Burger"
SIGNATURE_MAX_SKEW_SECONDS = 300
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


class BurgerIngestError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def sign_burger_request(app_id: str, app_key: str, timestamp: str, body: bytes) -> str:
    message = f"{app_id}\n{timestamp}\n".encode("utf-8") + body
    return hmac.new(app_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def ingest_burger_pod(
    *,
    body: bytes,
    app_id: str,
    timestamp: str,
    signature: str,
    config_path: str = "config.local.json",
    now: datetime | None = None,
    max_bytes: int | None = None,
) -> dict:
    full_cfg = _load_full_config(config_path)
    burger_cfg = _require_burger_config(full_cfg)
    _verify_signature(
        burger_cfg,
        app_id=app_id,
        timestamp=timestamp,
        signature=signature,
        body=body,
        now=now or datetime.now(timezone.utc),
    )
    payload = _parse_payload(body)
    picking_name = _clean_picking_name(payload.get("picking_name"))
    content = _decode_image(payload.get("image_base64"))
    mime_type, extension = _detect_image(content)
    _check_size(content, max_bytes)
    if not str((full_cfg.get("pod_sync") or {}).get("base_url") or "").strip():
        raise BurgerIngestError(503, "missing pod_sync.base_url")

    warehouse = sanitize_path_component(burger_cfg.get("warehouse"), DEFAULT_WAREHOUSE)
    safe_picking = _safe_picking_component(picking_name)
    content_sha256 = hashlib.sha256(content).hexdigest()
    filename = f"{safe_picking}_{content_sha256[:12]}{extension}"
    base_dir = Path(full_cfg["download"]["base_dir"])
    destination = base_dir / warehouse / safe_picking / filename
    manifest_path = base_dir / "_integrity" / warehouse / f"{SOURCE_TYPE}.jsonl"
    source_file_id = f"{picking_name}:{content_sha256}"

    lock_path = manifest_path.with_name(manifest_path.name + ".lock")
    with exclusive_file_lock(lock_path):
        existing = _find_existing_image(manifest_path, picking_name, content_sha256)
        if existing is not None:
            file_url = _require_file_url(full_cfg, existing["local_path"])
            _refresh_burger_reconcile(base_dir)
            return {
                "picking_name": picking_name,
                "file_url": file_url,
                "duplicate": True,
            }

        write_result = save_bytes_to_file(content, destination, max_bytes=max_bytes)
        file_url = _require_file_url(full_cfg, destination)
        relative_path = destination.relative_to(base_dir).as_posix()
        entry = build_manifest_entry(
            source_name=SOURCE_TYPE,
            source_cfg={"warehouse": warehouse},
            destination=destination,
            write_result=write_result,
            source_file_id=source_file_id,
            source_path=relative_path,
            file_name=filename,
            mime_type=mime_type,
            source_size=len(content),
            source_url=file_url,
            downloaded_via=SOURCE_TYPE,
            extra={"shipment_reference": picking_name},
        )
        append_manifest_entry(manifest_path, entry)

    _refresh_burger_reconcile(base_dir)
    return {
        "picking_name": picking_name,
        "file_url": file_url,
        "duplicate": False,
    }


def _refresh_burger_reconcile(storage_root: Path) -> None:
    from .emtc_reconcile import refresh_burger_reconcile_dataset

    refresh_burger_reconcile_dataset(storage_root)


def _load_full_config(config_path: str) -> dict:
    try:
        full_cfg = load_config(config_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BurgerIngestError(503, "Burger API is not configured") from exc
    resolved = resolve_project_path(config_path, "config.local.json")
    return normalize_runtime_config(full_cfg, resolved)


def _require_burger_config(full_cfg: dict) -> dict:
    section = full_cfg.get("burger_api")
    if not isinstance(section, dict) or section.get("enabled") is False:
        raise BurgerIngestError(503, "Burger API is not configured")
    app_id = str(section.get("app_id") or "").strip()
    app_key = str(section.get("app_key") or "").strip()
    if not app_id or not app_key:
        raise BurgerIngestError(503, "Burger API is not configured")
    return section


def _verify_signature(
    burger_cfg: dict,
    *,
    app_id: str,
    timestamp: str,
    signature: str,
    body: bytes,
    now: datetime,
) -> None:
    expected_app_id = str(burger_cfg.get("app_id") or "").strip()
    app_key = str(burger_cfg.get("app_key") or "").strip()
    provided_app_id = str(app_id or "").strip()
    timestamp_text = str(timestamp or "").strip()
    provided_signature = str(signature or "").strip().lower()
    if not hmac.compare_digest(provided_app_id.encode("utf-8"), expected_app_id.encode("utf-8")):
        raise BurgerIngestError(401, "invalid signature")
    try:
        stamp = int(timestamp_text)
    except (TypeError, ValueError):
        raise BurgerIngestError(401, "invalid signature") from None
    if abs(int(now.timestamp()) - stamp) > SIGNATURE_MAX_SKEW_SECONDS:
        raise BurgerIngestError(401, "invalid signature")
    expected_signature = sign_burger_request(expected_app_id, app_key, timestamp_text, body)
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise BurgerIngestError(401, "invalid signature")


def _parse_payload(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BurgerIngestError(400, "request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise BurgerIngestError(400, "request body must be a JSON object")
    return payload


def _clean_picking_name(value) -> str:
    if not isinstance(value, str):
        raise BurgerIngestError(400, "picking_name is required")
    picking_name = value.strip()
    if not picking_name:
        raise BurgerIngestError(400, "picking_name is required")
    return picking_name


def _safe_picking_component(picking_name: str) -> str:
    cleaned = sanitize_path_component(picking_name, "").strip()
    if not cleaned or cleaned in {".", ".."} or len(cleaned) > 180:
        raise BurgerIngestError(400, "picking_name is invalid")
    return cleaned


def _decode_image(value) -> bytes:
    if not isinstance(value, str):
        raise BurgerIngestError(400, "image_base64 is required")
    text = value.strip()
    if text.lower().startswith("data:"):
        header, separator, payload = text.partition(",")
        if not separator or "base64" not in header.lower():
            raise BurgerIngestError(400, "image_base64 data URL must be base64")
        text = payload
    compact = "".join(text.split())
    if not compact:
        raise BurgerIngestError(400, "image_base64 is required")
    padded = compact + ("=" * ((-len(compact)) % 4))
    try:
        content = base64.b64decode(padded, validate=True)
    except binascii.Error as exc:
        raise BurgerIngestError(400, "image_base64 is not valid base64") from exc
    if not content:
        raise BurgerIngestError(400, "image_base64 is required")
    return content


def _detect_image(content: bytes) -> tuple[str, str]:
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    for magic, mime_type, extension in _IMAGE_SIGNATURES:
        if content.startswith(magic):
            return mime_type, extension
    raise BurgerIngestError(400, "image must be JPEG, PNG, WebP, or GIF")


def _check_size(content: bytes, max_bytes: int | None) -> None:
    limit = DEFAULT_MAX_DOWNLOAD_BYTES if max_bytes is None else max_bytes
    if limit and limit > 0 and len(content) > limit:
        raise BurgerIngestError(413, f"image exceeds limit of {limit} bytes")


def _find_existing_image(manifest_path: Path, picking_name: str, content_sha256: str) -> dict | None:
    if not manifest_path.exists():
        return None
    for entry in load_manifest_entries(manifest_path):
        if entry.get("shipment_reference") != picking_name or entry.get("sha256") != content_sha256:
            continue
        local_path = Path(str(entry.get("local_path") or ""))
        if local_path.is_file():
            return entry
    return None


def _require_file_url(full_cfg: dict, local_path: Path | str) -> str:
    file_url = get_local_file_url(full_cfg, str(local_path))
    if not file_url:
        raise BurgerIngestError(503, "unable to build local POD URL")
    return file_url
