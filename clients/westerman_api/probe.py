#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.common.sync_integrity import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    append_manifest_entry,
    build_manifest_entry,
    compute_sha256,
    load_manifest_entries,
    normalize_runtime_config,
    resolve_manifest_path,
    safe_relative_path,
    save_bytes_to_file,
    save_stream_to_file,
    verify_manifest_entry,
)


DEFAULT_DOCUMENT_TIMEZONE = "Europe/Amsterdam"


class WestermanError(SystemExit):
    """Safe, structured Westerman failure for CLI and batch callers."""

    def __init__(
        self,
        message,
        *,
        error_code="westerman_error",
        http_status=None,
        retryable=False,
        auth_error=False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status
        self.retryable = bool(retryable)
        self.auth_error = bool(auth_error)


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        full_cfg = normalize_runtime_config(json.load(fh), path)
    return full_cfg["westerman_api"], full_cfg.get("download", {})


def _raise_generic_status_safely(response, operation):
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        raise WestermanError(
            f"Westerman generic {operation} failed with HTTP {status_code}.",
            error_code=f"generic_{operation}_http_error",
            http_status=status_code,
            retryable=status_code == 429 or status_code >= 500,
            auth_error=status_code in {401, 403},
        )


def _allowed_download_hosts(cfg):
    configured = cfg.get("allowed_download_hosts") or []
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.split(",") if item.strip()]
    hosts = {str(host).lower() for host in configured if host}
    if not hosts and cfg.get("base_url"):
        host = (urlparse(cfg["base_url"]).hostname or "").lower()
        if host:
            hosts.add(host)
    return hosts


def _ensure_allowed_download_url(cfg, download_url):
    host = (urlparse(download_url).hostname or "").lower()
    allowed_hosts = _allowed_download_hosts(cfg)
    if allowed_hosts and host not in allowed_hosts:
        raise SystemExit(f"Refusing Westerman download URL from untrusted host: {host}")


def get_nested_value(payload, dotted_path):
    if not dotted_path:
        return None
    current = payload
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def sanitize_filename(name):
    return (name or "api_download.bin").replace("/", "_")


def sanitize_path_component(name):
    return (name or "api").replace("/", "_")


def resolve_output_dir(explicit_output_dir, download_cfg, source_cfg):
    if explicit_output_dir:
        return Path(explicit_output_dir)
    if download_cfg.get("base_dir"):
        return Path(download_cfg["base_dir"]) / sanitize_path_component(source_cfg.get("warehouse") or "api")
    return Path("downloads/api")


def _endpoint_url(cfg, path_key, default_path):
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("Westerman Indicium base_url must be a valid HTTPS URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("Westerman Indicium base_url must not contain credentials, query, or fragment.")
    endpoint_path = str(cfg.get(path_key) or default_path).strip()
    return f"{base_url}/{endpoint_path.lstrip('/')}"


def _validated_proxy_url(cfg):
    raw_proxy = str(cfg.get("proxy_url") or "").strip()
    if not raw_proxy:
        return None
    parsed = urlparse(raw_proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WestermanError(
            "Westerman proxy_url must be a valid HTTP or HTTPS URL.",
            error_code="invalid_proxy_url",
        )
    if parsed.query or parsed.fragment:
        raise WestermanError(
            "Westerman proxy_url must not contain a query or fragment.",
            error_code="invalid_proxy_url",
        )
    return raw_proxy


def create_session(cfg):
    """Create a Westerman-only session isolated from ambient proxy variables."""
    session = requests.Session()
    session.trust_env = False
    proxy_url = _validated_proxy_url(cfg)
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def _configured_token(cfg):
    token = str(cfg.get("api_key") or cfg.get("api_token") or "").strip()
    placeholder_markers = ("PASTE_", "CHANGE_ME", "YOUR_TOKEN", "EXAMPLE")
    if not token or any(marker in token.upper() for marker in placeholder_markers):
        raise SystemExit("Westerman API token is not configured.")
    return token


def _indicium_headers(cfg):
    token = _configured_token(cfg)
    prefix = str(cfg.get("api_key_prefix", "Bearer") or "").strip()
    if prefix and not token.lower().startswith(f"{prefix.lower()} "):
        token = f"{prefix} {token}"
    return {
        cfg.get("api_key_header", "Authorization"): token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _parse_indicium_response(response, operation):
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise WestermanError(
            f"Westerman {operation} failed with HTTP {status_code}.",
            error_code=f"{operation}_http_error",
            http_status=status_code,
            retryable=status_code == 429 or status_code >= 500,
            auth_error=status_code in {401, 403},
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SystemExit(f"Westerman {operation} returned invalid JSON.") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise SystemExit(f"Westerman {operation} returned an unexpected response envelope.")
    result = payload[0]
    if str(result.get("status") or "").strip().casefold() != "ok":
        raise SystemExit(f"Westerman {operation} returned status {result.get('status') or 'unknown'}.")
    body = result.get("body")
    if not isinstance(body, dict):
        raise SystemExit(f"Westerman {operation} response did not contain an object body.")
    return body


def _document_list(shipment):
    documents = shipment.get("shipment_document") or []
    if isinstance(documents, dict):
        documents = [documents]
    return [document for document in documents if isinstance(document, dict)]


def _pod_document_types(cfg):
    configured = cfg.get("pod_document_types", ["POD"])
    if isinstance(configured, str):
        configured = configured.split(",")
    values = {
        str(value).strip().casefold()
        for value in configured
        if str(value).strip()
    }
    return values or {"pod"}


def _safe_document_metadata(document):
    allowed_keys = (
        "document_id",
        "document_file_name",
        "document_type",
        "mime_type",
    )
    return {key: document.get(key) for key in allowed_keys if document.get(key) not in (None, "")}


def _provider_timezone(cfg):
    """Return the zone used for provider timestamps without an offset."""

    timezone_name = str(
        cfg.get("document_timezone") or DEFAULT_DOCUMENT_TIMEZONE
    ).strip()
    if not timezone_name:
        timezone_name = DEFAULT_DOCUMENT_TIMEZONE
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SystemExit(
            "Westerman document_timezone must be a valid IANA timezone."
        ) from exc


def _as_manifest_timestamp(value, cfg, *, field_name):
    """Normalize an ISO provider datetime to second-precision UTC.

    Offset-aware values keep their supplied offset. Naive values are treated
    as local Westerman time (Europe/Amsterdam by default) before conversion.
    """

    if value in (None, ""):
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        # Provider metadata is advisory. A malformed timestamp must not turn
        # an otherwise valid POD into a permanent document failure.
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_provider_timezone(cfg))
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _delivery_manifest_timestamp(shipment, cfg):
    """Normalize delivery date/time, using local midnight when time is absent."""

    raw_date = shipment.get("delivery_date")
    if raw_date in (None, ""):
        return None
    try:
        parsed_date = date.fromisoformat(str(raw_date).strip())
    except ValueError:
        return None

    raw_time = shipment.get("delivery_time")
    normalized_time = str(raw_time).strip() if raw_time not in (None, "") else "00:00:00"
    if normalized_time.endswith(("Z", "z")):
        normalized_time = f"{normalized_time[:-1]}+00:00"
    try:
        parsed_time = time.fromisoformat(normalized_time)
    except ValueError:
        return None
    parsed = datetime.combine(parsed_date, parsed_time)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_provider_timezone(cfg))
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _document_manifest_timestamp(cfg, shipment, document, document_body):
    """Resolve the Phase timestamp using a deterministic provider priority.

    The first valid value from ``download_document.document_date_time`` and
    the document metadata from ``get_order_info`` wins. If neither is usable,
    the shipment's delivery date/time is used; a missing delivery time means
    local midnight.
    """

    for raw_document_time in (
        document_body.get("document_date_time"),
        document.get("document_date_time"),
    ):
        if raw_document_time in (None, ""):
            continue
        normalized = _as_manifest_timestamp(
            raw_document_time,
            cfg,
            field_name="document_date_time",
        )
        if normalized is not None:
            return normalized
    return _delivery_manifest_timestamp(shipment, cfg)


def _canonical_reference(value):
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _safe_leaf(value, default):
    path = safe_relative_path(value, default_name=default)
    return "_".join(path.parts) or default


def _decode_document_content(document_body):
    encoded = document_body.get("content_base64")
    if not isinstance(encoded, str) or not encoded.strip():
        raise SystemExit("Westerman download_document response did not contain content_base64.")
    compact = "".join(encoded.split())
    padding = len(compact) - len(compact.rstrip("="))
    estimated_size = max(0, (len(compact) * 3) // 4 - padding)
    if DEFAULT_MAX_DOWNLOAD_BYTES and estimated_size > DEFAULT_MAX_DOWNLOAD_BYTES:
        raise SystemExit(
            f"Westerman document exceeds the {DEFAULT_MAX_DOWNLOAD_BYTES}-byte download limit."
        )
    try:
        content = base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise SystemExit("Westerman download_document returned invalid base64 content.") from exc
    if not content:
        raise SystemExit("Westerman download_document returned an empty document.")
    return content


def _validate_document_content(content, mime_type, file_name):
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().casefold()
    suffix = Path(file_name or "").suffix.casefold()
    if normalized_mime == "application/pdf" or suffix == ".pdf":
        if b"%PDF-" not in content[:1024]:
            raise SystemExit("Westerman POD document does not contain a valid PDF header.")
    elif normalized_mime == "image/png" or suffix == ".png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SystemExit("Westerman POD document does not contain a valid PNG header.")
    elif normalized_mime in {"image/jpeg", "image/jpg"} or suffix in {".jpg", ".jpeg"}:
        if not content.startswith(b"\xff\xd8"):
            raise SystemExit("Westerman POD document does not contain a valid JPEG header.")
    elif normalized_mime == "image/tiff" or suffix in {".tif", ".tiff"}:
        if not content.startswith((b"II*\x00", b"MM\x00*")):
            raise SystemExit("Westerman POD document does not contain a valid TIFF header.")
    else:
        raise SystemExit("Westerman POD document has an unsupported MIME type or file extension.")


def _load_latest_manifest_entries(manifest_path):
    if not Path(manifest_path).exists():
        return {}
    try:
        entries = load_manifest_entries(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("Westerman integrity manifest is unreadable.") from exc
    latest = {}
    for entry in entries:
        source_file_id = entry.get("source_file_id")
        if source_file_id not in (None, ""):
            latest[str(source_file_id)] = entry
    return latest


def _manifest_metadata_matches(existing, expected):
    keys = (
        "source_path",
        "file_name",
        "mime_type",
        "source_url",
        "downloaded_via",
        "shipment_reference",
        "tracking_number",
        "document_type",
        "source_modified_time",
        "source_created_time",
    )
    return all(existing.get(key) == expected.get(key) for key in keys)


def _verified_manifest_entry(entry):
    try:
        return verify_manifest_entry(entry)["status"] == "ok"
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _run_indicium(
    args,
    cfg,
    download_cfg,
    shipment_reference,
    *,
    session=None,
    emit=True,
    raise_on_document_failure=True,
    fallback_tracking_number=None,
):
    # Validate administrator-controlled timezone configuration independently
    # of optional vendor timestamps. Provider timestamp values themselves are
    # advisory and safely normalize to None when malformed.
    _provider_timezone(cfg)
    headers = _indicium_headers(cfg)
    lookup_url = _endpoint_url(cfg, "order_info_path", "/get_order_info")
    download_url = _endpoint_url(cfg, "download_document_path", "/download_document")
    timeout = int(cfg.get("timeout_seconds", 30))
    download_timeout = int(cfg.get("download_timeout_seconds", 120))
    session = session or create_session(cfg)

    try:
        response = session.post(
            lookup_url,
            headers=headers,
            json={"shipment_reference": shipment_reference},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise WestermanError(
            f"Westerman get_order_info request failed: {type(exc).__name__}.",
            error_code="get_order_info_request_failed",
            retryable=True,
        ) from exc

    body = _parse_indicium_response(response, "get_order_info")
    shipments = body.get("Shipment") or []
    if isinstance(shipments, dict):
        shipments = [shipments]
    shipments = [shipment for shipment in shipments if isinstance(shipment, dict)]
    requested_reference = _canonical_reference(shipment_reference)
    if any(shipment.get("shipment_reference") in (None, "") for shipment in shipments):
        raise SystemExit("Westerman get_order_info response omitted shipment_reference.")
    if any(
        _canonical_reference(shipment.get("shipment_reference")) != requested_reference
        for shipment in shipments
    ):
        raise SystemExit("Westerman get_order_info returned a different shipment_reference.")
    document_records = [
        (shipment, document)
        for shipment in shipments
        for document in _document_list(shipment)
    ]
    documents = [document for _shipment, document in document_records]
    pod_types = _pod_document_types(cfg)
    explicit_pod_documents = [
        document
        for document in documents
        if str(document.get("document_type") or "").strip().casefold() in pod_types
    ]
    raw_download_candidates = [
        (shipment, document)
        for shipment, document in document_records
        if not str(document.get("document_type") or "").strip()
        or str(document.get("document_type") or "").strip().casefold() in pod_types
    ]
    download_candidates = []
    seen_document_ids = set()
    duplicate_document_count = 0
    for shipment, document in raw_download_candidates:
        document_id = document.get("document_id")
        dedupe_key = str(document_id).strip() if document_id not in (None, "") else None
        if dedupe_key and dedupe_key in seen_document_ids:
            duplicate_document_count += 1
            continue
        if dedupe_key:
            seen_document_ids.add(dedupe_key)
        download_candidates.append((shipment, document))

    result = {
        "api_mode": "indicium",
        "http_status": response.status_code,
        "shipment_reference": shipment_reference,
        "shipment_count": len(shipments),
        "document_count": len(documents),
        "pod_document_count": len(explicit_pod_documents),
        "download_candidate_count": len(download_candidates),
        "duplicate_document_count": duplicate_document_count,
        "pod_documents": [_safe_document_metadata(document) for document in explicit_pod_documents],
        "download_requested": bool(args.download),
    }
    if not args.download:
        if emit:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    downloads = []
    already_downloaded = []
    skipped = []
    failed = []
    output_dir = resolve_output_dir(args.output_dir, download_cfg, cfg)
    manifest_path = resolve_manifest_path(
        "westerman_api",
        cfg,
        download_cfg,
        explicit_output_dir=args.output_dir,
    )
    latest_manifest_entries = _load_latest_manifest_entries(manifest_path)
    for shipment, document in download_candidates:
        document_id = document.get("document_id")
        if document_id in (None, ""):
            failed.append({"reason": "missing_document_id", **_safe_document_metadata(document)})
            continue
        try:
            download_response = session.get(
                download_url,
                headers=headers,
                params={"document_id": document_id},
                timeout=download_timeout,
                allow_redirects=False,
            )
            document_body = _parse_indicium_response(download_response, "download_document")
            returned_document_id = document_body.get("document_id")
            if returned_document_id in (None, ""):
                raise SystemExit("Westerman download_document response omitted document_id.")
            if str(returned_document_id).strip() != str(document_id).strip():
                raise SystemExit("Westerman download_document returned a different document_id.")
            effective_type = str(
                document_body.get("document_type") or document.get("document_type") or ""
            ).strip()
            if effective_type.casefold() not in pod_types:
                skipped.append(
                    {
                        "reason": "not_a_pod_document",
                        **_safe_document_metadata(document_body),
                    }
                )
                continue

            source_document_time = _document_manifest_timestamp(
                cfg,
                shipment,
                document,
                document_body,
            )

            content = _decode_document_content(document_body)
            mime_type = document_body.get("mime_type") or document.get("mime_type")
            default_suffix = ".pdf" if str(mime_type).casefold() == "application/pdf" else ".bin"
            source_file_name = str(
                document_body.get("document_file_name")
                or document.get("document_file_name")
                or f"document_{document_id}{default_suffix}"
            )
            safe_file_name = _safe_leaf(source_file_name, f"document_{document_id}{default_suffix}")
            _validate_document_content(content, mime_type, safe_file_name)
            safe_document_id = _safe_leaf(document_id, "document")
            content_sha256 = hashlib.sha256(content).hexdigest()
            source_path = "/".join(safe_relative_path(shipment_reference, safe_file_name).parts)
            manifest_extra = {
                "shipment_reference": shipment_reference,
                "document_type": effective_type,
            }
            tracking_number = shipment.get("external_id") or fallback_tracking_number
            if tracking_number not in (None, ""):
                manifest_extra["tracking_number"] = str(tracking_number)
            expected_metadata = {
                "source_path": source_path,
                "file_name": safe_file_name,
                "mime_type": mime_type,
                "source_url": download_url,
                "downloaded_via": "indicium_content_base64",
                "source_modified_time": source_document_time,
                "source_created_time": source_document_time,
                **manifest_extra,
            }
            existing = latest_manifest_entries.get(str(document_id))
            existing_is_reusable = bool(
                existing
                and existing.get("sha256") == content_sha256
                and _verified_manifest_entry(existing)
            )
            if existing_is_reusable and source_document_time is None:
                # Do not discard a previously verified provider timestamp just
                # because a later response omitted or malformed advisory time.
                preserved_time = existing.get("source_modified_time") or existing.get(
                    "source_created_time"
                )
                if preserved_time:
                    source_document_time = preserved_time
                    expected_metadata["source_modified_time"] = preserved_time
                    expected_metadata["source_created_time"] = preserved_time
            if existing_is_reusable and _manifest_metadata_matches(existing, expected_metadata):
                already_downloaded.append(
                    {
                        "document_id": str(document_id),
                        "document_type": effective_type,
                        "file_name": safe_file_name,
                        "downloaded_to": existing.get("local_path"),
                        "integrity_status": "ok",
                    }
                )
                continue

            if existing_is_reusable:
                destination = Path(existing["local_path"])
            else:
                local_name = f"{safe_document_id}_{content_sha256[:12]}_{safe_file_name}"
                relative_destination = safe_relative_path(shipment_reference, local_name)
                destination = output_dir / relative_destination
            if (
                destination.is_file()
                and destination.stat().st_size == len(content)
                and compute_sha256(destination) == content_sha256
            ):
                write_result = {"local_size": len(content), "sha256": content_sha256}
                file_status = "reused"
            else:
                write_result = save_bytes_to_file(content, destination)
                file_status = "written"
            manifest_entry = build_manifest_entry(
                source_name="westerman_api",
                source_cfg=cfg,
                destination=destination,
                write_result=write_result,
                source_file_id=str(document_id),
                source_path=source_path,
                file_name=safe_file_name,
                mime_type=mime_type,
                source_modified_time=source_document_time,
                source_created_time=source_document_time,
                source_url=download_url,
                downloaded_via="indicium_content_base64",
                extra=manifest_extra,
            )
            integrity_result = verify_manifest_entry(manifest_entry)
            if integrity_result["status"] != "ok":
                raise SystemExit("Downloaded Westerman document failed local integrity verification.")
            append_manifest_entry(manifest_path, manifest_entry)
            latest_manifest_entries[str(document_id)] = manifest_entry
            downloads.append(
                {
                    "document_id": str(document_id),
                    "document_type": effective_type,
                    "file_name": safe_file_name,
                    "downloaded_to": str(destination),
                    "file_status": file_status,
                    "integrity_status": integrity_result["status"],
                }
            )
        except WestermanError as exc:
            failed.append(
                {
                    "document_id": str(document_id),
                    "reason": exc.error_code,
                    "http_status": exc.http_status,
                    "retryable": exc.retryable,
                    "auth_error": exc.auth_error,
                }
            )
        except SystemExit as exc:
            failed.append(
                {
                    "document_id": str(document_id),
                    "reason": str(exc),
                    "retryable": False,
                    "auth_error": False,
                }
            )
        except requests.RequestException as exc:
            failed.append(
                {
                    "document_id": str(document_id),
                    "reason": f"request_failed:{type(exc).__name__}",
                    "retryable": True,
                    "auth_error": False,
                }
            )
        except (OSError, ValueError) as exc:
            failed.append(
                {
                    "document_id": str(document_id),
                    "reason": f"local_processing_failed:{type(exc).__name__}",
                    "retryable": False,
                    "auth_error": False,
                }
            )

    result.update(
        {
            "downloaded_count": len(downloads),
            "already_downloaded_count": len(already_downloaded),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "downloads": downloads,
            "already_downloaded": already_downloaded,
            "skipped_documents": skipped,
            "failed_documents": failed,
            "manifest_path": str(manifest_path) if downloads or already_downloaded else None,
        }
    )
    if emit:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if failed and raise_on_document_failure:
        raise SystemExit(
            f"{len(failed)} Westerman document(s) failed; first error: {failed[0]['reason']}"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description="Probe or download POD documents from Westerman API.")
    parser.add_argument("--config", required=True)
    reference_group = parser.add_mutually_exclusive_group(required=True)
    reference_group.add_argument("--shipment-reference")
    reference_group.add_argument(
        "--tracking-number",
        help="Backward-compatible alias; Indicium mode treats it as shipment_reference.",
    )
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--output-dir")
    args = parser.parse_args()

    cfg, download_cfg = load_config(args.config)
    lookup_reference = args.shipment_reference or args.tracking_number
    api_mode = str(cfg.get("api_mode") or cfg.get("protocol") or "generic").strip().casefold()
    if api_mode == "indicium":
        return _run_indicium(args, cfg, download_cfg, lookup_reference)
    if api_mode != "generic":
        raise SystemExit(f"Unsupported Westerman api_mode: {api_mode}")
    args.tracking_number = lookup_reference
    timeout = int(cfg.get("timeout_seconds", 30))
    method = (cfg.get("http_method") or "GET").upper()
    url = f"{cfg['base_url'].rstrip('/')}{cfg['lookup_path']}"
    headers = {
        cfg.get("api_key_header", "Authorization"): cfg["api_key"],
        "Accept": "application/json",
    }
    params = {cfg.get("tracking_param_name", "tracking_number"): args.tracking_number}

    session = requests.Session()
    if method == "POST":
        response = session.post(url, headers=headers, json=params, timeout=timeout, stream=True)
    else:
        response = session.get(url, headers=headers, params=params, timeout=timeout, stream=True)

    content_type = response.headers.get("Content-Type") or ""
    result = {
        "http_status": response.status_code,
        "content_type": content_type,
        "content_length": response.headers.get("Content-Length"),
    }

    payload = None
    try:
        body = response.json()
        result["body_type"] = "json"
        result["body_diagnostic"] = "json_response_body_omitted"
        payload = body
    except ValueError:
        result["body_type"] = "text"
        result["body_diagnostic"] = "non_json_response_body_omitted"

    print(json.dumps(result, ensure_ascii=False, indent=2))
    _raise_generic_status_safely(response, "lookup")

    if not args.download:
        return

    output_dir = resolve_output_dir(args.output_dir, download_cfg, cfg)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = resolve_manifest_path("westerman_api", cfg, download_cfg, explicit_output_dir=args.output_dir)

    if "application/pdf" in content_type or content_type.startswith("image/") or content_type == "application/octet-stream":
        suffix = ".pdf" if "pdf" in content_type else ""
        destination = output_dir / sanitize_filename(f"{args.tracking_number}{suffix}")
        write_result = save_stream_to_file(response, destination)
        manifest_entry = build_manifest_entry(
            source_name="westerman_api",
            source_cfg=cfg,
            destination=destination,
            write_result=write_result,
            source_file_id=args.tracking_number,
            file_name=destination.name,
            mime_type=content_type or None,
            source_size=response.headers.get("Content-Length"),
            verify_remote_size=bool(response.headers.get("Content-Length")),
            source_url=str(response.url),
            downloaded_via="direct_binary",
            extra={"tracking_number": args.tracking_number},
        )
        append_manifest_entry(manifest_path, manifest_entry)
        integrity_result = verify_manifest_entry(manifest_entry)
        print(
            json.dumps(
                {
                    "downloaded_to": str(destination),
                    "mode": "direct_binary",
                    "manifest_path": str(manifest_path),
                    "integrity_status": integrity_result["status"],
                    "integrity_checks": integrity_result["checks"],
                },
                ensure_ascii=False,
            )
        )
        if integrity_result["status"] != "ok":
            raise SystemExit("Downloaded API file failed local integrity verification.")
        return

    if payload is not None:
        base64_field = cfg.get("base64_content_field")
        download_url_field = cfg.get("download_url_field")
        file_name_field = cfg.get("file_name_field")

        if base64_field:
            encoded = get_nested_value(payload, base64_field)
            if encoded:
                destination = output_dir / sanitize_filename(get_nested_value(payload, file_name_field) or f"{args.tracking_number}.bin")
                write_result = save_bytes_to_file(base64.b64decode(encoded), destination)
                manifest_entry = build_manifest_entry(
                    source_name="westerman_api",
                    source_cfg=cfg,
                    destination=destination,
                    write_result=write_result,
                    source_file_id=args.tracking_number,
                    file_name=destination.name,
                    source_url=str(response.url),
                    downloaded_via="base64_field",
                    extra={"tracking_number": args.tracking_number},
                )
                append_manifest_entry(manifest_path, manifest_entry)
                integrity_result = verify_manifest_entry(manifest_entry)
                print(
                    json.dumps(
                        {
                            "downloaded_to": str(destination),
                            "mode": "base64_field",
                            "manifest_path": str(manifest_path),
                            "integrity_status": integrity_result["status"],
                            "integrity_checks": integrity_result["checks"],
                        },
                        ensure_ascii=False,
                    )
                )
                if integrity_result["status"] != "ok":
                    raise SystemExit("Downloaded API file failed local integrity verification.")
                return

        if download_url_field:
            download_url = get_nested_value(payload, download_url_field)
            if download_url:
                _ensure_allowed_download_url(cfg, download_url)
                filename = get_nested_value(payload, file_name_field) or f"{args.tracking_number}.bin"
                download_response = session.get(download_url, timeout=timeout, stream=True)
                _raise_generic_status_safely(download_response, "download")
                destination = output_dir / sanitize_filename(filename)
                write_result = save_stream_to_file(download_response, destination)
                manifest_entry = build_manifest_entry(
                    source_name="westerman_api",
                    source_cfg=cfg,
                    destination=destination,
                    write_result=write_result,
                    source_file_id=args.tracking_number,
                    file_name=destination.name,
                    mime_type=download_response.headers.get("Content-Type"),
                    source_size=download_response.headers.get("Content-Length"),
                    verify_remote_size=bool(download_response.headers.get("Content-Length")),
                    source_url=download_url,
                    downloaded_via="download_url_field",
                    extra={"tracking_number": args.tracking_number},
                )
                append_manifest_entry(manifest_path, manifest_entry)
                integrity_result = verify_manifest_entry(manifest_entry)
                print(
                    json.dumps(
                        {
                            "downloaded_to": str(destination),
                            "mode": "download_url_field",
                            "manifest_path": str(manifest_path),
                            "integrity_status": integrity_result["status"],
                            "integrity_checks": integrity_result["checks"],
                        },
                        ensure_ascii=False,
                    )
                )
                if integrity_result["status"] != "ok":
                    raise SystemExit("Downloaded API file failed local integrity verification.")
                return

    raise SystemExit("Download test requested, but API response did not expose a supported file payload.")


if __name__ == "__main__":
    main()
