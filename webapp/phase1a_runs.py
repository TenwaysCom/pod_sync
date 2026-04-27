from __future__ import annotations

import json
from pathlib import Path

from .actions import PROJECT_ROOT


PHASE1A_OUT_ROOT = PROJECT_ROOT / "phase1a" / "out"


def _safe_read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def discover_phase1a_runs(limit: int = 12):
    if not PHASE1A_OUT_ROOT.exists():
        return []

    runs = []
    for summary_path in PHASE1A_OUT_ROOT.glob("*/phase1a_summary.json"):
        summary = _safe_read_json(summary_path)
        if not summary:
            continue
        run_dir = summary_path.parent.resolve()
        runs.append(
            {
                "run_name": run_dir.name,
                "run_dir": str(run_dir),
                "summary_path": str(summary_path.resolve()),
                "generated_at": summary.get("generated_at"),
                "job_id": summary.get("job_id"),
                "document_count": summary.get("document_count", 0),
                "matched_document_count": summary.get("matched_document_count", 0),
                "unmatched_document_count": summary.get("unmatched_document_count", 0),
                "matched_picking_count": summary.get("matched_picking_count", 0),
                "source_count": summary.get("source_count", 0),
                "output_dir": summary.get("output_dir"),
                "artifacts": summary.get("artifacts", {}),
            }
        )

    runs.sort(key=lambda item: (item.get("generated_at") or "", item["run_name"]), reverse=True)
    return runs[:limit]


def get_phase1a_run(run_name: str):
    summary_path = (PHASE1A_OUT_ROOT / run_name / "phase1a_summary.json").resolve()
    if not summary_path.exists():
        return None
    summary = _safe_read_json(summary_path)
    if not summary:
        return None
    run_dir = summary_path.parent.resolve()
    return {
        "run_name": run_dir.name,
        "run_dir": str(run_dir),
        "summary_path": str(summary_path),
        "summary": summary,
        "artifacts": summary.get("artifacts", {}),
    }


def get_latest_phase1a_run():
    runs = discover_phase1a_runs(limit=1)
    if not runs:
        return None
    return get_phase1a_run(runs[0]["run_name"])


def resolve_phase1a_summary_path(config_path: str | Path):
    resolved_config = Path(config_path).resolve()
    config_payload = _safe_read_json(resolved_config)
    if not config_payload:
        return None
    output_dir = config_payload.get("output_dir")
    if not output_dir:
        return None
    output_path = Path(output_dir)
    if output_path.is_absolute():
        resolved_output_dir = output_path.resolve()
    else:
        resolved_output_dir = (resolved_config.parent / output_path).resolve()
    summary_path = resolved_output_dir / "phase1a_summary.json"
    if summary_path.exists():
        return summary_path
    return None


def resolve_artifact_path(run_name: str, artifact_key: str):
    run = get_phase1a_run(run_name)
    if not run:
        return None
    artifact_path = run["artifacts"].get(artifact_key)
    if not artifact_path:
        return None
    path = Path(artifact_path).resolve()
    run_dir = Path(run["run_dir"]).resolve()
    if run_dir not in path.parents and path != run_dir:
        return None
    if not path.exists():
        return None
    return path
