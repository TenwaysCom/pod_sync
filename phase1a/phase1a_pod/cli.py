import argparse
import json

from .pipeline import run_phase1a


def main():
    parser = argparse.ArgumentParser(description="Run POD Phase 1A parsing, matching, and payload export.")
    parser.add_argument("--config", required=True, help="Path to phase1a config JSON.")
    args = parser.parse_args()

    summary = run_phase1a(args.config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
