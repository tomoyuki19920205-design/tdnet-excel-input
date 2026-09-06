"""Generic frozen-cohort controller for isolated Company News validation runs.

The controller has no production route.  It accepts only a previously frozen
cohort, executes at most the explicitly approved number of companies, and
requires a new explicit resume command after any stop.  Network/model work is
owned by injected company and official-source executors, which keeps lifecycle
tests entirely offline.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import company_news_lightweight as lightweight
from tools.company_news_official_source import (
    CompanyIdentityRegistry,
    CompanyIdentityRecord,
    DurableArtifactStore,
    FetchPolicy,
    HttpFetchJournal,
    OfficialSourceResolution,
    OfficialSourceResolver,
    OfficialSourceError,
    OfficialWindowCoverage,
    RepositoryExistingTransport,
    WindowsSchannelTransport,
    apply_registry_identity,
    promote_verified_identity,
    validate_window_coverage,
)
from tools.company_news_overlap_scope import classify_news_monitor_scope


SCHEMA = "company_news_frozen_cohort_run_v1"
BOOTSTRAP_SCHEMA = "company_news_frozen_input_bootstrap_v1"
OFFICIAL_PLAN_SCHEMA = "company_news_official_source_plan_v1"
IDENTITY_RESOLUTION_PLAN_SCHEMA = "company_news_identity_resolution_plan_v1"
IR_NIGHTLY_VERIFIED_BODY_SCHEMA = "company_ir_nightly_verified_body_v1"
RUN_STATES = {"PREPARED", "RUNNING", "STOPPED", "COMPLETED", "FAILED"}
COMPANY_STATES = {"PENDING", "RUNNING", "COMPLETED", "FAILED", "SAFE_UNKNOWN"}
DOCUMENT_PROVENANCE_STATES = (
    "DISCOVERED_IN_INDEX", "URL_DISCOVERED", "BODY_FETCH_ATTEMPTED",
    "BODY_VERIFIED", "ACCESS_FAILED",
)
TICKER = re.compile(r"^[0-9A-Z]{4}$")
DEFAULT_HARD_TOKEN_GATE = 500_000
DEFAULT_PREFERRED_TOKEN_GATE = 400_000
RUNTIME_FIELDS = ("assignment_id", "ticker", "company_name", "search_from", "search_to",
                  "strategy_version", "position")
FORBIDDEN_BOOTSTRAP_KEYS = {
    "bucket", "historical_result", "historical_result_label", "old_event",
    "material_answer", "gold_url", "selection_reason",
}


class MultiRunnerError(RuntimeError):
    pass


class CohortIntegrityError(MultiRunnerError):
    pass


class ResumeRequired(MultiRunnerError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CohortIntegrityError(f"JSON object required: {path}")
    return value


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _create_exclusive(path: Path, value: Any) -> None:
    """Create a guard without a check/create race; a partial guard still fails closed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise MultiRunnerError("run guard already exists") from exc


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return _sha_bytes(raw)


def _normalized_company_name(value: str) -> str:
    normalized = re.sub(r"[\s\u3000]", "", value or "")
    for designator in ("株式会社", "㈱", "(株)", "（株）"):
        normalized = normalized.replace(designator, "")
    return normalized.casefold()


def _contains_forbidden_bootstrap_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(str(key).casefold() in FORBIDDEN_BOOTSTRAP_KEYS
                   or _contains_forbidden_bootstrap_key(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden_bootstrap_key(child) for child in value)
    return False


@dataclass(frozen=True)
class FrozenCompany:
    position: int
    source_universe_position: int
    security_code: str
    company_name: str

    def assignment(self, *, search_from: str, search_to: str) -> dict[str, Any]:
        return {
            "assignment_id": f"phase22-{self.position:02d}-{self.security_code}",
            "ticker": self.security_code,
            "company_name": self.company_name,
            "search_from": search_from,
            "search_to": search_to,
            "strategy_version": lightweight.STRATEGY,
            "position": self.position,
        }


@dataclass(frozen=True)
class FrozenCohort:
    path: Path
    file_sha256: str
    cohort_sha256: str
    universe_order_sha256: str
    companies: tuple[FrozenCompany, ...]

    @classmethod
    def load(cls, path: Path, *, approved_company_count: int,
             max_companies: int, expected_cohort_sha256: str | None = None) -> "FrozenCohort":
        path = path.resolve()
        if not path.is_file():
            raise CohortIntegrityError("frozen cohort file is missing")
        document = _read(path)
        rows = document.get("companies")
        if not isinstance(rows, list) or not rows:
            raise CohortIntegrityError("companies must be a non-empty array")
        if len(rows) != approved_company_count:
            raise CohortIntegrityError("approved company count mismatch")
        if len(rows) > max_companies:
            raise CohortIntegrityError("cohort exceeds explicit safety hard cap")
        required = {"position", "source_universe_position", "security_code", "company_name"}
        if any(not isinstance(row, dict) or not required <= set(row) for row in rows):
            raise CohortIntegrityError("cohort company fields are incomplete")
        companies = tuple(FrozenCompany(
            position=row["position"], source_universe_position=row["source_universe_position"],
            security_code=str(row["security_code"]), company_name=row["company_name"],
        ) for row in rows)
        if [row.position for row in companies] != list(range(1, len(companies) + 1)):
            raise CohortIntegrityError("positions must be explicit and contiguous")
        if any(type(row.source_universe_position) is not int or not row.company_name.strip()
               or not TICKER.fullmatch(row.security_code) for row in companies):
            raise CohortIntegrityError("malformed company identity")
        tickers = [row.security_code for row in companies]
        if len(set(tickers)) != len(tickers):
            raise CohortIntegrityError("duplicate ticker")
        universe_positions = [row.source_universe_position for row in companies]
        if universe_positions != sorted(universe_positions) or len(set(universe_positions)) != len(companies):
            raise CohortIntegrityError("frozen universe order changed")
        hash_rows = [{key: source[key] for key in (
            "position", "source_universe_position", "security_code", "company_name", "bucket")}
            for source in rows]
        calculated = _canonical_sha(hash_rows)
        stored = document.get("cohort_sha256")
        if not isinstance(stored, str) or calculated != stored:
            raise CohortIntegrityError("cohort SHA mismatch")
        if expected_cohort_sha256 is not None and stored != expected_cohort_sha256:
            raise CohortIntegrityError("cohort SHA does not match approved value")
        order_sha = document.get("source_universe_order_sha256")
        if not isinstance(order_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", order_sha):
            raise CohortIntegrityError("universe order SHA is invalid")
        return cls(path, _sha_bytes(path.read_bytes()), stored, order_sha, companies)

    def assert_unchanged(self) -> None:
        current = FrozenCohort.load(
            self.path, approved_company_count=len(self.companies),
            max_companies=len(self.companies), expected_cohort_sha256=self.cohort_sha256)
        if current.file_sha256 != self.file_sha256 or current.companies != self.companies:
            raise CohortIntegrityError("frozen cohort file changed after prepare")


def _load_repository_identity_records(
        repository_artifacts_root: Path) -> tuple[dict[str, CompanyIdentityRecord], dict[str, str]]:
    """Load only canonical identity-registry artifacts, never benchmark answers."""
    records: dict[str, CompanyIdentityRecord] = {}
    conflicts: set[str] = set()
    observed: dict[str, set[str]] = {}
    if not repository_artifacts_root.is_dir():
        return records, {}
    patterns = ("**/*company-identity-registry*.json",)
    for path in sorted({path for pattern in patterns
                        for path in repository_artifacts_root.glob(pattern)}):
        try:
            registry = CompanyIdentityRegistry.load(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        for record in registry.to_dict()["records"]:
            parsed = CompanyIdentityRecord(**record)
            observed.setdefault(parsed.security_code, set()).add(parsed.status)
            if parsed.status != "VERIFIED":
                continue
            prior = records.get(parsed.security_code)
            if prior and (prior.official_domain != parsed.official_domain
                          or prior.verified_official_url != parsed.verified_official_url):
                conflicts.add(parsed.security_code)
                records.pop(parsed.security_code, None)
            elif parsed.security_code not in conflicts:
                records[parsed.security_code] = parsed
    for security_code, statuses in observed.items():
        if "STALE" in statuses:
            conflicts.add(security_code)
            records.pop(security_code, None)
    states = {security_code: ("STALE" if security_code in conflicts else (
        "VERIFIED" if security_code in records else "UNVERIFIED"))
        for security_code in observed}
    return records, states


def _load_company_master(company_master_db: Path,
                         tickers: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not company_master_db.is_file():
        return {}
    uri = company_master_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_ir_companies'"
        ).fetchone()
        if table is None:
            raise MultiRunnerError("company master has no company_ir_companies table")
        placeholders = ",".join("?" for _ in tickers)
        available = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(company_ir_companies)").fetchall()}
        wanted = (
            "ticker", "company_name", "official_url", "official_domain", "ir_top_url",
            "ir_library_url", "ir_event_url", "discovery_status", "source_origin",
            "last_validated_at", "last_http_status", "last_error",
        )
        selection = [name if name in available else f"NULL AS {name}" for name in wanted]
        rows = connection.execute(
            f"SELECT {','.join(selection)} FROM company_ir_companies "
            f"WHERE ticker IN ({placeholders})", tuple(tickers)).fetchall()
        return {str(row["ticker"]): dict(row) for row in rows}
    finally:
        connection.close()


def _load_ir_nightly_sources(company_master_db: Path,
                             tickers: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Read exact URLs that IR Nightly has actually fetched successfully."""
    result = {str(ticker): [] for ticker in tickers}
    if not company_master_db.is_file():
        return result
    connection = sqlite3.connect(company_master_db.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_ir_sources'"
        ).fetchone()
        if table is None:
            return result
        available = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(company_ir_sources)").fetchall()}
        required = {"ticker", "source_url"}
        if not required <= available:
            return result
        optional = ("status", "baseline_completed_at", "last_checked_at", "last_success_at",
                    "last_error", "failure_count", "page_kind", "discovered_from",
                    "provenance_url", "is_external", "verified_from_official")
        selection = ["ticker", "source_url"] + [
            name if name in available else f"NULL AS {name}" for name in optional]
        placeholders = ",".join("?" for _ in tickers)
        order = "ticker,id" if "id" in available else "ticker,source_url"
        rows = connection.execute(
            f"SELECT {','.join(selection)} FROM company_ir_sources "
            f"WHERE ticker IN ({placeholders}) ORDER BY {order}", tuple(tickers)).fetchall()
        for row in rows:
            result[str(row["ticker"])].append(dict(row))
        return result
    finally:
        connection.close()


def _exact_official_url(value: Any, official_domain: str) -> str | None:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    domain = str(official_domain or "").lower().rstrip(".")
    if (parsed.scheme.lower() not in {"http", "https"} or not host or not domain
            or (host != domain and not host.endswith("." + domain))):
        return None
    return str(value)


def _select_identity_url_authority(
        authority: Mapping[str, Any] | None,
        sources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Keep domain ownership separate from an exact URL suitable for a fetch."""
    domain = str((authority or {}).get("official_domain") or "").strip().lower().rstrip(".")
    successful = [dict(row) for row in sources
                  if row.get("last_success_at") and row.get("status") == "active"
                  and row.get("verified_from_official") in {1, True}
                  and _exact_official_url(row.get("source_url"), domain)]
    priority = {"top": 0, "library": 1, "event": 2}
    successful.sort(key=lambda row: (
        0 if urlsplit(str(row["source_url"])).scheme.lower() == "https" else 1,
        priority.get(str(row.get("page_kind") or ""), 9),
        str(row.get("source_url"))))
    if successful:
        row = successful[0]
        return {
            "identity_fetch_url": row["source_url"],
            "identity_fetch_url_source": "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH",
            "identity_fetch_verification": "KNOWN_SUCCESSFUL_FETCH",
            "previously_successful": True,
            "last_success_at": row.get("last_success_at"),
        }
    master_url = _exact_official_url((authority or {}).get("official_url"), domain)
    if master_url:
        return {
            "identity_fetch_url": master_url,
            "identity_fetch_url_source": "MASTER_EXACT_URL",
            "identity_fetch_verification": "UNVERIFIED",
            "previously_successful": False,
            "last_success_at": None,
        }
    return {
        "identity_fetch_url": deterministic_https_root(domain) if domain else None,
        "identity_fetch_url_source": "DERIVED_ROOT_URL" if domain else None,
        "identity_fetch_verification": "UNVERIFIED",
        "previously_successful": False,
        "last_success_at": None,
    }


def _load_ir_nightly_verified_body_records(
        repository_artifacts_root: Path) -> dict[str, CompanyIdentityRecord]:
    """Promote only strict IR Nightly manifests backed by saved bytes and hashes."""
    records: dict[str, CompanyIdentityRecord] = {}
    root = repository_artifacts_root.resolve()
    if not root.is_dir():
        return records
    for manifest_path in sorted(root.glob("**/*ir-nightly-verified-body*.json")):
        try:
            manifest = _read(manifest_path)
            if (manifest.get("schema_version") != IR_NIGHTLY_VERIFIED_BODY_SCHEMA
                    or _contains_forbidden_bootstrap_key(manifest)):
                continue
            body_path = (manifest_path.parent / str(manifest["body_path"])).resolve()
            body_path.relative_to(root)
            body = body_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != manifest.get("body_sha256"):
                continue
            datetime.fromisoformat(str(manifest["last_success_at"]).replace("Z", "+00:00"))
            provisional = CompanyIdentityRecord(
                security_code=str(manifest["ticker"]), company_name=str(manifest["company_name"]),
                normalized_name=_normalized_company_name(str(manifest["company_name"])),
                official_domain=str(manifest["official_domain"]),
                verified_official_url=str(manifest["source_url"]),
                verification_source=str(manifest_path), verification_method=None,
                verified_at=None, evidence_hash=None, status="UNVERIFIED", evidence_excerpt=None)
            resolver = OfficialSourceResolver(RepositoryExistingTransport({
                str(manifest["source_url"]): (body, str(manifest["mime_type"]), 200)}))
            resolution = resolver.resolve(provisional, str(manifest["source_url"]), FetchPolicy())
            record = promote_verified_identity(
                provisional, resolution,
                verification_source=f"IR Nightly verified body manifest: {manifest_path}")
            records[record.security_code] = record
        except (KeyError, OSError, ValueError, TypeError, json.JSONDecodeError, OfficialSourceError):
            continue
    return records


def _phase22e_failure_details(report_path: Path | None) -> dict[str, dict[str, Any]]:
    if report_path is None or not report_path.is_file():
        return {}
    report = _read(report_path)
    result: dict[str, dict[str, Any]] = {}
    for row in report.get("matrix") or []:
        ticker = str(row.get("ticker"))
        detail = {
            "requested_url": row.get("deterministic_root_url"),
            "failure": row.get("failure"), "http_requests": row.get("http_requests"),
            "failure_stage": "DNS_PREFLIGHT" if row.get("failure") == "DNS" else None,
            "outer_exception": None, "exception_chain": [], "certificate_evidence": False,
            "protocol_evidence": None,
        }
        company_dir = report_path.parent / "companies" / f"{int(row['position']):02d}-{ticker}"
        journals = sorted(company_dir.glob("identity-http/http-journal/*.json"))
        if journals:
            journal = _read(journals[-1])
            events = [event for event in journal.get("history") or []
                      if event.get("phase") == "RESPONSE_RECEIVED"]
            if events:
                metadata = events[-1].get("transport_metadata") or {}
                certificate = metadata.get("certificate_summary") or {}
                detail.update(
                    requested_url=journal.get("requested_url"),
                    failure_stage=metadata.get("failure_stage"),
                    outer_exception=metadata.get("exception_type"),
                    exception_chain=metadata.get("exception_chain") or [],
                    certificate_evidence=bool(certificate.get("leaf") or certificate.get("chain_elements")),
                    protocol_evidence=metadata.get("protocol"),
                    error_detail=metadata.get("error_detail"),
                )
        result[ticker] = detail
    return result


def generate_identity_resolution_plan(
        cohort: FrozenCohort, *, company_master_db: Path,
        repository_artifacts_root: Path, output_path: Path,
        phase22e_report_path: Path | None = None) -> dict[str, Any]:
    """Create an offline-only exact-URL plan; this function has no transport path."""
    tickers = [row.security_code for row in cohort.companies]
    registry_records, _states = _load_repository_identity_records(repository_artifacts_root)
    nightly_body_records = _load_ir_nightly_verified_body_records(repository_artifacts_root)
    verified = {**nightly_body_records, **registry_records}
    master = _load_company_master(company_master_db, tickers)
    nightly_sources = _load_ir_nightly_sources(company_master_db, tickers)
    failures = _phase22e_failure_details(phase22e_report_path)
    companies = []
    for company in cohort.companies:
        authority = master.get(company.security_code)
        domain = str((authority or {}).get("official_domain") or "") or None
        record = verified.get(company.security_code)
        selected = _select_identity_url_authority(authority, nightly_sources[company.security_code])
        if record and record.normalized_name == _normalized_company_name(company.company_name):
            selected = {
                "identity_fetch_url": record.verified_official_url,
                "identity_fetch_url_source": "REGISTRY_VERIFIED_BODY",
                "identity_fetch_verification": "VERIFIED_BODY",
                "previously_successful": True, "last_success_at": record.verified_at,
            }
            classification, requires_network = "A_EXISTING_VERIFIED_ARTIFACT", False
        else:
            candidate = selected.get("identity_fetch_url")
            source = selected.get("identity_fetch_url_source")
            is_https = bool(candidate and urlsplit(str(candidate)).scheme.lower() == "https")
            if source in {"IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", "MASTER_EXACT_URL"} and is_https:
                classification, requires_network = "B_EXACT_AUTHORITATIVE_URL_ONE_GET", True
            elif source in {"IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", "MASTER_EXACT_URL"}:
                classification, requires_network = "C_EXACT_URL_INCOMPATIBLE_WITH_HTTPS_POLICY", False
            else:
                classification, requires_network = "C_EXACT_AUTHORITATIVE_URL_MISSING", False
        failure = failures.get(company.security_code) or {}
        derived_host = (urlsplit(str(failure.get("requested_url") or "")).hostname or "").lower()
        exact_host = (urlsplit(str(selected.get("identity_fetch_url") or "")).hostname or "").lower()
        clusters = []
        if derived_host and exact_host and derived_host != exact_host:
            clusters.extend(["A_DERIVED_APEX_INAPPROPRIATE", "B_SAVED_CANONICAL_HOST_EXISTS"])
        if failure.get("failure") == "DNS":
            clusters.append("D_DERIVED_HOST_DNS_FAILURE")
        if failure.get("failure") == "TLS_HANDSHAKE" and any(
                "資格情報がありません" in str(value) for value in failure.get("exception_chain") or []):
            clusters.append("C_SCHANNEL_HELPER_COMMON_FAILURE")
        if not clusters and failure:
            clusters.append("F_INSUFFICIENT_EVIDENCE")
        companies.append({
            "position": company.position, "ticker": company.security_code,
            "company_name": company.company_name, "official_domain": domain,
            "company_root_url": (authority or {}).get("official_url"),
            "ir_root_url": (authority or {}).get("ir_top_url"),
            "ir_library_url": (authority or {}).get("ir_library_url"),
            "ir_news_or_event_url": (authority or {}).get("ir_event_url"),
            "nightly_successful_urls": [row.get("source_url") for row in nightly_sources[company.security_code]
                                        if row.get("last_success_at") and row.get("status") == "active"],
            "candidate_exact_url": selected.get("identity_fetch_url"),
            "source": selected.get("identity_fetch_url_source"),
            "authority_level": selected.get("identity_fetch_verification"),
            "previously_successful": selected.get("previously_successful"),
            "last_success_at": selected.get("last_success_at"),
            "requires_network": requires_network, "classification": classification,
            "reason": ("saved verified body completes identity" if not requires_network and classification.startswith("A_")
                       else "one exact HTTPS GET is required to verify body/company linkage" if requires_network
                       else "saved exact URL is HTTP but VERIFIED identity contract requires HTTPS"
                       if classification == "C_EXACT_URL_INCOMPATIBLE_WITH_HTTPS_POLICY"
                       else "no saved exact official URL"),
            "phase22e_failure": failure, "failure_clusters": sorted(set(clusters)),
        })
    plan = {
        "schema_version": IDENTITY_RESOLUTION_PLAN_SCHEMA, "created_at": _now(),
        "offline_only": True, "research_companies": 0, "http_calls": 0,
        "web_calls": 0, "model_calls": 0,
        "cohort_path": str(cohort.path), "cohort_sha256": cohort.cohort_sha256,
        "cohort_file_sha256": cohort.file_sha256,
        "universe_order_sha256": cohort.universe_order_sha256,
        "ordered_tickers": tickers,
        "authority_hierarchy": ["REGISTRY_VERIFIED_BODY", "IR_NIGHTLY_VERIFIED_BODY",
                                "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", "MASTER_EXACT_URL",
                                "DERIVED_ROOT_URL"],
        "contract": {
            "domain_authority_is_exact_url_authority": False,
            "configured_url_is_verified_identity": False,
            "derived_root_is_lowest_authority": True,
            "derived_root_failure_blocks_official_source_globally": False,
            "www_may_be_used_only_from_saved_evidence": True,
            "historical_benchmark_answer_input": False,
        },
        "companies": companies,
        "draft_verified_count": sum(row["classification"].startswith("A_") for row in companies),
        "network_needed_count": sum(row["requires_network"] for row in companies),
        "unresolved_url_count": sum(row["classification"].startswith("C_") for row in companies),
        "exact_authoritative_url_count": sum(row["source"] in {
            "REGISTRY_VERIFIED_BODY", "IR_NIGHTLY_KNOWN_SUCCESSFUL_FETCH", "MASTER_EXACT_URL"}
            for row in companies),
        "bare_domain_only_count": sum(row["source"] == "DERIVED_ROOT_URL" for row in companies),
        "network_candidate_tickers": [row["ticker"] for row in companies if row["requires_network"]],
        "unresolved_tickers": [row["ticker"] for row in companies
                               if row["classification"].startswith("C_")],
        "common_transport_review_required": sum(
            "C_SCHANNEL_HELPER_COMMON_FAILURE" in row["failure_clusters"] for row in companies) > 1,
    }
    if _contains_forbidden_bootstrap_key(plan):
        raise CohortIntegrityError("identity resolution plan contains historical answer leakage")
    cohort.assert_unchanged()
    _atomic(output_path.resolve(), plan)
    return plan


def _official_plan(cohort: FrozenCohort,
                   verified: Mapping[str, CompanyIdentityRecord]) -> dict[str, Any]:
    companies = []
    for company in cohort.companies:
        record = verified[company.security_code]
        companies.append({
            "position": company.position,
            "ticker": company.security_code,
            "company_name": company.company_name,
            "identity_status": "VERIFIED",
            "official_domain": record.official_domain,
            "verified_root_url": record.verified_official_url,
            "allowed_hosts": [record.official_domain],
            "identity_registry_reference": "phase22-company-identity-registry.json",
            "transport_preference": ["repository_existing", "windows-schannel-v1"],
        })
    return {
        "schema_version": OFFICIAL_PLAN_SCHEMA,
        "cohort_sha256": cohort.cohort_sha256,
        "company_count": len(companies),
        "ordered_tickers": [row.security_code for row in cohort.companies],
        "identity_registry_reference": "phase22-company-identity-registry.json",
        "companies": companies,
        "resolver_policy": {
            "manual_url_input": False,
            "historical_result_input": False,
            "search_engine_discovery": False,
            "root_authority": "frozen_verified_identity_registry",
            "target_discovery": "deterministic_links_from_verified_official_parent",
            "delegated_resource_authority": "exact_embedded_url_only",
            "https_only": True,
        },
        # Compatibility containers are intentionally empty at bootstrap.
        "family_urls": {},
        "repository_artifacts": {},
    }


def validate_frozen_inputs(cohort: FrozenCohort, registry_path: Path,
                           plan_path: Path) -> tuple[CompanyIdentityRegistry, dict[str, Any]]:
    registry = CompanyIdentityRegistry.load(registry_path)
    registry_records = registry.to_dict()["records"]
    plan = _read(plan_path)
    if plan.get("schema_version") != OFFICIAL_PLAN_SCHEMA:
        raise CohortIntegrityError("official plan schema mismatch")
    tickers = [row.security_code for row in cohort.companies]
    registry_tickers = [str(row["security_code"]) for row in registry_records]
    if len(registry_tickers) != len(tickers) or set(registry_tickers) != set(tickers):
        raise CohortIntegrityError("identity registry cohort/count mismatch")
    if (plan.get("cohort_sha256") != cohort.cohort_sha256
            or plan.get("company_count") != len(tickers)
            or plan.get("ordered_tickers") != tickers):
        raise CohortIntegrityError("official plan cohort/order mismatch")
    rows = plan.get("companies")
    if not isinstance(rows, list) or len(rows) != len(tickers):
        raise CohortIntegrityError("official plan company matrix incomplete")
    if _contains_forbidden_bootstrap_key(plan):
        raise CohortIntegrityError("official plan contains result leakage")
    for company, row in zip(cohort.companies, rows, strict=True):
        record = registry.usable(company.security_code, company.company_name)
        if record is None:
            raise CohortIntegrityError(f"identity registry incomplete: {company.security_code}")
        if (row.get("position") != company.position
                or row.get("ticker") != company.security_code
                or row.get("company_name") != company.company_name
                or row.get("identity_status") != "VERIFIED"
                or row.get("official_domain") != record.official_domain
                or row.get("verified_root_url") != record.verified_official_url):
            raise CohortIntegrityError(f"official plan identity mismatch: {company.security_code}")
    if _contains_forbidden_bootstrap_key({"records": registry_records}):
        raise CohortIntegrityError("identity registry contains result leakage")
    return registry, plan


def bootstrap_inputs(cohort: FrozenCohort, *, company_master_db: Path,
                     repository_artifacts_root: Path, output_dir: Path,
                     offline_only: bool) -> dict[str, Any]:
    """Build a result-neutral bootstrap matrix and freeze only complete inputs."""
    if not offline_only:
        raise MultiRunnerError("bootstrap-inputs requires --offline-only")
    verified, observed_states = _load_repository_identity_records(repository_artifacts_root)
    tickers = [row.security_code for row in cohort.companies]
    master = _load_company_master(company_master_db, tickers)
    matrix = []
    for company in cohort.companies:
        record = verified.get(company.security_code)
        authority = master.get(company.security_code)
        if record is not None and record.normalized_name != _normalized_company_name(company.company_name):
            record = None
        status = "VERIFIED" if record else observed_states.get(company.security_code, "UNVERIFIED")
        domain = record.official_domain if record else (authority or {}).get("official_domain")
        root_url = record.verified_official_url if record else (authority or {}).get("official_url")
        missing = []
        if record is None:
            missing.append("verified official body/repository identity artifact")
        if not domain:
            missing.append("official domain authority")
        if not root_url or not str(root_url).startswith("https://"):
            missing.append("verified HTTPS root URL")
        matrix.append({
            "position": company.position, "ticker": company.security_code,
            "company": company.company_name, "identity_status": status,
            "identity_source": record.verification_source if record else (
                "company_ir_monitor.db:company_ir_companies" if authority else None),
            "official_domain": domain, "verified_root_url": record.verified_official_url if record else None,
            "known_root_url": root_url, "repository_evidence_available": record is not None,
            "offline_bootstrap_complete": not missing, "missing_requirement": missing,
        })
    ready = all(row["offline_bootstrap_complete"] for row in matrix)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_path = output_dir / "phase22-company-identity-registry.json"
    plan_path = output_dir / "phase22-official-source-plan.json"
    report = {
        "schema_version": BOOTSTRAP_SCHEMA, "created_at": _now(),
        "status": "READY" if ready else "INCOMPLETE_OFFLINE_EVIDENCE",
        "offline_only": True, "research_companies": 0, "http_calls": 0,
        "web_calls": 0, "model_calls": 0,
        "cohort_path": str(cohort.path), "cohort_sha256": cohort.cohort_sha256,
        "cohort_file_sha256": cohort.file_sha256,
        "universe_order_sha256": cohort.universe_order_sha256,
        "company_count": len(matrix), "ordered_tickers": tickers,
        "matrix": matrix,
        "verified_count": sum(row["identity_status"] == "VERIFIED" for row in matrix),
        "unverified_count": sum(row["identity_status"] == "UNVERIFIED" for row in matrix),
        "stale_count": sum(row["identity_status"] == "STALE" for row in matrix),
        "registry_path": str(registry_path) if ready else None,
        "plan_path": str(plan_path) if ready else None,
    }
    if ready:
        selected = [verified[ticker] for ticker in tickers]
        registry = CompanyIdentityRegistry(selected)
        plan = _official_plan(cohort, verified)
        if _contains_forbidden_bootstrap_key(registry.to_dict()) or _contains_forbidden_bootstrap_key(plan):
            raise CohortIntegrityError("bootstrap result leakage detected")
        registry.save(registry_path)
        _atomic(plan_path, plan)
        validate_frozen_inputs(cohort, registry_path, plan_path)
        report.update(
            registry_logical_sha256=_canonical_sha(registry.to_dict()),
            registry_file_sha256=_sha_bytes(registry_path.read_bytes()),
            plan_logical_sha256=_canonical_sha(plan),
            plan_file_sha256=_sha_bytes(plan_path.read_bytes()),
        )
    _atomic(output_dir / "phase22-bootstrap-report.json", report)
    return report


def deterministic_https_root(official_domain: str) -> str:
    domain = str(official_domain or "").strip().lower().rstrip(".")
    if (not domain or "/" in domain or ":" in domain or "@" in domain
            or not re.fullmatch(r"[a-z0-9.-]+", domain)
            or any(not label or label.startswith("-") or label.endswith("-")
                   for label in domain.split("."))):
        raise MultiRunnerError("invalid company-master official domain")
    return f"https://{domain}/"


def literal_identity_page_candidate(resolution: OfficialSourceResolution,
                                    official_domain: str) -> str | None:
    """Choose one literal same-domain corporate link; never construct a path."""
    if resolution.access_status != "VERIFIED" or resolution.normalized_content is None:
        return None
    markers = re.compile(r"(?:^|[-_/])(company|corporate|about|profile)(?:[-_/\.?#]|$)", re.I)
    candidates = []
    for value in resolution.normalized_content.relevant_links:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        domain = official_domain.lower().rstrip(".")
        if (parsed.scheme.lower() == "https" and (host == domain or host.endswith("." + domain))
                and markers.search(parsed.path + ("?" + parsed.query if parsed.query else ""))):
            candidates.append(value)
    return sorted(set(candidates))[0] if candidates else None


def _default_live_resolver(company_dir: Path, company: FrozenCompany) -> tuple[OfficialSourceResolver, HttpFetchJournal]:
    store = DurableArtifactStore.prepare(company_dir / "identity-http")
    journal = HttpFetchJournal(
        store, run_id=f"phase22-identity-{company.position:02d}-{company.security_code}",
        company=company.security_code)
    return OfficialSourceResolver(WindowsSchannelTransport(
        journal=journal, retain_raw_html=True)), journal


def bootstrap_identities_schannel(
        cohort: FrozenCohort, *, company_master_db: Path,
        repository_artifacts_root: Path, output_dir: Path,
        max_http_requests: int = 24,
        resolver_factory: Callable[[Path, FrozenCompany], tuple[OfficialSourceResolver, Any]] | None = None,
        prepare_run_dir: Path | None = None, search_from: str = "2026-08-25",
        search_to: str = "2026-08-31") -> dict[str, Any]:
    """Verify cohort identities with repository evidence, then bounded Schannel GETs."""
    if max_http_requests < 1 or max_http_requests > 24:
        raise MultiRunnerError("identity bootstrap HTTP budget must be 1..24")
    verified, observed_states = _load_repository_identity_records(repository_artifacts_root)
    tickers = [row.security_code for row in cohort.companies]
    master = _load_company_master(company_master_db, tickers)
    nightly_sources = _load_ir_nightly_sources(company_master_db, tickers)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_path = output_dir / "phase22-company-identity-registry.json"
    plan_path = output_dir / "phase22-official-source-plan.json"
    if registry_path.exists() or plan_path.exists():
        raise MultiRunnerError("identity bootstrap formal artifact collision")
    factory = resolver_factory or _default_live_resolver
    total_requests = 0
    repository_hits = 0
    matrix = []
    for company in cohort.companies:
        record = verified.get(company.security_code)
        authority = master.get(company.security_code)
        state = observed_states.get(company.security_code, "UNVERIFIED")
        failure = None
        transport = None
        request_count = 0
        verified_url = record.verified_official_url if record else None
        if record is not None and record.normalized_name == _normalized_company_name(company.company_name):
            state, transport = "VERIFIED", "repository_existing"
            repository_hits += 1
        elif authority is None or not authority.get("official_domain"):
            state, failure = "UNVERIFIED", "company-master official domain unavailable"
        elif total_requests >= max_http_requests:
            state, failure = "BLOCKED", "global HTTP budget exhausted"
        else:
            domain = str(authority["official_domain"])
            selected = _select_identity_url_authority(
                authority, nightly_sources[company.security_code])
            root_url = selected["identity_fetch_url"]
            if not root_url or urlsplit(str(root_url)).scheme.lower() != "https":
                matrix.append({
                    "position": company.position, "ticker": company.security_code,
                    "company": company.company_name, "identity_state": "UNVERIFIED",
                    "official_domain": domain,
                    "deterministic_root_url": deterministic_https_root(domain),
                    "identity_fetch_url": root_url,
                    "identity_fetch_url_source": selected["identity_fetch_url_source"],
                    "identity_fetch_verification": selected["identity_fetch_verification"],
                    "verified_url": None, "transport": None, "http_requests": 0,
                    "failure": "exact authoritative HTTPS URL unavailable",
                })
                continue
            provisional = CompanyIdentityRecord(
                security_code=company.security_code, company_name=company.company_name,
                normalized_name=_normalized_company_name(company.company_name),
                official_domain=domain, verified_official_url=root_url,
                verification_source=(
                    f"company_ir_monitor.db:{selected['identity_fetch_url_source'].lower()}"),
                verification_method=None, verified_at=None, evidence_hash=None,
                status="UNVERIFIED", evidence_excerpt=None)
            company_dir = output_dir / "companies" / f"{company.position:02d}-{company.security_code}"
            resolver, journal = factory(company_dir, company)
            remaining_before_root = max_http_requests - total_requests
            root_result = resolver.resolve(
                provisional, root_url,
                FetchPolicy(max_redirects=1 if remaining_before_root >= 2 else 0))
            request_count = int(journal.request_count())
            total_requests += request_count
            transport = root_result.fetch_method
            result = root_result
            try:
                record = promote_verified_identity(
                    provisional, result,
                    verification_source="Phase2.2e company-master root + Schannel verified body")
            except OfficialSourceError as exc:
                identity_url = literal_identity_page_candidate(result, domain)
                remaining_company_budget = 2 - request_count
                remaining_global_budget = max_http_requests - total_requests
                if identity_url and remaining_company_budget > 0 and remaining_global_budget > 0:
                    result = resolver.resolve(provisional, identity_url, FetchPolicy(max_redirects=0))
                    new_count = int(journal.request_count())
                    total_requests += new_count - request_count
                    request_count = new_count
                    try:
                        record = promote_verified_identity(
                            provisional, result,
                            verification_source=(
                                "Phase2.2e literal official identity page + Schannel verified body"))
                    except OfficialSourceError as identity_exc:
                        record, failure = None, str(identity_exc)
                else:
                    record, failure = None, str(exc)
            if record is not None:
                verified[company.security_code] = record
                state, verified_url, failure = "VERIFIED", record.verified_official_url, None
            else:
                state = "BLOCKED" if result.access_status == "BLOCKED" else "UNVERIFIED"
                failure = result.failure_reason or failure or "official body lacks direct company identity"
        matrix.append({
            "position": company.position, "ticker": company.security_code,
            "company": company.company_name, "identity_state": state,
            "official_domain": (authority or {}).get("official_domain") if authority else (
                record.official_domain if record else None),
            "deterministic_root_url": (
                deterministic_https_root(authority["official_domain"])
                if authority and authority.get("official_domain") else None),
            "identity_fetch_url": (
                _select_identity_url_authority(
                    authority, nightly_sources[company.security_code])["identity_fetch_url"]
                if authority and authority.get("official_domain") else None),
            "identity_fetch_url_source": (
                _select_identity_url_authority(
                    authority, nightly_sources[company.security_code])["identity_fetch_url_source"]
                if authority and authority.get("official_domain") else None),
            "verified_url": verified_url, "transport": transport,
            "http_requests": request_count, "failure": failure,
        })
    ready = all(row["identity_state"] == "VERIFIED" for row in matrix)
    report = {
        "schema_version": BOOTSTRAP_SCHEMA, "created_at": _now(),
        "status": "READY" if ready else "INCOMPLETE_IDENTITY_VERIFICATION",
        "mode": "windows-schannel-v1", "research_companies": 0,
        "web_calls": 0, "model_calls": 0, "http_requests": total_requests,
        "http_request_limit": max_http_requests, "repository_existing_hits": repository_hits,
        "cohort_path": str(cohort.path), "cohort_sha256": cohort.cohort_sha256,
        "cohort_file_sha256": cohort.file_sha256,
        "universe_order_sha256": cohort.universe_order_sha256,
        "company_count": len(matrix),
        "ordered_tickers": [row.security_code for row in cohort.companies],
        "matrix": matrix,
        "verified_count": sum(row["identity_state"] == "VERIFIED" for row in matrix),
        "unverified_count": sum(row["identity_state"] == "UNVERIFIED" for row in matrix),
        "blocked_count": sum(row["identity_state"] == "BLOCKED" for row in matrix),
        "registry_path": str(registry_path) if ready else None,
        "plan_path": str(plan_path) if ready else None,
        "prepare_result": None,
    }
    if ready:
        registry = CompanyIdentityRegistry([verified[row.security_code] for row in cohort.companies])
        plan = _official_plan(cohort, verified)
        if _contains_forbidden_bootstrap_key(registry.to_dict()) or _contains_forbidden_bootstrap_key(plan):
            raise CohortIntegrityError("bootstrap result leakage detected")
        registry.save(registry_path)
        _atomic(plan_path, plan)
        validate_frozen_inputs(cohort, registry_path, plan_path)
        report.update(
            registry_logical_sha256=_canonical_sha(registry.to_dict()),
            registry_file_sha256=_sha_bytes(registry_path.read_bytes()),
            plan_logical_sha256=_canonical_sha(plan),
            plan_file_sha256=_sha_bytes(plan_path.read_bytes()),
        )
        if prepare_run_dir is not None:
            manifest = prepare_run(
                cohort, prepare_run_dir, search_from=search_from, search_to=search_to,
                approved_company_count=len(cohort.companies),
                identity_registry_path=registry_path, official_plan_path=plan_path)
            report["prepare_result"] = {
                "state": manifest["state"], "run_dir": str(prepare_run_dir.resolve()),
                "company_directories": 0,
            }
    _atomic(output_dir / "phase22-schannel-bootstrap-report.json", report)
    return report


@dataclass(frozen=True)
class OfficialPreflight:
    status: str
    registry: CompanyIdentityRegistry
    window: OfficialWindowCoverage
    model_context: dict[str, Any]
    telemetry: dict[str, Any]
    gaps: tuple[str, ...] = ()
    document_fetcher: Callable[..., dict[str, Any]] | None = None


class OfficialPreflightProvider(Protocol):
    def prepare(self, assignment: Mapping[str, Any], company_dir: Path) -> OfficialPreflight: ...


class CompanyExecutor(Protocol):
    def __call__(self, root: Path, company_dir: Path, assignment: dict[str, Any],
                 official: OfficialPreflight) -> tuple[dict[str, Any], dict[str, Any]]: ...


class OfficialResolverIntegration:
    """Bound non-LLM official resolver used before any company model context.

    Family URLs must be stable metadata or deterministically derived from a
    verified official parent.  This class never searches or guesses URLs.
    """
    def __init__(self, registry: CompanyIdentityRegistry,
                 repository_resolver: OfficialSourceResolver,
                 live_resolver: OfficialSourceResolver | None,
                 family_urls: Mapping[str, Mapping[str, str]]) -> None:
        self.registry = registry
        self.repository_resolver = repository_resolver
        self.live_resolver = live_resolver
        self.family_urls = {str(code): dict(urls) for code, urls in family_urls.items()}
        self._cache: dict[str, OfficialSourceResolution] = {}

    def _resolve(self, identity, url: str) -> OfficialSourceResolution:
        if url in self._cache:
            return self._cache[url]
        result = self.repository_resolver.resolve(identity, url)
        if result.access_status != "VERIFIED" and self.live_resolver is not None:
            result = self.live_resolver.resolve(identity, url)
        self._cache[url] = result
        return result

    def prepare(self, assignment: Mapping[str, Any], company_dir: Path) -> OfficialPreflight:
        ticker, name = str(assignment["ticker"]), str(assignment["company_name"])
        identity = self.registry.usable(ticker, name)
        if identity is None:
            window = OfficialWindowCoverage(ticker, assignment["search_from"], assignment["search_to"],
                                            "BLOCKED", (), ("verified identity unavailable",), ())
            return OfficialPreflight("SAFE_UNKNOWN", self.registry, window, {},
                                     {"http_requests": 0, "response_bytes": 0,
                                      "transports": [], "fetch_failures": 0}, window.gaps)
        urls = self.family_urls.get(ticker, {})
        required = {"news", "ir", "announcements"}
        results: dict[str, OfficialSourceResolution] = {}
        gaps = []
        for family in sorted(required):
            url = urls.get(family)
            if not url:
                gaps.append(f"{family}: exact official URL unavailable")
                continue
            result = self._resolve(identity, url)
            results[family] = result
            if result.access_status != "VERIFIED":
                gaps.append(f"{family}: {result.failure_reason or result.access_status}")
        state = "COMPLETE" if not gaps and set(results) == required else (
            "BLOCKED" if not results else "PARTIAL")
        window = validate_window_coverage(OfficialWindowCoverage(
            ticker, assignment["search_from"], assignment["search_to"], state,
            tuple(sorted(results)), tuple(gaps),
            tuple(row.body_hash for row in results.values() if row.body_hash)))
        cards = []
        for family, result in results.items():
            if result.access_status != "VERIFIED" or result.normalized_content is None:
                continue
            body = result.normalized_content
            cards.append({"url": body.canonical_url, "source_type": "company_official",
                          "title": body.title, "published_at": None,
                          "excerpt": body.bounded_excerpt, "supports": f"official {family} window",
                          "query_id": f"official-preflight-{family}",
                          "verification_status": "VERIFIED"})
        transports = sorted({row.fetch_method for row in results.values()})
        telemetry = {"http_requests": sum(row.fetch_method != "repository_existing" for row in results.values()),
                     "response_bytes": None, "transports": transports,
                     "fetch_failures": sum(row.access_status != "VERIFIED" for row in results.values())}
        model_identity = {"verified_ticker": ticker,
                          "source_url": identity.verified_official_url,
                          "evidence": "Stable VERIFIED CompanyIdentityRegistry evidence reused."}
        status = "READY" if state == "COMPLETE" else "SAFE_UNKNOWN"
        return OfficialPreflight(status, self.registry, window,
                                 {"company_identity": model_identity,
                                  "evidence_cards": cards, "telemetry": telemetry},
                                 telemetry, tuple(gaps),
                                 lambda **kwargs: self.fetch_document(identity, **kwargs))

    def fetch_document(self, identity, *, title: str, url: str,
                       tdnet_covered: bool = False) -> dict[str, Any]:
        decision = classify_news_monitor_scope(title, url, tdnet_covered=tdnet_covered)
        if not decision["body_fetch_required"]:
            return {**decision, "provenance_state": "URL_DISCOVERED",
                    "http_requests": 0, "resolution": None}
        result = self._resolve(identity, url)
        state = "BODY_VERIFIED" if result.access_status == "VERIFIED" else "ACCESS_FAILED"
        return {**decision, "provenance_state": state,
                "http_requests": int(result.fetch_method != "repository_existing"),
                "resolution": result}

    def resolve_delegated(self, *, resolver: OfficialSourceResolver, identity,
                          exact_embedded_url: str) -> OfficialSourceResolution:
        """Resolve one exact delegated URL using its separately policy-bound resolver."""
        return resolver.resolve(identity, exact_embedded_url, FetchPolicy(max_redirects=0))


class ConfiguredOfficialProvider:
    """Create company-scoped resolver/journal instances from frozen metadata."""
    def __init__(self, *, registry_path: Path, plan_path: Path,
                 allow_live_http: bool) -> None:
        self.registry = CompanyIdentityRegistry.load(registry_path)
        plan = _read(plan_path)
        self.family_urls = plan.get("family_urls", {})
        self.repository_artifacts = plan.get("repository_artifacts", {})
        self.allow_live_http = allow_live_http

    def prepare(self, assignment: Mapping[str, Any], company_dir: Path) -> OfficialPreflight:
        artifacts = {}
        for url, record in self.repository_artifacts.items():
            path = Path(record["path"])
            artifacts[url] = (path.read_bytes(), record["mime_type"], int(record.get("status", 200)))
        repository = OfficialSourceResolver(RepositoryExistingTransport(artifacts))
        live = None
        if self.allow_live_http:
            store = DurableArtifactStore.prepare(company_dir / "official-http")
            journal = HttpFetchJournal(store, run_id=assignment["assignment_id"],
                                       company=assignment["ticker"])
            live = OfficialSourceResolver(WindowsSchannelTransport(
                journal=journal, retain_raw_html=True))
        integration = OfficialResolverIntegration(
            self.registry, repository, live, self.family_urls)
        return integration.prepare(assignment, company_dir)


def execute_lightweight_company(root: Path, company_dir: Path,
                                assignment: dict[str, Any],
                                official: OfficialPreflight) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use the same canonical execution path as the historical Phase1 harness."""
    experiment = company_dir.parents[1]
    response, telemetry = lightweight.run_company(
        root, experiment, assignment, official_context=official.model_context)
    response = apply_registry_identity(response, official.registry, assignment)
    document_states = []
    additional_http = 0
    for document in response["strategy_telemetry"].get("documents", []):
        title, url = str(document.get("title") or ""), str(document.get("url") or "")
        if document.get("body_status") == "VERIFIED":
            document_states.append({"url": url, "state": "BODY_VERIFIED",
                                    "state_history": ["DISCOVERED_IN_INDEX", "URL_DISCOVERED",
                                                      "BODY_VERIFIED"],
                                    "authority": "bounded_research_evidence"})
            continue
        if official.document_fetcher is None:
            document_states.append({"url": url, "state": "DISCOVERED_IN_INDEX",
                                    "state_history": ["DISCOVERED_IN_INDEX"],
                                    "authority": "no_exact_fetcher"})
            continue
        fetched = official.document_fetcher(
            title=title, url=url,
            tdnet_covered=document.get("scope_label") == "OUT_OF_SCOPE_TDNET")
        additional_http += fetched["http_requests"]
        state = fetched["provenance_state"]
        history = ["DISCOVERED_IN_INDEX", "URL_DISCOVERED"]
        if fetched["body_fetch_required"]:
            history.extend(["BODY_FETCH_ATTEMPTED", state])
        document_states.append({"url": url, "state": state,
                                "state_history": history,
                                "scope_label": fetched["scope_label"]})
        if not fetched["body_fetch_required"]:
            document.update(scope_label=fetched["scope_label"],
                            scope_reason=fetched["scope_reason"], material=False,
                            body_status="SKIPPED", incremental_information=False,
                            incremental_information_evidence=None)
            continue
        result = fetched["resolution"]
        if result.access_status == "VERIFIED" and result.normalized_content is not None:
            document.update(body_status="VERIFIED", final_url=result.final_url,
                            attempt_evidence=(
                                f"Python OfficialSourceResolver: {result.fetch_method}; "
                                f"sha256={result.body_hash}"))
            provenance = response["strategy_telemetry"].setdefault("source_provenance", [])
            if not any(lightweight.canonicalize_url(str(row.get("url") or "")) ==
                       lightweight.canonicalize_url(result.final_url) for row in provenance):
                provenance.append({"url": result.final_url, "source_type": "company_official",
                                   "content_verified": True,
                                   "evidence_excerpt": result.normalized_content.bounded_excerpt})
        else:
            document.update(body_status="INACCESSIBLE",
                            attempt_evidence=result.failure_reason or result.access_status)
    lightweight._atomic_json(company_dir / "official-document-provenance.json", {
        "states": document_states, "index_only_promoted": False})
    response = lightweight.apply_source_count_authority(response)
    evidence = lightweight._read_json(company_dir / "evidence-state.json")
    discovery = lightweight._read_json(company_dir / "workers" / "company-web" / "last-message.json")
    response, _ = lightweight.apply_coverage_authority(
        response, evidence["search_record_registry"], discovery, evidence["cards"])
    payload, strategy = response["research_result"], response["strategy_telemetry"]
    lightweight.strategy_v5.validate_generated_response(response)
    lightweight.validate_payload(payload)
    lightweight.strategy_v5.validate(
        strategy, assignment, payload, identity_registry=official.registry,
        official_window_coverage=official.window)
    telemetry.update(validation_status="PASS",
                     canonical_schema_sha256=lightweight.strategy_v5.canonical_schema_sha256())
    telemetry.setdefault("non_llm", {}).update(
        http_requests=official.telemetry.get("http_requests", 0) + additional_http)
    return response, telemetry


def exhaustive_auditor(response: dict[str, Any], company_dir: Path) -> dict[str, Any]:
    """Run the independent semantic audit over saved per-company authorities."""
    from tools.company_news_phase18j_offline_replay import exhaustive_audit
    evidence = _read(company_dir / "evidence-state.json")
    discovery = _read(company_dir / "workers" / "company-web" / "last-message.json")
    return exhaustive_audit(response, evidence["search_record_registry"], discovery)


def prepare_run(cohort: FrozenCohort, run_dir: Path, *, search_from: str,
                search_to: str, approved_company_count: int,
                hard_token_gate: int = DEFAULT_HARD_TOKEN_GATE,
                preferred_token_gate: int = DEFAULT_PREFERRED_TOKEN_GATE,
                identity_registry_path: Path | None = None,
                official_plan_path: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if (run_dir / "manifest.json").exists() or (run_dir / "queue.json").exists():
        raise MultiRunnerError("run artifact collision")
    assignments = []
    for company in cohort.companies:
        assignment = company.assignment(search_from=search_from, search_to=search_to)
        assignments.append({**assignment, "status": "PENDING"})
    manifest = {
        "schema_version": SCHEMA, "state": "PREPARED", "created_at": _now(),
        "cohort_path": str(cohort.path), "cohort_file_sha256": cohort.file_sha256,
        "cohort_sha256": cohort.cohort_sha256,
        "universe_order_sha256": cohort.universe_order_sha256,
        "approved_company_count": approved_company_count,
        "ordered_tickers": [row.security_code for row in cohort.companies],
        "hard_token_gate": hard_token_gate, "preferred_token_gate": preferred_token_gate,
        "resume_approved": False, "production_routing": False,
        "production_write_forbidden": True, "automatic_retry": False,
    }
    if identity_registry_path is not None or official_plan_path is not None:
        if identity_registry_path is None or official_plan_path is None:
            raise MultiRunnerError("identity registry and official plan must be frozen together")
        identity_registry_path, official_plan_path = (
            identity_registry_path.resolve(), official_plan_path.resolve())
        identity_registry, official_plan = validate_frozen_inputs(
            cohort, identity_registry_path, official_plan_path)
        manifest.update(
            identity_registry_path=str(identity_registry_path),
            identity_registry_logical_sha256=_canonical_sha(identity_registry.to_dict()),
            identity_registry_sha256=_sha_bytes(identity_registry_path.read_bytes()),
            official_plan_path=str(official_plan_path),
            official_plan_logical_sha256=_canonical_sha(official_plan),
            official_plan_sha256=_sha_bytes(official_plan_path.read_bytes()),
        )
    _atomic(run_dir / "manifest.json", manifest)
    _atomic(run_dir / "queue.json", {"schema_version": SCHEMA, "assignments": assignments})
    return manifest


def load_frozen_official_config(manifest: Mapping[str, Any]) -> tuple[Path, Path]:
    """Reopen the exact offline official inputs frozen when the run was prepared."""
    required = (
        "identity_registry_path", "identity_registry_logical_sha256", "identity_registry_sha256",
        "official_plan_path", "official_plan_logical_sha256", "official_plan_sha256",
    )
    if any(not manifest.get(key) for key in required):
        raise CohortIntegrityError("frozen official preflight metadata is missing")
    registry_path = Path(str(manifest["identity_registry_path"]))
    plan_path = Path(str(manifest["official_plan_path"]))
    if not registry_path.is_file() or not plan_path.is_file():
        raise CohortIntegrityError("frozen official preflight metadata changed")
    registry = CompanyIdentityRegistry.load(registry_path)
    plan = _read(plan_path)
    if (_sha_bytes(registry_path.read_bytes()) != manifest["identity_registry_sha256"]
            or _canonical_sha(registry.to_dict()) != manifest["identity_registry_logical_sha256"]
            or _sha_bytes(plan_path.read_bytes()) != manifest["official_plan_sha256"]
            or _canonical_sha(plan) != manifest["official_plan_logical_sha256"]):
        raise CohortIntegrityError("frozen official preflight metadata changed")
    return registry_path, plan_path


class FrozenCohortController:
    def __init__(self, *, root: Path, run_dir: Path, official: OfficialPreflightProvider,
                 executor: CompanyExecutor, auditor: Callable[[dict[str, Any], Path], dict[str, Any]]) -> None:
        self.root, self.run_dir = root.resolve(), run_dir.resolve()
        self.official, self.executor, self.auditor = official, executor, auditor

    def _load(self) -> tuple[dict[str, Any], dict[str, Any], FrozenCohort]:
        manifest, queue = _read(self.run_dir / "manifest.json"), _read(self.run_dir / "queue.json")
        if manifest.get("schema_version") != SCHEMA or manifest.get("state") not in RUN_STATES:
            raise MultiRunnerError("invalid run manifest")
        cohort = FrozenCohort.load(Path(manifest["cohort_path"]),
            approved_company_count=manifest["approved_company_count"],
            max_companies=manifest["approved_company_count"],
            expected_cohort_sha256=manifest["cohort_sha256"])
        if cohort.file_sha256 != manifest["cohort_file_sha256"]:
            raise CohortIntegrityError("cohort file changed after prepare")
        if [row.security_code for row in cohort.companies] != manifest["ordered_tickers"]:
            raise CohortIntegrityError("cohort order changed after prepare")
        return manifest, queue, cohort

    def _stop(self, manifest: dict[str, Any], reason: str, *, failure: bool = False) -> None:
        manifest.update(state="FAILED" if failure else "STOPPED", stop_reason=reason,
                        stopped_at=_now(), resume_approved=False)
        _atomic(self.run_dir / "manifest.json", manifest)

    def _advance_manifest(self, manifest: dict[str, Any], queue: dict[str, Any]) -> None:
        if any(row["status"] == "PENDING" for row in queue["assignments"]):
            manifest["state"] = "PREPARED"
        else:
            manifest.update(state="COMPLETED", completed_at=_now())
        _atomic(self.run_dir / "manifest.json", manifest)

    def run_next(self) -> dict[str, Any]:
        manifest, queue, _ = self._load()
        if manifest["state"] in {"STOPPED", "FAILED"} and not manifest.get("resume_approved"):
            raise ResumeRequired("explicit resume approval is required")
        if manifest["state"] == "COMPLETED":
            return {"status": "EMPTY"}
        guard = self.run_dir / "guard.json"
        assignment = next((row for row in queue["assignments"] if row["status"] == "PENDING"), None)
        if assignment is None:
            manifest.update(state="COMPLETED", completed_at=_now())
            _atomic(self.run_dir / "manifest.json", manifest)
            return {"status": "COMPLETED"}
        if any(row["status"] == "RUNNING" for row in queue["assignments"]):
            raise MultiRunnerError("running assignment exists without guard")
        run_token = secrets.token_hex(32)
        _create_exclusive(guard, {
            "schema_version": SCHEMA, "assignment_id": assignment["assignment_id"],
            "run_token": run_token, "created_at": _now()})
        assignment.update(status="RUNNING", run_token=run_token, started_at=_now())
        manifest["state"] = "RUNNING"
        _atomic(self.run_dir / "queue.json", queue); _atomic(self.run_dir / "manifest.json", manifest)
        company_dir = self.run_dir / "companies" / f"{assignment['position']:02d}-{assignment['ticker']}"
        try:
            if company_dir.exists():
                raise MultiRunnerError("artifact collision")
            company_dir.mkdir(parents=True)
            runtime = {key: assignment[key] for key in RUNTIME_FIELDS}
            prior = [row for row in queue["assignments"] if row["position"] < assignment["position"]]
            runtime_text = json.dumps(runtime, ensure_ascii=False, sort_keys=True)
            leakage = [row["ticker"] for row in prior
                       if json.dumps(row["ticker"], ensure_ascii=False) in runtime_text
                       or json.dumps(row["company_name"], ensure_ascii=False) in runtime_text]
            _atomic(company_dir / "runtime-input-audit.json", {
                "allowed_fields": list(RUNTIME_FIELDS), "runtime_input_sha256": _canonical_sha(runtime),
                "previous_company_marker_hits": leakage,
                "previous_company_source_marker_hits": [],
                "previous_company_payload_included": False,
            })
        except Exception as exc:
            assignment.update(status="FAILED", error_type=type(exc).__name__,
                              error_message=str(exc))
            _atomic(self.run_dir / "queue.json", queue)
            guard.unlink(missing_ok=True)
            self._stop(manifest, f"{type(exc).__name__}: {exc}", failure=True)
            return {"status": "FAILED", "reason": str(exc)}
        try:
            official = self.official.prepare(runtime, company_dir)
            _atomic(company_dir / "official-preflight.json", {
                "status": official.status, "official_window": asdict(official.window),
                "telemetry": official.telemetry, "gaps": list(official.gaps)})
            if official.status == "SAFE_UNKNOWN":
                assignment.update(status="SAFE_UNKNOWN", completed_at=_now(),
                                  result_type="UNKNOWN", remaining_gaps=list(official.gaps),
                                  total_tokens=0, worker_count=0, web_calls=0)
                _atomic(self.run_dir / "queue.json", queue)
                guard.unlink(missing_ok=True)
                self._advance_manifest(manifest, queue)
                return {"status": "SAFE_UNKNOWN", "assignment": assignment}
            response, telemetry = self.executor(self.root, company_dir, runtime, official)
            audit = self.auditor(response, company_dir)
            _atomic(company_dir / "exhaustive-semantic-audit.json", audit)
            if audit.get("status") != "PASS":
                raise MultiRunnerError("semantic architecture gate failed")
            total = telemetry.get("total_tokens")
            if type(total) is not int:
                raise MultiRunnerError("exact total token telemetry missing")
            if leakage:
                raise MultiRunnerError("previous-company runtime marker leakage")
            _atomic(company_dir / "canonical-response.json", response)
            _atomic(company_dir / "token-telemetry.json", telemetry)
            assignment.update(status="COMPLETED", completed_at=_now(),
                              result_type="news_present" if response["research_result"]["items"] else "no_news",
                              total_tokens=total, worker_count=telemetry.get("worker_count"),
                              web_calls=telemetry.get("web_call_count"),
                              first_model_input_tokens=(telemetry.get("workers") or [{}])[0].get("input_tokens"),
                              cached_input_tokens=telemetry.get("cached_input_tokens"))
            _atomic(self.run_dir / "queue.json", queue)
            guard.unlink(missing_ok=True)
            if total > manifest["hard_token_gate"]:
                self._stop(manifest, f"token hard gate exceeded: {total}")
            else:
                manifest["preferred_gate_warning"] = total > manifest["preferred_token_gate"]
                self._advance_manifest(manifest, queue)
            return {"status": assignment["status"], "assignment": assignment}
        except Exception as exc:
            assignment.update(status="FAILED", completed_at=_now(), error_type=type(exc).__name__,
                              error_message=str(exc))
            _atomic(self.run_dir / "queue.json", queue)
            guard.unlink(missing_ok=True)
            self._stop(manifest, f"{type(exc).__name__}: {exc}", failure=True)
            return {"status": "FAILED", "assignment": assignment}

    def approve_resume(self) -> dict[str, Any]:
        manifest, _, _ = self._load()
        if manifest["state"] != "STOPPED":
            raise ResumeRequired("only a stopped run can receive explicit resume approval")
        manifest["resume_approved"] = True
        manifest["resume_approved_at"] = _now()
        _atomic(self.run_dir / "manifest.json", manifest)
        return manifest


def approve_resume(run_dir: Path) -> dict[str, Any]:
    manifest = _read(run_dir / "manifest.json")
    if manifest.get("state") != "STOPPED":
        raise ResumeRequired("only a stopped run can receive explicit resume approval")
    manifest["resume_approved"] = True
    manifest["resume_approved_at"] = _now()
    _atomic(run_dir / "manifest.json", manifest)
    return manifest


def aggregate(run_dir: Path) -> dict[str, Any]:
    manifest, queue = _read(run_dir / "manifest.json"), _read(run_dir / "queue.json")
    rows = queue["assignments"]
    completed = [row for row in rows if row["status"] == "COMPLETED"]
    tokens = [row["total_tokens"] for row in completed]
    return {
        "state": manifest["state"], "completed": len(completed),
        "safe_unknown": sum(row["status"] == "SAFE_UNKNOWN" for row in rows),
        "failed": sum(row["status"] == "FAILED" for row in rows),
        "pending": sum(row["status"] == "PENDING" for row in rows),
        "total_tokens": sum(tokens), "mean_tokens": statistics.mean(tokens) if tokens else None,
        "median_tokens": statistics.median(tokens) if tokens else None,
        "max_tokens": max(tokens) if tokens else None,
        "ordered_results": [{key: row.get(key) for key in (
            "position", "ticker", "status", "result_type", "total_tokens")} for row in rows],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--cohort-file", type=Path, required=True)
    prepare.add_argument("--run-dir", type=Path, required=True)
    prepare.add_argument("--cohort-sha256", required=True)
    prepare.add_argument("--approved-company-count", type=int, required=True)
    prepare.add_argument("--max-companies", type=int, required=True)
    prepare.add_argument("--search-from", required=True)
    prepare.add_argument("--search-to", required=True)
    prepare.add_argument("--identity-registry", type=Path, required=True)
    prepare.add_argument("--official-plan", type=Path, required=True)
    bootstrap = sub.add_parser("bootstrap-inputs")
    bootstrap.add_argument("--cohort-file", type=Path, required=True)
    bootstrap.add_argument("--cohort-sha256", required=True)
    bootstrap.add_argument("--approved-company-count", type=int, required=True)
    bootstrap.add_argument("--max-companies", type=int, required=True)
    bootstrap.add_argument("--company-master-db", type=Path, required=True)
    bootstrap.add_argument("--repository-artifacts-root", type=Path, required=True)
    bootstrap.add_argument("--output-dir", type=Path, required=True)
    bootstrap_mode = bootstrap.add_mutually_exclusive_group(required=True)
    bootstrap_mode.add_argument("--offline-only", action="store_true")
    bootstrap_mode.add_argument("--windows-schannel", action="store_true")
    bootstrap.add_argument("--max-http-requests", type=int, default=24)
    bootstrap.add_argument("--prepare-run-dir", type=Path)
    bootstrap.add_argument("--search-from", default="2026-08-25")
    bootstrap.add_argument("--search-to", default="2026-08-31")
    forensic = sub.add_parser("forensic-identity-authority")
    forensic.add_argument("--cohort-file", type=Path, required=True)
    forensic.add_argument("--cohort-sha256", required=True)
    forensic.add_argument("--approved-company-count", type=int, required=True)
    forensic.add_argument("--max-companies", type=int, required=True)
    forensic.add_argument("--company-master-db", type=Path, required=True)
    forensic.add_argument("--repository-artifacts-root", type=Path, required=True)
    forensic.add_argument("--phase22e-report", type=Path)
    forensic.add_argument("--output", type=Path, required=True)
    report = sub.add_parser("aggregate"); report.add_argument("--run-dir", type=Path, required=True)
    run_next = sub.add_parser("run-next")
    run_next.add_argument("--root", type=Path, default=Path.cwd())
    run_next.add_argument("--run-dir", type=Path, required=True)
    run_next.add_argument("--allow-live-http", action="store_true")
    resume = sub.add_parser("approve-resume")
    resume.add_argument("--run-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command in {"bootstrap-inputs", "forensic-identity-authority"}:
        cohort = FrozenCohort.load(args.cohort_file,
            approved_company_count=args.approved_company_count,
            max_companies=args.max_companies,
            expected_cohort_sha256=args.cohort_sha256)
        if args.command == "forensic-identity-authority":
            value = generate_identity_resolution_plan(
                cohort, company_master_db=args.company_master_db,
                repository_artifacts_root=args.repository_artifacts_root,
                phase22e_report_path=args.phase22e_report, output_path=args.output)
        elif args.offline_only:
            if args.prepare_run_dir is not None:
                raise MultiRunnerError("offline-only incomplete bootstrap cannot prepare a run")
            value = bootstrap_inputs(
                cohort, company_master_db=args.company_master_db,
                repository_artifacts_root=args.repository_artifacts_root,
                output_dir=args.output_dir, offline_only=True)
        else:
            value = bootstrap_identities_schannel(
                cohort, company_master_db=args.company_master_db,
                repository_artifacts_root=args.repository_artifacts_root,
                output_dir=args.output_dir, max_http_requests=args.max_http_requests,
                prepare_run_dir=args.prepare_run_dir,
                search_from=args.search_from, search_to=args.search_to)
    elif args.command == "prepare":
        cohort = FrozenCohort.load(args.cohort_file,
            approved_company_count=args.approved_company_count,
            max_companies=args.max_companies,
            expected_cohort_sha256=args.cohort_sha256)
        value = prepare_run(cohort, args.run_dir, search_from=args.search_from,
                            search_to=args.search_to,
                            approved_company_count=args.approved_company_count,
                            identity_registry_path=args.identity_registry,
                            official_plan_path=args.official_plan)
    elif args.command == "aggregate":
        value = aggregate(args.run_dir)
    elif args.command == "approve-resume":
        value = approve_resume(args.run_dir)
    else:
        manifest = _read(args.run_dir / "manifest.json")
        registry_path, plan_path = load_frozen_official_config(manifest)
        provider = ConfiguredOfficialProvider(
            registry_path=registry_path, plan_path=plan_path,
            allow_live_http=args.allow_live_http)
        value = FrozenCohortController(
            root=args.root, run_dir=args.run_dir, official=provider,
            executor=execute_lightweight_company, auditor=exhaustive_auditor,
        ).run_next()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if args.command == "bootstrap-inputs" and value.get("status") != "READY":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
