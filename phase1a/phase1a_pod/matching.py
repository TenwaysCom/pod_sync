import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .io_utils import read_csv, read_json, read_jsonl
from .parsing import normalize_picking_name, normalize_tracking, parse_bool


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def split_tracking_numbers(value):
    if value in (None, ""):
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                raw_items = json.loads(text)
            except json.JSONDecodeError:
                raw_items = [part.strip() for part in text.replace(";", ",").split(",")]
        else:
            raw_items = [part.strip() for part in text.replace(";", ",").split(",")]
    values = []
    for item in raw_items:
        normalized = normalize_tracking(item)
        if normalized and normalized not in values:
            values.append(normalized)
    return values


@dataclass(frozen=True)
class ScopePicking:
    odoo_picking_id: str | None
    picking_name: str
    normalized_picking_name: str
    tracking_numbers: tuple[str, ...]
    eligible: bool
    in_scope: bool
    company_id: str | None
    raw: dict

    @property
    def ref(self):
        if self.odoo_picking_id not in (None, ""):
            return f"{self.picking_name}#{self.odoo_picking_id}"
        return self.picking_name


class ScopeIndex:
    def __init__(self, pickings):
        self.pickings = pickings
        self.by_picking = defaultdict(list)
        self.by_tracking = defaultdict(list)
        for picking in pickings:
            self.by_picking[picking.normalized_picking_name].append(picking)
            for tracking_number in picking.tracking_numbers:
                self.by_tracking[tracking_number].append(picking)


def load_scope_records(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = read_json(path)
        if isinstance(payload, dict):
            if "items" in payload:
                return payload["items"]
            return [payload]
        return payload
    if suffix == ".jsonl":
        return read_jsonl(path)
    if suffix == ".csv":
        return read_csv(path)
    raise SystemExit(f"Unsupported Odoo scope file: {path}")


def build_scope_index(path):
    pickings = []
    for record in load_scope_records(path):
        picking_name = record.get("picking_name")
        normalized_picking_name = normalize_picking_name(picking_name)
        if not normalized_picking_name:
            continue
        odoo_picking_id = record.get("odoo_picking_id")
        if odoo_picking_id not in (None, ""):
            odoo_picking_id = str(odoo_picking_id)
        pickings.append(
            ScopePicking(
                odoo_picking_id=odoo_picking_id,
                picking_name=str(picking_name),
                normalized_picking_name=normalized_picking_name,
                tracking_numbers=tuple(split_tracking_numbers(record.get("tracking_numbers"))),
                eligible=parse_bool(record.get("eligible"), default=True),
                in_scope=parse_bool(record.get("in_scope"), default=True),
                company_id=str(record.get("company_id")) if record.get("company_id") not in (None, "") else None,
                raw=dict(record),
            )
        )
    return ScopeIndex(pickings)


def _dedupe_candidates(candidates):
    deduped = []
    seen = set()
    for candidate in candidates:
        key = (candidate.picking_name, candidate.odoo_picking_id)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def _candidate_refs(candidates):
    return [candidate.ref for candidate in candidates]


def _resolve_from_candidates(keyed_candidates, *, key_type):
    keyed_candidates = list(keyed_candidates)
    candidate_key_map = defaultdict(list)
    deduped_candidates = []
    seen = set()
    for key_value, candidate in keyed_candidates:
        candidate_id = (candidate.picking_name, candidate.odoo_picking_id)
        if key_value and key_value not in candidate_key_map[candidate_id]:
            candidate_key_map[candidate_id].append(key_value)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        deduped_candidates.append(candidate)

    candidates = _dedupe_candidates(deduped_candidates)
    eligible_candidates = [item for item in candidates if item.eligible and item.in_scope]
    last_attempt = utc_now()

    if len(eligible_candidates) == 1:
        selected = eligible_candidates[0]
        selected_id = (selected.picking_name, selected.odoo_picking_id)
        return {
            "match_status": "matched",
            "selected": selected,
            "match_key_type": key_type,
            "match_key": next((value for value in candidate_key_map[selected_id] if value), None),
            "candidate_count": len(eligible_candidates),
            "candidate_picking_refs": _candidate_refs(eligible_candidates),
            "reason_code": None,
            "reason_message": None,
            "last_match_attempt_at": last_attempt,
        }

    if len(eligible_candidates) > 1:
        return {
            "match_status": "unmatched",
            "selected": None,
            "match_key_type": key_type,
            "match_key": None,
            "candidate_count": len(eligible_candidates),
            "candidate_picking_refs": _candidate_refs(eligible_candidates),
            "reason_code": "MULTIPLE_PICKING_CANDIDATES",
            "reason_message": "Multiple eligible Odoo pickings matched the same POD document.",
            "last_match_attempt_at": last_attempt,
        }

    if candidates:
        if any(not item.in_scope for item in candidates):
            reason_code = "ODOO_SCOPE_FILTERED_OUT"
            reason_message = "Matching pickings exist but are filtered out of the current Odoo scope."
        else:
            reason_code = "PICKING_NOT_ELIGIBLE"
            reason_message = "Matching pickings exist but are not eligible for POD backfill."
        return {
            "match_status": "unmatched",
            "selected": None,
            "match_key_type": key_type,
            "match_key": None,
            "candidate_count": len(candidates),
            "candidate_picking_refs": _candidate_refs(candidates),
            "reason_code": reason_code,
            "reason_message": reason_message,
            "last_match_attempt_at": last_attempt,
        }

    return None


def match_document(doc, scope_index):
    picking_candidates = []
    for picking_name in doc["inferred_picking_names"]:
        for candidate in scope_index.by_picking.get(picking_name, []):
            picking_candidates.append((picking_name, candidate))
    result = _resolve_from_candidates(
        picking_candidates,
        key_type="picking_name",
    )
    if result:
        return result

    tracking_candidates = []
    for tracking_number in doc["inferred_tracking_numbers"]:
        for candidate in scope_index.by_tracking.get(tracking_number, []):
            tracking_candidates.append((tracking_number, candidate))
    result = _resolve_from_candidates(
        tracking_candidates,
        key_type="tracking_number",
    )
    if result:
        return result

    reason_code = "NO_PICKING_FOUND"
    reason_message = "No Odoo picking matched the inferred picking_name or tracking_number."
    if not doc["inferred_picking_names"] and not doc["inferred_tracking_numbers"]:
        reason_code = "INVALID_PICKING_KEY"
        reason_message = "The POD file name/path did not yield a usable picking_name or tracking_number."
    elif not doc["inferred_tracking_numbers"] and not doc["inferred_picking_names"]:
        reason_code = "INVALID_TRACKING_KEY"
        reason_message = "The POD file name/path did not yield a usable tracking_number."

    return {
        "match_status": "unmatched",
        "selected": None,
        "match_key_type": None,
        "match_key": None,
        "candidate_count": 0,
        "candidate_picking_refs": [],
        "reason_code": reason_code,
        "reason_message": reason_message,
        "last_match_attempt_at": utc_now(),
    }
