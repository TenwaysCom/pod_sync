#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path


PATTERNS = [
    re.compile(r"\b[0-9]{8,24}\b"),
    re.compile(r"\b[A-Z0-9]{10,24}\b"),
    re.compile(r"\b[A-Z]{2,6}[0-9]{6,20}\b"),
]


def find_candidates(text):
    candidates = []
    for pattern in PATTERNS:
        candidates.extend(pattern.findall(text.upper()))
    return sorted(set(candidates))


def main():
    parser = argparse.ArgumentParser(description="Extract tracking candidates from file names or paths.")
    parser.add_argument("input_file", help="Text file with one filename or path per line.")
    args = parser.parse_args()

    for raw_line in Path(args.input_file).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        print(
            json.dumps(
                {
                    "input": line,
                    "tracking_candidates": find_candidates(line),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
