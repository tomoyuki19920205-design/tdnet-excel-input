"""Verbatim, document-scoped forecast reasons. No summarization or generation."""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path


def _key(value: str) -> str:
    # Recognition only; never normalize the saved wording.
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", "", "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf"))


_LOG = logging.getLogger(__name__)


class ForecastReasonEnvironmentError(RuntimeError):
    """The scheduled Python cannot read PDF files for forecast reasons."""


def require_pdf_reader():
    """Fail the job visibly when its PDF dependency is missing or broken."""
    try:
        import pymupdf
        return pymupdf
    except (ImportError, OSError) as exc:
        raise ForecastReasonEnvironmentError(
            "forecast_reason_pdf_reader_unavailable: install requirements.txt "
            "into the scheduled task's Python environment"
        ) from exc
_MAJOR = re.compile(r"^(?:\d+[.、](?=\D|\d{4}年)|[IVX]+(?:[.、]|(?=連結|個別|業績)))")
_SUB = re.compile(r"^\(\d+\)")
_END = re.compile(r"^(?:以上$|[\(\[〔【<]*(?:ご)?注意|[\(\[〔【<]*注(?:\d+|記)?(?:[\)\]〕】>:：]|$)|[\(\[〔【<]*注記事項|※|[\(\[〔【<]?(?:業績|業績の|業績等の)?予想に関する注意事項|[\(\[〔【<]?将来予測に関する注意事項)")
_REFERENCE_HEADING = re.compile(r"^(?:[\(\[〔【<](?:ご)?参考(?:資料|情報)?[\)\]〕】>]|(?:ご)?参考(?:資料|情報)?(?:\(\d+\)|[:：]|$))")
_CAUTION_HEADING = re.compile(r"^(?:[\(\[〔【<]?(?:業績|業績等の)?予想に関する留意事項|[\(\[〔【<]?将来の見通しに関する|[\(\[〔【<]?業績予想に関する注記事項)")


def _is_reason_heading(key: str) -> bool:
    """Recognize a whole heading, not a sentence referring to another reason."""
    key = key.strip(":：")
    for left, right in (('(', ')'), ('[', ']'), ('〔', '〕'), ('【', '】'), ('《', '》'), ('<', '>')):
        # "(2)修正の理由(補足)" begins and ends with parentheses but the
        # first pair is a section number, not a wrapper around the heading.
        if key.startswith(left) and key.endswith(right):
            depth = 0
            closing_index = None
            for index, char in enumerate(key):
                if char == left:
                    depth += 1
                elif char == right:
                    depth -= 1
                    if depth == 0:
                        closing_index = index
                        break
            if closing_index == len(key) - 1:
                key = key[1:-1]
                break
    if re.fullmatch(r"\d+[.、]理由", key):
        return True
    return (len(key) <= 110 and "「" not in key and "」" not in key
            and re.search(r"理由(?:について|等|及び概要|ならびに今後の展開|及び今後の展開|及び.{1,50}について)?(?:\([^)]*\))?$", key)
            and any(term in key for term in ("修正", "差異", "業績予想を未定")))


def _bullet_scope(key: str) -> str | None:
    """Separate bullet-headed forecast and dividend chapters with generic reason headings."""
    if len(key) > 50 or not key.startswith(('●', '■', '◆')):
        return None
    heading = key[1:]
    if not heading.endswith(('修正について', '変更について', '修正', '変更')):
        return None
    if '配当' in heading and '業績' not in heading:
        return 'dividend'
    if '業績' in heading and '配当' not in heading:
        return 'forecast'
    return None


def _section_number(key: str) -> int | None:
    match = re.match(r"^(\d+)[.、]", key)
    return int(match.group(1)) if match else None


def extract_reason_sections(lines: list[dict], *, title: str = "") -> list[dict]:
    """Slice original lines, retaining each period/basis heading independently."""
    sections: list[dict] = []
    context: list[str] = []
    forecast_context: list[str] = []
    dividend_scope = "配当" in _key(title) and "業績" not in _key(title)
    bullet_dividend_scope = False
    current = None
    current_reopened = False
    body: list[dict] = []

    def finish():
        nonlocal current, current_reopened, body
        if current and any(row['text'].strip() for row in body):
            while body and not body[-1]['text'].strip(): body.pop()
            current.update(text='\n'.join(row['text'] for row in body).strip('\n'),
                           page_end=body[-1]['page'], line_end=body[-1]['line'])
            sections.append(current)
        current, current_reopened, body = None, False, []

    for index, row in enumerate(lines):
        text, key = row['text'], _key(row['text'])
        if not key:
            if current and body and body[-1]['text']: body.append(row)
            continue
        scope = _bullet_scope(key)
        if scope:
            finish()
            dividend_scope = scope == 'dividend'
            bullet_dividend_scope = dividend_scope
            context = [text.strip()]
            if not dividend_scope:
                forecast_context = context.copy()
            continue
        # Some difference disclosures label the explanatory paragraph
        # "業績予想との差異について" rather than "理由". A table heading with the
        # same wording is excluded by requiring prose immediately below.
        next_text = ''.join(_key(v['text']) for v in lines[index + 1:index + 3])
        intro_rows = []
        for following in lines[index + 1:index + 13]:
            if not following['text'].strip() and intro_rows:
                break
            if following['text'].strip():
                intro_rows.append(following['text'])
        intro_text = ''.join(_key(value) for value in intro_rows)
        first_intro = _key(intro_rows[0]) if intro_rows else ''
        forecast_intro = ('配当' not in intro_text and '株' not in first_intro
                          and ('業績予想' in first_intro or
                               first_intro.startswith(('売上高', '営業利益', '経常利益', '特別利益'))))
        unheaded_difference = (key.endswith('業績予想との差異について')
                               and len(next_text) >= 50
                               and any(word in next_text for word in ('前回', '公表', '集計', '精査'))
                               and '売上高営業利益経常利益' not in next_text)
        scoped_generic_reason = (bool(re.fullmatch(r'\(\d+\)理由', key)) and not dividend_scope
                                 and any(any(term in _key(h) for term in ('業績', '差異')) for h in context))
        if _is_reason_heading(key) or scoped_generic_reason or unheaded_difference:
            finish()
            prior_dividend_scope = dividend_scope
            if '配当' in key and not any(word in key for word in ('業績', '差異')):
                dividend_scope = True
            elif dividend_scope and not bullet_dividend_scope and (
                    (any(word in key for word in ('業績', '差異')) and '配当' not in key)
                    or (forecast_context and forecast_intro)):
                dividend_scope = False
                context = forecast_context.copy()
            if not dividend_scope:
                current = dict(heading=text.strip(), context=context.copy(),
                               page_start=row['page'], line_start=row['line'])
                current_reopened = prior_dividend_scope
            continue
        major = bool(_MAJOR.match(key))
        # Numbered period/basis paragraphs inside a reason are body text.
        # A later top-level heading or a dividend section ends the reason.
        current_number = _section_number(_key(current['heading'])) if current else None
        next_number = _section_number(key) if major else None
        current_sub_number = re.match(r'^\((\d+)\)', _key(current['heading'])) if current else None
        next_sub_number = re.match(r'^\((\d+)\)', key)
        next_subsection = bool(current_sub_number and next_sub_number
                               and int(next_sub_number.group(1)) > int(current_sub_number.group(1))
                               and 'セグメント' in key and '見通し' in key)
        next_top_level = (next_number is not None and
                          (current['heading'].lstrip().startswith(('(', '（'))
                           or (current_number is not None and next_number > current_number))) if current else False
        roman_top_level = bool(major and re.match(r'^[ⅠⅡⅢⅣIVX]', key)
                               and any(word in key for word in ('連結', '個別', '業績')))
        caution_text = ''.join(_key(v['text']) for v in lines[index:index + 3])
        caution_starts = (key.startswith(('なお', 'また', '本資料', '当資料', '実際', '将来', '業績予想'))
                          or key.startswith(('上記の予想', '上記予想', '上記の業績予想',
                                             '上記業績予想', '上記の通期連結業績予想')))
        caution_paragraph = (caution_starts
                             and any(word in key for word in ('予想', '見通し', '将来', '実際の業績'))
                             and any(word in caution_text for word in ('入手可能', '予想数値と異なる',
                                                                         '実際の業績', '不確実', '合理的である',
                                                                         '責任を負いません')))
        dividend_paragraph = (current and current_reopened and body and body[-1]['text'] == '' and
                              (('株主' in key and '利益還元' in key) or
                               key.lstrip('【〔（([〈《').startswith('配当予想')))
        if current and (_END.match(key) or _REFERENCE_HEADING.match(key) or _CAUTION_HEADING.match(key) or caution_paragraph
                        or dividend_paragraph
                        or ('配当' in key and (major or _SUB.match(key)))
                        or next_top_level or next_subsection or roman_top_level):
            finish()
            if dividend_paragraph:
                dividend_scope = True
        if major:
            if '配当' in key:
                dividend_scope = True
                context = [text.strip()]
            elif any(word in key for word in ('業績', '連結', '個別')):
                dividend_scope = False
                bullet_dividend_scope = False
                # Roman headings identify basis; numbered headings identify period.
                if re.match(r'^[ⅠⅡⅢⅣIVX]', key): context = [text.strip()]
                else: context = [h for h in context if re.match(r'^[ⅠⅡⅢⅣIVX]', _key(h))] + [text.strip()]
                forecast_context = context.copy()
        elif not current and not dividend_scope and ('年' in key and '月期' in key and any(w in key for w in ('予想', '差異'))):
            context = [h for h in context if re.match(r'^[ⅠⅡⅢⅣIVX]', _key(h))] + [text.strip()]
            forecast_context = context.copy()
        elif not current and not dividend_scope and _SUB.match(key) and ('連結' in key or '個別' in key):
            context = [h for h in context if not _SUB.match(_key(h)) or '年' in _key(h)] + [text.strip()]
            forecast_context = context.copy()
        if current: body.append(row)
    finish()
    return sections


def read_pdf_lines(pdf_path: str) -> list[dict]:
    """Read all native PDF pages, preserving physical lines and paragraph gaps."""
    fitz = require_pdf_reader()
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
                if bbox[1] > page.rect.height * .92 and re.fullmatch(
                        r'(?:\d+(?:\s*/\s*\d+)?|[―‐－-]\s*\d+\s*[―‐－-])', text.strip()): continue
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
    except ForecastReasonEnvironmentError:
        _LOG.exception('Forecast reason PDF reader environment error for %s', doc_id)
        raise
    except Exception as exc:
        # Extraction failure must not block the existing numeric notification.
        result['status'] = 'unavailable'
        result['failure_code'] = 'pdf_parse_failed'
        _LOG.warning('Forecast reason extraction failed for %s: %s', doc_id, exc)
    return result
