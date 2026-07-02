"""
build_public_api.py - publish a static, read-only data API to GitHub Pages.
===========================================================================
Exports only NON-SENSITIVE tables - data already published openly by the IPC /
paralympic.org (rankings, records, Minimum Entry Standard lists, championship
standards, historical Games results). Athlete PII and internal analysis
(classification master lists, reclassification flags, athlete profiles/DOB,
KSA-specific analysis) are deliberately EXCLUDED via an allowlist.

Output tree (served by GitHub Pages):
    public_api/
      index.html          - human-readable API docs
      index.json          - machine manifest (endpoints, rows, columns, updated)
      rankings.csv / .parquet
      records.csv / .parquet / .json
      records/{TYPE}.json
      mes/{edition}.json
      championship_standards.json
      games.csv / .parquet
      reference.json       - event / class / type code vocabularies

Big tables are CSV + parquet only (JSON would be tens of MB); small tables also
get JSON. Every endpoint is listed in index.json with its available formats.

Usage:
    python build_public_api.py                 # read data/parquet_cache -> public_api/
    python build_public_api.py --out docs/api  # custom output dir
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

CACHE = Path("data/parquet_cache")

# ALLOWLIST - only these tables are ever published. Everything else stays private.
PUBLIC_TABLES = {
    "results": "Every performance (full-field results): athlete, event-class, mark, "
               "position, competition, date. PII columns (DOB, height, weight) stripped.",
    "rankings": "World + regional performance rankings (2009-2026).",
    "records": "World / Paralympic / Championship / area records by event-class.",
    "mes_qualification": "Minimum Entry Standard qualification lists per Games edition.",
    "championship_standards": "Computed gold/silver/bronze/finalist standards per event-class.",
    "games_results": "Historical Paralympic Games athletics results (1960-present).",
}

# Columns stripped from EVERY published table - a global PII safety net, so no
# sensitive field leaks even if a source table gains one. Names + marks stay
# (public competition data); dates of birth and anthropometrics do not.
SENSITIVE_COLUMNS = {"dob", "date_of_birth", "birthdate", "birth_date",
                     "height", "weight", "bmi"}

JSON_ROW_LIMIT = 20000  # tables larger than this are CSV+parquet only (no JSON)

# Pre-generated per-country slices so partners can pull one NPC in a single
# fetch (e.g. results/KSA.csv) instead of filtering the whole table.
NPC_SLICES = ["KSA"]
_COUNTRY_COLS = ("npc", "nationality", "country_code", "country")


def _strip_sensitive(df: pd.DataFrame) -> pd.DataFrame:
    drop = [c for c in df.columns if c.lower() in SENSITIVE_COLUMNS]
    return df.drop(columns=drop) if drop else df


def _country_col(df: pd.DataFrame) -> str | None:
    return next((c for c in _COUNTRY_COLS if c in df.columns), None)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_table(df: pd.DataFrame, name: str, out: Path, endpoints: list) -> None:
    formats = []
    (out / f"{name}.csv").write_text(df.to_csv(index=False), encoding="utf-8")
    formats.append("csv")
    df.to_parquet(out / f"{name}.parquet", index=False)
    formats.append("parquet")
    if len(df) <= JSON_ROW_LIMIT:
        (out / f"{name}.json").write_text(
            df.to_json(orient="records"), encoding="utf-8")
        formats.append("json")
    endpoints.append({"endpoint": name, "rows": int(len(df)),
                      "columns": list(df.columns), "formats": formats,
                      "description": PUBLIC_TABLES.get(name, "")})


def _write_json(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=None), encoding="utf-8")


def build(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    endpoints: list = []

    # --- core tables --------------------------------------------------------
    for name in PUBLIC_TABLES:
        src = CACHE / f"{name}.parquet"
        if not src.exists():
            print(f"skip {name}: {src} not found")
            continue
        df = _strip_sensitive(pd.read_parquet(src))
        _write_table(df, name, out_dir, endpoints)
        print(f"published {name}: {len(df):,} rows")

        # per-country slices (e.g. results/KSA.csv) - one fetch per NPC
        ccol = _country_col(df)
        if ccol:
            (out_dir / name).mkdir(exist_ok=True)
            for npc in NPC_SLICES:
                sub = df[df[ccol] == npc]
                if sub.empty:
                    continue
                (out_dir / name / f"{npc}.csv").write_text(
                    sub.to_csv(index=False), encoding="utf-8")
                fmts = ["csv"]
                if len(sub) <= JSON_ROW_LIMIT:
                    _write_json(sub.to_dict(orient="records"),
                                out_dir / name / f"{npc}.json")
                    fmts.append("json")
                endpoints.append({"endpoint": f"{name}/{npc}", "rows": int(len(sub)),
                                  "formats": fmts,
                                  "description": f"{PUBLIC_TABLES.get(name, name)} - {npc} only"})

        # convenient sub-endpoints (small JSON slices)
        if name == "mes_qualification" and "edition" in df.columns:
            (out_dir / "mes").mkdir(exist_ok=True)
            for ed, g in df.groupby("edition"):
                _write_json(g.to_dict(orient="records"), out_dir / "mes" / f"{ed}.json")
                endpoints.append({"endpoint": f"mes/{ed}", "rows": int(len(g)),
                                  "formats": ["json"],
                                  "description": f"MES qualification list - {ed}"})
        if name == "records" and "record_type" in df.columns:
            (out_dir / "records").mkdir(exist_ok=True)
            for rt, g in df.groupby("record_type"):
                safe = str(rt).replace("/", "-")
                _write_json(g.to_dict(orient="records"), out_dir / "records" / f"{safe}.json")
                endpoints.append({"endpoint": f"records/{safe}", "rows": int(len(g)),
                                  "formats": ["json"], "description": f"{rt} records"})

    # --- reference vocabularies (code lists) --------------------------------
    try:
        import ipc_common as ipc
        reference = {
            "ranking_types": ipc.RANKING_TYPES,
            "record_types": ipc.RECORD_TYPES,
            "ranking_years": ipc.RANKING_LISTS,
            "mes_games": ipc.MES_GAMES,
        }
        _write_json(reference, out_dir / "reference.json")
        endpoints.append({"endpoint": "reference", "formats": ["json"],
                          "description": "Code vocabularies: ranking/record types, years, Games editions."})
    except Exception as e:  # noqa: BLE001
        print(f"reference.json skipped: {e}")

    manifest = {
        "name": "World Para Athletics - Team Saudi open data API",
        "description": "Read-only static API of publicly-available Para Athletics "
                       "competition data. Non-sensitive tables only; no athlete PII.",
        "updated": _now(),
        "license": "IPC/WPA source data, re-published for analysis. Not for commercial use.",
        "endpoints": endpoints,
    }
    _write_json(manifest, out_dir / "index.json")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")  # serve dirs verbatim
    _write_docs(out_dir, manifest)
    return manifest


def _write_docs(out_dir: Path, manifest: dict) -> None:
    rows = "".join(
        f"<tr><td><a href='{e['endpoint']}.{e['formats'][0]}'>{e['endpoint']}</a></td>"
        f"<td>{e.get('rows','')}</td><td>{', '.join(e['formats'])}</td>"
        f"<td>{e.get('description','')}</td></tr>"
        for e in manifest["endpoints"])
    html = f"""<!doctype html><html><head><meta charset="utf-8">
    <title>{manifest['name']}</title><style>
    body{{font-family:Inter,Segoe UI,sans-serif;max-width:1000px;margin:2rem auto;
        padding:0 1rem;color:#18342a}}
    h1{{color:#235036}} th{{background:#235036;color:#fff;text-align:left;padding:.4rem .6rem}}
    td{{border-bottom:1px solid #e7ece9;padding:.35rem .6rem;font-size:.88rem}}
    code{{background:#f4f7f5;padding:.1rem .3rem;border-radius:4px}}
    </style></head><body>
    <h1>{manifest['name']}</h1>
    <p>{manifest['description']}</p>
    <p>Updated <b>{manifest['updated']}</b>. Machine manifest:
       <a href="index.json">index.json</a>.</p>
    <p>Fetch an endpoint by appending its path + format, e.g.
       <code>./mes/nagoya_2026.json</code> or <code>./rankings.csv</code>.</p>
    <table><thead><tr><th>endpoint</th><th>rows</th><th>formats</th>
    <th>description</th></tr></thead><tbody>{rows}</tbody></table>
    <p style="color:#789;font-size:.8rem;margin-top:1.5rem">Source: IPC SDMS /
    World Para Athletics. Non-sensitive competition data only - no athlete
    dates of birth, classification review status, or internal analysis.</p>
    </body></html>"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the public static data API")
    ap.add_argument("--out", default="public_api", help="output directory")
    args = ap.parse_args()
    manifest = build(Path(args.out))
    print(f"\nAPI built: {len(manifest['endpoints'])} endpoints -> {Path(args.out).resolve()}")
    print(f"Open {Path(args.out) / 'index.html'} to browse.")


if __name__ == "__main__":
    main()
