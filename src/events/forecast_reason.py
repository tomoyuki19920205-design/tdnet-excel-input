"""Verbatim, document-scoped forecast reasons. No summarization or generation."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path


def _key(value: str) -> str:
    # Recognition only; never normalize the saved wording.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


_REASON = re.compile(r"^(?:(?:\d+[.、]|\(\d+\))|[()])*"
                     r"(?:(?:連結|個別)?業績予想(?:の)?)?"
                     r"(?:修正(?:の)?理由|差異(?:が生じた|の)理由)[)]?[:：]?$" )
_MAJOR = re.compile(r"^(?:\d+[.、](?=\D|\d{4}年)|[IVX]+(?:[.、]|(?=連結|個別|業績)))")
_SUB = re.compile(r"^\(\d+\)")
_END = re.compile(r"^(?:以上$|\(?注\)?|※|ご参考|\(?参考\)?)")


def extract_reason_sections(lines: list[dict], *, title: str = "") -> list[dict]:
    """Slice original lines, retaining each period/basis heading independently."""
    sections: list[dict] = []
    context: list[str] = []
    dividend_scope = "配当" in _key(title) and "業績" not in _key(title)
    current = None
    body: list[dict] = []

    def finish():
        nonlocal current, body
        if current and any(row['text'].strip() for row in body):
            while body and not body[-1]['text'].strip(): body.pop()
            current.update(text='\n'.join(row['text'] for row in body).strip('\n'),
                           page_end=body[-1]['page'], line_end=body[-1]['line'])
            sections.append(current)
        current, body = None, []

    for row in lines:
        text, key = row['text'], _key(row['text'])
        if not key:
            if current and body and body[-1]['text']: body.append(row)
            continue
        reason = _REASON.fullmatch(key)
        if reason:
            finish()
            if not dividend_scope:
                current = dict(heading=text.strip(), context=context.copy(),
                               page_start=row['page'], line_start=row['line'])
            continue
        major = bool(_MAJOR.match(key))
        if current and (major or _SUB.match(key) or _END.match(key)):
            finish()
        if major:
            if '配当' in key:
                dividend_scope = True
                context = [text.strip()]
            elif any(word in key for word in ('業績', '連結', '個別')):
                dividend_scope = False
                # Roman headings identify basis; numbered headings identify period.
                if re.match(r'^[ⅠⅡⅢⅣIVX]', key): context = [text.strip()]
                else: context = [h for h in context if re.match(r'^[ⅠⅡⅢⅣIVX]', _key(h))] + [text.strip()]
        elif not current and ('年' in key and '月期' in key and any(w in key for w in ('予想', '差異'))):
            context = [h for h in context if re.match(r'^[ⅠⅡⅢⅣIVX]', _key(h))] + [text.strip()]
        elif not current and _SUB.match(key) and ('連結' in key or '個別' in key):
            context = [h for h in context if not _SUB.match(_key(h)) or '年' in _key(h)] + [text.strip()]
        if current: body.append(row)
    finish()
    return sections


def read_pdf_lines(pdf_path: str) -> list[dict]:
    """Read all native PDF pages, preserving physical lines and paragraph gaps."""
    import fitz
    rows = []
    with fitz.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf, 1):
            physical = []
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    spans = line['spans']
                    text = ''.join(span['text'] for span in spans).rstrip()
                    # Decode the dot in Wingdings' private-use encoding as a bullet.
                    text = text.replace('\uf09e', '・')
                    if text.strip(): physical.append((line['bbox'], text, spans[0]['size']))
            physical.sort(key=lambda row: (round(row[0][1] / 3), row[0][0]))
            merged = []
            for bbox, text, size in physical:
                if bbox[1] > page.rect.height * .92 and re.fullmatch(r'\d+(?:\s*/\s*\d+)?', text.strip()): continue
                if merged and abs(merged[-1][0][1] - bbox[1]) < 3:
                    old_box, old_text, old_size = merged[-1]
                    merged[-1] = ((old_box[0], old_box[1], bbox[2], max(old_box[3], bbox[3])), old_text + '  ' + text, old_size)
                else: merged.append((bbox, text, size))
            previous = None
            for index, (bbox, text, size) in enumerate(merged, 1):
                paragraph = previous and (bbox[1] - previous[0][1] > size * 2.1 or
                    (previous[1].rstrip().endswith('。') and
                     (bbox[0] - previous[0][0] > size * .5 or text.startswith((' ', '　')))))
                if paragraph and rows and rows[-1]['text']:
                    rows.append(dict(text='', page=page_no, line=index))
                rows.append(dict(text=text, page=page_no, line=index))
                previous = (bbox, text, size)
    return rows


def extract_forecast_reason(pdf_path: str, source_url: str, doc_id: str = '', title: str = '') -> dict:
    result = dict(version=1, status='unavailable', source_url=source_url, doc_id=doc_id,
                  method='native_pdf_lines_v1', sections=[])
    if not pdf_path or not Path(pdf_path).is_file(): return result
    try:
        result['pdf_sha256'] = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
        result['sections'] = extract_reason_sections(read_pdf_lines(pdf_path), title=title)
        if result['sections']: result['status'] = 'available'
    except Exception:
        # Extraction failure must not block the existing numeric notification.
        result['status'] = 'unavailable'
    return result
