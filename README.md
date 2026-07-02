# WPA Open Data API

Read-only static API of **publicly-available** World Para Athletics competition
data, published to GitHub Pages. Non-sensitive tables only — no athlete dates of
birth, anthropometrics, classification review status, or internal analysis.

**Live API:** https://lukejgallagher.github.io/wpa-open-data/
(start at `index.html`; machine manifest at `index.json`)

## How it works
A weekly GitHub Action pulls the latest parquet from Azure Blob, runs
`build_public_api.py`, and deploys the `public_api/` tree to Pages. No data is
stored in this repo.

## Endpoints
See `index.json` for the full list. Examples:
- `rankings.csv` / `.parquet`
- `results.csv` (PII columns stripped) · `results/KSA.csv`
- `records.json` · `records/WR.json`
- `mes/nagoya_2026.json` — Asian Para Games qualification list
- `championship_standards.json` · `reference.json`

## Consume it
```python
import pandas as pd
base = "https://lukejgallagher.github.io/wpa-open-data"
ksa = pd.read_csv(f"{base}/results/KSA.csv")
mes = pd.read_json(f"{base}/mes/nagoya_2026.json")
```

Source: IPC SDMS / World Para Athletics. Data refreshes weekly.

## Setup (one-time)
1. Copy `build_public_api.py` and `azure_blob_storage.py` from the private
   working repo into this repo (they carry no data or secrets).
2. Repo → Settings → Secrets → add `AZURE_STORAGE_CONNECTION_STRING`.
3. Repo → Settings → Pages → Source = GitHub Actions.
4. Actions → "Publish open data API" → Run workflow.
