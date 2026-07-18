#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.common.sync_integrity import (
    append_manifest_entry,
    build_manifest_entry,
    load_manifest_skip_index,
    manifest_entry_is_current,
    normalize_runtime_config,
    resolve_manifest_path,
    safe_relative_path,
    save_stream_to_file,
    verify_manifest_entry,
)

DEFAULT_GRAPH_SCOPE = "https://graph.microsoft.com/.default offline_access"
PUBLIC_VROOM_ENDPOINT = "https://api.onedrive.com/v1.0"
PUBLIC_MPC_ENDPOINT = "https://my.microsoftpersonalcontent.com/_api/v2.1"
CONFIG_SECTION_KEY = "cmc_onedrive"
LEGACY_CONFIG_SECTION_KEY = "onedrive"
GRAPH_COLLECTION_RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}
GRAPH_COLLECTION_MAX_RETRIES = 5
GRAPH_COLLECTION_MAX_CURSOR_RESTARTS = 2
GRAPH_COLLECTION_CURSOR_RESTART_DELAY_SECONDS = 5.0
HTTP_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SENSITIVE_QUERY_VALUE_PATTERN = re.compile(
    r"(?i)(?:(?:\$|%24)?skiptoken|tempauth)\s*=\s*[^\s&,]+"
)
SENSITIVE_QUERY_NAME_PATTERN = re.compile(r"(?i)(?:\$|%24)?skiptoken|tempauth")


class SafeRequestError(requests.HTTPError):
    """An HTTP/network failure whose message never contains a request URL."""

    def __init__(self, operation: str, response=None):
        status_code = getattr(response, "status_code", None)
        message = f"{operation} failed"
        if status_code is not None:
            message += f" (HTTP {status_code})"
        super().__init__(message + ".", response=response)


class GraphCollectionCursorError(SafeRequestError):
    """A Graph continuation cursor stayed invalid after page-level retries."""

    def __init__(self, response):
        super().__init__("Microsoft Graph continuation cursor", response=response)


def get_onedrive_config_section(full_cfg):
    return full_cfg.get(CONFIG_SECTION_KEY) or full_cfg.get(LEGACY_CONFIG_SECTION_KEY) or {}


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        full_cfg = normalize_runtime_config(json.load(fh), path)
    cfg = get_onedrive_config_section(full_cfg)
    if cfg.get("shared_url"):
        cfg["shared_url"] = sanitize_shared_url(normalize_shared_url(cfg["shared_url"]))
    return full_cfg, cfg, full_cfg.get("download", {})


def make_headers(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def has_refresh_flow(cfg):
    return bool(cfg.get("client_id") and cfg.get("refresh_token"))


def build_token_url(cfg):
    tenant_id = cfg.get("tenant_id") or "common"
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def write_updated_config(config_path, full_cfg):
    path = Path(config_path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(full_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


def extract_query_param(url, name):
    parsed = urlparse(url)
    for key, values in parse_qs(parsed.query).items():
        if key.lower() == name.lower() and values:
            return values[-1]
    return ""


def append_query_params(url, extra_params):
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in extra_params.items():
        if value not in (None, ""):
            params[key] = [str(value)]

    flat_params = []
    for key, values in params.items():
        for value in values:
            flat_params.append((key, value))

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(flat_params),
            parsed.fragment,
        )
    )


def is_public_vroom_access(shared_access):
    return bool(shared_access and shared_access.get("api_base") and shared_access.get("authkey"))


def resolve_shared_access(session, shared_url, timeout):
    landing_url = shared_url
    if urlparse(shared_url).netloc in {"1drv.ms", "onedrive.live.com"}:
        try:
            landing_response = session.raw_session.get(shared_url, timeout=timeout, allow_redirects=True)
            landing_response.raise_for_status()
            landing_url = landing_response.url
        except requests.RequestException:
            landing_url = shared_url

    authkey = extract_query_param(shared_url, "authkey") or extract_query_param(landing_url, "authkey")
    redeem = extract_query_param(shared_url, "redeem") or extract_query_param(landing_url, "redeem")
    migrated_to_spo = (
        extract_query_param(shared_url, "migratedtospo") or extract_query_param(landing_url, "migratedtospo")
    )
    is_migrated = str(migrated_to_spo).lower() == "true"

    return {
        "landing_url": landing_url,
        "authkey": authkey,
        "redeem": redeem,
        "is_migrated": is_migrated,
        "api_base": PUBLIC_MPC_ENDPOINT if authkey and is_migrated else PUBLIC_VROOM_ENDPOINT if authkey else "",
    }


def ensure_shared_link_access(shared_access, headers):
    if is_public_vroom_access(shared_access):
        return
    if headers:
        return
    raise SystemExit(
        "Public OneDrive folder links need either a Graph access_token or a final shared_url with authkey. "
        "A redeem/validatepermission link without authkey cannot be listed anonymously."
    )


def append_shared_access(url, shared_access):
    if not is_public_vroom_access(shared_access):
        return url
    return append_query_params(url, {"authKey": shared_access["authkey"]})


class RefreshingGraphSession:
    def __init__(self, raw_session, cfg, timeout, full_cfg, config_path):
        self.raw_session = raw_session
        self.cfg = cfg
        self.timeout = timeout
        self.full_cfg = full_cfg
        self.config_path = config_path

    def __getattr__(self, name):
        return getattr(self.raw_session, name)

    def refresh_access_token(self, headers=None):
        payload = {
            "client_id": self.cfg["client_id"],
            "refresh_token": self.cfg["refresh_token"],
            "grant_type": "refresh_token",
            "scope": self.cfg.get("scope") or DEFAULT_GRAPH_SCOPE,
        }
        if self.cfg.get("client_secret"):
            payload["client_secret"] = self.cfg["client_secret"]

        try:
            response = self.raw_session.post(
                build_token_url(self.cfg),
                data=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SafeRequestError(
                "OneDrive OAuth token refresh",
                response=getattr(exc, "response", None),
            ) from None
        _raise_for_status_safely(response, "OneDrive OAuth token refresh")
        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise SystemExit("OneDrive OAuth refresh succeeded but no access_token was returned.")

        self.cfg["access_token"] = access_token
        refreshed_refresh_token = payload.get("refresh_token")
        if refreshed_refresh_token:
            self.cfg["refresh_token"] = refreshed_refresh_token
        write_updated_config(self.config_path, self.full_cfg)

        if headers is not None:
            headers.clear()
            headers.update(make_headers(access_token))
        return access_token

    def prepare_headers(self, headers=None, force_refresh=False):
        prepared = headers if headers is not None else {}
        if force_refresh and has_refresh_flow(self.cfg):
            try:
                self.refresh_access_token(headers=prepared)
                return prepared
            except requests.RequestException:
                if not self.cfg.get("access_token"):
                    raise

        if self.cfg.get("access_token"):
            prepared.clear()
            prepared.update(make_headers(self.cfg["access_token"]))
        elif prepared:
            prepared.clear()
        return prepared

    def get(self, url, headers=None, **kwargs):
        is_graph_url = url.startswith("https://graph.microsoft.com/")
        request_headers = headers if headers is not None else {}

        if is_graph_url:
            if request_headers:
                request_headers = self.prepare_headers(request_headers)
            elif self.cfg.get("access_token"):
                request_headers = self.prepare_headers({})

        retries = 0
        max_retries = 5
        while True:
            response = self.raw_session.get(url, headers=dict(request_headers) if request_headers else None, **kwargs)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 2 ** retries))
                if retries < max_retries:
                    time.sleep(retry_after)
                    retries += 1
                    continue

            if response.status_code == 401 and has_refresh_flow(self.cfg) and is_graph_url:
                self.refresh_access_token(headers=request_headers)
                response = self.raw_session.get(url, headers=dict(request_headers), **kwargs)
            return response


def build_root_children_url(drive_id, root_path, shared_access=None):
    normalized = str(PurePosixPath(root_path))
    if is_public_vroom_access(shared_access):
        if drive_id:
            return append_shared_access(f"{shared_access['api_base']}/drives/{drive_id}/root:{normalized}:/children", shared_access)
        return append_shared_access(f"{shared_access['api_base']}/me/drive/root:{normalized}:/children", shared_access)
    if drive_id:
        return f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:{normalized}:/children"
    return f"https://graph.microsoft.com/v1.0/me/drive/root:{normalized}:/children"


def build_item_metadata_path(drive_id, item_id, shared_access=None):
    if is_public_vroom_access(shared_access):
        if drive_id:
            return f"{shared_access['api_base']}/drives/{drive_id}/items/{item_id}"
        return f"{shared_access['api_base']}/me/drive/items/{item_id}"
    if drive_id:
        return f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
    return f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}"


def build_item_metadata_url(drive_id, item_id, shared_access=None):
    return append_shared_access(build_item_metadata_path(drive_id, item_id, shared_access=shared_access), shared_access)


def build_children_url(drive_id, item_id, shared_access=None):
    return append_shared_access(f"{build_item_metadata_path(drive_id, item_id, shared_access=shared_access)}/children", shared_access)


def build_content_url(drive_id, item_id, shared_access=None):
    return append_shared_access(f"{build_item_metadata_path(drive_id, item_id, shared_access=shared_access)}/content", shared_access)


def encode_share_url(shared_url):
    encoded = base64.b64encode(shared_url.encode("utf-8")).decode("ascii")
    encoded = encoded.rstrip("=").replace("/", "_").replace("+", "-")
    return f"u!{encoded}"


def normalize_shared_url(shared_url):
    parsed = urlparse(shared_url)
    if parsed.netloc != "onedrive.live.com":
        return shared_url

    params = parse_qs(parsed.query)
    if any(params.get(key) for key in ("authkey", "v", "id", "resid", "cid", "migratedtospo")):
        return shared_url

    redeem_values = params.get("redeem")
    if not redeem_values:
        return shared_url

    encoded = redeem_values[0]
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(encoded + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return shared_url
    return decoded


def sanitize_shared_url(shared_url):
    parsed = urlparse(shared_url)
    if parsed.netloc != "1drv.ms":
        return shared_url

    params = parse_qs(parsed.query)
    filtered = {}
    if params.get("e"):
        filtered["e"] = params["e"][-1]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(filtered),
            "",
        )
    )


def build_share_item_path(shared_url, shared_access=None):
    token = encode_share_url(shared_url)
    if is_public_vroom_access(shared_access):
        return f"{shared_access['api_base']}/shares/{token}/driveItem"
    return f"https://graph.microsoft.com/v1.0/shares/{token}/driveItem"


def build_share_item_url(shared_url, shared_access=None):
    return append_shared_access(build_share_item_path(shared_url, shared_access=shared_access), shared_access)


def build_share_children_url(shared_url, shared_access=None):
    return append_shared_access(f"{build_share_item_path(shared_url, shared_access=shared_access)}/children", shared_access)


def build_share_content_url(shared_url, shared_access=None):
    return append_shared_access(f"{build_share_item_path(shared_url, shared_access=shared_access)}/content", shared_access)


def _has_graph_skiptoken(url: str) -> bool:
    return "$skiptoken=" in url or "%24skiptoken=" in url.lower()


def new_collection_cursor_stats() -> dict[str, int]:
    return {
        "cursor_restart_count": 0,
        "cursor_recovered_count": 0,
        "cursor_failure_count": 0,
    }


def _increment_cursor_stat(cursor_stats, key: str, amount: int = 1) -> None:
    if cursor_stats is not None:
        cursor_stats[key] = int(cursor_stats.get(key, 0)) + amount


def _safe_diagnostic_value(value, max_chars: int = 160) -> str:
    normalized = " ".join(str(value or "").split())[:max_chars]
    return re.sub(r"[^A-Za-z0-9_.!:@-]", "?", normalized)


def _collection_item_id(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    try:
        item_index = parts.index("items")
    except ValueError:
        return ""
    if item_index + 1 >= len(parts):
        return ""
    return _safe_diagnostic_value(parts[item_index + 1])


def _graph_error_diagnostics(response) -> dict:
    diagnostics = {}
    headers = getattr(response, "headers", {}) or {}
    request_id = (
        headers.get("request-id")
        or headers.get("x-ms-request-id")
        or headers.get("client-request-id")
    )
    if request_id:
        diagnostics["request_id"] = _safe_diagnostic_value(request_id)

    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if isinstance(error, dict):
        if error.get("code"):
            diagnostics["error_code"] = _safe_diagnostic_value(error["code"])
        if error.get("message"):
            # Graph may echo an opaque continuation token without labelling it
            # as a skiptoken. Never copy the provider's raw message to logs.
            diagnostics["error_message"] = "Microsoft Graph rejected the continuation cursor."
    return diagnostics


def _raise_for_status_safely(response, operation: str) -> None:
    if getattr(response, "status_code", 0) >= 400:
        raise SafeRequestError(operation, response=response)


def _safe_get(session, url: str, operation: str, **kwargs):
    try:
        response = session.get(url, **kwargs)
    except requests.RequestException as exc:
        raise SafeRequestError(operation, response=getattr(exc, "response", None)) from None
    _raise_for_status_safely(response, operation)
    return response


def _redact_exception_text(value, max_chars: int = 300) -> str:
    text = " ".join(str(value or "").split())
    text = HTTP_URL_PATTERN.sub("[redacted-url]", text)
    text = SENSITIVE_QUERY_VALUE_PATTERN.sub("[redacted-parameter]", text)
    text = SENSITIVE_QUERY_NAME_PATTERN.sub("[redacted-parameter]", text)
    return text[:max_chars]


def _safe_exception_summary(exc: BaseException) -> str:
    if isinstance(exc, SafeRequestError):
        return str(exc)
    if isinstance(exc, requests.RequestException):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        message = type(exc).__name__
        if status_code is not None:
            message += f" (HTTP {status_code})"
        return message
    return _redact_exception_text(exc) or type(exc).__name__


def _collection_item_version(item: dict) -> tuple:
    """Return stable metadata used to distinguish a changed item after restart.

    The temporary download URL is deliberately excluded because Graph may mint
    a different signed URL for an otherwise unchanged driveItem.
    """

    parent_reference = item.get("parentReference", {}) or {}
    file_info = item.get("file", {}) or {}
    return (
        item.get("eTag"),
        item.get("cTag"),
        item.get("lastModifiedDateTime"),
        item.get("size"),
        item.get("name"),
        parent_reference.get("driveId"),
        parent_reference.get("id"),
        parent_reference.get("path"),
        file_info.get("mimeType"),
        "folder" in item,
    )


def _emit_cursor_event(
    event: str,
    base_url: str,
    response,
    restart_count: int,
    *,
    delay_seconds: float | None = None,
    duplicate_items_skipped: int | None = None,
    changed_items_replayed: int | None = None,
) -> None:
    payload = {
        "event": event,
        "source": "onedrive",
        "http_status": getattr(response, "status_code", None),
        "restart_count": restart_count,
        "max_restarts": GRAPH_COLLECTION_MAX_CURSOR_RESTARTS,
    }
    item_id = _collection_item_id(base_url)
    if item_id:
        payload["collection_item_id"] = item_id
    if delay_seconds is not None:
        payload["delay_seconds"] = delay_seconds
    if duplicate_items_skipped is not None:
        payload["duplicate_items_skipped"] = duplicate_items_skipped
    if changed_items_replayed is not None:
        payload["changed_items_replayed"] = changed_items_replayed
    payload.update(_graph_error_diagnostics(response))
    print(json.dumps(payload, ensure_ascii=False))


def _retry_after_seconds(response, attempt: int) -> float:
    raw_value = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(raw_value))
    except (TypeError, ValueError):
        return min(30.0, float(2 ** attempt))


def _is_retryable_collection_response(response, url: str) -> bool:
    if response.status_code in GRAPH_COLLECTION_RETRY_STATUSES:
        return True
    # Microsoft Graph personal-drive continuation tokens occasionally return
    # transient 400 responses even though the nextLink was produced by Graph.
    return response.status_code == 400 and _has_graph_skiptoken(url)


def _get_collection_page(session, url, headers, timeout):
    attempt = 0
    while True:
        try:
            response = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            if attempt >= GRAPH_COLLECTION_MAX_RETRIES:
                raise SafeRequestError(
                    "Microsoft Graph collection request",
                    response=getattr(exc, "response", None),
                ) from None
            time.sleep(min(30.0, float(2 ** attempt)))
            attempt += 1
            continue

        if not _is_retryable_collection_response(response, url):
            _raise_for_status_safely(response, "Microsoft Graph collection request")
            return response
        if attempt >= GRAPH_COLLECTION_MAX_RETRIES:
            if response.status_code == 400 and _has_graph_skiptoken(url):
                raise GraphCollectionCursorError(response)
            _raise_for_status_safely(response, "Microsoft Graph collection request")
            return response

        time.sleep(_retry_after_seconds(response, attempt))
        attempt += 1


def iter_collection(session, url, headers, timeout, cursor_stats=None):
    base_url = url
    next_url = url
    seen_item_versions = {}
    restart_count = 0
    duplicate_items_skipped = 0
    changed_items_replayed = 0
    last_cursor_response = None
    restart_succeeded = False
    recovery_recorded = False

    def _record_recovery():
        nonlocal recovery_recorded
        if not restart_count or recovery_recorded:
            return
        recovery_recorded = True
        _increment_cursor_stat(cursor_stats, "cursor_recovered_count")
        _emit_cursor_event(
            "collection_cursor_recovered",
            base_url,
            last_cursor_response,
            restart_count,
            duplicate_items_skipped=duplicate_items_skipped,
            changed_items_replayed=changed_items_replayed,
        )

    try:
        while next_url:
            try:
                response = _get_collection_page(session, next_url, headers, timeout)
            except GraphCollectionCursorError as exc:
                last_cursor_response = exc.response
                if restart_count >= GRAPH_COLLECTION_MAX_CURSOR_RESTARTS:
                    _increment_cursor_stat(cursor_stats, "cursor_failure_count")
                    _emit_cursor_event(
                        "collection_cursor_failed",
                        base_url,
                        exc.response,
                        restart_count,
                        duplicate_items_skipped=duplicate_items_skipped,
                        changed_items_replayed=changed_items_replayed,
                    )
                    raise

                restart_count += 1
                restart_succeeded = False
                _increment_cursor_stat(cursor_stats, "cursor_restart_count")
                delay_seconds = min(
                    30.0,
                    GRAPH_COLLECTION_CURSOR_RESTART_DELAY_SECONDS * (2 ** (restart_count - 1)),
                )
                _emit_cursor_event(
                    "collection_cursor_restart",
                    base_url,
                    exc.response,
                    restart_count,
                    delay_seconds=delay_seconds,
                    duplicate_items_skipped=duplicate_items_skipped,
                    changed_items_replayed=changed_items_replayed,
                )
                time.sleep(delay_seconds)
                next_url = base_url
                continue

            if restart_count:
                restart_succeeded = True
            payload = response.json()
            for item in payload.get("value", []):
                item_id = item.get("id")
                item_version = _collection_item_version(item)
                if item_id and item_id in seen_item_versions:
                    if seen_item_versions[item_id] == item_version:
                        duplicate_items_skipped += 1
                        continue
                    changed_items_replayed += 1
                    item = dict(item)
                    item["_cursor_replayed_changed"] = True
                if item_id:
                    seen_item_versions[item_id] = item_version
                yield item
            next_url = payload.get("@odata.nextLink")

        _record_recovery()
    except GeneratorExit:
        # A max_items boundary closes this child generator before its normal
        # epilogue. If the restarted page already succeeded, preserve the
        # recovery signal instead of reporting restart=1/recovered=0.
        if restart_succeeded:
            _record_recovery()
        raise


def normalize_parent_path(parent_path):
    if not parent_path:
        return PurePosixPath("/")
    if ":/" in parent_path:
        parent_path = parent_path.split(":/", 1)[1]
    return PurePosixPath(parent_path or "/")


def get_item_drive_id(item):
    parent_reference = item.get("parentReference", {}) or {}
    remote_item = item.get("remoteItem", {}) or {}
    remote_parent = remote_item.get("parentReference", {}) or {}
    return parent_reference.get("driveId") or remote_parent.get("driveId")


def serialize_item(item, full_path=None, share_root=False):
    resolved_path = full_path or str(normalize_parent_path((item.get("parentReference", {}) or {}).get("path")) / item["name"])
    return {
        "source": "onedrive",
        "id": item.get("id"),
        "name": item.get("name"),
        "path": resolved_path,
        "is_folder": "folder" in item,
        "mime_type": item.get("file", {}).get("mimeType"),
        "size": item.get("size"),
        "created_time": item.get("createdDateTime"),
        "modified_time": item.get("lastModifiedDateTime"),
        "download_url": item.get("@microsoft.graph.downloadUrl"),
        "web_url": item.get("webUrl"),
        "parent_reference": item.get("parentReference", {}),
        "drive_id": get_item_drive_id(item),
        "share_root": share_root,
        "_cursor_replayed_changed": bool(item.get("_cursor_replayed_changed")),
    }


def public_item_output(item):
    output = dict(item)
    output.pop("download_url", None)
    output.pop("_cursor_replayed_changed", None)
    return output


def emit_item(item, full_path=None, share_root=False):
    output = serialize_item(item, full_path=full_path, share_root=share_root)
    print(json.dumps(public_item_output(output), ensure_ascii=False))
    return output


def walk(session, drive_id, headers, timeout, root_path, recursive, max_items, shared_access=None):
    queue = [(root_path, build_root_children_url(drive_id, root_path, shared_access=shared_access))]
    seen = 0
    first_file = None
    while queue and seen < max_items:
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = emit_item(item, full_path)
            seen += 1
            if not output["is_folder"] and first_file is None:
                first_file = output
            if seen >= max_items:
                return first_file
            if recursive and "folder" in item:
                child_drive_id = output.get("drive_id") or drive_id
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))
    return first_file


def walk_from_item_id(session, drive_id, item_id, headers, timeout, recursive, max_items, shared_access=None):
    root_response = _safe_get(
        session,
        build_item_metadata_url(drive_id, item_id, shared_access=shared_access),
        "Microsoft Graph item metadata request",
        headers=headers,
        timeout=timeout,
    )
    root_item = root_response.json()
    root_path = root_item.get("name") or f"[item:{item_id}]"

    if "folder" not in root_item:
        return emit_item(root_item, full_path=root_path)

    queue = [(root_path, build_children_url(drive_id, item_id, shared_access=shared_access))]
    seen = 0
    first_file = None
    while queue and seen < max_items:
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = emit_item(item, full_path)
            seen += 1
            if not output["is_folder"] and first_file is None:
                first_file = output
            if seen >= max_items:
                return first_file
            if recursive and "folder" in item:
                child_drive_id = output.get("drive_id") or drive_id
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))
    return first_file


def walk_shared_link(session, shared_url, headers, timeout, recursive, max_items, shared_access=None):
    root_response = _safe_get(
        session,
        build_share_item_url(shared_url, shared_access=shared_access),
        "Microsoft Graph shared item request",
        headers=headers,
        timeout=timeout,
    )
    root_item = root_response.json()
    shared_root_path = str(PurePosixPath("[shared]") / root_item["name"])

    if "folder" not in root_item:
        return emit_item(root_item, full_path=shared_root_path, share_root=True)

    queue = [(shared_root_path, build_share_children_url(shared_url, shared_access=shared_access))]
    seen = 0
    first_file = None
    while queue and seen < max_items:
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = emit_item(item, full_path)
            seen += 1
            if not output["is_folder"] and first_file is None:
                first_file = output
            if seen >= max_items:
                return first_file
            if recursive and "folder" in item:
                child_drive_id = output.get("drive_id")
                if not child_drive_id:
                    raise SystemExit("Cannot resolve drive_id for a shared OneDrive folder child.")
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))
    return first_file


def fetch_item(session, headers, timeout, drive_id, item_id, shared_access=None):
    response = _safe_get(
        session,
        build_item_metadata_url(drive_id, item_id, shared_access=shared_access),
        "Microsoft Graph item metadata request",
        headers=headers,
        timeout=timeout,
    )
    return serialize_item(response.json())


def sanitize_filename(name):
    return (name or "onedrive_download.bin").replace("/", "_")


def sanitize_path_component(name):
    return (name or "onedrive").replace("/", "_")


def resolve_output_dir(explicit_output_dir, download_cfg, source_cfg):
    if explicit_output_dir:
        return Path(explicit_output_dir)
    if download_cfg.get("base_dir"):
        return Path(download_cfg["base_dir"]) / sanitize_path_component(source_cfg.get("warehouse") or "onedrive")
    return Path("downloads/onedrive")


def normalize_limit(limit):
    if limit is None or limit <= 0:
        return None
    return limit


def limit_reached(seen, max_items):
    return max_items is not None and seen >= max_items


def iter_root_items(
    session,
    drive_id,
    headers,
    timeout,
    root_path,
    recursive,
    max_items,
    emit=True,
    shared_access=None,
    cursor_stats=None,
):
    queue = [(root_path, build_root_children_url(drive_id, root_path, shared_access=shared_access))]
    seen = 0
    while queue and not limit_reached(seen, max_items):
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout, cursor_stats=cursor_stats):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = serialize_item(item, full_path=full_path)
            if emit:
                print(json.dumps(public_item_output(output), ensure_ascii=False))
            yield output
            seen += 1
            if limit_reached(seen, max_items):
                return
            if recursive and output["is_folder"]:
                child_drive_id = output.get("drive_id") or drive_id
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))


def iter_root_item_children(
    session,
    drive_id,
    item_id,
    headers,
    timeout,
    recursive,
    max_items,
    emit=True,
    shared_access=None,
    cursor_stats=None,
):
    root_response = _safe_get(
        session,
        build_item_metadata_url(drive_id, item_id, shared_access=shared_access),
        "Microsoft Graph item metadata request",
        headers=headers,
        timeout=timeout,
    )
    root_item = root_response.json()
    root_path = root_item.get("name") or f"[item:{item_id}]"

    if "folder" not in root_item:
        output = serialize_item(root_item, full_path=root_path)
        if emit:
            print(json.dumps(public_item_output(output), ensure_ascii=False))
        yield output
        return

    queue = [(root_path, build_children_url(drive_id, item_id, shared_access=shared_access))]
    seen = 0
    while queue and not limit_reached(seen, max_items):
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout, cursor_stats=cursor_stats):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = serialize_item(item, full_path=full_path)
            if emit:
                print(json.dumps(public_item_output(output), ensure_ascii=False))
            yield output
            seen += 1
            if limit_reached(seen, max_items):
                return
            if recursive and output["is_folder"]:
                child_drive_id = output.get("drive_id") or drive_id
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))


def iter_shared_items(
    session,
    shared_url,
    headers,
    timeout,
    recursive,
    max_items,
    emit=True,
    shared_access=None,
    cursor_stats=None,
):
    root_response = _safe_get(
        session,
        build_share_item_url(shared_url, shared_access=shared_access),
        "Microsoft Graph shared item request",
        headers=headers,
        timeout=timeout,
    )
    root_item = root_response.json()
    shared_root_path = str(PurePosixPath("[shared]") / root_item["name"])

    if "folder" not in root_item:
        output = serialize_item(root_item, full_path=shared_root_path, share_root=True)
        if emit:
            print(json.dumps(public_item_output(output), ensure_ascii=False))
        yield output
        return

    queue = [(shared_root_path, build_share_children_url(shared_url, shared_access=shared_access))]
    seen = 0
    while queue and not limit_reached(seen, max_items):
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout, cursor_stats=cursor_stats):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = serialize_item(item, full_path=full_path)
            if emit:
                print(json.dumps(public_item_output(output), ensure_ascii=False))
            yield output
            seen += 1
            if limit_reached(seen, max_items):
                return
            if recursive and output["is_folder"]:
                child_drive_id = output.get("drive_id")
                if not child_drive_id:
                    raise SystemExit("Cannot resolve drive_id for a shared OneDrive folder child.")
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))


def iter_all_items(
    session,
    cfg,
    headers,
    timeout,
    recursive,
    max_items,
    emit=True,
    shared_access=None,
    cursor_stats=None,
):
    drive_id = cfg.get("drive_id") or ""
    if cfg.get("shared_url"):
        yield from iter_shared_items(
            session,
            cfg["shared_url"],
            headers,
            timeout,
            recursive,
            max_items,
            emit=emit,
            shared_access=shared_access,
            cursor_stats=cursor_stats,
        )
    elif cfg.get("root_item_id"):
        if not drive_id:
            raise SystemExit("OneDrive root_item_id mode requires drive_id.")
        yield from iter_root_item_children(
            session,
            drive_id,
            cfg["root_item_id"],
            headers,
            timeout,
            recursive,
            max_items,
            emit=emit,
            shared_access=shared_access,
            cursor_stats=cursor_stats,
        )
    elif cfg.get("root_path"):
        yield from iter_root_items(
            session,
            drive_id,
            headers,
            timeout,
            cfg["root_path"],
            recursive,
            max_items,
            emit=emit,
            shared_access=shared_access,
            cursor_stats=cursor_stats,
        )


def build_relative_download_path(item, cfg):
    path = PurePosixPath(item.get("path") or item.get("name") or "")
    parts = [part for part in path.parts if part not in ("/", "")]

    if cfg.get("shared_url"):
        if parts and parts[0] == "[shared]":
            parts = parts[1:]
        if len(parts) > 1:
            parts = parts[1:]
    elif cfg.get("root_item_id"):
        if len(parts) > 1:
            parts = parts[1:]
    elif cfg.get("root_path"):
        try:
            relative = path.relative_to(PurePosixPath(cfg["root_path"]))
            parts = [part for part in relative.parts if part not in ("/", "")]
        except ValueError:
            parts = [item.get("name") or "onedrive_download.bin"]

    return safe_relative_path(*parts, default_name=item.get("name") or "onedrive_download.bin")


def download_and_record_file(
    session,
    headers,
    timeout,
    default_drive_id,
    item,
    cfg,
    download_cfg,
    output_dir,
    shared_url=None,
    shared_access=None,
    relative_path=None,
    explicit_output_dir=None,
    manifest_lock=None,
):
    response = download_file(
        session=session,
        headers=headers,
        timeout=timeout,
        default_drive_id=default_drive_id,
        item=item,
        shared_url=shared_url,
        shared_access=shared_access,
    )
    target_path = relative_path or Path(sanitize_filename(item["name"]))
    destination = output_dir / target_path
    try:
        write_result = save_stream_to_file(response, destination)
    except requests.RequestException as exc:
        raise SafeRequestError(
            "OneDrive file download stream",
            response=getattr(exc, "response", None),
        ) from None
    manifest_path = resolve_manifest_path("onedrive", cfg, download_cfg, explicit_output_dir=explicit_output_dir)
    manifest_entry = build_manifest_entry(
        source_name="onedrive",
        source_cfg=cfg,
        destination=destination,
        write_result=write_result,
        source_file_id=item.get("id"),
        source_path=item.get("path"),
        file_name=item.get("name"),
        mime_type=item.get("mime_type"),
        source_size=item.get("size"),
        verify_remote_size=True,
        source_modified_time=item.get("modified_time"),
        source_created_time=item.get("created_time"),
        source_drive_id=item.get("drive_id"),
        source_url=item.get("web_url"),
        downloaded_via="shared_url" if shared_url else "graph_content",
    )
    if manifest_lock is not None:
        with manifest_lock:
            append_manifest_entry(manifest_path, manifest_entry)
    else:
        append_manifest_entry(manifest_path, manifest_entry)
    integrity_result = verify_manifest_entry(manifest_entry)
    result = {
        "downloaded_to": str(destination),
        "source_file": item.get("path"),
        "file_id": item.get("id"),
        "manifest_path": str(manifest_path),
        "integrity_status": integrity_result["status"],
        "integrity_checks": integrity_result["checks"],
        "local_size": write_result["local_size"],
    }
    if integrity_result["status"] != "ok":
        raise SystemExit("Downloaded OneDrive file failed local integrity verification.")
    return result


def load_skip_index(manifest_path):
    """基于 manifest 建立增量跳过索引：source_file_id -> 最新完整记录。"""

    return load_manifest_skip_index(manifest_path)


def sync_all_files(session, cfg, download_cfg, headers, timeout, items, explicit_output_dir=None, shared_access=None, workers=1, incremental=False):
    drive_id = cfg.get("drive_id") or ""
    output_dir = resolve_output_dir(explicit_output_dir, download_cfg, cfg)
    manifest_path = resolve_manifest_path("onedrive", cfg, download_cfg, explicit_output_dir=explicit_output_dir)
    skip_index = load_skip_index(manifest_path) if incremental else {}
    verify_incremental_hash = bool(download_cfg.get("verify_incremental_hash", True))
    summary = {"event": "sync_summary", "source": "onedrive", "downloaded_files": 0, "skipped_files": 0, "failed_files": 0, "downloaded_bytes": 0}

    def _download_one(item):
        return download_and_record_file(
            session=session,
            headers=headers,
            timeout=timeout,
            default_drive_id=drive_id,
            item=item,
            cfg=cfg,
            download_cfg=download_cfg,
            output_dir=output_dir,
            shared_url=cfg.get("shared_url"),
            shared_access=shared_access,
            relative_path=build_relative_download_path(item, cfg),
            explicit_output_dir=explicit_output_dir,
            manifest_lock=manifest_lock if workers > 1 else None,
        )

    def _handle_result(item, result, exc=None):
        if exc is not None:
            summary["failed_files"] += 1
            print(
                json.dumps(
                    {
                        "event": "download_failed",
                        "source": "onedrive",
                        "file_id": item.get("id"),
                        "source_file": item.get("path"),
                        "error": _safe_exception_summary(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            summary["downloaded_files"] += 1
            summary["downloaded_bytes"] += int(result["local_size"])
            print(json.dumps({"event": "downloaded", **result}, ensure_ascii=False))

    manifest_lock = None
    total_seen = 0

    if workers <= 1:
        for item in items:
            if item.get("is_folder"):
                continue
            total_seen += 1
            file_id = item.get("id")
            if incremental and not item.get("_cursor_replayed_changed") and file_id in skip_index:
                if manifest_entry_is_current(
                    skip_index[file_id],
                    source_modified_time=item.get("modified_time"),
                    verify_hash=verify_incremental_hash,
                ):
                    summary["skipped_files"] += 1
                    continue
            try:
                result = _download_one(item)
                _handle_result(item, result)
            except Exception as exc:
                _handle_result(item, None, exc=exc)
    else:
        import threading
        manifest_lock = threading.Lock()
        futures_map = {}
        active_future_by_file_id = {}

        def _consume_future(future, item):
            try:
                result = future.result()
                _handle_result(item, result)
            except Exception as exc:
                _handle_result(item, None, exc=exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for item in items:
                if item.get("is_folder"):
                    continue
                total_seen += 1
                file_id = item.get("id")
                if incremental and not item.get("_cursor_replayed_changed") and file_id in skip_index:
                    if manifest_entry_is_current(
                        skip_index[file_id],
                        source_modified_time=item.get("modified_time"),
                        verify_hash=verify_incremental_hash,
                    ):
                        summary["skipped_files"] += 1
                        continue

                # A cursor restart can replay the same item id with newer
                # metadata. Serialize those versions so two workers never
                # overwrite the same destination or append manifests out of
                # order. Unchanged replays are filtered by iter_collection.
                previous_future = active_future_by_file_id.pop(file_id, None) if file_id else None
                if previous_future is not None:
                    previous_item = futures_map.pop(previous_future)
                    _consume_future(previous_future, previous_item)
                future = executor.submit(_download_one, item)
                futures_map[future] = item
                if file_id:
                    active_future_by_file_id[file_id] = future

            for future in concurrent.futures.as_completed(futures_map):
                item = futures_map[future]
                _consume_future(future, item)

    summary["total_files_seen"] = total_seen
    return summary


def download_file(session, headers, timeout, default_drive_id, item, shared_url=None, shared_access=None):
    if item.get("download_url"):
        return _safe_get(
            session,
            item["download_url"],
            "OneDrive file download",
            timeout=timeout,
            stream=True,
        )

    if shared_url and item.get("share_root"):
        return _safe_get(
            session,
            build_share_content_url(shared_url, shared_access=shared_access),
            "OneDrive shared file download",
            headers=headers,
            timeout=timeout,
            stream=True,
        )

    drive_id = item.get("drive_id") or default_drive_id
    if not drive_id:
        raise SystemExit(f"Cannot resolve drive_id for OneDrive item {item.get('id')}.")

    return _safe_get(
        session,
        build_content_url(drive_id, item["id"], shared_access=shared_access),
        "Microsoft Graph file download",
        headers=headers,
        timeout=timeout,
        stream=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Probe OneDrive folder structure.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--download-first-file", action="store_true")
    parser.add_argument("--download-all-files", action="store_true")
    parser.add_argument("--download-file-id")
    parser.add_argument("--output-dir")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent download workers (default: 1)")
    parser.add_argument("--incremental", action="store_true", help="Skip files whose source_modified_time has not changed according to manifest")
    args = parser.parse_args()

    full_cfg, cfg, download_cfg = load_config(args.config)
    timeout = int(cfg.get("timeout_seconds", 30))
    drive_id = cfg.get("drive_id") or ""
    max_items = normalize_limit(args.limit)

    download_modes = sum(bool(mode) for mode in (args.download_first_file, args.download_all_files, args.download_file_id))
    if download_modes > 1:
        raise SystemExit("Choose only one of --download-first-file, --download-all-files, or --download-file-id.")

    with requests.Session() as raw_session:
        session = RefreshingGraphSession(raw_session, cfg, timeout, full_cfg, args.config)
        shared_access = resolve_shared_access(session, cfg["shared_url"], timeout) if cfg.get("shared_url") else None
        headers = session.prepare_headers({}, force_refresh=has_refresh_flow(cfg))
        if cfg.get("shared_url"):
            ensure_shared_link_access(shared_access, headers)
            if is_public_vroom_access(shared_access):
                headers = {}

        if not args.download_file_id and not cfg.get("shared_url") and not cfg.get("root_path") and not cfg.get("root_item_id"):
            raise SystemExit("OneDrive config requires shared_url, root_path, or root_item_id when listing files.")

        first_file = None
        if not args.download_file_id:
            cursor_stats = new_collection_cursor_stats()
            listed_items = iter_all_items(
                session=session,
                cfg=cfg,
                headers=headers,
                timeout=timeout,
                recursive=args.recursive,
                max_items=max_items,
                emit=True,
                shared_access=shared_access,
                cursor_stats=cursor_stats,
            )
            if args.download_all_files:
                summary = sync_all_files(
                    session=session,
                    cfg=cfg,
                    download_cfg=download_cfg,
                    headers=headers,
                    timeout=timeout,
                    items=listed_items,
                    explicit_output_dir=args.output_dir,
                    shared_access=shared_access,
                    workers=args.workers,
                    incremental=args.incremental,
                )
                summary.update(cursor_stats)
                print(json.dumps(summary, ensure_ascii=False))
                if summary["failed_files"]:
                    raise SystemExit("OneDrive batch sync completed with failures.")
                return

            for item in listed_items:
                if not item["is_folder"] and first_file is None:
                    first_file = item

        target_file = first_file
        if args.download_file_id:
            target_file = fetch_item(
                session=session,
                headers=headers,
                timeout=timeout,
                drive_id=drive_id,
                item_id=args.download_file_id,
                shared_access=shared_access,
            )

        if args.download_first_file or args.download_file_id:
            if not target_file:
                raise SystemExit("No downloadable file found in the listed results.")
            result = download_and_record_file(
                session=session,
                headers=headers,
                timeout=timeout,
                default_drive_id=drive_id,
                item=target_file,
                cfg=cfg,
                download_cfg=download_cfg,
                output_dir=resolve_output_dir(args.output_dir, download_cfg, cfg),
                shared_url=cfg.get("shared_url"),
                shared_access=shared_access,
                explicit_output_dir=args.output_dir,
            )
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
