"""
ipc_common.py - Shared foundation for the IPC SDMS scrapers (rebuild).
=======================================================================
The IPC SDMS site (https://www.ipc-services.org/sdms) moved to a clean,
deterministic URL scheme. Every dataset is a plain GET (with a browser
User-Agent) and every page exposes a structured export by swapping the
format segment:  html -> xml | excel | pdf.

    Rankings  /sdms/web/rankings/ath/{fmt}/type/{T}/list/{ID}/location/{loc}
    Records   /sdms/web/records/ath/{fmt}/type/{T}
    CML       /sdms/web/cml/ath/{fmt}/season/{S}
    MASH      /sdms/public/mash/ath/{fmt}/season/{S}

This replaces the old Playwright stack (scrape_rankings.py / scrape_records6.py)
which clicked dropdowns against the dead /ranking/at and /record/at paths.

This module owns:
  - the HTTP session (UA header + retry/backoff)
  - the code vocabularies (year->list-id, ranking types, record types, seasons)
  - the XML parsers for rankings / records / CML / MASH feeds
  - CSV writing (UTF-8, matches convert_csv_to_parquet.py)

No browser, no JavaScript. The website is the golden source of truth.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

BASE = "https://www.ipc-services.org/sdms"

# The server returns 403 to non-browser agents. A normal Chrome UA is enough;
# no cookies, no JS rendering required (pages are server-rendered).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

logger = logging.getLogger("ipc")


def make_session() -> requests.Session:
    """A requests Session pre-loaded with the browser UA header."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    return s


def fetch(session: requests.Session, url: str, *, retries: int = 4,
          timeout: int = 90) -> str:
    """GET a URL with exponential backoff. Returns response text."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            # SDMS XML/HTML is UTF-8; let requests honour the header but
            # fall back to UTF-8 if the charset is missing.
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = "utf-8"
            return r.text
        except Exception as e:  # noqa: BLE001 - we want to retry on anything
            last_err = e
            wait = 2 ** attempt
            logger.warning("fetch failed (%d/%d) %s -> %s; retry in %ds",
                           attempt, retries, url, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"fetch failed after {retries} attempts: {url}") from last_err


def fetch_xml(session: requests.Session, url: str, **kw) -> ET.Element:
    """GET a URL and parse the body as XML, returning the root element."""
    text = fetch(session, url, **kw)
    # ET.fromstring chokes on a leading BOM / whitespace occasionally.
    return ET.fromstring(text.lstrip("﻿").strip())


# --------------------------------------------------------------------------
# URL builders
# --------------------------------------------------------------------------

def rankings_url(rtype: str, list_id: int, location: str = "outdoor",
                 fmt: str = "xml") -> str:
    return f"{BASE}/web/rankings/ath/{fmt}/type/{rtype}/list/{list_id}/location/{location}"


def records_url(rtype: str, fmt: str = "xml") -> str:
    return f"{BASE}/web/records/ath/{fmt}/type/{rtype.lower()}"


def cml_url(season: str, fmt: str = "xml") -> str:
    return f"{BASE}/web/cml/ath/{fmt}/season/{season.lower()}"


def mash_url(season: str, fmt: str = "xml") -> str:
    return f"{BASE}/public/mash/ath/{fmt}/season/{season.lower()}"


# --------------------------------------------------------------------------
# Code vocabularies (scraped from the live index pages, 2026-06-13)
# --------------------------------------------------------------------------

# Ranking list IDs map to a calendar year. World + all regional types share
# the same list id for a given year.
RANKING_LISTS: dict[int, int] = {
    2026: 1155, 2025: 1091, 2024: 1058, 2023: 996, 2022: 936, 2021: 857,
    2020: 748, 2019: 653, 2018: 549, 2017: 479, 2016: 382, 2015: 282,
    2014: 177, 2013: 128, 2012: 87, 2011: 11, 2010: 10, 2009: 12,
}

RANKING_TYPES: dict[str, str] = {
    "world": "World Rankings",
    "afr": "African Rankings",
    "amr": "Americas Rankings",
    "asr": "Asian Rankings",
    "eur": "European Rankings",
    "ocr": "Oceania Rankings",
    "annual-bst": "Annual Recorded Best Performances",
    "qualify": "Minimum Entry Standard Rankings",
}

RANKING_LOCATIONS = ("outdoor", "indoor")

# Minimum Entry Standard (qualification) ranking lists are keyed by Games
# edition, not year. IDs discovered from the qualify ranking-list dropdown.
MES_GAMES: dict[str, int] = {
    "nagoya_2026": 1151,     # Aichi Nagoya 2026 Asian Para Games
    "glasgow_2026": 1149,    # Glasgow 2026 Commonwealth Games
}

RECORD_TYPES: dict[str, str] = {
    "WR": "World Record",
    "PR": "Paralympic Record",
    "CR": "Championship Record",
    "AFR": "African Record",
    "AMR": "Americas Record",
    "ASR": "Asian Record",
    "EUR": "European Record",
    "OCR": "Oceanian Record",
    "APG": "Asian Para Games Record",
    "ECR": "European Championship Record",
    "PAG": "Parapan American Games Record",
}

# Master-list seasons. Add new codes here as the IPC publishes them.
SEASONS: tuple[str, ...] = ("S26", "S25")

KSA = "KSA"  # Saudi Arabia NPC code


# --------------------------------------------------------------------------
# XML parsers
# --------------------------------------------------------------------------
# The SDMS feeds carry no XML namespace, so plain tag names work with
# ElementTree. Each parser flattens its feed to one row per result/athlete.

def _athletes_from_competitor(competitor: ET.Element) -> list[dict]:
    """Flatten a <Competitor>/<Composition> block into per-athlete dicts.

    Individual events have one athlete; relays have several. Returns one
    dict per athlete so relay legs are preserved.
    """
    rows = []
    for ath in competitor.findall("./Composition/Athlete"):
        desc = ath.find("Description")
        d = desc.attrib if desc is not None else {}
        rows.append({
            "athlete_code": ath.get("Code", ""),
            "athlete_order": ath.get("Order", ""),
            "given_name": d.get("GivenName", ""),
            "family_name": d.get("FamilyName", ""),
            "gender": d.get("Gender", ""),
            "npc": d.get("Organisation", competitor.get("Organisation", "")),
            "birth_year": d.get("BirthYear", ""),
            "class": d.get("Class", ""),
        })
    if not rows:  # competitor with no composition (team-only entry)
        rows.append({
            "athlete_code": competitor.get("Code", ""),
            "athlete_order": "", "given_name": "", "family_name": "",
            "gender": "", "npc": competitor.get("Organisation", ""),
            "birth_year": "", "class": "",
        })
    return rows


def parse_rankings_xml(root: ET.Element) -> pd.DataFrame:
    """Flatten a rankings XML feed (DT type 'Official Rankings').

    Structure: Rankings(event) -> Ranking(entry) -> Competitor -> Athlete.
    Wind speed is carried as an <ExtRank Code='WIND'>.
    """
    rows: list[dict] = []
    for event in root.iter("Rankings"):
        event_code = event.get("Code", "")
        event_desc = event.get("Description", "")
        # Event-level class label (e.g. "T11")
        cls_label = ""
        for ei in event.findall("./ExtendedInfos/ExtendedInfo"):
            if ei.get("Type") == "CLASSES" and ei.get("Code") == "LABEL":
                cls_label = ei.get("Value", "")
        for rank in event.findall("Ranking"):
            wind = ""
            country_name = ""
            for ext in rank.findall("ExtRank"):
                if ext.get("Code") == "WIND":
                    wind = ext.get("Value", "")
                elif ext.get("Code") == "COUNTRY":
                    country_name = ext.get("Value", "")
            base = {
                "event_code": event_code,
                "event": event_desc,
                "event_class": cls_label,
                "rank": rank.get("Rank", ""),
                "performance": rank.get("Value", ""),
                "value_type": rank.get("ValueType", ""),
                "wind": wind,
                "date": rank.get("Date", ""),
                "competition": rank.get("Competition", "").strip(),
                "place": rank.get("Place", ""),
                "country": rank.get("Country", ""),
                "country_name": country_name,
            }
            for comp in rank.findall("Competitor"):
                for ath in _athletes_from_competitor(comp):
                    rows.append({**base, **ath})
            if not rank.findall("Competitor"):
                rows.append(base)
    return pd.DataFrame(rows)


def parse_records_xml(root: ET.Element) -> pd.DataFrame:
    """Flatten a records XML feed (DT_RECORD).

    Structure: Record(event) -> RecordType -> RecordData(entry)
               -> Competitor -> Athlete.
    """
    rows: list[dict] = []
    for record in root.iter("Record"):
        event_code = record.get("Code", "")
        desc = record.find("Description")
        event_name = desc.get("Name", "") if desc is not None else ""
        for rtype in record.findall("RecordType"):
            record_type = rtype.get("RecordType", "")
            shared = rtype.get("Shared", "")
            for data in rtype.findall("RecordData"):
                wind = ""
                for ext in data.findall("Extension"):
                    if ext.get("Code") == "WIND":
                        wind = ext.get("Value", "")
                base = {
                    "event_code": event_code,
                    "event": event_name,
                    "record_type": record_type,
                    "shared": shared,
                    "result_type": data.get("ResultType", ""),
                    "performance": data.get("Result", ""),
                    "wind": wind,
                    "historical": data.get("Historical", ""),
                    "current": data.get("Current", ""),
                    "date": data.get("Date", ""),
                    "country": data.get("Country", ""),
                    "country_name": data.get("CountryName", ""),
                    "place": data.get("Place", ""),
                    "competition": data.get("Competition", "").strip(),
                }
                comps = data.findall("Competitor")
                for comp in comps:
                    for ath in _athletes_from_competitor(comp):
                        rows.append({**base, **ath})
                if not comps:
                    rows.append(base)
    return pd.DataFrame(rows)


def _classification_columns(athlete: ET.Element) -> dict:
    """Collapse an athlete's <Classification> entries into T/F columns.

    Entries are grouped by Pos; each group has a CLG (T or F), a CLASS
    (e.g. T54 / F32) and a STATUS. Returns t_class/t_status/f_class/f_status.
    """
    groups: dict[str, dict] = {}
    for entry in athlete.findall("./Classification/ClassificationEntry"):
        pos = entry.get("Pos", "1")
        code = entry.get("Code", "")
        val = entry.get("Value", "")
        g = groups.setdefault(pos, {})
        g[code] = val
    out = {"t_class": "", "t_status": "", "f_class": "", "f_status": ""}
    for g in groups.values():
        grp = (g.get("CLG") or "").upper()
        if grp == "T":
            out["t_class"], out["t_status"] = g.get("CLASS", ""), g.get("STATUS", "")
        elif grp == "F":
            out["f_class"], out["f_status"] = g.get("CLASS", ""), g.get("STATUS", "")
    return out


def parse_masterlist_xml(root: ET.Element) -> pd.DataFrame:
    """Flatten a CML or MASH XML feed.

    Structure: Team(NPC) -> Composition -> Athlete -> Classification.
    MASH feeds add MASH / MC attributes on the athlete; they are captured
    when present and left blank for CML.
    """
    rows: list[dict] = []
    for team in root.iter("Team"):
        npc = team.get("Code", "")
        npc_name = team.get("Name", "")
        for ath in team.findall("./Composition/Athlete"):
            row = {
                "sdms_id": ath.get("Code", ""),
                "npc": ath.get("Organisation", npc),
                "npc_name": npc_name,
                "family_name": ath.get("FamilyName", ""),
                "given_name": ath.get("GivenName", ""),
                "gender": ath.get("Gender", ""),
                "birth_year": ath.get("BirthYear", ""),
                "main_function": ath.get("MainFunctionId", ""),
                "current": ath.get("Current", ""),
                # MASH-only attributes (blank for CML)
                "mash": ath.get("MASH", ath.get("Mash", "")),
                "mc": ath.get("MC", ath.get("Mc", "")),
            }
            row.update(_classification_columns(ath))
            rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def save_csv(df: pd.DataFrame, path: str | Path) -> Path:
    """Write a DataFrame as UTF-8 CSV (matches convert_csv_to_parquet.py).

    Note: the legacy cloud_scraper wrote Latin-1, but the parquet converter
    reads UTF-8 - this rebuild standardises on UTF-8 for the IPC feeds.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    logger.info("saved %d rows -> %s", len(df), p)
    return p


def setup_logging(name: str) -> logging.Logger:
    """Console + file logging into ./logs, consistent with cloud_scraper.py."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(name)
