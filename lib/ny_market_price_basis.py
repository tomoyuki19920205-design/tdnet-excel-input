"""Strict parsing of exchange close and corporate-action evidence."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def close_matches(left: float, right: float) -> bool:
    # Float transport noise only; never the old two-cent penny-stock tolerance.
    return math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-7)


def official_regular_close(info: dict, symbol: str, session: date) -> tuple[float, str]:
    if info.get("symbol") != symbol or info.get("assetClass") != "STOCKS":
        raise ValueError("official close symbol/asset mapping mismatch")
    matches = []
    for field in ("primaryData", "secondaryData"):
        item = info.get(field) or {}
        stamp = str(item.get("lastTradeTimestamp") or "")
        match = re.fullmatch(r"Closed at (\w{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M) ET", stamp)
        if not match:
            continue
        observed = datetime.strptime(match[1], "%b %d, %Y %I:%M %p")
        if observed.date() == session:
            value = float(str(item["lastSalePrice"]).replace("$", "").replace(",", ""))
            if not math.isfinite(value) or value <= 0:
                raise ValueError("invalid official close")
            matches.append((value, stamp))
    if len(matches) != 1:
        raise ValueError("dated official regular close is missing or ambiguous")
    return matches[0]


def validate_notice_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "www.nasdaqtrader.com"
            or parsed.path != "/TraderNews.aspx" or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError("split notice must be an official Nasdaq Trader notice")


def parse_exchange_split_notice(raw: bytes, url: str, symbol: str) -> dict:
    validate_notice_url(url)
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    # Limit parsing to the symbol's announcement, not site navigation or other notices.
    pattern = (r"\(" + re.escape(symbol) + r"\) will effect .*?\((\d+)\s*[-:]\s*(\d+)\) reverse split.*?"
               r"will become effective on (\w+, \w+ \d{1,2}, \d{4})\.")
    found = re.findall(pattern, text, re.I)
    if len(found) != 1:
        raise ValueError("split notice symbol, ratio or effective session is ambiguous")
    new, old, effective = found[0]
    if int(new) <= 0 or int(old) <= int(new):
        raise ValueError("invalid reverse split ratio")
    return {
        "symbol": symbol, "new_shares": int(new), "old_shares": int(old),
        "effective_session_date": datetime.strptime(effective, "%A, %B %d, %Y").date().isoformat(),
        "source_identifier": url, "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_vendor_actions(events: dict, notice: dict) -> None:
    """A supplied notice must never hide a different or additional vendor event."""
    if not events:
        return
    if set(events) != {"splits"} or not isinstance(events["splits"], dict) or len(events["splits"]) != 1:
        raise ValueError("additional corporate actions require separate verification")
    event = next(iter(events["splits"].values()))
    try:
        ratio = float(event["numerator"]) / float(event["denominator"])
        from zoneinfo import ZoneInfo
        from datetime import timezone
        effective = datetime.fromtimestamp(int(event["date"]), timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("vendor split event is incomplete") from exc
    if not close_matches(ratio, notice["new_shares"] / notice["old_shares"]) or effective.isoformat() != notice["effective_session_date"]:
        raise ValueError("vendor split event conflicts with official notice")


def previous_on_target_basis(previous: float, previous_session: date, target_session: date, action: dict) -> float:
    effective = date.fromisoformat(action["effective_session_date"])
    if previous_session < effective <= target_session:
        return previous * action["old_shares"] / action["new_shares"]
    return previous


def classify_official_discrepancy(*, official: dict, history_previous: float,
                                  history_target: float, minute: dict | None,
                                  action: dict) -> tuple[str | None, dict]:
    """Only classify after dated official and independently captured boundary evidence agree."""
    if not official.get("target_close_verified") or not minute or minute.get("price_field") != "boundary_open":
        return None, {}
    previous, target = official["previous_close"], official["target_close"]
    if not close_matches(minute["previous_close"], official.get("previous_close_raw", previous)) or not close_matches(minute["target_close"], target):
        return None, {}
    notice = action.get("official_action")
    if notice:
        factor = notice["old_shares"] / notice["new_shares"]
        effective = date.fromisoformat(notice["effective_session_date"])
        target_session = date.fromisoformat(official["target_session_date"])
        if target_session < effective and close_matches(history_previous / factor, previous) and close_matches(history_target, target):
            return "corporate_action_timing_mismatch", {
                "history_previous_raw": history_previous, "history_previous_normalized": history_previous / factor,
                "normalization_divisor": factor, "effective_session_date": effective.isoformat(),
                "comparison_basis": "pre_action_regular_close",
            }
    last_trade = minute.get("previous_last_regular_bar_close")
    basis_unchanged = action.get("status") == "checked_none" or (
        notice and date.fromisoformat(official["target_session_date"]) < date.fromisoformat(notice["effective_session_date"]))
    if (basis_unchanged and last_trade is not None and close_matches(history_previous, last_trade)
            and close_matches(history_target, target) and not close_matches(history_previous, previous)):
        return "provider_error", {
            "diagnosis": "vendor_daily_matches_last_minute_trade_not_exchange_close",
            "last_regular_minute_close": last_trade, "official_previous_close": previous,
        }
    return None, {}
