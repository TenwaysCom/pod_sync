#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import json
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
    Path(config_path).write_text(json.dumps(full_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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

        response = self.raw_session.post(
            build_token_url(self.cfg),
            data=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
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


def iter_collection(session, url, headers, timeout):
    next_url = url
    while next_url:
        response = session.get(next_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("value", []):
            yield item
        next_url = payload.get("@odata.nextLink")


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
    }


def emit_item(item, full_path=None, share_root=False):
    output = serialize_item(item, full_path=full_path, share_root=share_root)
    print(json.dumps(output, ensure_ascii=False))
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
    root_response = session.get(
        build_item_metadata_url(drive_id, item_id, shared_access=shared_access),
        headers=headers,
        timeout=timeout,
    )
    root_response.raise_for_status()
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
    root_response = session.get(
        build_share_item_url(shared_url, shared_access=shared_access),
        headers=headers,
        timeout=timeout,
    )
    root_response.raise_for_status()
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
    response = session.get(
        build_item_metadata_url(drive_id, item_id, shared_access=shared_access),
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
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


def iter_root_items(session, drive_id, headers, timeout, root_path, recursive, max_items, emit=True, shared_access=None):
    queue = [(root_path, build_root_children_url(drive_id, root_path, shared_access=shared_access))]
    seen = 0
    while queue and not limit_reached(seen, max_items):
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = serialize_item(item, full_path=full_path)
            if emit:
                print(json.dumps(output, ensure_ascii=False))
            yield output
            seen += 1
            if limit_reached(seen, max_items):
                return
            if recursive and output["is_folder"]:
                child_drive_id = output.get("drive_id") or drive_id
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))


def iter_root_item_children(session, drive_id, item_id, headers, timeout, recursive, max_items, emit=True, shared_access=None):
    root_response = session.get(
        build_item_metadata_url(drive_id, item_id, shared_access=shared_access),
        headers=headers,
        timeout=timeout,
    )
    root_response.raise_for_status()
    root_item = root_response.json()
    root_path = root_item.get("name") or f"[item:{item_id}]"

    if "folder" not in root_item:
        output = serialize_item(root_item, full_path=root_path)
        if emit:
            print(json.dumps(output, ensure_ascii=False))
        yield output
        return

    queue = [(root_path, build_children_url(drive_id, item_id, shared_access=shared_access))]
    seen = 0
    while queue and not limit_reached(seen, max_items):
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = serialize_item(item, full_path=full_path)
            if emit:
                print(json.dumps(output, ensure_ascii=False))
            yield output
            seen += 1
            if limit_reached(seen, max_items):
                return
            if recursive and output["is_folder"]:
                child_drive_id = output.get("drive_id") or drive_id
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))


def iter_shared_items(session, shared_url, headers, timeout, recursive, max_items, emit=True, shared_access=None):
    root_response = session.get(
        build_share_item_url(shared_url, shared_access=shared_access),
        headers=headers,
        timeout=timeout,
    )
    root_response.raise_for_status()
    root_item = root_response.json()
    shared_root_path = str(PurePosixPath("[shared]") / root_item["name"])

    if "folder" not in root_item:
        output = serialize_item(root_item, full_path=shared_root_path, share_root=True)
        if emit:
            print(json.dumps(output, ensure_ascii=False))
        yield output
        return

    queue = [(shared_root_path, build_share_children_url(shared_url, shared_access=shared_access))]
    seen = 0
    while queue and not limit_reached(seen, max_items):
        current_path, url = queue.pop(0)
        for item in iter_collection(session, url, headers, timeout):
            full_path = str(PurePosixPath(current_path) / item["name"])
            output = serialize_item(item, full_path=full_path)
            if emit:
                print(json.dumps(output, ensure_ascii=False))
            yield output
            seen += 1
            if limit_reached(seen, max_items):
                return
            if recursive and output["is_folder"]:
                child_drive_id = output.get("drive_id")
                if not child_drive_id:
                    raise SystemExit("Cannot resolve drive_id for a shared OneDrive folder child.")
                queue.append((full_path, build_children_url(child_drive_id, item["id"], shared_access=shared_access)))


def iter_all_items(session, cfg, headers, timeout, recursive, max_items, emit=True, shared_access=None):
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
    write_result = save_stream_to_file(response, destination)
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
    """基于 manifest 建立增量跳过索引：source_file_id -> (source_modified_time, local_path)"""
    if not manifest_path.exists():
        return {}
    from clients.common.sync_integrity import load_manifest_entries
    entries = {}
    try:
        for entry in load_manifest_entries(manifest_path):
            file_id = entry.get("source_file_id")
            if file_id:
                entries[file_id] = (
                    entry.get("source_modified_time"),
                    entry.get("local_path"),
                )
    except (json.JSONDecodeError, OSError):
        return {}
    return entries


def sync_all_files(session, cfg, download_cfg, headers, timeout, items, explicit_output_dir=None, shared_access=None, workers=1, incremental=False):
    drive_id = cfg.get("drive_id") or ""
    output_dir = resolve_output_dir(explicit_output_dir, download_cfg, cfg)
    manifest_path = resolve_manifest_path("onedrive", cfg, download_cfg, explicit_output_dir=explicit_output_dir)
    skip_index = load_skip_index(manifest_path) if incremental else {}
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
                        "error": str(exc),
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
            if incremental and file_id in skip_index:
                mtime, local_path = skip_index[file_id]
                if mtime == item.get("modified_time") and local_path and Path(local_path).exists():
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for item in items:
                if item.get("is_folder"):
                    continue
                total_seen += 1
                file_id = item.get("id")
                if incremental and file_id in skip_index:
                    mtime, local_path = skip_index[file_id]
                    if mtime == item.get("modified_time") and local_path and Path(local_path).exists():
                        summary["skipped_files"] += 1
                        continue
                future = executor.submit(_download_one, item)
                futures_map[future] = item

            for future in concurrent.futures.as_completed(futures_map):
                item = futures_map[future]
                try:
                    result = future.result()
                    _handle_result(item, result)
                except Exception as exc:
                    _handle_result(item, None, exc=exc)

    summary["total_files_seen"] = total_seen
    return summary


def download_file(session, headers, timeout, default_drive_id, item, shared_url=None, shared_access=None):
    if item.get("download_url"):
        response = session.get(item["download_url"], timeout=timeout, stream=True)
        response.raise_for_status()
        return response

    if shared_url and item.get("share_root"):
        response = session.get(
            build_share_content_url(shared_url, shared_access=shared_access),
            headers=headers,
            timeout=timeout,
            stream=True,
        )
        response.raise_for_status()
        return response

    drive_id = item.get("drive_id") or default_drive_id
    if not drive_id:
        raise SystemExit(f"Cannot resolve drive_id for OneDrive item {item.get('id')}.")

    response = session.get(
        build_content_url(drive_id, item["id"], shared_access=shared_access),
        headers=headers,
        timeout=timeout,
        stream=True,
    )
    response.raise_for_status()
    return response


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
            listed_items = iter_all_items(
                session=session,
                cfg=cfg,
                headers=headers,
                timeout=timeout,
                recursive=args.recursive,
                max_items=max_items,
                emit=True,
                shared_access=shared_access,
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
