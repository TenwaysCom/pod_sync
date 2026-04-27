import csv
import json
from pathlib import Path


def ensure_directory(path):
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path):
    records = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            record = json.loads(stripped)
            record["_line_number"] = line_number
            records.append(record)
    return records


def read_csv(path):
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_json(path, payload):
    with Path(path).open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def write_csv(path, rows):
    rows = list(rows)
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
