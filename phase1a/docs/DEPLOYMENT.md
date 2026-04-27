# Phase 1A Deployment

This repository is designed to deploy as a standalone Python project.

Current assumptions:

- no database
- no Node.js runtime
- no Odoo runtime dependency
- local storage stays inside the repository
- sync output defaults to `./storage/pod`

## Target Layout

Recommended deployment path:

- repo root: `/srv/pod_sync`
- sync storage: `/srv/pod_sync/storage/pod`
- Phase 1A output: `/srv/pod_sync/phase1a/out/...`

## Prerequisites

- Linux host
- Python 3.11+ recommended
- `python3 -m venv`
- outbound access to:
  - `graph.microsoft.com`
  - `login.microsoftonline.com`
  - `www.googleapis.com`
  - `oauth2.googleapis.com`

## Deployment Steps

### 1. Clone repository

```bash
git clone git@github.com:TenwaysCom/pod_sync.git /srv/pod_sync
cd /srv/pod_sync
```

### 2. Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Create local config

```bash
cp config.example.json config.local.json
```

Fill real values in `config.local.json`:

- `cmc_onedrive.client_id`
- `cmc_onedrive.refresh_token`
- `emtc_gdrive.client_id`
- `emtc_gdrive.client_secret`
- `emtc_gdrive.refresh_token`

Do not commit `config.local.json`.

### 4. Prepare storage directories

```bash
mkdir -p storage/pod
mkdir -p storage/pod/_integrity
mkdir -p storage/pod/CMC
mkdir -p storage/pod/EMTC
mkdir -p storage/pod/Westerman
```

### 5. Verify cloud access

```bash
source .venv/bin/activate
python3 clients/cmc_onedrive/probe.py --config config.local.json --limit 1
python3 clients/emtc_gdrive/probe.py --config config.local.json --limit 1
```

### 6. Run Phase 1A

```bash
cd phase1a
source ../.venv/bin/activate
python3 run_phase1a.py --config examples/phase1a_from_sync.example.json
```

The default example reads:

- `../../config.local.json`
- manifests under `../storage/pod/_integrity/...`

## Post-Deployment Checks

Check these paths after a real sync run:

- `storage/pod/_integrity/CMC/onedrive.jsonl`
- `storage/pod/_integrity/EMTC/gdrive.jsonl`
- `phase1a/out/<run_name>/phase1a_summary.json`
- `phase1a/out/<run_name>/phase1a_dashboard.html`

## Notes

- Relative paths are resolved from the config file location.
- You do not need `/data/backup/pod` unless you intentionally switch back to an absolute storage path.
