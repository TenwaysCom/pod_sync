import hashlib
import re
from pathlib import Path


"""POD filename parsing rules.

Core rules used by the current Phase 1A / POD recognition pipeline:

- EMTC Google Drive
  - Files like `2026-03-19_3SYZXG8293874.png` use the trailing token as
    the tracking number.
  - Postal tracking formats such as `LA857793468NL` and `CF438625735NL`
    are also treated as tracking numbers.

- CMC OneDrive
  - `EU22114.pdf` is restored to `WCB2B/OUT/22114`.
  - `HK23898.pdf` is restored to `WCB2C/OUT/23898`.
  - `WBB2BRET01301` is treated as `WBB2B/RET/01301`.
  - `Other07400` is treated as `Other/07400`.
  - Trailing alphabetic characters after the numeric portion are stripped,
    e.g. `WCB2BOUT19782A` -> `WCB2B/OUT/19782`.
  - Pure numeric file names like `23735.pdf` are also restored to
    `WCB2B/OUT/23735`.
  - Numeric page suffixes such as `23735-1.pdf` or `EU22114-2.pdf` are
    treated as extra pages of the same POD, not separate orders.
  - Explicit picking keys like `WCB2BOUT20655`, `WCB2C-OUT-23898`,
    and `WHOUT15504` are used directly.
"""


# Tracking extraction is intentionally broad because EMTC mostly exposes
# tracking numbers rather than outbound picking names in its file names.
TRACKING_PATTERNS = [
    re.compile(r"(?<![A-Z0-9])WHOUT\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])WCB2BOUT\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])WCB2COUT\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])WBB2BRET\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"(?i)(?<![A-Z0-9])Other\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])3SHOHR\d{6,}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{8,14}[A-Z]{2}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])[0-9][A-Z]{2,10}\d{6,24}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])[A-Z]{2,10}\d{6,24}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])[0-9]{8,24}(?![A-Z0-9])"),
]

# Direct picking keys already embedded in file names.
# Lookbehind is intentionally omitted so that patterns can match after
# shorthand prefixes like EU/HK (e.g. EUWBB2BRET01162).
PICKING_PATTERNS = [
    re.compile(r"WHOUT\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"WCB2B[-_ /]?OUT[-_ /]?\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"WCB2C[-_ /]?OUT[-_ /]?\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"WBB2B[-_ /]?RET[-_ /]?\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"WCB2B[-_ /]?RET[-_ /]?\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"WRB2B[-_ /]?RET[-_ /]?\d{4,}[A-Z]?(?![A-Z0-9])"),
    re.compile(r"(?i)Other\d{4,}[A-Z]?(?![A-Z0-9])"),
]

# CMC shorthand file names.
PREFIX_GROUP_PATTERNS = {
    "EU": re.compile(r"(?i)\bEU[-_ ]?(\d+[A-Z]?(?:-\d+[A-Z]?){0,12})\b"),
    "HK": re.compile(r"(?i)\bHK[-_ ]?(\d+[A-Z]?(?:-\d+[A-Z]?){0,12})\b"),
}

# Some CMC files are stored as bare order numbers like `23735.pdf` or
# paginated variants such as `23735-1.pdf`.
CMC_NUMERIC_STEM_PATTERN = re.compile(r"^(?P<base>\d{4,})(?P<pages>(?:-\d{1,2}){0,12})$")


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


def _strip_trailing_alpha(value):
    """Remove trailing alphabetic characters after the last digit sequence.

    Only strips noise letters from picking-like tokens.
    Preserves postal tracking codes (e.g. LA857793468NL, CF438625735NL).

    Examples:
        WCB2BOUT19782A -> WCB2BOUT19782
        EU22114B -> EU22114
        Other07400X -> Other07400
        LA857793468NL -> LA857793468NL   (preserved)
    """
    if not value:
        return value
    upper = value.upper()
    # Postal tracking codes: two letters, digits, two letters — trailing letters are real
    if re.fullmatch(r"[A-Z]{2}\d+[A-Z]{2}", upper):
        return upper
    match = re.search(r"^(.+?\d+)[A-Z]+$", upper)
    if match:
        return match.group(1)
    return upper


def _expand_prefixed_groups(prefix, group_text):
    parts = [part for part in group_text.upper().split("-") if part]
    if not parts:
        return []

    base = parts[0]
    if not re.fullmatch(r"\d+[A-Z]?", base):
        return []

    trailing_parts = parts[1:]
    # Single-digit trailing chunks are treated as page markers, e.g.
    # `EU22114-1.pdf` or `HK23898-2.pdf`.
    if trailing_parts and all(re.fullmatch(r"\d{1,2}", part) and int(part) <= 9 for part in trailing_parts):
        return [_strip_trailing_alpha(f"{prefix}{base}")]

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

    return [_strip_trailing_alpha(f"{prefix}{part}") for part in resolved]


def extract_picking_candidates(*texts):
    candidates = []
    for text in texts:
        upper_text = (text or "").upper()
        for pattern in PICKING_PATTERNS:
            candidates.extend(pattern.findall(upper_text))
        for prefix, pattern in PREFIX_GROUP_PATTERNS.items():
            for match in pattern.finditer(upper_text):
                candidates.extend(_expand_prefixed_groups(prefix, match.group(1)))
    normalized = []
    for candidate in candidates:
        candidate = _strip_trailing_alpha(candidate)
        key = normalize_picking_name(candidate)
        if key and key not in normalized:
            normalized.append(key)
    return normalized


def _extract_cmc_numeric_filename_candidates(file_name):
    stem = Path(file_name or "").stem.upper()
    match = CMC_NUMERIC_STEM_PATTERN.fullmatch(stem)
    if not match:
        return []

    trailing_parts = [part for part in match.group("pages").split("-") if part]
    if trailing_parts and not all(int(part) <= 9 for part in trailing_parts):
        return []

    restored = normalize_picking_name(f"WCB2B/OUT/{match.group('base')}")
    return [restored] if restored else []


def _restore_cmc_picking_candidates(*texts):
    restored = []
    for candidate in extract_picking_candidates(*texts):
        if candidate.startswith("EU"):
            suffix = candidate[len("EU"):]
            restored_candidate = normalize_picking_name(f"WCB2B/OUT/{suffix}")
            if restored_candidate and restored_candidate not in restored:
                restored.append(restored_candidate)
        elif candidate.startswith("HK"):
            suffix = candidate[len("HK"):]
            restored_candidate = normalize_picking_name(f"WCB2C/OUT/{suffix}")
            if restored_candidate and restored_candidate not in restored:
                restored.append(restored_candidate)
    return restored


def _picking_candidate_suffix(candidate):
    if candidate.startswith("WCB2BOUT"):
        return candidate[len("WCB2BOUT"):]
    if candidate.startswith("WCB2COUT"):
        return candidate[len("WCB2COUT"):]
    if candidate.startswith("WBB2BRET"):
        return candidate[len("WBB2BRET"):]
    if candidate.startswith("Other"):
        return candidate[len("Other"):]
    if candidate.startswith("EU"):
        return candidate[len("EU"):]
    if candidate.startswith("HK"):
        return candidate[len("HK"):]
    return None


def _has_effective_multiple_picking_candidates(candidates):
    if len(candidates) <= 1:
        return False
    suffixes = [_picking_candidate_suffix(candidate) for candidate in candidates]
    if all(suffix is not None for suffix in suffixes) and len(set(suffixes)) == 1:
        return False
    return True


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
        candidate = _strip_trailing_alpha(candidate)
        key = normalize_tracking(candidate)
        if not key or _looks_like_date_token(key):
            continue
        if key not in normalized:
            normalized.append(key)
    return normalized


def _merge_unique(*groups):
    merged = []
    for group in groups:
        for item in group:
            if item and item not in merged:
                merged.append(item)
    return merged


def _is_cmc_source(source_name):
    return canonicalize_key(source_name).startswith("CMC")


def infer_document_fields(record, source_name=None, source_type=None):
    source_name = source_name or record.get("warehouse") or record.get("source") or "UNKNOWN"
    source_type = source_type or record.get("source") or "unknown"
    source_path = record.get("source_path") or record.get("local_path") or record.get("file_name") or ""
    file_name = record.get("file_name") or Path(source_path).name
    file_url = record.get("source_url")
    inferred_picking_names = extract_picking_candidates(file_name, source_path, record.get("local_path"))
    if _is_cmc_source(source_name):
        # CMC may encode the same outbound order in several file-name styles:
        # EU/HK shorthand, direct WCB2B/WCB2C keys, or bare numeric names.
        inferred_picking_names = _merge_unique(
            _extract_cmc_numeric_filename_candidates(file_name),
            _restore_cmc_picking_candidates(file_name, source_path, record.get("local_path")),
            inferred_picking_names,
        )
    inferred_tracking_numbers = [
        item for item in extract_tracking_candidates(file_name, source_path, record.get("local_path")) if item not in inferred_picking_names
    ]

    parse_notes = []
    if not inferred_picking_names and not inferred_tracking_numbers:
        parse_notes.append("picking_name_not_found")
    if file_url in (None, ""):
        parse_notes.append("source_url_missing")
    if _has_effective_multiple_picking_candidates(inferred_picking_names):
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
