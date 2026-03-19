#!/usr/bin/env python3
"""quarantine PDFのセグメント表周辺テキストを出力する"""
import pdfplumber, os, sys, re

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '.')

from src.extractor import _SEGMENT_HEADER_KW, _SEG_SALES_KW, _SEG_PROFIT_KW

docs = os.path.join('data', 'docs')
out = []

for fname in sorted(os.listdir(docs)):
    if not fname.endswith('.pdf'):
        continue
    path = os.path.join(docs, fname)
    try:
        with pdfplumber.open(path) as pdf:
            text = ''
            for p in pdf.pages[:8]:
                t = p.extract_text()
                if t:
                    text += t + '\n'
    except:
        continue

    found_kw = None
    for kw in _SEGMENT_HEADER_KW:
        if kw in text:
            found_kw = kw
            break

    if not found_kw:
        continue

    lines = text.split('\n')
    start = None
    for i, line in enumerate(lines):
        if found_kw in line:
            start = i
            break

    if start is None:
        continue

    header_text = '\n'.join(lines[start:min(start+10, len(lines))])
    has_sales = any(kw in header_text for kw in _SEG_SALES_KW)
    has_profit = any(kw in header_text for kw in _SEG_PROFIT_KW)

    if not has_sales and not has_profit:
        out.append(f"=== {fname} (kw={found_kw}) ===")
        out.append(f"  has_sales={has_sales}, has_profit={has_profit}")
        for j in range(start, min(start+12, len(lines))):
            out.append(f"  L{j}: {lines[j][:100]}")
        out.append("")

report = '\n'.join(out)
outpath = os.path.join('logs', 'quarantine_debug.txt')
os.makedirs('logs', exist_ok=True)
with open(outpath, 'w', encoding='utf-8') as f:
    f.write(report)
print(f"Saved ({len(out)} lines)")
