"""Canonical storage and validation for TSE 33-sector weekly reports."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = "sector_weekly_v1"
REPORT_TYPE = "sector_weekly"
JST = timezone(timedelta(hours=9))
IMPORTANCE = frozenset({"A+", "A", "B", "C"})
DIRECTIONS = frozenset({"positive", "negative", "mixed", "neutral"})
SOURCE_TYPES = frozenset({
    "company_ir", "government", "regulator", "industry_association", "news", "market_data", "other"
})

SECTORS: tuple[str, ...] = (
    "水産・農林業", "鉱業", "建設業", "食料品", "繊維製品", "パルプ・紙", "化学", "医薬品",
    "石油・石炭製品", "ゴム製品", "ガラス・土石製品", "鉄鋼", "非鉄金属", "金属製品", "機械",
    "電気機器", "輸送用機器", "精密機器", "その他製品", "電気・ガス業", "陸運業", "海運業",
    "空運業", "倉庫・運輸関連業", "情報・通信業", "卸売業", "小売業", "銀行業",
    "証券・商品先物取引業", "保険業", "その他金融業", "不動産業", "サービス業",
)

OUTSIDE_IN_SECTORS = frozenset({
    "鉱業", "石油・石炭製品", "化学", "ゴム製品", "ガラス・土石製品", "鉄鋼", "非鉄金属",
    "機械", "電気機器", "輸送用機器", "精密機器", "海運業", "空運業",
})

OUTSIDE_IN_WORKFLOW = """この業種は海外需給の影響が大きいグローバル業種です。日本企業名や国内ニュースから検索を始めず、必ず次の順序でoutside-in調査を行ってください。
1. 世界の価格・需給・在庫・設備投資・政策・規制を横断探索する
2. 海外大手企業の決算、CAPEX、生産計画、稼働率、受注、値上げ、顧客認定を確認する
3. 今週変化した重要ドライバー候補を抽出する
4. 日本企業名が書かれていない海外材料も含め、日本上場企業のどの事業へ波及するかmappingする
5. 数量、単価、稼働率、感応度等から売上・営業利益への影響を可能な範囲で定量化する
6. 会社計画・市場予想・株価への織り込みを評価する
7. 仮説が崩れる反証を確認する"""

MINING_NONFERROUS_PLAYBOOK = """【鉱業・非鉄金属プレイブック】
毎週、金・銀・銅だけで終わらず、原油、天然ガス、LNG、一般炭、原料炭、ウラン、鉄鉱石、銅、金、銀、亜鉛、鉛、アルミ、ニッケル、錫、リチウム、コバルト、マンガン、モリブデン、バナジウム、レアアース、アンチモン、タングステン、ガリウム、ゲルマニウム、チタン、タンタル、ニオブ、プラチナ・パラジウム等のPGM、その他日本上場企業に利益感応度があるマイナー金属をスクリーニングしてください。
日本企業へのmappingへ進む前に、少なくとも次の5群を別々の検索queryで確認してください。(1) エネルギー・バルク、(2) LME等のベースメタル、(3) リチウム・コバルト・マンガン等の電池金属、(4) レアアース・アンチモン・タングステン・ガリウム・ゲルマニウム・チタン・タンタル・ニオブ等の重要/マイナー金属、(5) 金銀・PGM。各群について、採用材料がなくても確認元と「重要変動なし」または不採用理由をmissed_candidatesへ残してください。5群の検索証跡がない状態で調査完了としてはいけません。
さらに、ExxonMobil・Chevron・Shell・QatarEnergy等の海外エネルギー大手と、BHP・Rio Tinto・Vale・Glencore・Freeport-McMoRan・Anglo American・Kazatomprom等の海外鉱山大手から、今週の決算、CAPEX、生産計画、稼働率または供給障害を確認してください。採用材料がない場合も、確認した企業群と不採用理由をmissed_candidatesへ残し、確認した一次資料をsourcesへ含めてください。
候補の目安は5営業日で概ね±8%以上、20営業日で概ね±15%以上、取引所在庫・現物プレミアム・TC/RC・加工賃の急変、鉱山/製錬所の停止・事故・ストライキ、輸出規制・関税・国家備蓄・制裁、海外大手の生産/CAPEX計画変更です。閾値は機械的な除外条件ではなく、値動きが小さくても日本企業の利益感応度が大きければ採用してください。"""

SEMICONDUCTOR_PLAYBOOK = """【半導体・電気機器・精密機器・機械プレイブック】
少なくとも次を分けて海外動向を確認してください。
- メモリ: Samsung Electronics、SK hynix、Micron等
- Foundry: TSMC、Samsung、Intel、UMC、GlobalFoundries、SMIC等
- 製造装置: ASML、Applied Materials、Lam Research、KLA等
- 後工程・テスト: ASE、Amkor、PTI等
- パッケージ基板: 台湾・韓国の主要基板メーカー
- 光通信: Broadcom、Marvell、Semtech、Coherent、Lumentum等
- 受動部品: Samsung Electro-Mechanics、Yageo等
- AIサーバー・ネットワーク: NVIDIA、AMD、主要ODM・スイッチ企業
- HDD、NAND、データセンター電源・冷却・送電設備
メモリはSamsung Electronics、SK hynix、Micronをそれぞれ個別に検索し、3社すべてについて対象期間内の一次資料または「確認したが採用材料なし」という不採用理由をmissed_candidatesへ残してください。会社名を列挙しただけで確認済みとしてはいけません。他の各サブセクターも、採用しない場合は確認先と不採用理由をmissed_candidatesへ残してください。
CAPEX変更は投資される工程・装置、日本企業の受注までの時間差、売上・営業利益感応度、会社計画への包含まで追ってください。「NVIDIAの売上増なので日本の半導体株にプラス」のような粗い結論は禁止します。"""

STEEL_PLAYBOOK = """【鉄鋼プレイブック】
高炉、電炉、特殊鋼、ステンレス、鉄鉱石、原料炭、鉄スクラップ、中国鋼材輸出、HRC等の地域別鋼材価格、メタルスプレッドを分離して分析してください。"""

SHIPPING_PLAYBOOK = """【海運プレイブック】
VLCC・原油タンカー、Product tanker、Dry bulk、Container、LNG船、LPG船、自動車船を別市場として分析してください。一つの運賃指数だけで海運全体を判断してはいけません。"""


def sector_research_context(code: int) -> str:
    """Return mandatory research routing and sector playbooks for one TSE sector."""
    name = sector_name(code)
    sections: list[str] = []
    if name in OUTSIDE_IN_SECTORS:
        sections.append(OUTSIDE_IN_WORKFLOW)
    if name in {"鉱業", "非鉄金属"}:
        sections.append(MINING_NONFERROUS_PLAYBOOK)
    if name in {"機械", "電気機器", "精密機器"}:
        sections.append(SEMICONDUCTOR_PLAYBOOK)
    if name == "鉄鋼":
        sections.append(STEEL_PLAYBOOK)
    if name == "海運業":
        sections.append(SHIPPING_PLAYBOOK)
    if not sections:
        return "この業種は共通調査手順を適用してください。海外材料が日本企業の利益に強く効く場合は国内の小規模材料より優先してください。"
    return "\n\n".join(sections)


class SectorValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WeeklyWindow:
    period_start: datetime
    period_end: datetime
    week_key: str


@dataclass(frozen=True)
class ValidatedSectorReport:
    report: dict[str, Any]


def now_jst() -> datetime:
    return datetime.now(JST)


def iso_seconds(value: datetime) -> str:
    if value.tzinfo is None:
        raise SectorValidationError("datetime must include a timezone")
    return value.isoformat(timespec="seconds")


def parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SectorValidationError(f"{field} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SectorValidationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SectorValidationError(f"{field} must include a timezone")
    return parsed


def weekly_window(value: datetime) -> WeeklyWindow:
    """Return the common prior-Saturday 06:00 through current-Saturday 05:59:59 JST window."""
    local = value.astimezone(JST)
    days_since_saturday = (local.weekday() - 5) % 7
    current_saturday = (local - timedelta(days=days_since_saturday)).date()
    cutoff = datetime.combine(current_saturday, datetime.min.time(), JST) + timedelta(hours=6)
    if local < cutoff:
        cutoff -= timedelta(days=7)
    return WeeklyWindow(
        period_start=cutoff - timedelta(days=7),
        period_end=cutoff - timedelta(seconds=1),
        week_key=(cutoff - timedelta(seconds=1)).date().isoformat(),
    )


def scheduled_sector(value: datetime) -> int | None:
    local = value.astimezone(JST)
    if local.weekday() == 5 and 6 <= local.hour <= 23:
        return local.hour - 5
    if local.weekday() == 6 and 0 <= local.hour <= 14:
        return 19 + local.hour
    return None


def in_retry_window(value: datetime) -> bool:
    local = value.astimezone(JST)
    return local.weekday() == 6 and 15 <= local.hour <= 23


def sector_name(code: int) -> str:
    if not isinstance(code, int) or not 1 <= code <= len(SECTORS):
        raise SectorValidationError("sector_code must be between 1 and 33")
    return SECTORS[code - 1]


def dedupe_key(window: WeeklyWindow, code: int) -> str:
    sector_name(code)
    return f"sector_weekly:{window.week_key}:{code:02d}"


def stable_report_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tse-sector-report:{key}"))


def _text(value: Any, field: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise SectorValidationError(f"{field} must be a string")
    result = value.strip()
    if len(result) < minimum or len(result) > maximum:
        raise SectorValidationError(f"{field} length must be {minimum}..{maximum}")
    return result


def _string_list(value: Any, field: str, minimum: int, maximum: int, item_max: int = 1000) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise SectorValidationError(f"{field} must be an array with {minimum}..{maximum} items")
    return [_text(item, f"{field}[]", item_max) for item in value]


def _http_url(value: Any, field: str) -> str:
    url = _text(value, field, 2048)
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise SectorValidationError(f"{field} must be an absolute http/https URL")
    return url


def validate_report(
    payload: Any,
    *,
    expected_code: int | None = None,
    expected_window: WeeklyWindow | None = None,
) -> ValidatedSectorReport:
    if not isinstance(payload, dict):
        raise SectorValidationError("payload must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("report_type") != REPORT_TYPE:
        raise SectorValidationError("unsupported sector report schema")
    code = payload.get("sector_code")
    name = sector_name(code)
    if expected_code is not None and code != expected_code:
        raise SectorValidationError("sector_code does not match assignment")
    if payload.get("sector_name") != name:
        raise SectorValidationError("sector_name does not match fixed TSE mapping")
    start = parse_datetime(payload.get("period_start"), "period_start")
    end = parse_datetime(payload.get("period_end"), "period_end")
    generated = parse_datetime(payload.get("generated_at"), "generated_at")
    if end < start:
        raise SectorValidationError("period_end precedes period_start")
    if expected_window and (
        start.astimezone(JST) != expected_window.period_start or end.astimezone(JST) != expected_window.period_end
    ):
        raise SectorValidationError("report period does not match the common weekly window")
    importance = payload.get("importance")
    direction = payload.get("direction")
    if importance not in IMPORTANCE:
        raise SectorValidationError("importance must be A+, A, B, or C")
    if direction not in DIRECTIONS:
        raise SectorValidationError("direction must be positive, negative, mixed, or neutral")
    bullets = _string_list(payload.get("summary_bullets"), "summary_bullets", 3, 6, 240)
    full_report = _text(payload.get("full_report_md"), "full_report_md", 100_000, 200)
    watchlist_raw = payload.get("watchlist_companies")
    if not isinstance(watchlist_raw, list) or len(watchlist_raw) > 20:
        raise SectorValidationError("watchlist_companies must be an array of at most 20 items")
    watchlist: list[dict[str, str]] = []
    for item in watchlist_raw:
        if not isinstance(item, dict):
            raise SectorValidationError("watchlist company must be an object")
        ticker = _text(item.get("code"), "watchlist.code", 5)
        if not re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", ticker):
            raise SectorValidationError("watchlist.code must be a TSE ticker")
        item_direction = item.get("direction")
        if item_direction not in DIRECTIONS:
            raise SectorValidationError("watchlist.direction is invalid")
        watchlist.append({"code": ticker, "name": _text(item.get("name"), "watchlist.name", 200), "direction": item_direction})
    watchpoints = _string_list(payload.get("next_week_watchpoints", []), "next_week_watchpoints", 0, 20, 1000)
    missed = _string_list(payload.get("missed_candidates", []), "missed_candidates", 0, 20, 1000)
    sources_raw = payload.get("sources")
    if not isinstance(sources_raw, list) or not 1 <= len(sources_raw) <= 100:
        raise SectorValidationError("sources must contain 1..100 items")
    sources: list[dict[str, Any]] = []
    for item in sources_raw:
        if not isinstance(item, dict):
            raise SectorValidationError("source must be an object")
        source_type = item.get("source_type")
        if source_type not in SOURCE_TYPES:
            raise SectorValidationError("source_type is invalid")
        published_at = item.get("published_at")
        if published_at is not None:
            if isinstance(published_at, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
                # Preserve source date precision; do not invent a publication time.
                published_at = published_at
            else:
                published_at = iso_seconds(parse_datetime(published_at, "sources.published_at"))
        sources.append({
            "title": _text(item.get("title"), "sources.title", 500),
            "url": _http_url(item.get("url"), "sources.url"),
            "source_name": _text(item.get("source_name"), "sources.source_name", 200),
            "source_type": source_type,
            "published_at": published_at,
        })
    run_id = _text(payload.get("run_id"), "run_id", 200)
    key = _text(payload.get("dedupe_key"), "dedupe_key", 200)
    expected_key = dedupe_key(expected_window or WeeklyWindow(start, end, end.astimezone(JST).date().isoformat()), code)
    if key != expected_key or run_id != key:
        raise SectorValidationError("run_id/dedupe_key does not match week and sector")
    report = {
        "id": stable_report_id(key), "schema_version": SCHEMA_VERSION, "report_type": REPORT_TYPE,
        "sector_code": code, "sector_name": name, "period_start": iso_seconds(start), "period_end": iso_seconds(end),
        "generated_at": iso_seconds(generated), "importance": importance, "direction": direction,
        "summary_bullets": json.dumps(bullets, ensure_ascii=False), "full_report_md": full_report,
        "watchlist_companies": json.dumps(watchlist, ensure_ascii=False),
        "next_week_watchpoints": json.dumps(watchpoints, ensure_ascii=False),
        "missed_candidates": json.dumps(missed, ensure_ascii=False), "sources": json.dumps(sources, ensure_ascii=False),
        "run_id": run_id, "dedupe_key": key,
    }
    return ValidatedSectorReport(report=report)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_sector_reports (
 id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, report_type TEXT NOT NULL, sector_code INTEGER NOT NULL,
 sector_name TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, generated_at TEXT NOT NULL,
 importance TEXT NOT NULL, direction TEXT NOT NULL, summary_bullets TEXT NOT NULL, full_report_md TEXT NOT NULL,
 watchlist_companies TEXT NOT NULL, next_week_watchpoints TEXT NOT NULL, missed_candidates TEXT NOT NULL,
 sources TEXT NOT NULL, run_id TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_sector_report_runs (
 run_id TEXT PRIMARY KEY, report_type TEXT NOT NULL, sector_code INTEGER NOT NULL, sector_name TEXT NOT NULL,
 period_start TEXT NOT NULL, period_end TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0, last_error_type TEXT, last_error_message TEXT, started_at TEXT,
 completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sector_weekly_work_assignments (
 assignment_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, stable_key TEXT NOT NULL UNIQUE,
 sector_code INTEGER NOT NULL, sector_name TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
 claim_owner TEXT, claimed_at TEXT, lease_expires_at TEXT, started_at TEXT, completed_at TEXT,
 last_error_type TEXT, last_error_message TEXT, submitted_payload_hash TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sector_reports_generated ON canonical_sector_reports(generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_sector_runs_status ON canonical_sector_report_runs(status, period_end DESC);
CREATE INDEX IF NOT EXISTS ix_sector_work_ready ON sector_weekly_work_assignments(status,available_at,sector_code);
"""


def connect_sector_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SQLITE_SCHEMA)
    return conn


def ensure_week_runs(conn: sqlite3.Connection, window: WeeklyWindow) -> None:
    now = iso_seconds(now_jst())
    with conn:
        for code in range(1, 34):
            key = dedupe_key(window, code)
            conn.execute(
                "INSERT OR IGNORE INTO canonical_sector_report_runs "
                "(run_id,report_type,sector_code,sector_name,period_start,period_end,dedupe_key,status,attempt_count,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, REPORT_TYPE, code, sector_name(code), iso_seconds(window.period_start), iso_seconds(window.period_end), key, "pending", 0, now, now),
            )


def mark_run(conn: sqlite3.Connection, key: str, status: str, *, error: Exception | None = None, increment: bool = False) -> None:
    if status not in {"pending", "running", "success", "failed", "retry_pending"}:
        raise ValueError("invalid sector run status")
    now = iso_seconds(now_jst())
    started = now if status == "running" else None
    completed = now if status in {"success", "failed"} else None
    error_type = type(error).__name__ if error else None
    error_message = str(error)[:2000] if error else None
    with conn:
        conn.execute(
            "UPDATE canonical_sector_report_runs SET status=?, attempt_count=attempt_count+?, last_error_type=?, "
            "last_error_message=?, started_at=COALESCE(?,started_at), completed_at=?, updated_at=? WHERE run_id=?",
            (status, 1 if increment else 0, error_type, error_message, started, completed, now, key),
        )


def upsert_report(conn: sqlite3.Connection, validated: ValidatedSectorReport) -> None:
    now = iso_seconds(now_jst())
    report = validated.report
    columns = list(report) + ["created_at", "updated_at"]
    values = [report[name] for name in report] + [now, now]
    updates = ",".join(
        f"{name}=excluded.{name}" for name in report
        if name not in {"id", "dedupe_key", "created_at"}
    ) + ",updated_at=excluded.updated_at"
    with conn:
        conn.execute(
            f"INSERT INTO canonical_sector_reports ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(dedupe_key) DO UPDATE SET {updates}", values,
        )


def rows_for_sync(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in {"canonical_sector_reports", "canonical_sector_report_runs"}:
        raise ValueError("unsupported sector table")
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    if table == "canonical_sector_reports":
        for row in rows:
            for field in ("summary_bullets", "watchlist_companies", "next_week_watchpoints", "missed_candidates", "sources"):
                row[field] = json.loads(row[field])
    return rows
