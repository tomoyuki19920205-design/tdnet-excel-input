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
_CAUTION_HEADING = re.compile(r"^(?:[\(\[〔【<]?(?:業績|業績等の)?予想に関する留意事項|[\(\[〔【<]?見通しに関する留意事項|[\(\[〔【<]?将来の見通しに関する|[\(\[〔【<]?業績予想に関する注記事項)")
_DIVIDEND_CHAPTER = re.compile(r"^(?:期末配当|配当予想|剰余金の配当)(?:の)?(?:内容|修正(?:について)?|決定(?:について)?|について)$")


def _is_reason_heading(key: str) -> bool:
    """Recognize a whole heading, not a sentence referring to another reason."""
    key = key.strip(":：")
    for left, right in (('(', ')'), ('[', ']'), ('〔', '〕'), ('【', '】'), ('《', '》'), ('〈', '〉'), ('<', '>')):
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
            and re.search(r"理由(?:について|等|及び概要|ならびに今後の展開|及び今後の展開|および今後の対応|に関する補足|及び.{1,50}について)?(?:\([^)]*\))?$", key)
            and any(term in key for term in ("修正", "差異", "業績予想を未定", "業績予想公表")))


def _bullet_scope(key: str) -> str | None:
    """Separate bullet-headed forecast and dividend chapters with generic reason headings."""
    if len(key) > 50 or not key.startswith(('●', '■', '◆', '・')):
        return None
    heading = key[1:]
    if not heading.endswith(('修正について', '変更について', '修正', '変更', '予想について')):
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
    period_context_by_basis: dict[str, list[str]] = {}
    dividend_scope = "配当" in _key(title) and "業績" not in _key(title)
    bullet_dividend_scope = False
    current = None
    current_reopened = False
    body: list[dict] = []
    skip_dividend_note = False

    def finish():
        nonlocal current, current_reopened, body
        if current and any(_key(row['text']) for row in body):
            while body and not _key(body[-1]['text']): body.pop()
            current.update(text='\n'.join(row['text'] for row in body).strip('\n'),
                           page_end=body[-1]['page'], line_end=body[-1]['line'])
            sections.append(current)
        current, current_reopened, body = None, False, []

    for index, row in enumerate(lines):
        text, key = row['text'], _key(row['text'])
        if skip_dividend_note:
            if (not key or _MAJOR.match(key) or _END.match(key)
                    or _REFERENCE_HEADING.match(key) or _CAUTION_HEADING.match(key)
                    or _is_reason_heading(key)):
                skip_dividend_note = False
            else:
                continue
        if not key:
            if current and body and body[-1]['text']: body.append(row)
            continue
        # A dividend aside can sit inside an otherwise continuous forecast
        # reason, followed by more forecast prose (live 6255). Skip only that
        # original paragraph, then continue the same forecast section.
        if current and body and not _key(body[-1]['text']):
            paragraph = []
            for following in lines[index:index + 4]:
                if not _key(following['text']) and paragraph:
                    break
                paragraph.append(_key(following['text']))
            joined = ''.join(paragraph)
            dividend_aside = (key.startswith(('配当予想につきましては', '配当予想について'))
                              or (key.startswith('なお') and '配当予想の変更はありません' in joined))
            if dividend_aside and '受取配当' not in joined:
                skip_dividend_note = True
                continue
        scope = _bullet_scope(key)
        if scope:
            # A forecast bullet inside a reason is a period/basis subsection,
            # not the end of that reason. Live 3766 and 1994 put their full
            # forecast explanations directly below such bullets.
            reason_context = current['context'].copy() if current and scope == 'forecast' else None
            finish()
            dividend_scope = scope == 'dividend'
            bullet_dividend_scope = dividend_scope
            context = [text.strip()]
            if not dividend_scope:
                forecast_context = context.copy()
                if reason_context is not None:
                    current = dict(heading=text.strip(), context=reason_context,
                                   page_start=row['page'], line_start=row['line'])
            continue
        # The next PDF page can start an unnumbered dividend chapter with no
        # blank line or numbered heading (live 7255). Its generic "（理由）"
        # must never reopen a forecast reason or carry its table into the body.
        if _DIVIDEND_CHAPTER.fullmatch(key):
            finish()
            dividend_scope = True
            bullet_dividend_scope = False
            context = [text.strip()]
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
        forecast_metric_intro = (sum(term in intro_text for term in
                                     ('売上高', '営業利益', '経常利益', '事業利益', '税引前利益', '利益予想')) >= 2
                                 and any(term in intro_text for term in ('修正', '増加', '減少', '上回', '下回')))
        forecast_intro = ('配当' not in intro_text
                          and not first_intro.startswith(('株主', '配当', '剰余金', '１株', '1株'))
                          and not re.search(r'1株につき\d+円', intro_text)
                          and ('業績予想' in first_intro or
                               first_intro.startswith(('売上高', '営業利益', '経常利益', '特別利益'))
                               or forecast_metric_intro))
        unheaded_difference = (key.endswith('業績予想との差異について')
                               and len(next_text) >= 50
                               and any(word in next_text for word in ('前回', '公表', '集計', '精査'))
                               and '売上高営業利益経常利益' not in next_text)
        scoped_generic_reason = (bool(re.fullmatch(r'\(\d+\)理由', key)) and not dividend_scope
                                 and any(any(term in _key(h) for term in ('業績', '差異')) for h in context))
        scoped_publication_reason = (bool(re.fullmatch(r'\(\d+\)公表の理由', key)) and not dividend_scope
                                     and any(any(term in _key(h) for term in ('業績', '差異')) for h in context))
        forecast_overview = (bool(re.fullmatch(r'\d+[.、](?:連結)?業績予想の概要(?:について)?', key))
                             and '業績予想' in _key(title) and not dividend_scope)
        forecast_parent = (not dividend_scope and any(
            any(term in _key(h) for term in ('業績', '差異', '運用状況')) for h in context))
        alternate_reason = (forecast_parent and bool(re.fullmatch(
            r'(?:\(理由\)|理由|業績予想の理由|\d+[.、]開示の理由|\[予想との差異要因\]|<?新たに公表することとなった理由>?)', key)))
        if (_is_reason_heading(key) or scoped_generic_reason or scoped_publication_reason
                or unheaded_difference or forecast_overview or alternate_reason):
            finish()
            prior_dividend_scope = dividend_scope
            if '配当' in key and not any(word in key for word in ('業績', '差異')):
                dividend_scope = True
            elif dividend_scope and not bullet_dividend_scope and (
                    any(word in key for word in ('業績', '差異'))
                    or (forecast_context and forecast_intro)):
                dividend_scope = False
                context = forecast_context.copy()
            if not dividend_scope:
                heading_context = context.copy()
                if '連結' in key and '個別' not in key:
                    heading_context = period_context_by_basis.get('連結', heading_context)
                elif '個別' in key and '連結' not in key:
                    heading_context = period_context_by_basis.get('個別', heading_context)
                current = dict(heading=text.strip(), context=heading_context.copy(),
                               page_start=row['page'], line_start=row['line'])
                current_reopened = prior_dividend_scope
            continue
        major = bool(_MAJOR.match(key))
        # Numbered period/basis paragraphs inside a reason are body text.
        # A later top-level heading or a dividend section ends the reason.
        current_number = _section_number(_key(current['heading'])) if current else None
        next_number = _section_number(key) if major else None
        context_number = next((number for heading in reversed(current['context'] if current else [])
                               if (number := _section_number(_key(heading))) is not None), None)
        current_sub_number = re.match(r'^\((\d+)\)', _key(current['heading'])) if current else None
        next_sub_number = re.match(r'^\((\d+)\)', key)
        next_subsection = bool(current_sub_number and next_sub_number
                               and int(next_sub_number.group(1)) > int(current_sub_number.group(1))
                               and (('セグメント' in key and '見通し' in key)
                                    or ('通期' in key and '予想' in key and '理由' not in key)))
        difference_table_after_reason = bool(current and '差異の理由' in _key(current['heading'])
                                             and re.match(r'^\(\d+\)差異の内容', key))
        next_top_level = (next_number is not None and
                          (current['heading'].lstrip().startswith(('(', '（'))
                           or (current_number is not None and next_number > current_number)
                           or (current_number is None and context_number is not None
                               and next_number > context_number))) if current else False
        roman_top_level = bool(major and re.match(r'^[ⅠⅡⅢⅣIVX]', key)
                               and any(word in key for word in ('連結', '個別', '業績')))
        # A blank line ends the current paragraph. Looking past it can treat
        # the last line of a reason as a caution because the *next* paragraph
        # contains "入手可能" (the live 2737 revision did exactly this).
        caution_rows = []
        for following in lines[index:index + 3]:
            if not following['text'].strip():
                break
            caution_rows.append(following['text'])
        caution_text = ''.join(_key(value) for value in caution_rows)
        caution_starts = (key.startswith(('なお', 'また', '本資料', '当資料', '実際', '将来', '業績予想', '本業績予想'))
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
        # Keep the original section intact when forecast prose resumes after
        # an unlabeled dividend aside (the live 3948 and 6255 disclosures).
        later_forecast_prose = False
        following_paragraph = False
        for following in lines[index + 1:index + 13]:
            following_key = _key(following['text'])
            if not following_key:
                following_paragraph = True
                continue
            if _MAJOR.match(following_key) or _END.match(following_key):
                break
            if (following_paragraph and '業績予想' in following_key and '配当' not in following_key and
                    not _CAUTION_HEADING.match(following_key)):
                later_forecast_prose = True
                break
        dividend_paragraph_rows = []
        for following in lines[index:index + 5]:
            if not following['text'].strip() and dividend_paragraph_rows:
                break
            dividend_paragraph_rows.append(_key(following['text']))
        dividend_paragraph_text = ''.join(dividend_paragraph_rows)
        subsidiary_dividend_cause = ('子会社' in dividend_paragraph_text
                                     and '配当' in dividend_paragraph_text
                                     and any(term in dividend_paragraph_text for term in
                                             ('利益が増加', '利益は増加', '純利益が増加', '利益が上回', '利益は上回')))
        dividend_note_starts = key.startswith(('なお、配当予想', 'なお配当予想', 'なお、期末配当',
                                               '配当予想につきまして', '配当予想について'))
        separate_dividend_note = (current and body and (not _key(body[-1]['text']) or dividend_note_starts)
                                  and '配当' in key and '受取配当' not in key
                                  and not subsidiary_dividend_cause
                                  and not later_forecast_prose
                                  and key.startswith(('なお', 'また', '配当', '期末配当')))
        if current and (_END.match(key) or _REFERENCE_HEADING.match(key) or _CAUTION_HEADING.match(key) or caution_paragraph
                        or dividend_paragraph or separate_dividend_note
                        or ('配当' in key and (major or _SUB.match(key)))
                        or next_top_level or next_subsection or roman_top_level
                        or difference_table_after_reason):
            finish()
            if dividend_paragraph:
                dividend_scope = True
        if major:
            if '配当' in key and not ('運用状況' in key and '予想' in key):
                dividend_scope = True
                context = [text.strip()]
            elif any(word in key for word in ('業績', '連結', '個別', '運用状況')):
                dividend_scope = False
                bullet_dividend_scope = False
                # Roman headings identify basis; numbered headings identify period.
                if re.match(r'^[ⅠⅡⅢⅣIVX]', key): context = [text.strip()]
                else: context = [h for h in context if re.match(r'^[ⅠⅡⅢⅣIVX]', _key(h))] + [text.strip()]
                forecast_context = context.copy()
                if '連結' in key and '個別' not in key:
                    period_context_by_basis['連結'] = context.copy()
                elif '個別' in key and '連結' not in key:
                    period_context_by_basis['個別'] = context.copy()
        elif (not current and not dividend_scope and not _END.match(key)
              and '予想期末' not in key and '年' in key and '月期' in key
              and any(w in key for w in ('予想', '差異'))):
            context = [h for h in context if re.match(r'^[ⅠⅡⅢⅣIVX]', _key(h))] + [text.strip()]
            forecast_context = context.copy()
            if re.match(r'^(?:\(?\d+\)?[.、]?|20\d{2}年)', key) and '連結' in key and '個別' not in key:
                period_context_by_basis['連結'] = context.copy()
            elif re.match(r'^(?:\(?\d+\)?[.、]?|20\d{2}年)', key) and '個別' in key and '連結' not in key:
                period_context_by_basis['個別'] = context.copy()
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
                    text = text.replace('\uf09e', '・').replace('\uf06c', '・')
                    if text.strip(): physical.append((line['bbox'], text, spans[0]['size']))
            physical.sort(key=lambda row: (round(row[0][1] / 3), row[0][0]))
            merged = []
            for bbox, text, size in physical:
                page_number = re.fullmatch(
                    r'(?:\d+(?:\s*/\s*\d+)?|[―‐－-]\s*\d+\s*[―‐－-])', text.strip())
                if page_number and (bbox[1] > page.rect.height * .92 or
                                    (bbox[1] > page.rect.height * .90 and
                                     page.rect.width * .35 < (bbox[0] + bbox[2]) / 2 < page.rect.width * .65)):
                    continue
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


def _extract_narrative_before_table(lines: list[dict], title: str) -> list[dict]:
    """Forecast amendment disclosures that put one reason paragraph before the table.

    This fallback intentionally rejects multi-paragraph and correction layouts;
    their boundaries need review rather than a plausible partial quotation.
    """
    if _key(title).startswith(('訂正', '(訂正')):
        return []
    candidates = []
    for index, row in enumerate(lines):
        heading = _key(row['text'])
        if (len(heading) > 110 or '業績予想' not in heading or '修正' not in heading
                or '理由' in heading or '配当' in heading and '業績' not in heading
                or not _MAJOR.match(heading)):
            continue
        first = index + 1
        while first < len(lines) and not _key(lines[first]['text']):
            first += 1
        end = first
        while end < len(lines) and _key(lines[end]['text']):
            if _MAJOR.match(_key(lines[end]['text'])) or _END.match(_key(lines[end]['text'])):
                break
            end += 1
        paragraph = lines[first:end]
        body_key = ''.join(_key(item['text']) for item in paragraph)
        following = ''.join(_key(item['text']) for item in lines[end:end + 35])
        if (not paragraph or len(body_key) < 45 or len(body_key) > 1500
                or not any(term in body_key for term in ('により', 'に伴い', 'ことから'))
                or not any(term in body_key for term in ('修正', '前回予想', '前回発表予想'))
                or not ('売上高' in following and '利益' in following
                        or '前回発表予想' in following)):
            continue
        # A second prose paragraph before the table could continue the reason.
        # Do not publish a truncated first paragraph in that case.
        next_index = end
        while next_index < len(lines) and not _key(lines[next_index]['text']):
            next_index += 1
        next_key = _key(lines[next_index]['text']) if next_index < len(lines) else ''
        if (next_key and len(next_key) >= 25 and not _MAJOR.match(next_key)
                and not _END.match(next_key) and not _CAUTION_HEADING.match(next_key)
                and not next_key.startswith(('なお、配当', 'なお配当', '親会社', '売上高', '事業利益'))
                and '前回発表予想' not in next_key):
            continue
        candidates.append(dict(heading=row['text'].strip(), context=[row['text'].strip()],
                               text='\n'.join(item['text'] for item in paragraph),
                               page_start=paragraph[0]['page'], line_start=paragraph[0]['line'],
                               page_end=paragraph[-1]['page'], line_end=paragraph[-1]['line']))
    return candidates if len(candidates) == 1 else []


def _extract_bullet_difference_prose(lines: list[dict]) -> list[dict]:
    """A forecast-difference bullet followed by prose, without a 理由 label."""
    candidates = []
    for index, row in enumerate(lines):
        heading = _key(row['text'])
        if (not heading.startswith(('・', '●', '■')) or len(heading) > 80
                or '業績予想' not in heading or '差異' not in heading
                or '配当' in heading):
            continue
        body = []
        for following in lines[index + 1:]:
            key = _key(following['text'])
            if key and (_END.match(key) or _MAJOR.match(key)
                        or key.startswith(('・', '●', '■'))):
                break
            body.append(following)
        while body and not _key(body[-1]['text']):
            body.pop()
        prose = ''.join(_key(item['text']) for item in body)
        if (not body or len(prose) < 100 or '配当予想' in prose
                or not any(term in prose for term in ('予想を下回', '予想を上回', '実績値との差異'))):
            continue
        candidates.append(dict(heading=row['text'].strip(), context=[row['text'].strip()],
                               text='\n'.join(item['text'] for item in body),
                               page_start=body[0]['page'], line_start=body[0]['line'],
                               page_end=body[-1]['page'], line_end=body[-1]['line']))
    return candidates if len(candidates) == 1 else []


def _extract_narrative_after_table_before_dividend(lines: list[dict], title: str) -> list[dict]:
    """Read an unheaded reason only when a following dividend chapter bounds it."""
    if '業績' not in _key(title):
        return []
    candidates = []
    for index, row in enumerate(lines):
        heading = _key(row['text'])
        if (not _MAJOR.match(heading) or len(heading) > 120
                or '配当' in heading or '理由' in heading
                or not ('業績予想' in heading or '業績' in heading and '差異' in heading)):
            continue
        next_major = next((j for j in range(index + 1, len(lines))
                           if _MAJOR.match(_key(lines[j]['text']))), None)
        if next_major is None or '配当' not in _key(lines[next_major]['text']):
            continue
        table_end = max((j for j in range(index + 1, next_major)
                         if any(term in _key(lines[j]['text']) for term in
                                ('増減率', '増減額', '前期実績', '前年同期比増減率'))), default=None)
        if table_end is None:
            continue
        start = next((j for j in range(table_end + 1, next_major)
                      if len(_key(lines[j]['text'])) >= 35
                      and not _SUB.match(_key(lines[j]['text']))
                      and not _MAJOR.match(_key(lines[j]['text']))
                      and any(term in _key(lines[j]['text']) for term in
                              ('業績', '売上高', '営業利益', '連結', '好調'))
                      and not re.match(r'^[\(（](?:ご参考|20\d{2}年)', _key(lines[j]['text']))), None)
        if start is None:
            continue
        body = []
        for following in lines[start:next_major]:
            key = _key(following['text'])
            if key and (_END.match(key) or _CAUTION_HEADING.match(key)
                        or re.match(r'^[\(（]?注(?:記|\d)', key)):
                break
            body.append(following)
        while body and not _key(body[-1]['text']):
            body.pop()
        prose = ''.join(_key(item['text']) for item in body)
        if (not body or len(prose) < 70 or len(prose) > 2500
                or not any(term in prose for term in ('により', 'ため', 'ことから', '好調', '上回', '下回', '見通し'))
                or any(term in prose for term in ('増減率(%)', '今回修正予想(B)'))):
            continue
        scoped_heading = next((lines[j]['text'].strip() for j in range(start - 1, table_end, -1)
                               if _SUB.match(_key(lines[j]['text']))
                               and '業績予想' in _key(lines[j]['text'])), None)
        candidates.append(dict(heading=scoped_heading or row['text'].strip(),
                               context=[row['text'].strip()] + ([scoped_heading] if scoped_heading else []),
                               text='\n'.join(item['text'] for item in body),
                               page_start=body[0]['page'], line_start=body[0]['line'],
                               page_end=body[-1]['page'], line_end=body[-1]['line']))
    return candidates if len(candidates) == 1 else []


def _extract_difference_after_table(lines: list[dict], title: str) -> list[dict]:
    """Find prose immediately following each forecast-difference table.

    Keep connected/individual and interim/full-year chapters separate. A
    table without bounded sentence-form prose remains an extraction failure.
    """
    if '差異' not in _key(title) or '訂正' in _key(title):
        return []
    chapters = [(i, row) for i, row in enumerate(lines)
                if _MAJOR.match(_key(row['text'])) and
                '差異' in _key(row['text']) and
                any(t in _key(row['text']) for t in ('業績', '実績値')) and
                len(_key(row['text'])) < 125]
    result = []
    for i, heading in chapters:
        end = next((j for j in range(i+1, len(lines))
                    if _MAJOR.match(_key(lines[j]['text'])) or _END.match(_key(lines[j]['text']))), len(lines))
        region = lines[i+1:end]
        table = next((k for k, row in enumerate(region)
                      if '増減率' in _key(row['text'])), None)
        if table is None:
            continue
        start = next((k for k in range(table+1, len(region))
                      if len(_key(region[k]['text'])) >= 42 and
                      '。' in ''.join(r['text'] for r in region[k:k+5]) and
                      any(term in _key(region[k]['text']) for term in
                          ('業績', '事業', '売上高', '売上収益', '営業利益', '経常利益', '連結', '予想'))
                      and not any(term in _key(region[k]['text']) for term in
                                  ('前期実績', '今回修正予想', '増減額'))), None)
        if start is None:
            continue
        body = region[start:]
        while body and not _key(body[-1]['text']): body.pop()
        # Two-column native text can resume in the middle of a sentence after
        # the table (live 8616: "同じ。）し...").  That is a partial quotation.
        if body:
            prefix = _key(body[0]['text'])[:15]
            close = min((prefix.find(c) for c in ('）', ')') if c in prefix), default=-1)
            opens = [prefix.find(c) for c in ('（', '(') if c in prefix]
            if close >= 0 and (not opens or close < min(opens)):
                continue
        prose = ''.join(_key(row['text']) for row in body)
        if (not body or len(prose) < 80 or len(prose) > 3000 or
                not any(word in prose for word in ('ました', '見込み', 'なりました'))):
            continue
        result.append(dict(heading=heading['text'].strip(), context=[heading['text'].strip()],
                           text='\n'.join(row['text'] for row in body),
                           page_start=body[0]['page'], line_start=body[0]['line'],
                           page_end=body[-1]['page'], line_end=body[-1]['line']))
    if len(result) == 1:
        outlook = next((i for i, row in enumerate(lines)
                        if re.fullmatch(r'\d+[.、]通期業績に与える影響', _key(row['text']))
                        and (row['page'], row['line']) >
                        (result[0]['page_end'], result[0]['line_end'])), None)
        if outlook is not None:
            end = next((j for j in range(outlook+1, len(lines))
                        if _MAJOR.match(_key(lines[j]['text'])) or _END.match(_key(lines[j]['text']))), len(lines))
            body = lines[outlook+1:end]
            while body and not _key(body[-1]['text']): body.pop()
            if body and len(''.join(_key(r['text']) for r in body)) >= 50:
                result.append(dict(heading=lines[outlook]['text'].strip(),
                                   context=[lines[outlook]['text'].strip()],
                                   text='\n'.join(r['text'] for r in body),
                                   page_start=body[0]['page'], line_start=body[0]['line'],
                                   page_end=body[-1]['page'], line_end=body[-1]['line']))
    return result


def _extract_attached_subsidiary_individual_note(lines: list[dict], title: str) -> list[dict]:
    """Parent disclosure attaching subsidiary results with an individual-results note."""
    key = _key(title)
    if '子会社' not in key or '個別業績' not in key or '差異' not in key:
        return []
    for i, row in enumerate(lines):
        if _key(row['text']).lstrip('※') != '業績に関する注記':
            continue
        if not any('個別業績の概要' in _key(x['text']) for x in lines[max(0, i-45):i]):
            continue
        body=[]
        for following in lines[i+1:i+8]:
            following_key=_key(following['text'])
            if following_key.startswith(('※','決算短信は','業績予想の適切な利用')):
                break
            if following_key:
                body.append(following)
        prose=''.join(_key(x['text']) for x in body)
        if (not body or len(prose)>700 or
                not any(t in prose for t in ('利益','売上高','売上収益')) or
                not any(t in prose for t in ('により','ため','ことから'))):
            continue
        return [dict(heading=row['text'].strip(),
                     context=['添付子会社決算短信・個別業績の概要'],
                     text='\n'.join(x['text'] for x in body),
                     page_start=body[0]['page'], line_start=body[0]['line'],
                     page_end=body[-1]['page'], line_end=body[-1]['line'])]
    return []


def _correction_sections(lines: list[dict], title: str) -> tuple[list[dict], str] | None:
    """Use each corrected reason location, never the document's final 訂正後.

    A complete corrected republication takes precedence over change snippets.
    Snippets are accepted only when their numbered correction label explicitly
    names a forecast/difference reason. Other corrected financial tables remain
    outside the reason field.
    """
    if not any(word in _key(title) for word in ('訂正', '再訂正')):
        return None
    complete = next((i for i, row in enumerate(lines)
                     if '訂正後全文' in _key(row['text'])), None)
    if complete is not None:
        return extract_reason_sections(lines[complete + 1:], title=title), 'native_pdf_corrected_fulltext_v2'
    markers = [i for i, row in enumerate(lines)
               if re.fullmatch(r'[\[【(（]訂正後[\]】)）]', _key(row['text']))]
    if not markers:
        return None
    sections = []
    label = ''
    active_label = ''
    active = []

    def finish():
        nonlocal active
        while active and not _key(active[-1]['text']):
            active.pop()
        if active and active_label:
            sections.append(dict(heading=active_label, context=[active_label],
                                 text='\n'.join(row['text'] for row in active).strip('\n'),
                                 page_start=active[0]['page'], line_start=active[0]['line'],
                                 page_end=active[-1]['page'], line_end=active[-1]['line']))
        active = []

    for row in lines:
        key = _key(row['text'])
        # NFKC in _key converts the circled correction numbers to digits.
        numbered_label = bool(re.match(r'^\d+「', key))
        if numbered_label:
            finish()
            label = row['text'].strip() if ('理由' in key and any(w in key for w in ('差異', '業績', 'セグメント'))) else ''
            active_label = ''
            continue
        if re.fullmatch(r'[\[【(（]訂正前[\]】)）]', key):
            finish()
            active_label = ''
            continue
        if re.fullmatch(r'[\[【(（]訂正後[\]】)）]', key):
            finish()
            active_label = label
            continue
        if active_label and not _END.match(key):
            active.append(row)
        elif active_label and _END.match(key):
            finish()
            active_label = ''
    finish()
    if not sections:
        for marker in markers:
            next_boundary = next((i for i in range(marker + 1, len(lines))
                                  if re.fullmatch(r'[\[【(（]訂正(?:前|後)[\]】)）]',
                                                  _key(lines[i]['text']))), len(lines))
            sections.extend(extract_reason_sections(lines[marker + 1:next_boundary], title=title))
        if sections:
            return sections, 'native_pdf_corrected_explicit_sections_v2'
    return sections, 'native_pdf_corrected_locations_v2'


def extract_forecast_reason(pdf_path: str, source_url: str, doc_id: str = '', title: str = '') -> dict:
    result = dict(version=1, status='source_unavailable', source_url=source_url, doc_id=doc_id,
                  method='native_pdf_lines_v1', sections=[])
    if not pdf_path or not Path(pdf_path).is_file():
        result['failure_code'] = 'original_pdf_unavailable'
        return result
    try:
        result['pdf_sha256'] = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
        lines = read_pdf_lines(pdf_path)
        correction = _correction_sections(lines, title)
        if correction is not None:
            result['sections'], result['method'] = correction
        else:
            result['sections'] = extract_reason_sections(lines, title=title)
        if not result['sections'] and correction is None:
            result['sections'] = _extract_narrative_before_table(lines, title)
            if result['sections']:
                result['method'] = 'native_pdf_narrative_before_table_v1'
        if not result['sections'] and correction is None:
            result['sections'] = _extract_bullet_difference_prose(lines)
            if result['sections']:
                result['method'] = 'native_pdf_difference_bullet_v1'
        if not result['sections'] and correction is None:
            result['sections'] = _extract_narrative_after_table_before_dividend(lines, title)
            if result['sections']:
                result['method'] = 'native_pdf_narrative_after_table_v1'
        if not result['sections'] and correction is None:
            result['sections'] = _extract_difference_after_table(lines, title)
            if result['sections']:
                result['method'] = 'native_pdf_difference_after_table_v2'
        if not result['sections'] and correction is None:
            result['sections'] = _extract_attached_subsidiary_individual_note(lines, title)
            if result['sections']:
                result['method'] = 'native_pdf_attached_individual_note_v2'
        if result['sections']:
            # Some PDFs have a corrupt native text layer, or switch to a
            # scanned page in the middle of the reason.  A heading alone is
            # not proof that the quoted body reached its end.
            unreadable = any(section['text'].count('\ufffd') >= 3 for section in result['sections'])
            if not unreadable:
                with require_pdf_reader().open(pdf_path) as pdf:
                    for section in result['sections']:
                        last_page = section['page_end']
                        if last_page >= len(pdf) or section['text'].rstrip().endswith(('。', '．', '.', '！', '!', '？', '?')):
                            continue
                        next_text = pdf[last_page].get_text().strip()
                        if len(next_text) < 30:
                            unreadable = True
                            break
            if unreadable:
                result['status'] = 'extraction_failed'
                result['failure_code'] = 'reason_continues_on_unreadable_page'
                result['sections'] = []
                return result
            for section in result['sections']:
                section['text_sha256'] = hashlib.sha256(section['text'].encode('utf-8')).hexdigest()
            result['status'] = 'available'
        else:
            result['status'] = 'extraction_failed'
            native = ''.join(_key(row['text']) for row in lines)
            result['failure_code'] = ('correction_reason_scope_not_found' if correction is not None else
                                      'native_text_unreadable' if
                                      sum('\u3040' <= ch <= '\u9fff' for ch in native) < 30
                                      else 'reason_scope_not_found')
    except ForecastReasonEnvironmentError:
        _LOG.exception('Forecast reason PDF reader environment error for %s', doc_id)
        raise
    except Exception as exc:
        # Extraction failure must not block the existing numeric notification.
        result['status'] = 'extraction_failed'
        result['failure_code'] = 'pdf_parse_failed'
        _LOG.warning('Forecast reason extraction failed for %s: %s', doc_id, exc)
    return result
