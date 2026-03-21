#!/usr/bin/env python3
"""Show what classify_special_row is matching"""
import sys, os, glob, zipfile, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from bs4 import BeautifulSoup
from src.segment.xbrl_segment_extractor import (
    _find_segment_files, _extract_segment_member,
    ALL_SALES_TAGS, ALL_PROFIT_TAGS,
    _COMPANY_SALES_SUFFIXES, _COMPANY_PROFIT_SUFFIXES,
    _parse_ixbrl_number, _detect_unit_from_html, _to_million_yen, _camel_to_readable,
)
from src.segment.normalize import normalize_segment_name, classify_special_row

zps = glob.glob("data/docs/**/*.zip", recursive=True)
raw = Path(zps[0]).read_bytes()
zf = zipfile.ZipFile(io.BytesIO(raw))
seg_files = _find_segment_files(zf)
content = zf.read(seg_files[0]).decode("utf-8", errors="replace")
unit = _detect_unit_from_html(content)
soup = BeautifulSoup(content, "html.parser")

for t in soup.find_all("ix:nonfraction"):
    ctx = t.get("contextref", "")
    if "duration" not in ctx.lower():
        continue
    member = _extract_segment_member(ctx)
    if not member:
        continue
    name_attr = (t.get("name") or "").lower()
    is_sales = name_attr in ALL_SALES_TAGS
    is_profit = name_attr in ALL_PROFIT_TAGS
    if not is_sales and not is_profit:
        local_name = name_attr.split(":")[-1] if ":" in name_attr else name_attr
        if any(local_name.endswith(s) for s in _COMPANY_SALES_SUFFIXES):
            is_sales = True
        elif any(local_name.endswith(s) for s in _COMPANY_PROFIT_SUFFIXES):
            is_profit = True
    if not is_sales and not is_profit:
        continue
    
    readable = _camel_to_readable(member)
    normalized = normalize_segment_name(readable) or readable
    special = classify_special_row(normalized)
    val_text = t.get_text(strip=True)
    val = _parse_ixbrl_number(val_text, t.get("sign"))
    if val is not None:
        val = _to_million_yen(val, unit)
    
    if special:
        print(f"SPECIAL({special}): member='{member}' readable='{readable}' normalized='{normalized}' val={val}")
    else:
        print(f"OK: member='{member}' readable='{readable}' normalized='{normalized}' val={val}")

zf.close()
