#!/usr/bin/env python3
"""PDF テキスト抽出 単体診断ツール

Usage:
    python scripts/diagnose_pdf_extract.py <URL>
    python scripts/diagnose_pdf_extract.py --from-db          # DB の1件目のURLで実行
    python scripts/diagnose_pdf_extract.py --from-db --all    # DB 全件を診断
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# .env 読み込み
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_file = os.path.join(_ROOT, ".env")
if os.path.exists(env_file):
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def diagnose_url(url: str) -> dict:
    """1つのURLに対してPDFダウンロード→テキスト抽出を診断"""
    result = {
        "url": url,
        "http_status": None,
        "content_type": None,
        "byte_size": 0,
        "is_pdf": False,
        "page_count": 0,
        "page_text_lengths": [],
        "total_text_length": 0,
        "text_sample": "",
        "failure_reason": None,
        "success": False,
    }

    import requests
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TDNETDiagBot/1.0)"
        })
        result["http_status"] = resp.status_code
        result["content_type"] = resp.headers.get("Content-Type", "")
        result["byte_size"] = len(resp.content)

        if resp.status_code != 200:
            result["failure_reason"] = f"http_{resp.status_code}"
            return result

        # PDF チェック
        is_pdf = (
            "pdf" in result["content_type"].lower()
            or resp.content[:5] == b"%PDF-"
        )
        result["is_pdf"] = is_pdf
        if not is_pdf:
            result["failure_reason"] = f"non_pdf_response (content_type={result['content_type']})"
            return result

    except Exception as e:
        result["failure_reason"] = f"request_failed: {e}"
        return result

    # pdfplumber で抽出
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            result["page_count"] = len(pdf.pages)
            texts = []
            for i, page in enumerate(pdf.pages[:10]):
                page_text = page.extract_text() or ""
                texts.append(page_text)
                result["page_text_lengths"].append(len(page_text))

            full_text = "\n".join(texts)
            result["total_text_length"] = len(full_text)
            result["text_sample"] = full_text[:500]

            if result["total_text_length"] == 0:
                result["failure_reason"] = "zero_text_all_pages"
            elif all(l == 0 for l in result["page_text_lengths"][:3]):
                result["failure_reason"] = "zero_text_first_pages"
            else:
                result["success"] = True
                result["failure_reason"] = None

    except ImportError:
        result["failure_reason"] = "pdfplumber_not_installed"
    except Exception as e:
        result["failure_reason"] = f"pdf_parse_error: {e}"

    return result


def print_diagnosis(d: dict):
    """診断結果を見やすく表示"""
    success_marker = "✅" if d["success"] else "❌"
    print(f"\n{'='*60}")
    print(f"{success_marker} URL: {d['url'][:80]}...")
    print(f"  HTTP Status:      {d['http_status']}")
    print(f"  Content-Type:     {d['content_type']}")
    print(f"  Byte Size:        {d['byte_size']:,}")
    print(f"  Is PDF:           {d['is_pdf']}")
    print(f"  Page Count:       {d['page_count']}")
    print(f"  Page Text Lengths:{d['page_text_lengths']}")
    print(f"  Total Text Len:   {d['total_text_length']:,}")
    if d["failure_reason"]:
        print(f"  ❌ Failure Reason: {d['failure_reason']}")
    if d["text_sample"]:
        print(f"  Text Sample (first 300 chars):")
        print(f"  {'─'*50}")
        for line in d["text_sample"][:300].split("\n")[:8]:
            print(f"    {line}")
        print(f"  {'─'*50}")


def get_urls_from_fetcher(limit: int = 3) -> list[dict]:
    """fetcher を使って今日の doc_url を取得"""
    sys.path.insert(0, _ROOT)
    from src.fetcher import fetch_new_disclosures
    items = fetch_new_disclosures()
    urls = []
    for item in items[:limit]:
        urls.append({
            "url": item.doc_url,
            "ticker": item.ticker,
            "title": item.title[:60],
        })
    return urls


def get_urls_from_tdnet_html(target_date: str = None, limit: int = 5) -> list[dict]:
    """TDnet HTML から直接 doc_url を取得（フィルタなし）"""
    import requests
    from datetime import date

    if target_date is None:
        date_str = date.today().strftime("%Y%m%d")
    else:
        date_str = target_date.replace("-", "")

    url = f"https://www.release.tdnet.info/inbs/I_list_001_{date_str}.html"
    print(f"[DIAG] Fetching TDnet page: {url}")

    resp = requests.get(url, timeout=15, headers={"User-Agent": "TDnetDiag/1.0"})
    resp.encoding = "utf-8"

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        title = tds[3].get_text(strip=True)
        ticker_raw = tds[1].get_text(strip=True)
        ticker = ticker_raw.rstrip("0") if len(ticker_raw) == 5 and ticker_raw.endswith("0") else ticker_raw

        link_tag = tds[3].find("a") or tr.find("a")
        href = link_tag.get("href", "") if link_tag else ""
        if href and not href.startswith("http"):
            href = f"https://www.release.tdnet.info/inbs/{href}"

        # 業績予想修正関連のみ
        if href and ("修正" in title or "差異" in title) and "業績" in title:
            results.append({"url": href, "ticker": ticker, "title": title[:60]})
            if len(results) >= limit:
                break

    return results


def main():
    parser = argparse.ArgumentParser(description="PDF テキスト抽出 単体診断")
    parser.add_argument("url", nargs="?", default=None, help="診断する PDF URL")
    parser.add_argument("--date", type=str, default=None, help="対象日付 (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=3, help="診断件数上限")
    args = parser.parse_args()

    if args.url:
        d = diagnose_url(args.url)
        print_diagnosis(d)
    else:
        # TDnet HTML から予想修正の doc_url を取得して診断
        urls = get_urls_from_tdnet_html(target_date=args.date, limit=args.limit)
        if not urls:
            print("[DIAG] 業績予想修正の開示が見つかりません")
            # 直近の開示を数件試す
            print("[DIAG] フィルタなしで最初の3件を試行...")
            urls = get_urls_from_tdnet_html_nofilter(target_date=args.date, limit=3)

        print(f"\n[DIAG] {len(urls)} 件の URL を診断します")
        all_results = []
        for info in urls:
            print(f"\n[DIAG] {info['ticker']} {info['title']}")
            d = diagnose_url(info["url"])
            d["ticker"] = info["ticker"]
            d["title"] = info["title"]
            print_diagnosis(d)
            all_results.append(d)

        # サマリ
        print(f"\n{'='*60}")
        print(f"SUMMARY: {len(all_results)} URLs diagnosed")
        success = sum(1 for d in all_results if d["success"])
        print(f"  Success: {success}/{len(all_results)}")
        failures = [d for d in all_results if not d["success"]]
        if failures:
            reasons = {}
            for d in failures:
                r = d.get("failure_reason", "unknown")
                reasons[r] = reasons.get(r, 0) + 1
            print(f"  Failure reasons: {reasons}")


def get_urls_from_tdnet_html_nofilter(target_date: str = None, limit: int = 3) -> list[dict]:
    """TDnet HTML から全 doc_url を取得"""
    import requests
    from datetime import date

    if target_date is None:
        date_str = date.today().strftime("%Y%m%d")
    else:
        date_str = target_date.replace("-", "")

    url = f"https://www.release.tdnet.info/inbs/I_list_001_{date_str}.html"
    resp = requests.get(url, timeout=15, headers={"User-Agent": "TDnetDiag/1.0"})
    resp.encoding = "utf-8"

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        title = tds[3].get_text(strip=True)
        ticker_raw = tds[1].get_text(strip=True)
        link_tag = tds[3].find("a") or tr.find("a")
        href = link_tag.get("href", "") if link_tag else ""
        if href and not href.startswith("http"):
            href = f"https://www.release.tdnet.info/inbs/{href}"
        if href:
            results.append({"url": href, "ticker": ticker_raw, "title": title[:60]})
            if len(results) >= limit:
                break
    return results


if __name__ == "__main__":
    main()
