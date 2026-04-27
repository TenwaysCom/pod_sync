# Phase 1A Runbook

This runbook covers the normal operating flow:

1. verify cloud access
2. run sync probes or downloads
3. verify manifests and integrity
4. run Phase 1A
5. inspect dashboard output

## Start

```bash
cd /srv/pod_sync
source .venv/bin/activate
```

## 1. Cloud Access Checks

### OneDrive

```bash
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 1
```

### Google Drive

```bash
python3 clients/emtc_gdrive/probe.py --config config.local.json --limit 1
```

## 2. Sync Operations

### OneDrive first-file download

```bash
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 20 --download-first-file
```

Expected output locations:

- `storage/pod/CMC/...`
- `storage/pod/_integrity/CMC/onedrive.jsonl`

### Google Drive first-file download

```bash
python3 clients/emtc_gdrive/probe.py --config config.local.json --limit 20 --download-first-file
```

Expected output locations:

- `storage/pod/EMTC/...`
- `storage/pod/_integrity/EMTC/gdrive.jsonl`

### API download

```bash
python3 clients/westerman_api/probe.py --config config.local.json --tracking-number YOUR_TRACKING --download
```

## 3. Integrity Checks

### Full verification

```bash
python3 ops/verify_sync_integrity.py --config config.local.json --source all
```

### Quick verification

```bash
python3 ops/verify_sync_integrity.py --config config.local.json --source all --skip-sha256
```

### Status summary

```bash
python3 ops/check_sync_status.py --config config.local.json --quick
```

## 4. Run Phase 1A

### Reuse sync config

```bash
cd phase1a
python3 run_phase1a.py --config examples/phase1a_from_sync.example.json
```

This reads:

- `../../config.local.json`
- manifests under `../storage/pod/_integrity/...`

### Use example manifests

```bash
cd phase1a
python3 run_phase1a.py --config examples/phase1a_config.example.json
```

### Use custom config

```bash
cd phase1a
cp examples/phase1a_from_sync.example.json phase1a_live.json
python3 run_phase1a.py --config phase1a_live.json
```

## 5. Main Outputs

Each Phase 1A run produces:

- `parsed_documents.json`
- `pod_backfill_matched_report.csv`
- `pod_backfill_unmatched_report.csv`
- `odoo_backfill_payload.json`
- `phase1a_summary.json`
- `phase1a_dashboard.html`

Most important files:

1. `pod_backfill_matched_report.csv`
2. `pod_backfill_unmatched_report.csv`
3. `odoo_backfill_payload.json`
4. `phase1a_dashboard.html`

## 6. Inspection

Open:

- `phase1a/out/<run_name>/phase1a_dashboard.html`

Focus on:

1. `Source Health`
2. `Anomalies`
3. `Matched Payload Groups`
4. `Document Explorer`

## 7. Common Issues

### Phase 1A returns 0 documents

Usually one of these:

- sync was not actually downloaded
- manifest files do not exist under `storage/pod/_integrity/...`
- `config.local.json` path is wrong

Check:

```bash
find storage/pod/_integrity -type f
```

### Unmatched documents exist but Odoo should have records

Check the Odoo scope file:

- `picking_name`
- `tracking_numbers`
- eligibility / scope flags

### Change output directory

Update only the Phase 1A config:

- `output_dir`

Do not change sync storage unless you intentionally want a different local storage root.
