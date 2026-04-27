import hashlib
import re
from pathlib import Path


TRACKING_PATTERNS = [
    re.compile(r"(?<![A-Z0-9])WHOUT\d{4,}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])WCB2BOUT\d{4,}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])3SHOHR\d{6,}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])[A-Z]{2,10}\d{6,24}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])[0-9]{8,24}(?![A-Z0-9])"),
]

PICKING_PATTERNS = [
    re.compile(r"(?<![A-Z0-9])WHOUT\d{4,}(?![A-Z0-9])"),
]

EU_GROUP_PATTERN = re.compile(r"(?i)\bEU[-_ ]?(\d+[A-Z]?(?:-\d+[A-Z]?){0,12})\b")


def canonicalize_key(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def build_document_uid(source_type, source_name, source_file_id, source_path):
    stable_key = source_file_id or source_path or ""
    digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:12]
    return f"pod:{source_type}:{canonicalize_key(source_name)}:{digest}"


def normalize_tracking(value):
    normalized = canonicalize_key(value)
    return normalized or None


def normalize_picking_name(value):
    normalized = canonicalize_key(value)
    return normalized or None


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    return default


def _expand_eu_groups(group_text):
    parts = [part for part in group_text.upper().split("-") if part]
    if not parts:
        return []

    base = parts[0]
    if not re.fullmatch(r"\d+[A-Z]?", base):
        return []

    resolved = [base]
    previous_digits = re.match(r"^(\d+)", base).group(1)
    previous_number = int(previous_digits)

    for part in parts[1:]:
        if not re.fullmatch(r"\d+[A-Z]?", part):
            continue
        match = re.match(r"^(\d+)([A-Z]?)$", part)
        digits = match.group(1)
        suffix = match.group(2)
        if len(digits) >= len(previous_digits):
            full_digits = digits
        else:
            prefix_number = int(previous_digits[:-len(digits)] or "0")
            candidate_same_prefix = int(f"{prefix_number}{digits}")
            if candidate_same_prefix < previous_number:
                next_prefix_number = prefix_number + 1
                candidate_next_prefix = int(f"{next_prefix_number}{digits}")
                gap_same = abs(previous_number - candidate_same_prefix)
                gap_next = abs(candidate_next_prefix - previous_number)
                if gap_next < gap_same:
                    candidate_same_prefix = candidate_next_prefix
            full_digits = str(candidate_same_prefix)
        resolved.append(f"{full_digits}{suffix}")
        previous_digits = full_digits
        previous_number = int(full_digits)

    return [f"EU{part}" for part in resolved]


def extract_picking_candidates(*texts):
    candidates = []
    for text in texts:
        upper_text = (text or "").upper()
        for pattern in PICKING_PATTERNS:
            candidates.extend(pattern.findall(upper_text))
        for match in EU_GROUP_PATTERN.finditer(upper_text):
            candidates.extend(_expand_eu_groups(match.group(1)))
    normalized = []
    for candidate in candidates:
        key = normalize_picking_name(candidate)
        if key and key not in normalized:
            normalized.append(key)
    return normalized


def _looks_like_date_token(token):
    return bool(re.fullmatch(r"20\d{6,10}", token))


def extract_tracking_candidates(*texts):
    candidates = []
    for text in texts:
        upper_text = (text or "").upper()
        for pattern in TRACKING_PATTERNS:
            candidates.extend(pattern.findall(upper_text))
    normalized = []
    for candidate in candidates:
        key = normalize_tracking(candidate)
        if not key or _looks_like_date_token(key):
            continue
        if key not in normalized:
            normalized.append(key)
    return normalized


def infer_document_fields(record, source_name=None, source_type=None):
    source_name = source_name or record.get("warehouse") or record.get("source") or "UNKNOWN"
    source_type = source_type or record.get("source") or "unknown"
    source_path = record.get("source_path") or record.get("local_path") or record.get("file_name") or ""
    file_name = record.get("file_name") or Path(source_path).name
    file_url = record.get("source_url")
    inferred_picking_names = extract_picking_candidates(file_name, source_path, record.get("local_path"))
    inferred_tracking_numbers = [
        item for item in extract_tracking_candidates(file_name, source_path, record.get("local_path")) if item not in inferred_picking_names
    ]

    parse_notes = []
    if not inferred_picking_names:
        parse_notes.append("picking_name_not_found")
    if file_url in (None, ""):
        parse_notes.append("source_url_missing")
    if len(inferred_picking_names) > 1:
        parse_notes.append("multiple_picking_candidates")

    return {
        "document_uid": build_document_uid(
            source_type=source_type,
            source_name=source_name,
            source_file_id=record.get("source_file_id"),
            source_path=source_path,
        ),
        "source_name": source_name,
        "source_type": source_type,
        "source_file_id": record.get("source_file_id"),
        "source_path": source_path,
        "file_name": file_name,
        "file_url": file_url,
        "mime_type": record.get("mime_type"),
        "file_ext": Path(file_name).suffix.lower(),
        "source_modified_at": record.get("source_modified_time"),
        "first_seen_at": record.get("downloaded_at"),
        "last_seen_at": record.get("downloaded_at") or record.get("source_modified_time"),
        "local_path": record.get("local_path"),
        "inferred_picking_names": inferred_picking_names,
        "inferred_tracking_numbers": inferred_tracking_numbers,
        "parse_notes": parse_notes,
        "raw_record": record,
    }
