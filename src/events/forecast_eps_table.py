"""EPS comes from the labelled forecast row, never dividends or prior actuals."""
import re
import unicodedata

def forecast_eps_pair(text):
    text = unicodedata.normalize('NFKC', text)
    # The EPS column must be explicitly present before the forecast rows.
    positions = [i for i,c in enumerate(text) if not c.isspace()]
    compact = ''.join(text[i] for i in positions)
    header = re.search(r'1株(?:当たり|当り)(?:(?!配当|前回).){0,80}?純利益', compact)
    if not header:
        return None
    area = text[positions[header.end()-1]+1:]
    prev = re.search(r'前\s*回\s*(?:発\s*表\s*)?予\s*想(?:\s*[(]A[)])?', area)
    if not prev:
        return None
    after = area[prev.end():]
    rev = re.search(r'今\s*回\s*(?:修\s*正\s*|発\s*表\s*)?予\s*想(?:\s*[(]B[)])?', after)
    if not rev:
        return None
    end = re.search(r'増\s*減|修正の理由|配当|参考', after[rev.end():])
    if not end:
        return None
    def numbers(s):
        return [float(x.replace(',', '')) for x in re.findall(r'(?<![\d.])\d[\d,]*\.\d{2}(?!\d)', s)]
    a = numbers(after[:rev.start()])
    b = numbers(after[rev.end():rev.end()+end.start()])
    if len(a) == len(b) == 1 and max(a[0],b[0]) < 10000:
        return a[0], b[0]
    return None
