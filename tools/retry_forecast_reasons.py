"""Bounded reason-only retry for already registered forecast notifications.

Runs inside existing Reconcile/Nightly jobs. It never reprocesses an event,
modifies financial metrics, or sends notifications.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.pipeline.db import load_env
from src.events.forecast_extractor import _download_pdf_for_ocr
from src.events.forecast_reason import extract_forecast_reason, require_pdf_reader
from src.events.forecast_source import exchange_pdf_url

VERSION = 'forecast-reason-20261019-v3'
DOC_ID = re.compile(r'1401\d{14}')
UTC = timezone.utc


def _load_config() -> tuple[str, dict]:
    load_env(str(ROOT))
    if not os.environ.get('SUPABASE_SERVICE_ROLE_KEY'):
        # The production deployment keeps secrets in a sibling settings folder.
        # This fallback is only used when the task environment did not inject them.
        settings_candidates = [ROOT.parent / 'tdnet-production' / 'settings' / '.env',
                               Path.home() / 'tdnet-production' / 'settings' / '.env']
        settings = next((path for path in settings_candidates if path.is_file()), None)
        if settings:
            for line in settings.read_text(encoding='utf-8').splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    name, value = line.split('=', 1)
                    os.environ.setdefault(name.strip(), value.strip().strip('"'))
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base or not key:
        raise RuntimeError('forecast_reason_retry_missing_service_role_config')
    return base, {'apikey': key, 'Authorization': 'Bearer ' + key}


def _payload(row: dict) -> dict:
    value = row['raw_payload']
    return json.loads(value) if isinstance(value, str) else value


def _reason(row: dict) -> dict | None:
    value = _payload(row).get('extracted', {}).get('revision_reason')
    return value if isinstance(value, dict) else None


def should_retry(row: dict, *, now: datetime, local_pdf: Path | None) -> bool:
    old = _reason(row)
    if old is None:
        return True
    status = old.get('status')
    if status not in ('source_unavailable', 'extraction_failed', 'unavailable'):
        return False
    if old.get('review_result') or str(old.get('method', '')).startswith('reviewed_'):
        return False
    if status == 'extraction_failed' and old.get('retry_version') == VERSION:
        if not local_pdf or not local_pdf.is_file():
            return False
        if local_pdf and local_pdf.is_file() and old.get('pdf_sha256') == hashlib.sha256(local_pdf.read_bytes()).hexdigest():
            return False
    last = old.get('last_retry_at')
    if status == 'source_unavailable' and last and (not local_pdf or not local_pdf.is_file()):
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=1):
                return False
        except (ValueError, TypeError):
            pass
    return True


def source_candidates(source_url: str, ticker: str) -> list[str]:
    # The old feed embeds a TDNET URL inside a yanoshin redirect URL.
    match = re.search(r'https://www\.release\.tdnet\.info/inbs/1401\d{14}\.pdf', source_url)
    direct = match.group() if match else source_url
    urls = [direct]
    mirror = exchange_pdf_url(direct, ticker)
    if mirror:
        urls.append(mirror)
    return urls


def _read(base: str, headers: dict, params: dict) -> list[dict]:
    url = base + '/rest/v1/tdnet_events?' + urlencode(params)
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def _patch_reason(base: str, headers: dict, before: dict, reason: dict) -> None:
    fresh = _read(base, headers, {'select': '*', 'id': 'eq.' + before['id'], 'limit': '2'})
    if len(fresh) != 1 or fresh[0] != before:
        raise RuntimeError('forecast_reason_retry_concurrent_change:' + before['id'])
    payload = _payload(before)
    assert before['event_type'] == 'forecast' and isinstance(payload.get('extracted'), dict)
    payload['extracted']['revision_reason'] = reason
    replacement = json.dumps(payload, ensure_ascii=False) if isinstance(before['raw_payload'], str) else payload
    params = {'id': 'eq.' + before['id'], 'dedupe_key': 'eq.' + before['dedupe_key'],
              'raw_payload': 'eq.' + json.dumps(before['raw_payload'], ensure_ascii=False, separators=(',', ':'))}
    url = base + '/rest/v1/tdnet_events?' + urlencode(params)
    request = Request(url, data=json.dumps({'raw_payload': replacement}, ensure_ascii=False).encode(),
                      method='PATCH', headers={**headers, 'Content-Type': 'application/json',
                                               'Prefer': 'return=representation'})
    with urlopen(request, timeout=30) as response:
        updated = json.load(response)
    if len(updated) != 1:
        raise RuntimeError('forecast_reason_retry_cas_failed:' + before['id'])


def run(*, apply: bool = False, days: int = 14, limit: int = 250) -> dict:
    require_pdf_reader()
    base, headers = _load_config()
    now = datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat()
    rows = _read(base, headers, {'select': 'id,ticker,headline,source_url,dedupe_key,raw_payload,disclosed_at,event_type',
                                 'event_type': 'eq.forecast', 'disclosed_at': 'gte.' + since,
                                 'order': 'disclosed_at.desc', 'limit': str(limit)})
    counts = {'scanned': len(rows), 'retryable': 0, 'recovered': 0,
              'source_unavailable': 0, 'extraction_failed': 0, 'skipped': 0}
    for row in rows:
        source = row.get('source_url') or ''
        match = DOC_ID.search(source)
        local = ROOT / 'data' / 'docs' / (match.group() + '.pdf') if match else None
        if not should_retry(row, now=now, local_pdf=local):
            counts['skipped'] += 1
            continue
        counts['retryable'] += 1
        old = _reason(row)
        pdf_path = str(local) if local and local.is_file() else ''
        chosen_url = source
        temporary = False
        if not pdf_path:
            for url in source_candidates(source, row.get('ticker') or ''):
                pdf_path = _download_pdf_for_ocr(url)
                if pdf_path:
                    chosen_url = url
                    temporary = True
                    break
        try:
            if old and old.get('pdf_sha256') and pdf_path:
                current_sha = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
                if current_sha != old['pdf_sha256']:
                    print('SOURCE_MISMATCH', row['id'])
                    counts['skipped'] += 1
                    continue
            result = extract_forecast_reason(pdf_path, chosen_url, match.group() if match else '', row.get('headline') or '')
            if chosen_url != source:
                result['original_tdnet_url'] = source
            result['retry_version'] = VERSION
            result['last_retry_at'] = now.isoformat()
            if result['status'] == 'source_unavailable':
                counts['source_unavailable'] += 1
            elif result['status'] == 'extraction_failed':
                counts['extraction_failed'] += 1
            else:
                counts['recovered'] += 1
            print(row['id'], result['status'], result.get('failure_code', ''), len(result['sections']))
            if apply:
                full = _read(base, headers, {'select': '*', 'id': 'eq.' + row['id'], 'limit': '2'})
                if len(full) != 1 or _reason(full[0]) != old:
                    raise RuntimeError('forecast_reason_retry_changed_before_patch:' + row['id'])
                _patch_reason(base, headers, full[0], result)
        finally:
            if temporary:
                try:
                    Path(pdf_path).unlink()
                except OSError:
                    pass
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--limit', type=int, default=250)
    args = parser.parse_args()
    if not 1 <= args.days <= 30 or not 1 <= args.limit <= 1000:
        parser.error('days or limit out of range')
    run(apply=args.apply, days=args.days, limit=args.limit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
