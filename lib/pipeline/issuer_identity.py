"""Reject explicit issuer conflicts before PL aggregation; retain raw evidence.

Fiscal-end and name changes are review signals, not proof of another issuer.
Legacy records without a native identifier remain unverified, never guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Mapping

from src.common_ticker import normalize_ticker

_SECURITY = re.compile(r"^[0-9][0-9A-Z]{3}$")


def assess_issuer(code: str, payload: dict, master: Mapping | None = None) -> dict:
    """Compare identifiers from one native document, not financial similarity."""
    ticker = normalize_ticker(code)
    result = {"ticker": ticker, "allowed": True, "verified": False,
              "reason": None, "warnings": []}
    if not _SECURITY.fullmatch(ticker):
        return result | {"allowed": False, "reason": "invalid_security_code"}
    native_codes = [payload[k] for k in ("Code", "LocalCode") if payload.get(k)]
    if any(normalize_ticker(c) != ticker for c in native_codes):
        return result | {"allowed": False, "reason": "native_security_code_conflict"}
    result["verified"] = bool(native_codes)
    if not native_codes:
        result["warnings"].append("native_security_code_missing")
    if master:
        for source_key, master_key in (("EdinetCode", "edinet_code"),
                                       ("EDINETCode", "edinet_code"),
                                       ("CorporateNumber", "corporate_number")):
            actual, expected = payload.get(source_key), master.get(master_key)
            if actual and expected and str(actual).strip() != str(expected).strip():
                return result | {"allowed": False, "reason": master_key + "_conflict"}
        name = payload.get("CompanyName")
        if name and master.get("name_ja") and name != master["name_ja"]:
            result["warnings"].append("company_name_review")
        end = payload.get("CurFYEn") or payload.get("CurrentFiscalYearEndDate")
        known_ends = master.get("historical_fiscal_ends")
        if end and known_ends and end not in known_ends:
            result["warnings"].append("fiscal_period_review")
    return result


@dataclass
class IssuerAudit:
    logger: object
    rejected: list[dict] = field(default_factory=list)
    unverified_count: int = 0
    _cache: dict = field(default_factory=dict)

    def accepts(self, code, raw_json):
        # Native rows stay in SQLite. Rejected rows are excluded from the export
        # and their identifying evidence is written to the existing ingest log.
        raw_text = raw_json or "{}"
        digest = hashlib.sha256(str(raw_text).encode("utf-8")).hexdigest()
        key = (str(code), digest)
        if key in self._cache:
            return self._cache[key]
        try:
            payload = json.loads(raw_text)
            if not isinstance(payload, dict):
                raise ValueError("native payload is not an object")
            decision = assess_issuer(code, payload)
        except (ValueError, TypeError):
            payload = {}
            decision = {"allowed": False, "verified": False,
                        "reason": "invalid_native_payload"}
        if not decision["allowed"]:
            evidence = {"local_code": str(code),
                        "native_code": payload.get("Code") or payload.get("LocalCode"),
                        "disclosure_id": payload.get("DiscNo") or payload.get("DisclosureNumber"),
                        "raw_sha256": digest, "reason": decision["reason"]}
            self.rejected.append(evidence)
            self.logger.warning("[ISSUER_QUARANTINE] %s", json.dumps(evidence, ensure_ascii=False))
        elif not decision["verified"]:
            self.unverified_count += 1
        allowed = int(decision["allowed"])
        self._cache[key] = allowed
        return allowed


def register_sqlite_issuer_guard(connection, logger) -> IssuerAudit:
    audit = IssuerAudit(logger)
    connection.create_function("pl_issuer_identity_valid", 2, audit.accepts)
    return audit
