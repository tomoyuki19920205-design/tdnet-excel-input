"""Detect durable dividend/shareholder-return policy changes.

The detector is deliberately evidence based: a policy concept must appear
together with an explicit change action.  Merely restating an existing policy,
or explaining a one-off commemorative/special dividend, is not sufficient.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
import unicodedata


@dataclass
class DividendPolicyChange:
    detected: bool = False
    scope: str = ""  # dividend_policy / shareholder_return_policy
    label: str = ""  # 配当方針変更 / 還元方針変更
    action: str = ""  # introduce / change / abolish
    summary: str = ""
    before: str = ""
    after: str = ""
    metrics: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_NO_CHANGE_RE = re.compile(
    r"(?:配当|株主還元|利益還元)(?:の)?方針.{0,24}"
    r"(?:変更(?:は|が)(?:ない|ありません|ございません)|維持|継続)"
)
_POLICY_ACTION_RE = re.compile(
    r"(?:配当|株主還元|利益還元)(?:の)?方針.{0,32}?"
    r"(?:変更|見直し|改定|新設|導入|採用|撤廃|廃止)"
    r"|(?:変更|見直し|改定|新設|導入|採用|撤廃|廃止).{0,24}?"
    r"(?:配当|株主還元|利益還元)(?:の)?方針"
)
_DURABLE_RULE_ACTION_RE = re.compile(
    r"(?:配当性向|DOE|株主資本配当率|累進配当|最低(?:年間)?配当|"
    r"総還元性向|安定配当|継続配当).{0,60}?"
    r"(?:変更|見直し|改定|新設|導入|採用|撤廃|廃止|引き上げ|引き下げ)"
    r"|(?:変更|見直し|改定|新設|導入|採用|撤廃|廃止|引き上げ|引き下げ)"
    r".{0,36}?(?:配当性向|DOE|株主資本配当率|累進配当|最低(?:年間)?配当|"
    r"総還元性向|安定配当|継続配当)"
)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", _normalize(value))


def _sentences(value: str) -> list[str]:
    # PDF extractors insert hard line breaks inside ordinary sentences.  Join
    # them before sentence splitting so "配当方針を\n変更" remains one clause.
    text = re.sub(r"\s+", " ", _normalize(value))
    return [
        part.strip(" 。")
        for part in re.split(r"(?<=[。！？])", text)
        if part.strip(" 。")
    ]


def _bounded(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _focus_evidence(value: str, pattern: str, *, lead: int = 24, limit: int = 240) -> str:
    match = re.search(pattern, value)
    if not match:
        return _bounded(value, limit)
    start = max(0, match.start() - lead)
    return _bounded(value[start:start + limit], limit)


def _action(compact: str) -> str:
    if re.search(r"撤廃|廃止", compact):
        return "abolish"
    if re.search(r"新設|導入|採用|初配", compact):
        return "introduce"
    return "change"


def _percent_metric(text: str, concept: str, kind: str, label: str) -> dict | None:
    repeated = re.search(
        rf"(?:{concept})[^。]{{0,24}}?(\d+(?:\.\d+)?)\s*%[^。]{{0,36}}?"
        rf"(?:変更|見直し|改定|引き上げ|引き下げ)[^。]{{0,36}}?"
        rf"(?:{concept})[^。]{{0,24}}?(\d+(?:\.\d+)?)\s*%",
        text,
        re.IGNORECASE,
    )
    if repeated:
        before_value, revised_value = repeated.groups()
        if float(before_value) == float(revised_value):
            return None
        return {
            "kind": kind, "unit": "%", "action": "change",
            "before": float(before_value), "after": float(revised_value),
            "summary": f"{label}：{before_value}% → {revised_value}%",
        }
    for match in re.finditer(concept, text, re.IGNORECASE):
        window = text[max(0, match.start() - 36): match.end() + 84]
        has_action = re.search(r"変更|見直し|改定|新設|導入|採用|撤廃|廃止|引き上げ|引き下げ", window)
        if not has_action:
            continue
        metric: dict = {"kind": kind, "unit": "%"}
        concept_text = match.group(0)
        after_window = text[match.end(): match.end() + 84]
        direct_change = re.search(
            r"[^。]{0,24}?(\d+(?:\.\d+)?)\s*%[^。]{0,20}?(?:から|→|⇒)[^。]{0,20}?(\d+(?:\.\d+)?)\s*%",
            concept_text + after_window,
        )
        before_concept = re.search(
            r"(\d+(?:\.\d+)?)\s*%[^。]{0,20}?(?:から|→|⇒)[^。]{0,20}?$",
            text[max(0, match.start() - 64):match.start()],
        )
        after_value = re.search(r"(\d+(?:\.\d+)?)\s*%", after_window)
        if direct_change:
            before_value, revised_value = direct_change.groups()
            metric.update(before=float(before_value), after=float(revised_value), action="change")
            metric["summary"] = f"{label}：{before_value}% → {revised_value}%"
        elif before_concept and after_value:
            before_value, revised_value = before_concept.group(1), after_value.group(1)
            metric.update(before=float(before_value), after=float(revised_value), action="change")
            metric["summary"] = f"{label}：{before_value}% → {revised_value}%"
        elif after_value:
            value = after_value.group(1)
            metric.update(after=float(value), action=_action(_compact(window)))
            suffix = "を導入" if metric["action"] == "introduce" else "に変更"
            metric["summary"] = f"{label}{value}%{suffix}"
        else:
            metric.update(action=_action(_compact(window)))
            metric["summary"] = f"{label}を変更"
        return metric
    return None


def _extract_metrics(title: str, text: str) -> list[dict]:
    source = _normalize(f"{title}\n{text}")
    compact = _compact(source)
    metrics: list[dict] = []

    for concept, kind, label in (
        (r"配当性向(?:目標)?", "payout_ratio", "配当性向"),
        (r"(?:DOE|株主資本配当率)", "doe", "DOE"),
        (r"総還元性向", "total_payout_ratio", "総還元性向"),
    ):
        metric = _percent_metric(source, concept, kind, label)
        if metric:
            metrics.append(metric)

    if re.search(r"累進配当", compact) and re.search(r"累進配当.{0,40}(?:導入|採用|変更|撤廃|廃止)|(?:導入|採用|変更|撤廃|廃止).{0,30}累進配当", compact):
        action = _action(compact)
        wording = "累進配当を導入" if action == "introduce" else ("累進配当を撤廃" if action == "abolish" else "累進配当方針を変更")
        metrics.append({"kind": "progressive_dividend", "action": action, "summary": wording})

    minimum = re.search(r"最低(?:年間)?配当(?:額|金)?[^\d]{0,24}(\d+(?:\.\d+)?)\s*円", source)
    if minimum and re.search(r"最低(?:年間)?配当.{0,60}(?:設定|新設|導入|変更|撤廃|廃止)", compact):
        value = float(minimum.group(1))
        action = _action(compact)
        metrics.append({
            "kind": "minimum_dividend", "unit": "円", "after": value,
            "action": action,
            "summary": f"最低年間配当{minimum.group(1)}円を設定" if action != "abolish" else "最低配当額を撤廃",
        })

    # A first dividend is a durable transition from no-dividend policy even
    # when the actual amount remains undecided (the 245A pattern).
    no_dividend_before = re.search(r"これまで.{0,100}(?:配当を実施しておりません|無配)", compact)
    first_dividend = "初配" in compact or (
        no_dividend_before
        and re.search(r"(?:\d{4}年[^。]{0,30}より)?配当を実施する方針", compact)
    )
    if first_dividend:
        period = re.search(r"(\d{4}年\d{1,2}月期)(?:より|に係る)", compact)
        prefix = f"{period.group(1)}より" if period else ""
        metrics.append({
            "kind": "first_dividend", "action": "introduce",
            "before": "無配", "after": "配当実施",
            "summary": f"{prefix}初配を実施",
        })

    return metrics


def detect_dividend_policy_change(title: str, text: str = "") -> DividendPolicyChange:
    """Return a policy change only when explicit change evidence exists."""
    title_c = _compact(title)
    body_c = _compact(text)
    combined = f"{title_c}\n{body_c}"

    title_explicit = bool(re.search(
        r"(?:配当(?:方針|政策)|株主還元方針|利益還元方針|利益配分.{0,8}基本方針)"
        r"(?:の)?(?:一部)?(?:変更|見直し|改定|新設|決定|導入|撤廃|廃止)"
        r"|(?:配当性向|配当金額算定基準|配当金額の算定基準)(?:の)?(?:変更|見直し|引き上げ|引き下げ)"
        r"|(?:DOE|累進配当|中間配当).{0,16}(?:新設|導入|採用|撤廃|廃止)",
        title_c,
    ))

    body_positive = False
    for sentence in _sentences(text):
        clause = _compact(sentence)
        if _NO_CHANGE_RE.search(clause):
            continue
        explicit_policy = bool(re.search(
            r"(?:配当|株主還元|利益還元)(?:の)?(?:方針|政策).{0,24}?"
            r"(?:変更(?:し|する|いた)|見直し(?:し|する)|改定(?:し|する)|撤廃(?:し|する)|廃止(?:し|する))",
            clause,
        ))
        durable_new_rule = bool(re.search(
            r"(?:新たに|今般|今後|変更後|従来.{0,24}(?:から|より)).{0,50}?"
            r"(?:配当性向|DOE|株主資本配当率|累進配当|最低(?:年間)?配当|総還元性向|安定配当|継続配当)"
            r".{0,36}?(?:導入|採用|新設|変更|引き上げ|引き下げ|撤廃|廃止)"
            r"|(?:配当性向|DOE|株主資本配当率|累進配当|最低(?:年間)?配当|総還元性向|安定配当|継続配当)"
            r".{0,36}?(?:を新たに|を導入すること|を採用すること|を新設|を変更|を引き上げ|を引き下げ|を撤廃|を廃止)",
            clause,
        ))
        if explicit_policy or durable_new_rule:
            body_positive = True
            break

    detected = title_explicit or body_positive
    one_off_title = bool(re.search(r"記念配当|特別配当", title_c))
    general_policy_title = bool(re.search(r"配当(?:方針|政策)|株主還元方針|利益還元方針", title_c))
    if one_off_title and not general_policy_title:
        detected = False
    if not detected:
        return DividendPolicyChange()

    title_return_scope = bool(re.search(r"株主還元方針|利益還元方針|総還元性向", title_c))
    title_dividend_scope = bool(re.search(r"配当(?:方針|政策)|配当性向|配当金額算定基準", title_c))
    return_scope = title_return_scope or (
        not title_dividend_scope and bool(re.search(r"株主還元方針|利益還元方針|総還元性向", combined))
    )
    # Self-share acquisition broadens the policy only when it is mentioned in
    # the same disclosure as an explicit shareholder-return policy change.
    if re.search(r"自己株式(?:の)?取得", combined) and re.search(r"株主還元|利益還元", combined):
        return_scope = True
    scope = "shareholder_return_policy" if return_scope else "dividend_policy"
    label = "還元方針変更" if return_scope else "配当方針変更"

    sentences = _sentences(text)
    before_sentence = next((s for s in sentences if re.search(r"これまで|従来|変更前", s) and re.search(r"配当|還元|内部留保", s)), "")
    after_sentence = next((
        s for s in sentences
        if re.search(r"変更(?:し|する|いた)|導入(?:し|する)|採用(?:し|する)|撤廃(?:し|する)|廃止(?:し|する)|今後|より配当を実施", s)
        and re.search(r"配当|還元|DOE|累進", s)
    ), "")
    before = _focus_evidence(before_sentence, r"これまで|従来|変更前") if before_sentence else ""
    after = _focus_evidence(
        after_sentence,
        r"(?:配当|株主還元|利益還元)(?:の)?(?:方針|政策).{0,16}(?:変更|見直し)|(?:DOE|累進配当).{0,16}(?:導入|採用|変更)",
        lead=40,
    ) if after_sentence else ""
    evidence = []
    if title_explicit:
        evidence.append(_bounded(title))
    for sentence in (before, after):
        bounded = _bounded(sentence)
        if bounded and bounded not in evidence:
            evidence.append(bounded)

    metrics = _extract_metrics(title, text)
    summary = metrics[0]["summary"] if metrics else label
    return DividendPolicyChange(
        detected=True,
        scope=scope,
        label=label,
        action=_action(combined),
        summary=summary,
        before=before,
        after=after,
        metrics=metrics,
        evidence=evidence[:4],
    )
