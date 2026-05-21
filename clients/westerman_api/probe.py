#!/usr/bin/env python3
import argparse
import base64
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clients.common.sync_integrity import (
    append_manifest_entry,
    build_manifest_entry,
    normalize_runtime_config,
    resolve_manifest_path,
    save_bytes_to_file,
    save_stream_to_file,
    verify_manifest_entry,
)


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        full_cfg = normalize_runtime_config(json.load(fh), path)
    return full_cfg["westerman_api"], full_cfg.get("download", {})


def _safe_body_summary(body):
    if isinstance(body, dict):
        return {"type": "object", "keys": sorted(str(key) for key in body.keys())[:20]}
    if isinstance(body, list):
        return {"type": "array", "length": len(body)}
    return {"type": type(body).__name__}


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


def main():
    parser = argparse.ArgumentParser(description="Probe POD API endpoint.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--tracking-number", required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--output-dir")
    args = parser.parse_args()

    cfg, download_cfg = load_config(args.config)
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
        "final_url": str(response.url),
    }

    payload = None
    try:
        body = response.json()
        result["body_type"] = "json"
        result["body_summary"] = _safe_body_summary(body)
        payload = body
    except ValueError:
        result["body_type"] = "text"
        result["body_preview"] = response.text[:300]

    print(json.dumps(result, ensure_ascii=False, indent=2))
    response.raise_for_status()

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
                download_response.raise_for_status()
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
