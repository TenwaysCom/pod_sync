#!/usr/bin/env python3
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PROJECT_STORAGE = Path("storage") / "pod"


def sanitize_path_component(name, default):
    return (name or default).replace("/", "_")


def normalize_size(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_sha256(path, chunk_size=1024 * 256):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            if chunk:
                digest.update(chunk)
    return digest.hexdigest()


def save_stream_to_file(response, destination, chunk_size=1024 * 256):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as fh:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                fh.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    return {"local_size": total, "sha256": digest.hexdigest()}


def save_bytes_to_file(content, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    digest.update(content)
    destination.write_bytes(content)
    return {"local_size": len(content), "sha256": digest.hexdigest()}


def resolve_config_relative_path(raw_path, config_path):
    config_dir = Path(config_path).resolve().parent
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (config_dir / path).resolve()


def normalize_runtime_config(full_cfg, config_path):
    normalized = dict(full_cfg)
    download_cfg = dict(normalized.get("download", {}))

    base_dir = download_cfg.get("base_dir")
    if base_dir:
        download_cfg["base_dir"] = str(resolve_config_relative_path(base_dir, config_path))
    else:
        download_cfg["base_dir"] = str(resolve_config_relative_path(DEFAULT_PROJECT_STORAGE, config_path))

    if download_cfg.get("integrity_dir"):
        download_cfg["integrity_dir"] = str(resolve_config_relative_path(download_cfg["integrity_dir"], config_path))

    normalized["download"] = download_cfg
    return normalized


def resolve_integrity_root(download_cfg, explicit_output_dir=None):
    if explicit_output_dir:
        return Path(explicit_output_dir).parent / "_integrity"
    if download_cfg.get("integrity_dir"):
        return Path(download_cfg["integrity_dir"])
    if download_cfg.get("base_dir"):
        return Path(download_cfg["base_dir"]) / "_integrity"
    return Path("downloads/_integrity")


def resolve_report_dir(download_cfg, explicit_output_dir=None):
    return resolve_integrity_root(download_cfg, explicit_output_dir=explicit_output_dir) / "_reports"


def resolve_manifest_path(source_name, source_cfg, download_cfg, explicit_output_dir=None):
    warehouse = sanitize_path_component(source_cfg.get("warehouse"), source_name)
    source_file = sanitize_path_component(source_name, source_name)
    return resolve_integrity_root(download_cfg, explicit_output_dir=explicit_output_dir) / warehouse / f"{source_file}.jsonl"


def safe_relative_path(*parts, default_name="download.bin"):
    cleaned_parts = []
    for part in parts:
        if part in (None, "", ".", "/"):
            continue
        text = str(part).replace("\\", "/").strip("/")
        if not text:
            continue
        for piece in text.split("/"):
            cleaned = sanitize_path_component(piece, "item")
            if cleaned not in ("", ".", ".."):
                cleaned_parts.append(cleaned)
    if not cleaned_parts:
        cleaned_parts = [sanitize_path_component(default_name, "download.bin")]
    return Path(*cleaned_parts)


def build_manifest_entry(
    *,
    source_name,
    source_cfg,
    destination,
    write_result,
    source_file_id=None,
    source_path=None,
    file_name=None,
    mime_type=None,
    source_size=None,
    verify_remote_size=False,
    source_modified_time=None,
    source_created_time=None,
    source_drive_id=None,
    source_url=None,
    downloaded_via=None,
    extra=None,
):
    destination = Path(destination)
    entry = {
        "manifest_version": 1,
        "source": source_name,
        "warehouse": source_cfg.get("warehouse") or source_name,
        "source_file_id": source_file_id,
        "source_path": source_path,
        "file_name": file_name or destination.name,
        "mime_type": mime_type,
        "source_size": normalize_size(source_size),
        "verify_remote_size": bool(verify_remote_size and normalize_size(source_size) is not None),
        "source_modified_time": source_modified_time,
        "source_created_time": source_created_time,
        "source_drive_id": source_drive_id,
        "source_url": source_url,
        "downloaded_via": downloaded_via,
        "downloaded_at": utc_now(),
        "local_path": str(destination.resolve()),
        "local_size": int(write_result["local_size"]),
        "sha256": write_result["sha256"],
    }
    if extra:
        entry.update(extra)
    return entry


def append_manifest_entry(manifest_path, entry):
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False))
        fh.write("\n")


def load_manifest_entries(manifest_path):
    manifest_path = Path(manifest_path)
    entries = []
    with manifest_path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            entry = json.loads(stripped)
            entry["_manifest_line"] = line_number
            entries.append(entry)
    return entries


def verify_manifest_entry(entry, verify_hash=True):
    local_path = Path(entry["local_path"])
    checks = []
    actual_size = None
    actual_sha256 = None
    expected_local_size = normalize_size(entry.get("local_size"))
    expected_source_size = normalize_size(entry.get("source_size"))

    if not local_path.exists():
        checks.append("missing_file")
    else:
        actual_size = local_path.stat().st_size
        if expected_local_size is not None and actual_size != expected_local_size:
            checks.append("local_size_mismatch")
        if entry.get("verify_remote_size") and expected_source_size is not None and actual_size != expected_source_size:
            checks.append("remote_size_mismatch")
        if verify_hash and entry.get("sha256"):
            actual_sha256 = compute_sha256(local_path)
            if actual_sha256 != entry["sha256"]:
                checks.append("sha256_mismatch")

    return {
        "source": entry.get("source"),
        "warehouse": entry.get("warehouse"),
        "source_file_id": entry.get("source_file_id"),
        "file_name": entry.get("file_name"),
        "local_path": entry.get("local_path"),
        "manifest_line": entry.get("_manifest_line"),
        "status": "ok" if not checks else "failed",
        "checks": checks,
        "actual_size": actual_size,
        "actual_sha256": actual_sha256,
    }
