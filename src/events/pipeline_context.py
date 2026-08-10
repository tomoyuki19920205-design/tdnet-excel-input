import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

@dataclass
class DisclosureIdentity:
    ticker: str
    company_name: Optional[str] = None
    disc_no: Optional[str] = None
    tdnet_pdf_id: Optional[str] = None
    source_url: Optional[str] = None
    pdf_url: Optional[str] = None
    xbrl_zip_path: Optional[str] = None
    dedupe_key: Optional[str] = None
    filing_id: Optional[str] = None
    archive_path: Optional[str] = None
    identity_status: str = "PENDING"
    
    @classmethod
    def create_and_normalize(cls, ticker: str, disc_no: Optional[str] = None, tdnet_pdf_id: Optional[str] = None, source_url: Optional[str] = None) -> 'DisclosureIdentity':
        """
        Normalize IDs and generate dedupe_key and filing_id.
        - dedupe_key is preferred as tdnet_pdf_id, then ticker_discno.
        - filing_id is preferred as tdnet_pdf_id, then disc_no.
        """
        # Normalize tdnet_pdf_id from source_url if not provided
        if not tdnet_pdf_id and source_url:
            m = re.search(r'([a-zA-Z0-9]+)\.pdf$', source_url)
            if m:
                tdnet_pdf_id = m.group(1)

        dedupe_key = tdnet_pdf_id
        if not dedupe_key and disc_no:
            dedupe_key = f"{ticker}_{disc_no}"
            
        filing_id = tdnet_pdf_id or disc_no
        
        return cls(
            ticker=ticker,
            disc_no=disc_no,
            tdnet_pdf_id=tdnet_pdf_id,
            source_url=source_url,
            dedupe_key=dedupe_key,
            filing_id=filing_id,
            identity_status="NORMALIZED"
        )


@dataclass
class FilingPeriodEvidence:
    title_quarter: Optional[str] = None
    title_fiscal_year: Optional[str] = None
    trusted_quarter: Optional[str] = None
    trusted_fiscal_year: Optional[str] = None
    canonical_period: Optional[str] = None
    context_ref: Optional[str] = None
    context_start: Optional[str] = None
    context_end: Optional[str] = None
    expected_context_end: Optional[str] = None
    diff_days: Optional[int] = None
    date_guard_status: str = "UNVERIFIED"

    def resolve_period(self, master_period: Optional[str] = None) -> None:
        """
        Resolve the final canonical_period.
        If a master_period (like '2026-12-20') is provided from JQuants/Company Master,
        it should override the naive end of month calculation to prevent period mismatch.
        """
        if master_period:
            self.canonical_period = master_period
            self.date_guard_status = "RESOLVED_FROM_MASTER"
        else:
            self.canonical_period = self.expected_context_end
            self.date_guard_status = "RESOLVED_NAIVE"


@dataclass
class EarningsExtractionEvidence:
    metric: str
    value: Optional[float]
    tag_name: Optional[str] = None
    qname: Optional[str] = None
    namespace: Optional[str] = None
    context_ref: Optional[str] = None
    unit: Optional[str] = None
    scale: Optional[int] = None
    source_file: Optional[str] = None
    extraction_source: Optional[str] = None
    priority: int = 99
    fallback_used: bool = False


@dataclass
class CanonicalWritePlan:
    ticker: str
    period: str
    quarter: str
    metric: str
    value: float
    unit: str
    source: str
    filing_id: Optional[str] = None
    source_row_key: Optional[str] = None
    write_allowed: bool = False
    block_reason: Optional[str] = None

    @staticmethod
    def generate_source_row_key(ticker: str, period: str, quarter: str, metric: str, source: str, filing_id: Optional[str] = None) -> str:
        fid = filing_id if filing_id else ""
        return f"cf|{ticker}|{period}|{quarter}|{metric}|{source}|{fid}"
        
    @staticmethod
    def is_viewer_supported_source(source: str) -> bool:
        """
        Viewer API (api_latest_financials_canonical) historically filters or prioritizes
        certain sources (e.g. jquants, jquants_nxf, legacy_excel, jquants_forecast_fy).
        tdnet may be ignored by the view or replaced if its priority/format is mismatched.
        """
        supported = {
            'jquants', 'jquants_nxf', 'jquants_forecast_fy',
            'legacy_excel', 'tdnet_xbrl',
        }
        return source in supported

    def validate_and_prepare(self):
        """
        Determine if the plan is safe and effective to write.
        Normalizes source if needed, or flags unsupported sources.
        """
        self.source_row_key = self.generate_source_row_key(
            self.ticker, self.period, self.quarter, self.metric, self.source, self.filing_id
        )
        
        if not self.is_viewer_supported_source(self.source):
            self.block_reason = f"Source '{self.source}' may not be visible in Viewer API. Consider normalizing to 'jquants'."
            self.write_allowed = False
        else:
            self.write_allowed = True
