"""Offline reconstruction and parsing for explicitly delegated JSONP listings."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode, urlsplit


class DelegatedListingError(ValueError):
    pass


_CALLBACK = re.compile(
    r"^XJStorageClass\.jsonpReq\.jsonpCallback_[A-Za-z0-9_]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ListingParameter:
    name: str
    value: str
    source: str


@dataclass(frozen=True)
class ReconstructedJsonpRequest:
    endpoint: str
    parameters: tuple[ListingParameter, ...]
    final_url: str
    method: str
    encoding: str
    callback: str
    endpoint_source_sha256: str
    parameter_source_sha256: str
    category_map: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["parameters"] = [asdict(item) for item in self.parameters]
        value["category_map"] = [
            {"label": label, "doctype": doctype}
            for label, doctype in self.category_map
        ]
        return value


def _text(value: bytes | str) -> str:
    return value.decode("utf-8-sig") if isinstance(value, bytes) else value


def _one(pattern: str, text: str, field: str) -> str:
    found = re.findall(pattern, text, flags=re.MULTILINE)
    if len(found) != 1:
        raise DelegatedListingError(f"{field} must be derived exactly once from JavaScript")
    return found[0]


def _encode_uri_component_query(parameters: Sequence[tuple[str, str]]) -> str:
    return urlencode(parameters, doseq=False, safe="-_.!~*'()", quote_via=quote)


def reconstruct_xj_jsonp_request(
    lib_js: bytes | str,
    page_js: bytes | str,
    *,
    callback: str,
) -> ReconstructedJsonpRequest:
    """Reconstruct one XJ request only from the two retained JavaScript resources."""
    if not _CALLBACK.fullmatch(callback):
        raise DelegatedListingError("callback does not satisfy the retained XJ JSONP contract")
    lib, page = _text(lib_js), _text(page_js)
    endpoint = _one(r'^\s*url\s*=\s*"(https://[^\"]+)"\s*;', lib, "endpoint")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password):
        raise DelegatedListingError("endpoint must be public HTTPS without credentials")
    if '_buildQuery(t)' not in lib or 'document.createElement("script")' not in lib:
        raise DelegatedListingError("GET/JSONP construction evidence is incomplete")
    company = _one(r"\bcompany:\s*['\"]([^'\"]+)['\"]\s*[,}]", page, "company")
    categories = tuple(re.findall(
        r"\{label:\s*'([^']+)',\s*doctype:\s*'([0-9,]+)'\}", page))
    if not categories or "docInfoList.map(docInfo => docInfo.doctype).join(',')" not in page:
        raise DelegatedListingError("doctype aggregation is not derived from JavaScript")
    if "XJStorage.getData({doctype})" not in page:
        raise DelegatedListingError("listing call is not derived from JavaScript")
    if not re.search(r'output:\s*"json"', lib):
        raise DelegatedListingError("output is unknown")
    if not re.search(r'len:\s*"0"', lib):
        raise DelegatedListingError("len is unknown")
    if not re.search(r'callback:\s*`XJStorageClass\.jsonpReq\.\$\{r\}`', lib):
        raise DelegatedListingError("callback parameter is unknown")
    doctype = ",".join(value for _, value in categories)
    parameter_values = (
        ("company", company),
        ("doctype", doctype),
        ("output", "json"),
        ("len", "0"),
        ("callback", callback),
    )
    parameters = tuple(ListingParameter(name, value, "DERIVED_FROM_JS")
                       for name, value in parameter_values)
    return ReconstructedJsonpRequest(
        endpoint=endpoint,
        parameters=parameters,
        final_url=endpoint + "?" + _encode_uri_component_query(parameter_values),
        method="GET",
        encoding="encodeURIComponent-compatible UTF-8 percent encoding",
        callback=callback,
        endpoint_source_sha256=hashlib.sha256(lib.encode("utf-8")).hexdigest(),
        parameter_source_sha256=hashlib.sha256(page.encode("utf-8")).hexdigest(),
        category_map=categories,
    )


def parse_jsonp(payload: bytes | str, *, callback: str) -> Mapping[str, Any]:
    """Remove one exact JSONP wrapper and parse JSON without executing JavaScript."""
    if not _CALLBACK.fullmatch(callback):
        raise DelegatedListingError("invalid expected callback")
    text = _text(payload).strip()
    match = re.fullmatch(re.escape(callback) + r"\s*\(\s*(.*)\s*\)\s*;?", text, re.DOTALL)
    if not match:
        raise DelegatedListingError("JSONP callback mismatch or trailing JavaScript")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DelegatedListingError("JSONP payload is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise DelegatedListingError("JSONP root must be an object")
    items = value.get("items")
    if not isinstance(items, list):
        raise DelegatedListingError("JSONP object must contain an items array")
    return value


def _publication_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})(?:\s+\d{2}:\d{2}:\d{2})?", value.strip())
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def build_xj_document_inventory(
    payload: Mapping[str, Any],
    *,
    category_map: Sequence[tuple[str, str]],
    window_from: date,
    window_to: date,
) -> list[dict[str, Any]]:
    if window_from > window_to:
        raise DelegatedListingError("invalid fixed window")
    code_to_category = {
        code: label
        for label, values in category_map
        for code in values.split(",")
    }
    inventory: list[dict[str, Any]] = []
    for position, item in enumerate(payload.get("items", [])):
        if not isinstance(item, Mapping):
            raise DelegatedListingError("listing item must be an object")
        published = _publication_date(item.get("publishDate"))
        if published is None:
            window_class = "DATE_UNKNOWN"
        elif window_from <= published <= window_to:
            window_class = "IN_WINDOW"
        else:
            window_class = "OUT_OF_WINDOW"
        files = item.get("files") if isinstance(item.get("files"), list) else []
        normalized_files = []
        candidates = []
        for file_value in files:
            if not isinstance(file_value, Mapping):
                continue
            normalized = {
                "type": file_value.get("type"),
                "url": file_value.get("url"),
                "size": file_value.get("size"),
                "page": file_value.get("page"),
            }
            normalized_files.append(normalized)
            if normalized["type"] == "PDF-GENERAL" and isinstance(normalized["url"], str):
                candidates.append(normalized)
        disclosure_code = str(item.get("disclosureCode") or "")
        inventory.append({
            "position": position,
            "publish_date_raw": item.get("publishDate"),
            "publication_date": published.isoformat() if published else None,
            "publication_precision": "DATE_ONLY" if published else "UNKNOWN",
            "fiscal_year_end": item.get("fiscalYearEnd"),
            "title": item.get("title"),
            "disclosure_code": disclosure_code or None,
            "category": code_to_category.get(disclosure_code, "UNKNOWN"),
            "files": normalized_files,
            "canonical_document_candidates": candidates,
            "window_class": window_class,
        })
    return inventory


def summarize_inventory(inventory: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    categories: dict[str, int] = {}
    windows = {"IN_WINDOW": 0, "OUT_OF_WINDOW": 0, "DATE_UNKNOWN": 0}
    for item in inventory:
        category = str(item.get("category") or "UNKNOWN")
        categories[category] = categories.get(category, 0) + 1
        window_class = str(item.get("window_class") or "DATE_UNKNOWN")
        windows[window_class] = windows.get(window_class, 0) + 1
    return {
        "total": len(inventory),
        "category_counts": dict(sorted(categories.items())),
        "in_window": windows["IN_WINDOW"],
        "out_of_window": windows["OUT_OF_WINDOW"],
        "unknown_date": windows["DATE_UNKNOWN"],
    }


def find_inventory_by_date(inventory: Sequence[Mapping[str, Any]], target: date) -> list[dict[str, Any]]:
    return [dict(item) for item in inventory if item.get("publication_date") == target.isoformat()]


def derive_official_window_coverage(*, jsonp_parsed: bool,
                                    inventory_built: bool,
                                    required_listing_gaps: Sequence[str] = ()) -> str:
    if not jsonp_parsed or not inventory_built:
        return "BLOCKED"
    return "PARTIAL" if required_listing_gaps else "COMPLETE"
