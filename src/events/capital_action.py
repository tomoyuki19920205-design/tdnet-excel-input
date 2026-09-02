"""Classification and deterministic extraction for equity supply events."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, asdict


CAPITAL_ACTION = "capital_action"


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").replace("\u3000", " ")


_DIRECT_ISSUE = (
    "公募増資", "第三者割当増資", "株主割当増資", "海外募集",
    "新株式発行", "新株の発行", "募集株式の発行", "新規発行株式",
)
_OFFERING = (
    "株式の売出し", "株式売出し", "国内売出し", "海外売出し",
    "買取引受けによる売出し", "オーバーアロットメントによる売出し",
    "売出価格", "売出条件",
)
_DISTRIBUTION = ("立会外分売",)
_FALSE_CAPITAL_INCREASE = (
    "株式分割", "株式併合", "株式交換", "株式移転", "ストックオプション",
    "譲渡制限付株式報酬", "従業員向け株式報酬", "新株予約権", "転換社債",
)


def classify_capital_action(title: str, body: str = "") -> tuple[str, ...]:
    """Return detected actions. Title gates PDF fetching; body confirms details."""
    text = _norm(f"{title}\n{body}")
    actions: list[str] = []
    if any(k in text for k in _DISTRIBUTION):
        actions.append("off_exchange_distribution")
    if any(k in text for k in _OFFERING):
        actions.append("share_offering")

    direct = any(k in text for k in _DIRECT_ISSUE)
    treasury_only = (
        ("自己株式の処分" in text or "自己株式処分" in text)
        and not any(k in text for k in ("新株式発行", "新株の発行", "公募増資", "第三者割当増資"))
    )
    false_only = any(k in text for k in _FALSE_CAPITAL_INCREASE) and not direct
    if direct and not treasury_only and not false_only:
        actions.append("capital_increase")
    return tuple(actions)


def is_capital_action_title(title: str) -> bool:
    """Cheap title-only gate used before downloading a PDF."""
    return bool(classify_capital_action(title, ""))


def classify_status(title: str) -> str:
    text = _norm(title)
    if "中止" in text:
        return "cancelled"
    if "訂正" in text or "変更" in text:
        return "corrected"
    if "終了" in text:
        return "completed"
    if (
        any(k in text for k in ("価格決定", "条件決定", "実施日決定", "実施に関する"))
        or re.search(r"(?:価格|条件)[^\n]{0,12}決定", text)
    ):
        return "conditions_decided"
    if "実施" in text:
        return "implemented"
    return "announced"


def _number(raw: str, unit: str = "株") -> int | None:
    try:
        value = float(_norm(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    multiplier = 1000 if "千株" in _norm(unit) else 1
    return int(value * multiplier)


def _first_shares(text: str, labels: tuple[str, ...]) -> int | None:
    for label in labels:
        patterns = (
            rf"{label}[^\n\r\d]{{0,50}}([\d,]+(?:\.\d+)?)\s*(千株|株)",
            rf"{label}[^\n\r]{{0,80}}?([\d,]+(?:\.\d+)?)\s*(千株|株)",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return _number(match.group(1), match.group(2))
    return None


def _all_shares(text: str, labels: tuple[str, ...]) -> list[int]:
    matches: list[tuple[int, int, int]] = []
    for label in labels:
        for match in re.finditer(rf"{label}[^\n\r]{{0,80}}?([\d,]+(?:\.\d+)?)\s*(千株|株)", text):
            value = _number(match.group(1), match.group(2))
            if value:
                matches.append((match.start(), match.end(), value))
    selected: list[tuple[int, int, int]] = []
    for candidate in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(candidate[0] < end and start < candidate[1] for start, end, _ in selected):
            continue
        selected.append(candidate)
    return [value for _, _, value in sorted(selected)]


_DATE = r"((?:20\d{2}|令和\d+)年\s*\d{1,2}月\s*\d{1,2}日)"


def _date(value: str) -> str | None:
    value = _norm(value).replace(" ", "")
    match = re.match(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", value)
    if match:
        return f"{int(match.group(1)):04d}/{int(match.group(2)):02d}/{int(match.group(3)):02d}"
    match = re.match(r"令和(\d+)年(\d{1,2})月(\d{1,2})日", value)
    if match:
        return f"{2018 + int(match.group(1)):04d}/{int(match.group(2)):02d}/{int(match.group(3)):02d}"
    return None


def _labeled_date(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        segment = re.search(rf"{label}[^\n\r]{{0,100}}", text)
        if not segment:
            continue
        dates = re.findall(_DATE, segment.group(0))
        parsed = [d for d in (_date(v) for v in dates) if d]
        if len(parsed) >= 2:
            return f"{parsed[0]}～{parsed[1]}"
        if parsed:
            abbreviated = re.search(r"[～〜~－-]\s*(\d{1,2})月?\s*(\d{1,2})日", segment.group(0))
            if abbreviated:
                year = parsed[0][:4]
                return f"{parsed[0]}～{year}/{int(abbreviated.group(1)):02d}/{int(abbreviated.group(2)):02d}"
            return parsed[0]
    return None


def latest_issued_shares(db_path: str, ticker: str, disclosure_datetime: str) -> tuple[int | None, str | None]:
    """Use only the latest positive pre-disclosure per_share_data value."""
    if not db_path or db_path == ":memory:" or not ticker:
        return None, None
    cutoff = (disclosure_datetime or "")[:10]
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """SELECT shares_outstanding, disclosed_date FROM per_share_data
                   WHERE UPPER(ticker)=UPPER(?) AND shares_outstanding > 0
                     AND (? = '' OR disclosed_date <= ?)
                   ORDER BY disclosed_date DESC LIMIT 1""",
                (ticker, cutoff, cutoff),
            ).fetchone()
        if row:
            return int(row[0]), str(row[1])
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return None, None


@dataclass
class CapitalActionExtraction:
    actions: list[str]
    status: str
    status_detail: str | None = None
    offering_shares: int | None = None
    offering_oa_shares: int | None = None
    offering_max_shares: int | None = None
    new_shares: int | None = None
    additional_new_shares: int | None = None
    max_new_shares: int | None = None
    distribution_shares: int | None = None
    issued_shares_before: int | None = None
    issued_shares_ratio_source: str | None = None
    issued_shares_basis_date: str | None = None
    offering_ratio: float | None = None
    offering_max_ratio: float | None = None
    new_shares_ratio: float | None = None
    max_new_shares_ratio: float | None = None
    ratio_unavailable_reason: str | None = None
    price_decision_date_or_period: str | None = None
    application_period: str | None = None
    payment_date: str | None = None
    delivery_date: str | None = None
    allotment_date: str | None = None
    effective_date: str | None = None
    distribution_planned_period: str | None = None
    distribution_date: str | None = None
    distribution_price_yen: float | None = None
    distribution_purchase_limit_shares: int | None = None
    extraction_version: str = "capital-actions-v1"

    def to_dict(self) -> dict:
        return asdict(self)


def extract_capital_action(title: str, body: str, *, ticker: str = "", disclosure_datetime: str = "", db_path: str = "") -> CapitalActionExtraction | None:
    text = _norm(f"{title}\n{body}")
    actions = list(classify_capital_action(title, body))
    if not actions:
        return None

    issued = _first_shares(text, ("増資前の発行済株式総数", "売出し前の発行済株式総数", "発行済株式総数", "発行済株式数"))
    issued_source, basis_date = ("disclosure", (disclosure_datetime or "")[:10] or None) if issued else (None, None)
    if not issued:
        issued, basis_date = latest_issued_shares(db_path, ticker, disclosure_datetime)
        if issued:
            issued_source = "per_share_data"

    offering = _first_shares(text, ("売出株式数", "売出し株式数", "引受人の買取引受けによる売出し"))
    if offering is None:
        offering_parts = _all_shares(text, ("国内売出し", "海外売出し"))
        offering = sum(offering_parts) if offering_parts else None
    oa = _first_shares(text, ("オーバーアロットメントによる売出し", "オーバーアロットメント"))
    issue_parts = _all_shares(text, ("公募による新株式発行", "新規発行株式数", "発行新株式数", "募集株式の数", "第三者割当による新株式発行", "第三者割当増資"))
    new_shares = issue_parts[0] if issue_parts else None
    additional = sum(issue_parts[1:]) if len(issue_parts) > 1 else None
    max_new = sum(issue_parts) if issue_parts else None
    distribution = _first_shares(text, ("分売予定株式数", "分売株式数", "分売数量"))

    status = classify_status(title)
    status_detail = None
    title_n = _norm(title)
    if status == "conditions_decided":
        if "立会外分売" in title_n:
            status_detail = "立会外分売実施日決定"
        elif "売出" in title_n:
            status_detail = "売出価格決定"
        else:
            status_detail = "発行条件決定"
    elif status == "corrected":
        status_detail = f"{action_label(actions)}変更"
    elif status == "cancelled":
        status_detail = action_label(actions)
    result = CapitalActionExtraction(
        actions=actions,
        status=status,
        status_detail=status_detail,
        offering_shares=offering,
        offering_oa_shares=oa,
        offering_max_shares=(offering + oa) if offering is not None and oa is not None else offering,
        new_shares=new_shares,
        additional_new_shares=additional,
        max_new_shares=max_new,
        distribution_shares=distribution,
        issued_shares_before=issued,
        issued_shares_ratio_source=issued_source,
        issued_shares_basis_date=basis_date,
        price_decision_date_or_period=_labeled_date(text, ("価格決定期間", "売出価格決定日", "発行価格決定日", "条件決定日")),
        application_period=_labeled_date(text, ("申込期間", "申込期日")),
        payment_date=_labeled_date(text, ("払込期日", "払込日")),
        delivery_date=_labeled_date(text, ("受渡期日", "受渡日")),
        allotment_date=_labeled_date(text, ("割当日",)),
        effective_date=_labeled_date(text, ("効力発生日",)),
        distribution_planned_period=_labeled_date(text, ("分売予定期間", "分売実施予定期間")),
        distribution_date=_labeled_date(text, ("分売実施日", "実施日")),
    )
    price = re.search(r"分売価格[^\n\r\d]{0,30}([\d,]+(?:\.\d+)?)\s*円", text)
    if price:
        result.distribution_price_yen = float(price.group(1).replace(",", ""))
    result.distribution_purchase_limit_shares = _first_shares(text, ("買付申込数量の限度", "買付申込数量限度", "買付数量の限度"))

    if issued:
        if offering is not None:
            result.offering_ratio = offering / issued * 100
            result.offering_max_ratio = (result.offering_max_shares or offering) / issued * 100
        if new_shares is not None:
            result.new_shares_ratio = new_shares / issued * 100
            result.max_new_shares_ratio = (max_new or new_shares) / issued * 100
    elif any(a in actions for a in ("share_offering", "capital_increase")):
        result.ratio_unavailable_reason = "発行済株式数を確認できず"
    return result


def action_label(actions: list[str]) -> str:
    labels = {
        "capital_increase": "増資",
        "share_offering": "株式売出し",
        "off_exchange_distribution": "立会外分売",
    }
    return "・".join(labels[a] for a in ("capital_increase", "share_offering", "off_exchange_distribution") if a in actions)
