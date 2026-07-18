"""src/segment/zip_identity_verifier.py

ZIP identity 検証モジュール。

3 つの合格経路:
  経路A  exact_document_id_match
         ZIP ファイル名から抽出した内部書類 ID が requested_disclosure_no と完全一致する。
         trusted provenance 不要。

  経路B  official_linked_xbrl_match
         内部 ID が異なる場合、J-Quants 公式取得経路で生成した TrustedProvenance のすべての
         条件を満たした場合に限り合格する。

  経路C  official_linked_xbrl_match_without_internal_id
         Attachment iXBRLに内部書類IDが存在しない場合でも、J-Quantsの開示番号完全一致、
         ZIP SHA、ticker、period、quarter、document typeがすべて一致した場合に限り合格する。

このモジュールは純粋関数 (副作用なし) であり、ネットワーク・DB への書き込みは行わない。
秘密情報 (API token / signed URL / Authorization header) を保存・ログ出力しない。
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Optional

logger = logging.getLogger(__name__)

# ================================================================
# provenance schema version
# ================================================================
PROVENANCE_VERSION = "1"

# ================================================================
# 許可する document type  (決算短信の財務諸表本表 XBRL 種別)
# ================================================================
# TDNet/J-Quants で使用される tanshin XBRL の document_type コード。
# 添付書類 (attachment_xbrl) を含む形式が公式に返る。
# 変更する場合はテストを更新すること。
ALLOWED_DOCUMENT_TYPES: frozenset[str] = frozenset({
    "edjpfpi",            # 決算短信(国際会計基準) 連結・個別共通
    "edjpfpj",            # 決算短信(日本基準) 連結
    "edjpfpn",            # 決算短信(日本基準) 非連結
    "tanshin",            # 汎用 tanshin (古い識別子)
    "quarterly_earnings", # 四半期決算短信 (汎用)
    "attachment_xbrl",    # 添付書類型 XBRL (J-Quants 経由の場合に使用)
    "xbrl",               # 汎用フォールバック
})

# ================================================================
# データクラス
# ================================================================

@dataclass
class TrustedProvenance:
    """J-Quants 公式クライアントが生成する来歴情報。

    resolver が内部的に生成し、verifier に渡す。
    外部入力・raw_payload・ファイル名のみからは生成してはならない。
    secret (API token / signed URL / header 値) を保存しない。
    """
    source: str                    # 常に "jquants"
    requested_disclosure_no: str   # 公式 API へ渡した開示番号
    requested_file_type: str       # 常に "x" (XBRL)
    resolved_by_function: str      # 取得関数名 (例: "get_file_url")
    official_request_succeeded: bool
    response_status: int           # HTTP status code
    downloaded_size: int           # bytes
    downloaded_sha256: str         # hex
    internal_document_id: str      # ZIP 内部書類 ID
    ticker: str
    period: str                    # YYYY-MM-DD
    quarter: str                   # 例: "1Q"
    document_type: str
    resolved_at: str               # ISO8601 UTC
    provenance_version: str = PROVENANCE_VERSION

    def is_trusted_source(self) -> bool:
        return self.source == "jquants"


@dataclass
class ZipIdentityVerdict:
    """verify_zip_identity の返却値。"""
    passed: bool
    verdict: str                    # "exact_document_id_match" / "official_linked_xbrl_match" / ""
    rejection_reason: str           # 失敗時の reason code
    requested_id: str
    internal_id: str                # ZIP から抽出した内部 ID (単一)
    zip_sha256: str                 # 対象 ZIP の SHA-256
    details: dict = field(default_factory=dict)


# ================================================================
# 内部ヘルパー
# ================================================================

_DISCLOSURE_NO_RE = re.compile(r"(?:1401|0812)(\d{14})")
_DISCLOSURE_NO_RE2 = re.compile(r"(20\d{12})")
_DISCLOSURE_NO_RE3 = re.compile(r"(\d{14})")
_ALPHA_INTERNAL_ID_FROM_SUMMARY_RE = re.compile(
    r"^tse-[^-]+-(?P<entry_ticker>[A-Za-z0-9]{5})-"
    r"(?P<candidate>(?P<date>20\d{6})\d(?P<candidate_ticker>[A-Za-z0-9]{5}))"
    r"(?=[-_.])"
)
_ATTACHMENT_IXBRL_RE = re.compile(
    r"(?:^|/)XBRLData/Attachment/[^/]+\.(?:htm|html|xhtml)$", re.IGNORECASE
)
_INLINE_XBRL_NAMESPACES = frozenset({
    "http://www.xbrl.org/2008/inlineXBRL",
    "http://www.xbrl.org/2013/inlineXBRL",
})
_XBRLI_NAMESPACE = "http://www.xbrl.org/2003/instance"
_DEI_NAMESPACE_RE = re.compile(
    r"^http://disclosure\.edinet-fsa\.go\.jp/taxonomy/jpdei/"
    r"\d{4}-\d{2}-\d{2}/jpdei_cor$"
)
_DEI_FACT_NAMES = frozenset({
    "CurrentPeriodEndDateDEI", "CurrentFiscalYearEndDateDEI",
    "TypeOfCurrentPeriodDEI", "DocumentTypeDEI",
})


def _inline_fact_value(element: ElementTree.Element) -> str:
    return "".join(element.itertext()).strip()


def _expanded_name(value: str, namespaces: dict[str, str]) -> tuple[str, str]:
    if value.startswith("{") and "}" in value:
        uri, local = value[1:].split("}", 1)
        return uri, local
    if ":" in value:
        prefix, local = value.split(":", 1)
        return namespaces.get(prefix, ""), local
    return namespaces.get("", ""), value


def _consensus(values: set[str], field_name: str) -> str:
    if len(values) == 1:
        return next(iter(values))
    if len(values) > 1:
        logger.warning(
            "[ZIP_METADATA] Attachment iXBRL %s values=%s",
            f"{field_name.upper()}_CONFLICT", sorted(values),
        )
    return ""


def _normalize_dei_quarter(value: str) -> str:
    normalized = value.strip().upper().replace(" ", "")
    return {
        "FY": "FY", "4Q": "FY", "4": "FY",
        "1Q": "1Q", "Q1": "1Q", "1": "1Q",
        "2Q": "2Q", "Q2": "2Q", "2": "2Q",
        "3Q": "3Q", "Q3": "3Q", "3": "3Q",
    }.get(normalized, "")


def _extract_attachment_ixbrl_metadata(
    zf: zipfile.ZipFile, names: list[str]
) -> dict[str, str]:
    """Parse every Attachment iXBRL candidate and return consensus DEI identity."""
    candidates = [name for name in names if _ATTACHMENT_IXBRL_RE.search(name)]
    result = {"ticker": "", "period": "", "quarter": "", "document_type": ""}
    if not candidates:
        return result

    tickers: set[str] = set()
    current_periods: set[str] = set()
    fiscal_year_ends: set[str] = set()
    quarters: set[str] = set()
    document_types: set[str] = set()

    complete_candidates = 0
    for name in candidates:
        try:
            data = zf.read(name)
            namespaces: dict[str, str] = {}
            for _event, item in ElementTree.iterparse(io.BytesIO(data), events=("start-ns",)):
                namespaces[item[0]] = item[1]
            root = ElementTree.fromstring(data)
        except (ElementTree.ParseError, KeyError, OSError, UnicodeError, ValueError) as exc:
            logger.warning("[ZIP_METADATA] candidate=%s excluded=PARSE_ERROR err=%s", name, exc)
            continue

        elements = list(root.iter())
        ix_elements = [
            element for element in elements
            if element.tag.startswith("{")
            and element.tag[1:].split("}", 1)[0] in _INLINE_XBRL_NAMESPACES
        ]
        contexts = [e for e in elements if e.tag == f"{{{_XBRLI_NAMESPACE}}}context"]
        identifiers = [
            e for e in elements
            if e.tag == f"{{{_XBRLI_NAMESPACE}}}identifier" and _inline_fact_value(e)
        ]
        schema_refs = [e for e in elements if e.tag.rsplit("}", 1)[-1] == "schemaRef"]
        if not ix_elements:
            logger.info("[ZIP_METADATA] candidate=%s excluded=IX_ELEMENTS_MISSING", name)
            continue
        if not contexts:
            logger.info("[ZIP_METADATA] candidate=%s excluded=CONTEXT_MISSING", name)
            continue
        if not identifiers:
            logger.info("[ZIP_METADATA] candidate=%s excluded=ENTITY_IDENTIFIER_MISSING", name)
            continue
        if not schema_refs:
            logger.info("[ZIP_METADATA] candidate=%s excluded=SCHEMA_REF_MISSING", name)
            continue

        candidate_facts: dict[str, set[str]] = {fact: set() for fact in _DEI_FACT_NAMES}
        unsupported_namespace = False
        for element in ix_elements:
            namespace_uri, fact_name = _expanded_name(element.attrib.get("name", ""), namespaces)
            if fact_name not in _DEI_FACT_NAMES:
                continue
            if _DEI_NAMESPACE_RE.fullmatch(namespace_uri) is None:
                logger.warning(
                    "[ZIP_METADATA] candidate=%s excluded=DEI_NAMESPACE_UNSUPPORTED uri=%s",
                    name, namespace_uri,
                )
                unsupported_namespace = True
                break
            value = _inline_fact_value(element)
            if value:
                candidate_facts[fact_name].add(value)
        if unsupported_namespace:
            return result
        if not any(candidate_facts.values()):
            logger.info("[ZIP_METADATA] candidate=%s excluded=DEI_MISSING", name)
            continue

        candidate_tickers = {
            _normalize_ticker(_inline_fact_value(e)) for e in identifiers
            if "sicc" in e.attrib.get("scheme", "").lower()
        }
        candidate_periods = candidate_facts["CurrentPeriodEndDateDEI"]
        if not candidate_periods:
            candidate_periods = candidate_facts["CurrentFiscalYearEndDateDEI"]
        candidate_quarters = {
            value for raw in candidate_facts["TypeOfCurrentPeriodDEI"]
            if (value := _normalize_dei_quarter(raw))
        }
        candidate_documents = candidate_facts["DocumentTypeDEI"]
        tickers.update(candidate_tickers)
        current_periods.update(candidate_periods)
        fiscal_year_ends.update(candidate_facts["CurrentFiscalYearEndDateDEI"])
        quarters.update(candidate_quarters)
        document_types.update(candidate_documents)
        if candidate_tickers and candidate_periods and candidate_quarters and candidate_documents:
            complete_candidates += 1

    if complete_candidates == 0:
        logger.warning("[ZIP_METADATA] Attachment iXBRL excluded=COMPLETE_DEI_CANDIDATE_MISSING")
        return result

    result["ticker"] = _consensus(tickers, "ticker")
    result["period"] = _consensus(
        current_periods if current_periods else fiscal_year_ends, "period"
    )
    result["quarter"] = _consensus(quarters, "quarter")
    if _consensus(document_types, "document_type"):
        result["document_type"] = "attachment_xbrl"
    return result


def _extract_disclosure_no_from_str(value: str) -> Optional[str]:
    """ファイル名・文字列から14桁開示番号を抽出する。"""
    if not value:
        return None
    m = _DISCLOSURE_NO_RE.search(value)
    if m:
        return m.group(1)
    m = _DISCLOSURE_NO_RE2.search(value)
    if m:
        return m.group(1)
    m = _DISCLOSURE_NO_RE3.search(value)
    if m:
        return m.group(1)
    return None


def _normalize_ticker(value: str) -> str:
    if len(value) == 5 and value.endswith("0"):
        return value[:-1]
    return value


def _extract_alpha_internal_id_from_names(
    names: list[str], metadata_ticker: str
) -> str:
    """Summary エントリ名の英字入り内部書類 ID を限定的に抽出する。"""
    candidates: set[str] = set()
    normalized_metadata_ticker = _normalize_ticker(metadata_ticker)

    for name in names:
        path = PurePosixPath(name)
        if path.parent.name != "Summary":
            continue

        match = _ALPHA_INTERNAL_ID_FROM_SUMMARY_RE.match(path.name)
        if not match:
            continue

        candidate = match.group("candidate")
        candidate_ticker = match.group("candidate_ticker")
        if not any(char.isalpha() for char in candidate_ticker):
            continue
        try:
            datetime.strptime(match.group("date"), "%Y%m%d")
        except ValueError:
            continue

        entry_ticker = _normalize_ticker(match.group("entry_ticker"))
        candidate_ticker = _normalize_ticker(candidate_ticker)
        if (
            entry_ticker == candidate_ticker == normalized_metadata_ticker
        ):
            candidates.add(candidate)

    return next(iter(candidates)) if len(candidates) == 1 else ""


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_internal_ids_from_zip(zip_path: str) -> list[str]:
    """ZIP 内のトップレベルエントリ名から内部書類 ID を抽出する。

    Returns:
        ユニーク ID のリスト (通常は 1 件)。
    """
    ids: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            no = _extract_disclosure_no_from_str(name)
            if no and no not in ids:
                ids.append(no)
    return ids


def extract_actual_metadata_from_zip(
    zip_path: str,
    expected_period: str = "",
    expected_quarter: str = "",
) -> dict[str, str]:
    """ZIPファイルの実体（エントリ名、マニフェスト、およびSummary HTML等）からメタデータを安全に抽出する。
    
    戻り値キー: ticker, period, quarter, document_type, internal_document_id
    """
    meta = {
        "ticker": "",
        "period": "",
        "quarter": "",
        "document_type": "",
        "internal_document_id": "",
    }
    
    if not zip_path or not os.path.exists(zip_path):
        return meta
        
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            
            # 1. internal_document_id の抽出
            for name in names:
                no = _extract_disclosure_no_from_str(name)
                if no:
                    meta["internal_document_id"] = no
                    break
            
            # 2. ticker の抽出 (xsd や xbrl などのエントリ名から5桁または4桁のティッカーを探す)
            for name in names:
                m = re.search(r'tse-[^-]+-([a-zA-Z0-9]{4,5})[-_]', name)
                if m:
                    tk = m.group(1)
                    meta["ticker"] = tk[:-1] if len(tk) == 5 and tk.endswith("0") else tk
                    break

            if not meta["internal_document_id"]:
                meta["internal_document_id"] = _extract_alpha_internal_id_from_names(
                    names, meta["ticker"]
                )
            
            # 3. document_type (Summaryフォルダがある場合は attachment_xbrl とする)
            for name in names:
                if "Summary" in name:
                    meta["document_type"] = "attachment_xbrl"
                    break
            if not meta["document_type"]:
                for name in names:
                    for dt in ALLOWED_DOCUMENT_TYPES:
                        if dt in name:
                            meta["document_type"] = dt
                            break
            
            # 4. Summary HTM から period と quarter の詳細抽出
            summary_htm = None
            for name in names:
                if "Summary" in name and name.endswith(".htm"):
                    summary_htm = name
                    break
            
            if summary_htm:
                content = zf.read(summary_htm).decode("utf-8", errors="ignore")
                
                # identifier から ticker のバックアップ抽出
                if not meta["ticker"]:
                    m = re.search(r'scheme="[^"]+sicc"[^>]*>([a-zA-Z0-9]{4,5})</', content)
                    if m:
                        tk = m.group(1)
                        meta["ticker"] = tk[:-1] if len(tk) == 5 and tk.endswith("0") else tk
                
                # period (endDate または instant 日付の最大値)
                dates = re.findall(r'<(?:xbrli:endDate|xbrli:instant)>(\d{4}-\d{2}-\d{2})</', content)
                if dates:
                    # FY のSummaryには実績FY末と翌期forecast末が併存する。
                    # expected_periodがZIP内候補に完全一致する場合だけ実績として
                    # 優先し、存在しない場合は従来どおりの最大日付を返して
                    # 後段の厳格なperiod照合で拒否させる。
                    if (
                        expected_quarter == "FY"
                        and expected_period
                        and expected_period in dates
                    ):
                        meta["period"] = expected_period
                    else:
                        meta["period"] = max(dates)
                
                # quarter の抽出 (QuarterlyPeriod 要素値)
                m = re.search(r'name="tse-ed-t:QuarterlyPeriod"[^>]*>(\d+)</', content)
                if m:
                    meta["quarter"] = {"1": "1Q", "2": "2Q", "3": "3Q", "4": "FY"}.get(m.group(1), "")
                else:
                    m = re.search(r'QuarterlyPeriod[^>]*>(\d+)</', content)
                    if m:
                        meta["quarter"] = {"1": "1Q", "2": "2Q", "3": "3Q", "4": "FY"}.get(m.group(1), "")
                    elif "Q1" in content or "AccumulatedQ1" in content:
                        meta["quarter"] = "1Q"
                    elif "Q2" in content or "AccumulatedQ2" in content:
                        meta["quarter"] = "2Q"
                    elif "Q3" in content or "AccumulatedQ3" in content:
                        meta["quarter"] = "3Q"
                    else:
                        meta["quarter"] = "FY"

                # FY Summaryにはforecast context名としてAccumulatedQ2等が
                # 含まれ得る。actual periodの完全一致と年次markerを確認できる
                # 場合だけ、context非依存の文字列fallbackを上書きする。
                if (
                    expected_quarter == "FY"
                    and expected_period
                    and meta["period"] == expected_period
                    and meta["document_type"] == "attachment_xbrl"
                    and ("AnnualMember" in content or "YearEndMember" in content)
                ):
                    meta["quarter"] = "FY"
            else:
                attachment_meta = _extract_attachment_ixbrl_metadata(zf, names)
                if any(_ATTACHMENT_IXBRL_RE.search(name) for name in names):
                    # Attachment candidates are evaluated as one identity set.  Do not
                    # retain a filename-derived value when their consensus conflicts.
                    meta.update(attachment_meta)
    except Exception as e:
        logger.warning("[ZIP_METADATA] Failed to extract metadata from zip: %s", e)
        
    return meta


# ================================================================
# メイン関数
# ================================================================

def verify_zip_identity(
    zip_path: str,
    requested_disclosure_no: str,
    expected_ticker: str,
    expected_period: str,
    expected_quarter: str,
    trusted_provenance: Optional[TrustedProvenance] = None,
) -> ZipIdentityVerdict:
    """ZIP ファイルの identity を検証する。

    Args:
        zip_path:               検証対象 ZIP のパス
        requested_disclosure_no: 公式 API へ渡した / 期待する開示番号 (14桁)
        expected_ticker:        期待する ticker
        expected_period:        期待する period (YYYY-MM-DD)
        expected_quarter:       期待する quarter (例: "1Q")
        trusted_provenance:     J-Quants resolver から渡された来歴情報 (経路B に必須)

    Returns:
        ZipIdentityVerdict
    """
    _PASS = lambda verdict, internal_id, sha: ZipIdentityVerdict(
        passed=True,
        verdict=verdict,
        rejection_reason="",
        requested_id=requested_disclosure_no,
        internal_id=internal_id,
        zip_sha256=sha,
    )
    _FAIL = lambda reason, internal_id="", sha="", **kw: ZipIdentityVerdict(
        passed=False,
        verdict="",
        rejection_reason=reason,
        requested_id=requested_disclosure_no,
        internal_id=internal_id,
        zip_sha256=sha,
        details=kw,
    )


    # ── STEP 1: ZIP 存在確認 ──────────────────────────────────
    if not zip_path or not os.path.exists(zip_path):
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=zip_not_found requested=%s",
            requested_disclosure_no,
        )
        return _FAIL("zip_not_found")

    # ── STEP 2: 0 バイト拒否 ─────────────────────────────────
    if os.path.getsize(zip_path) == 0:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=zero_byte_zip requested=%s",
            requested_disclosure_no,
        )
        return _FAIL("zero_byte_zip")

    # ── STEP 3: ZIP 破損拒否 ─────────────────────────────────
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                logger.warning(
                    "[ZIP_IDENTITY] zip_identity_rejected reason=broken_zip requested=%s file=%s",
                    requested_disclosure_no, bad,
                )
                return _FAIL("broken_zip")
    except zipfile.BadZipFile:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=broken_zip requested=%s",
            requested_disclosure_no,
        )
        return _FAIL("broken_zip")

    # ── STEP 4-5: 内部 ID 混在チェック ──
    try:
        internal_ids = _extract_internal_ids_from_zip(zip_path)
    except Exception as e:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=broken_zip requested=%s err=%s",
            requested_disclosure_no, e,
        )
        return _FAIL("broken_zip")

    if len(internal_ids) > 1:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=multiple_internal_document_ids requested=%s ids=%s",
            requested_disclosure_no, internal_ids,
        )
        return _FAIL("multiple_internal_document_ids", details={"internal_ids": internal_ids})

    # ── ZIP 実体からメタデータを安全に抽出（独立抽出） ──
    meta = extract_actual_metadata_from_zip(
        zip_path,
        expected_period=expected_period,
        expected_quarter=expected_quarter,
    )
    
    # 抽出できない項目があればexpected値で補わずSTOP (不合格判定)
    if (not meta["ticker"] or not meta["period"] or not meta["quarter"] or
            not meta["document_type"]):
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=metadata_unresolved requested=%s got=%s",
            requested_disclosure_no, meta,
        )
        return _FAIL("metadata_unresolved", details=meta)

    internal_id = meta["internal_document_id"]

    # ── 期待値（expected）との直接照合 ──
    if expected_ticker != "ANY" and meta["ticker"] != expected_ticker:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=ticker_mismatch expected=%s actual=%s",
            expected_ticker, meta["ticker"],
        )
        return _FAIL("ticker_mismatch", internal_id)

    if expected_period != "ANY" and meta["period"] != expected_period:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=period_mismatch expected=%s actual=%s",
            expected_period, meta["period"],
        )
        return _FAIL("period_mismatch", internal_id)

    if expected_quarter != "ANY" and meta["quarter"] != expected_quarter:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=quarter_mismatch expected=%s actual=%s",
            expected_quarter, meta["quarter"],
        )
        return _FAIL("quarter_mismatch", internal_id)

    if meta["document_type"] not in ALLOWED_DOCUMENT_TYPES:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=document_type_mismatch doctype=%s",
            meta["document_type"],
        )
        return _FAIL("document_type_mismatch", internal_id)

    # ── STEP 6-7: 完全一致確認 → 経路A ───────────────────────
    if internal_id == requested_disclosure_no:
        try:
            sha = _sha256_file(zip_path)
        except Exception:
            sha = ""
        logger.info(
            "[ZIP_IDENTITY] zip_identity_verified verdict=exact_document_id_match requested=%s internal=%s sha256=%s",
            requested_disclosure_no, internal_id, sha,
        )
        return _PASS("exact_document_id_match", internal_id, sha)

    # ── STEP 8-18: 経路B (公式関連 XBRL) ────────────────────
    # STEP 8: trusted provenance 確認
    if trusted_provenance is None:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=provenance_missing requested=%s internal=%s",
            requested_disclosure_no, internal_id,
        )
        return _FAIL("provenance_missing", internal_id)

    # SHA-256 計算 (経路B では必ず計算)
    try:
        sha = _sha256_file(zip_path)
    except Exception as e:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=broken_zip requested=%s err=%s",
            requested_disclosure_no, e,
        )
        return _FAIL("broken_zip", internal_id)

    prov = trusted_provenance

    # STEP 9: requested_disclosure_no と provenance の一致
    if prov.requested_disclosure_no != requested_disclosure_no:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=provenance_requested_id_mismatch "
            "expected=%s prov=%s",
            requested_disclosure_no, prov.requested_disclosure_no,
        )
        return _FAIL("provenance_requested_id_mismatch", internal_id, sha)

    # STEP 10: ZIP hash と provenance hash の一致
    if prov.downloaded_sha256 != sha:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=provenance_hash_mismatch requested=%s",
            requested_disclosure_no,
        )
        return _FAIL("provenance_hash_mismatch", internal_id, sha)

    # STEP 11: source = jquants
    if not prov.is_trusted_source():
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=untrusted_source source=%s",
            prov.source,
        )
        return _FAIL("untrusted_source", internal_id, sha)

    # STEP 12: official request 成功確認
    if not prov.official_request_succeeded:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=official_request_failed requested=%s",
            requested_disclosure_no,
        )
        return _FAIL("official_request_failed", internal_id, sha)

    if prov.requested_file_type != "x":
        return _FAIL("provenance_file_type_mismatch", internal_id, sha)
    if prov.response_status != 200:
        return _FAIL("provenance_response_status_mismatch", internal_id, sha)
    if prov.downloaded_size != os.path.getsize(zip_path):
        return _FAIL("provenance_size_mismatch", internal_id, sha)
    if not prov.resolved_by_function:
        return _FAIL("provenance_resolver_missing", internal_id, sha)

    # ── J-Quants 公式来歴情報の偽装防止照合 ──
    # TrustedProvenance 内の属性が、ZIP 実体から抽出した値と完全一致することを確認
    if prov.ticker != meta["ticker"]:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=ticker_mismatch expected=%s prov=%s",
            meta["ticker"], prov.ticker,
        )
        return _FAIL("ticker_mismatch", internal_id, sha)

    if prov.period != meta["period"]:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=period_mismatch expected=%s prov=%s",
            meta["period"], prov.period,
        )
        return _FAIL("period_mismatch", internal_id, sha)

    if prov.quarter != meta["quarter"]:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=quarter_mismatch expected=%s prov=%s",
            meta["quarter"], prov.quarter,
        )
        return _FAIL("quarter_mismatch", internal_id, sha)

    if prov.document_type != meta["document_type"]:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=document_type_mismatch expected=%s prov=%s",
            meta["document_type"], prov.document_type,
        )
        return _FAIL("document_type_mismatch", internal_id, sha)

    if prov.internal_document_id != internal_id:
        logger.warning(
            "[ZIP_IDENTITY] zip_identity_rejected reason=internal_id_mismatch "
            "prov_internal=%s zip_internal=%s",
            prov.internal_document_id, internal_id,
        )
        return _FAIL("internal_id_mismatch", internal_id, sha)

    # ── 全条件一致: 経路B PASS ────────────────────────────────
    if not internal_id:
        logger.info(
            "[ZIP_IDENTITY] zip_identity_verified "
            "verdict=official_linked_xbrl_match_without_internal_id "
            "requested=%s ticker=%s period=%s quarter=%s doctype=%s sha256=%s",
            requested_disclosure_no, prov.ticker, prov.period, prov.quarter,
            prov.document_type, sha,
        )
        return _PASS("official_linked_xbrl_match_without_internal_id", "", sha)
    logger.info(
        "[ZIP_IDENTITY] zip_identity_verified verdict=official_linked_xbrl_match "
        "requested=%s internal=%s ticker=%s period=%s quarter=%s doctype=%s sha256=%s",
        requested_disclosure_no, internal_id,
        prov.ticker, prov.period, prov.quarter, prov.document_type, sha,
    )
    return _PASS("official_linked_xbrl_match", internal_id, sha)
