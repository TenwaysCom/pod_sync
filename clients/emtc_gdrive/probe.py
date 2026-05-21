#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import os
import sys
import threading
from pathlib import Path, PurePosixPath

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


EXPORTABLE_MIME_TYPES = {
    "application/vnd.google-apps.document": (".pdf", "application/pdf"),
    "application/vnd.google-apps.drawing": (".pdf", "application/pdf"),
    "application/vnd.google-apps.presentation": (".pdf", "application/pdf"),
    "application/vnd.google-apps.spreadsheet": (".pdf", "application/pdf"),
}
CONFIG_SECTION_KEY = "emtc_gdrive"
LEGACY_CONFIG_SECTION_KEY = "gdrive"


def get_gdrive_config_section(full_cfg):
    return full_cfg.get(CONFIG_SECTION_KEY) or full_cfg.get(LEGACY_CONFIG_SECTION_KEY) or {}


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        full_cfg = normalize_runtime_config(json.load(fh), path)
    cfg = get_gdrive_config_section(full_cfg)
    has_refresh_flow = cfg.get("client_id") and cfg.get("client_secret") and cfg.get("refresh_token")
    if not cfg.get("access_token") and not has_refresh_flow:
        raise SystemExit("Google Drive config requires access_token or refresh-token credentials.")
    return full_cfg, cfg, full_cfg.get("download", {})


def write_updated_config(config_path, full_cfg):
    path = Path(config_path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(full_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


def has_refresh_flow(cfg):
    return bool(cfg.get("client_id") and cfg.get("client_secret") and cfg.get("refresh_token"))


def refresh_access_token(session, cfg, timeout, full_cfg=None, config_path=None):
    response = session.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": cfg["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise SystemExit("Google OAuth refresh succeeded but no access_token was returned.")
    cfg["access_token"] = token
    if payload.get("refresh_token"):
        cfg["refresh_token"] = payload["refresh_token"]
    if full_cfg is not None and config_path:
        write_updated_config(config_path, full_cfg)
    return token


class RefreshingGDriveSession:
    def __init__(self, raw_session, cfg, timeout, full_cfg, config_path):
        self.raw_session = raw_session
        self.cfg = cfg
        self.timeout = timeout
        self.full_cfg = full_cfg
        self.config_path = config_path
        self._refresh_lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.raw_session, name)

    def refresh_access_token(self):
        with self._refresh_lock:
            return refresh_access_token(
                self.raw_session,
                self.cfg,
                self.timeout,
                full_cfg=self.full_cfg,
                config_path=self.config_path,
            )

    def prepare_headers(self, headers=None, force_refresh=False):
        prepared = dict(headers or {})
        if force_refresh and has_refresh_flow(self.cfg):
            try:
                token = self.refresh_access_token()
                prepared["Authorization"] = f"Bearer {token}"
                return prepared
            except requests.RequestException:
                if not self.cfg.get("access_token"):
                    raise

        if self.cfg.get("access_token"):
            prepared["Authorization"] = f"Bearer {self.cfg['access_token']}"
        return prepared

    def get(self, url, headers=None, **kwargs):
        request_headers = self.prepare_headers(headers)
        response = self.raw_session.get(
            url,
            headers=request_headers if request_headers else None,
            **kwargs,
        )
        if response.status_code == 401 and has_refresh_flow(self.cfg):
            refreshed_headers = self.prepare_headers(headers, force_refresh=True)
            response = self.raw_session.get(
                url,
                headers=refreshed_headers if refreshed_headers else None,
                **kwargs,
            )
        return response


def build_params(cfg, parent_id, limit, page_token=None):
    query = f"'{parent_id}' in parents and trashed = false"
    params = {
        "q": query,
        "pageSize": min(limit, 100) if limit is not None else 100,
        "fields": "nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime, webViewLink, webContentLink, parents, driveId)",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if cfg.get("shared_drive_id"):
        params["corpora"] = "drive"
        params["driveId"] = cfg["shared_drive_id"]
    if page_token:
        params["pageToken"] = page_token
    return params


def serialize_item(item, full_path=None):
    return {
        "source": "gdrive",
        "id": item.get("id"),
        "name": item.get("name"),
        "path": full_path or item.get("name"),
        "is_folder": item.get("mimeType") == "application/vnd.google-apps.folder",
        "mime_type": item.get("mimeType"),
        "size": item.get("size"),
        "created_time": item.get("createdTime"),
        "modified_time": item.get("modifiedTime"),
        "web_view_link": item.get("webViewLink"),
        "web_content_link": item.get("webContentLink"),
        "parents": item.get("parents", []),
        "drive_id": item.get("driveId"),
    }


def emit_item(item, full_path=None):
    output = serialize_item(item, full_path=full_path)
    print(json.dumps(output, ensure_ascii=False))
    return output


def sanitize_filename(name):
    return (name or "gdrive_download.bin").replace("/", "_")


def sanitize_path_component(name):
    return (name or "gdrive").replace("/", "_")


def resolve_output_dir(explicit_output_dir, download_cfg, source_cfg):
    if explicit_output_dir:
        return Path(explicit_output_dir)
    if download_cfg.get("base_dir"):
        return Path(download_cfg["base_dir"]) / sanitize_path_component(source_cfg.get("warehouse") or "gdrive")
    return Path("downloads/gdrive")


def fetch_file_metadata(session, headers, file_id, timeout):
    response = session.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        headers=headers,
        params={"fields": "id, name, mimeType, size, createdTime, modifiedTime, webViewLink, webContentLink, parents, driveId", "supportsAllDrives": "true"},
        timeout=timeout,
    )
    response.raise_for_status()
    return serialize_item(response.json())


def normalize_limit(limit):
    if limit is None or limit <= 0:
        return None
    return limit


def limit_reached(seen, max_items):
    return max_items is not None and seen >= max_items


def iter_items(session, cfg, headers, timeout, recursive, max_items, emit=True):
    if not cfg.get("folder_id"):
        raise SystemExit("Google Drive config requires folder_id for listing.")

    queue = [("", cfg["folder_id"])]
    seen = 0
    while queue and not limit_reached(seen, max_items):
        current_path, folder_id = queue.pop(0)
        page_token = None
        while not limit_reached(seen, max_items):
            remaining = None if max_items is None else max_items - seen
            params = build_params(cfg, folder_id, remaining, page_token=page_token)
            response = session.get(
                "https://www.googleapis.com/drive/v3/files",
                headers=headers,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            files = payload.get("files", [])
            if not files:
                break
            for item in files:
                full_path = str(PurePosixPath(current_path) / item["name"]) if current_path else item["name"]
                output = serialize_item(item, full_path=full_path)
                if emit:
                    print(json.dumps(output, ensure_ascii=False))
                yield output
                seen += 1
                if limit_reached(seen, max_items):
                    return
                if recursive and output["is_folder"]:
                    queue.append((full_path, item["id"]))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break


def build_relative_download_path(item):
    return safe_relative_path(*(PurePosixPath(item.get("path") or item.get("name") or "").parts), default_name=item.get("name") or "gdrive_download.bin")


def download_and_record_file(
    session,
    headers,
    item,
    timeout,
    cfg,
    download_cfg,
    output_dir,
    export_mime_type=None,
    relative_path=None,
    explicit_output_dir=None,
    manifest_lock=None,
):
    response, suffix = download_file(session=session, headers=headers, item=item, timeout=timeout, export_mime_type=export_mime_type)
    file_name = item["name"]
    if suffix and not file_name.lower().endswith(suffix):
        file_name = f"{file_name}{suffix}"

    target_path = relative_path or Path(sanitize_filename(file_name))
    if suffix and relative_path is not None:
        target_path = target_path.with_suffix(suffix)

    destination = output_dir / target_path
    write_result = save_stream_to_file(response, destination)
    manifest_path = resolve_manifest_path("gdrive", cfg, download_cfg, explicit_output_dir=explicit_output_dir)
    manifest_entry = build_manifest_entry(
        source_name="gdrive",
        source_cfg=cfg,
        destination=destination,
        write_result=write_result,
        source_file_id=item.get("id"),
        source_path=item.get("path"),
        file_name=file_name,
        mime_type=item.get("mime_type"),
        source_size=item.get("size"),
        verify_remote_size=not bool(suffix),
        source_modified_time=item.get("modified_time"),
        source_created_time=item.get("created_time"),
        source_drive_id=item.get("drive_id"),
        source_url=item.get("web_content_link") or item.get("web_view_link"),
        downloaded_via="export" if suffix else "media",
        extra={"parents": item.get("parents", [])},
    )
    if manifest_lock is not None:
        with manifest_lock:
            append_manifest_entry(manifest_path, manifest_entry)
    else:
        append_manifest_entry(manifest_path, manifest_entry)
    integrity_result = verify_manifest_entry(manifest_entry)
    result = {
        "downloaded_to": str(destination),
        "file_id": item["id"],
        "manifest_path": str(manifest_path),
        "integrity_status": integrity_result["status"],
        "integrity_checks": integrity_result["checks"],
        "local_size": write_result["local_size"],
    }
    if integrity_result["status"] != "ok":
        raise SystemExit("Downloaded Google Drive file failed local integrity verification.")
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


def sync_all_files(
    session,
    cfg,
    download_cfg,
    headers,
    timeout,
    items,
    export_mime_type=None,
    explicit_output_dir=None,
    workers=1,
    incremental=False,
):
    output_dir = resolve_output_dir(explicit_output_dir, download_cfg, cfg)
    manifest_path = resolve_manifest_path("gdrive", cfg, download_cfg, explicit_output_dir=explicit_output_dir)
    skip_index = load_skip_index(manifest_path) if incremental else {}
    summary = {"event": "sync_summary", "source": "gdrive", "downloaded_files": 0, "skipped_files": 0, "failed_files": 0, "downloaded_bytes": 0, "total_files_seen": 0}

    def _download_one(item):
        return download_and_record_file(
            session=session,
            headers=headers,
            item=item,
            timeout=timeout,
            cfg=cfg,
            download_cfg=download_cfg,
            output_dir=output_dir,
            export_mime_type=export_mime_type,
            relative_path=build_relative_download_path(item),
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
                        "source": "gdrive",
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

    if workers <= 1:
        for item in items:
            if item.get("is_folder"):
                continue
            summary["total_files_seen"] += 1
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
                summary["total_files_seen"] += 1
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

    return summary


def get_download_request(item, export_mime_type=None):
    mime_type = item.get("mime_type")
    file_id = item["id"]
    if mime_type in EXPORTABLE_MIME_TYPES:
        _, default_export_mime_type = EXPORTABLE_MIME_TYPES[mime_type]
        return (
            f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
            {"mimeType": export_mime_type or default_export_mime_type},
            EXPORTABLE_MIME_TYPES[mime_type][0],
        )
    return (
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        {"alt": "media", "supportsAllDrives": "true"},
        "",
    )


def download_file(session, headers, item, timeout, export_mime_type=None):
    url, params, suffix = get_download_request(item, export_mime_type=export_mime_type)
    response = session.get(
        url,
        headers=headers,
        params=params,
        timeout=timeout,
        stream=True,
    )
    response.raise_for_status()
    return response, suffix


def main():
    parser = argparse.ArgumentParser(description="Probe Google Drive folder structure.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--download-first-file", action="store_true")
    parser.add_argument("--download-all-files", action="store_true")
    parser.add_argument("--download-file-id")
    parser.add_argument("--download-export-mime-type")
    parser.add_argument("--output-dir")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent download workers (default: 1)")
    parser.add_argument("--incremental", action="store_true", help="Skip files already recorded in manifest with matching modified_time and existing local file.")
    args = parser.parse_args()

    full_cfg, cfg, download_cfg = load_config(args.config)
    timeout = int(cfg.get("timeout_seconds", 30))
    first_file = None
    max_items = normalize_limit(args.limit)

    download_modes = sum(bool(mode) for mode in (args.download_first_file, args.download_all_files, args.download_file_id))
    if download_modes > 1:
        raise SystemExit("Choose only one of --download-first-file, --download-all-files, or --download-file-id.")

    with requests.Session() as raw_session:
        session = RefreshingGDriveSession(raw_session, cfg, timeout, full_cfg, args.config)
        headers = session.prepare_headers(force_refresh=has_refresh_flow(cfg))
        if not args.download_file_id:
            listed_items = iter_items(
                session=session,
                cfg=cfg,
                headers=headers,
                timeout=timeout,
                recursive=args.recursive,
                max_items=max_items,
                emit=True,
            )
            if args.download_all_files:
                summary = sync_all_files(
                    session=session,
                    cfg=cfg,
                    download_cfg=download_cfg,
                    headers=headers,
                    timeout=timeout,
                    items=listed_items,
                    export_mime_type=args.download_export_mime_type,
                    explicit_output_dir=args.output_dir,
                    workers=args.workers,
                    incremental=args.incremental,
                )
                print(json.dumps(summary, ensure_ascii=False))
                if summary["failed_files"]:
                    raise SystemExit("Google Drive batch sync completed with failures.")
                return

            for item in listed_items:
                if not item["is_folder"] and first_file is None:
                    first_file = item

        target_file = first_file
        if args.download_file_id:
            target_file = fetch_file_metadata(
                session=session,
                headers=headers,
                file_id=args.download_file_id,
                timeout=timeout,
            )

        if args.download_first_file or args.download_file_id:
            if not target_file:
                raise SystemExit("No downloadable Google Drive file found.")
            result = download_and_record_file(
                session=session,
                headers=headers,
                item=target_file,
                timeout=timeout,
                cfg=cfg,
                download_cfg=download_cfg,
                output_dir=resolve_output_dir(args.output_dir, download_cfg, cfg),
                export_mime_type=args.download_export_mime_type,
                explicit_output_dir=args.output_dir,
            )
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
