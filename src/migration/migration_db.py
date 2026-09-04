# ============================================================
# migration_db.py — SQLite スキーマ定義 & CRUD（Phase2拡張版）
# ============================================================
"""
Phase0: quarterly_results / company_memos / quarterly_notes /
        segment_financials / migration_log
Phase2: audit_log 追加 + WAL + リトライ + 差分検出upsert

中間テーブル（migration_log / quarantine）への書き込みは
persist_policy に従い、通常モードでは無効化される。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib.pipeline.source_priority import get_priority

from ..persist_policy import should_persist_intermediates

logger = logging.getLogger("migration")

JST = timezone(timedelta(hours=9))

# database is locked 対策
_RETRY_MAX = 5
_RETRY_WAIT_SEC = 0.1


def _now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_note(text: str) -> str:
    """メモの正規化（改行コードの統一、末尾空白のトリム）"""
    if not text:
        return ""
    # \r\n や \r を \n に統一し、末尾の空白（改行含む）を削除
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def _to_millions(
    val: float | None,
    unit_raw: str | None,
    unit_multiplier: int | None,
    *,
    context: str = "",
) -> float | None:
    """segment_financials 保存前の百万円単位強制正規化。

    unit_raw / unit_multiplier を参照し変換する。
    値の大きさによる判定は禁止（誤変換リスク）。
    unit 不明時は変換せずにログを出す。
    """
    if val is None:
        return None
    unit = str(unit_raw or "").strip()
    mult = unit_multiplier

    # 千円判定
    if "千円" in unit or "Thousand" in unit or mult == 1_000:
        return val / 1_000
    # 百万円判定（変換不要）
    if "百万円" in unit or "Million" in unit or mult == 1_000_000:
        return val
    # 億円判定
    if "億円" in unit or "HundredMillion" in unit or mult == 100_000_000:
        return val * 100

    # unit 不明 → 変換せず警告のみ
    logger.warning(
        "[UnitNorm] unit不明のため変換スキップ: val=%s unit_raw=%r mult=%s %s",
        val, unit_raw, mult, context,
    )
    return val


@dataclass(frozen=True)
class SegmentUpsertResult:
    """provenance-aware segment upsert の判定結果。"""

    status: str
    row_id: int | None
    accepted: bool
    reason: str
    existing_source: str
    incoming_source: str

# ------------------------------------------------------------------
# テーブル作成 SQL
# ------------------------------------------------------------------
_TABLES = [
    # ① 四半期業績
    """
    CREATE TABLE IF NOT EXISTS quarterly_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_code TEXT NOT NULL,
        fiscal_year_end TEXT NOT NULL,
        quarter TEXT NOT NULL,
        sales REAL,
        gross_profit REAL,
        gross_margin REAL,
        sga REAL,
        operating_profit REAL,
        profit_before_tax REAL,
        net_income REAL,
        unit TEXT DEFAULT '百万円',
        source_doc_id TEXT,
        source_url TEXT,
        zip_hash TEXT,
        parser_version TEXT DEFAULT 'v2',
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(company_code, fiscal_year_end, quarter)
    );
    """,
    # ② 補助メモ（C〜L列）
    """
    CREATE TABLE IF NOT EXISTS company_memos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_code TEXT NOT NULL UNIQUE,
        col_c TEXT, col_d TEXT, col_e TEXT, col_f TEXT,
        col_g TEXT, col_h TEXT, col_i TEXT, col_j TEXT,
        col_k TEXT, col_l TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    """,
    # ③ 四半期メモ（Z列 — 履歴型）
    """
    CREATE TABLE IF NOT EXISTS quarterly_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_code TEXT NOT NULL,
        fiscal_year_end TEXT NOT NULL,
        quarter TEXT NOT NULL,
        note TEXT,
        created_at TEXT
    );
    """,
    # ④ セグメント（売上/利益ペア — 縦持ち）
    """
    CREATE TABLE IF NOT EXISTS segment_financials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_code TEXT NOT NULL,
        fiscal_year_end TEXT NOT NULL,
        quarter TEXT NOT NULL,
        segment_name TEXT NOT NULL,
        segment_order INTEGER NOT NULL,
        segment_sales REAL,
        segment_profit REAL,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(company_code, fiscal_year_end, quarter, segment_name)
    );
    """,
    # ⑤ 移行ログ（Phase0用）
    """
    CREATE TABLE IF NOT EXISTS migration_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        sheet_name TEXT,
        row_start INTEGER,
        row_end INTEGER,
        company_code TEXT,
        fiscal_year TEXT,
        quarter TEXT,
        log_level TEXT NOT NULL,
        log_type TEXT NOT NULL,
        message TEXT,
        created_at TEXT
    );
    """,
    # ⑥ 監査ログ（Phase2: 変更の列単位記録）
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        source TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        company_code TEXT NOT NULL,
        fiscal_year_end TEXT,
        quarter TEXT,
        field_name TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        tdnet_disclosure_id TEXT,
        run_id TEXT
    );
    """,
    # ⑦ 受注系メトリクス
    """
    CREATE TABLE IF NOT EXISTS order_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_code TEXT NOT NULL,
        fiscal_year_end TEXT NOT NULL,
        quarter TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        value REAL,
        raw_value REAL,
        unit TEXT DEFAULT '',
        confidence TEXT DEFAULT 'low',
        raw_text TEXT DEFAULT '',
        source_doc_id TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(company_code, fiscal_year_end, quarter, metric_name)
    );
    """,
    # ⑧ quarantine（抽出失敗・曖昧データの保留）
    """
    CREATE TABLE IF NOT EXISTS quarantine (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_code TEXT NOT NULL,
        fiscal_year_end TEXT DEFAULT '',
        quarter TEXT DEFAULT '',
        metric_type TEXT DEFAULT '',
        reason TEXT NOT NULL,
        detail TEXT DEFAULT '',
        source_doc_id TEXT DEFAULT '',
        created_at TEXT
    );
    """,
]


class MigrationDB:
    """決算データベース（移行 + 運用）"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for ddl in _TABLES:
            self._conn.execute(ddl)
        self._create_views()
        self._migrate_add_columns()
        self._conn.commit()

    def _create_views(self) -> None:
        """クリーンビューを作成/再作成する"""
        self._conn.execute("DROP VIEW IF EXISTS segment_financials_clean")
        self._conn.execute("""
            CREATE VIEW segment_financials_clean AS
            SELECT *
            FROM segment_financials
            WHERE data_source = 'tdnet'
              AND segment_name IS NOT NULL
              AND segment_name != ''
              AND (segment_sales IS NOT NULL OR segment_profit IS NOT NULL)
        """)

    def _migrate_add_columns(self) -> None:
        """既存DBに不足カラムを追加するマイグレーション"""
        cur = self._conn.execute("PRAGMA table_info(quarterly_results)")
        existing_cols = {row[1] for row in cur.fetchall()}
        migrations = [
            ("source_doc_id", "ALTER TABLE quarterly_results ADD COLUMN source_doc_id TEXT"),
            ("source_url", "ALTER TABLE quarterly_results ADD COLUMN source_url TEXT"),
            ("zip_hash", "ALTER TABLE quarterly_results ADD COLUMN zip_hash TEXT"),
            ("parser_version", "ALTER TABLE quarterly_results ADD COLUMN parser_version TEXT DEFAULT 'v2'"),
            ("field_sources", "ALTER TABLE quarterly_results ADD COLUMN field_sources TEXT"),
            ("profit_before_tax", "ALTER TABLE quarterly_results ADD COLUMN profit_before_tax REAL"),
            ("net_income", "ALTER TABLE quarterly_results ADD COLUMN net_income REAL"),
        ]
        for col_name, sql in migrations:
            if col_name not in existing_cols:
                self._conn.execute(sql)
                logger.info(f"[DB] マイグレーション: {col_name} カラム追加")

        # segment_financials マイグレーション
        cur2 = self._conn.execute("PRAGMA table_info(segment_financials)")
        seg_cols = {row[1] for row in cur2.fetchall()}
        seg_migrations = [
            ("raw_profit_label", "ALTER TABLE segment_financials ADD COLUMN raw_profit_label TEXT DEFAULT ''"),
            ("data_source", "ALTER TABLE segment_financials ADD COLUMN data_source TEXT DEFAULT 'excel_legacy'"),
            ("segment_name_norm", "ALTER TABLE segment_financials ADD COLUMN segment_name_norm TEXT"),
            ("extractor_route", "ALTER TABLE segment_financials ADD COLUMN extractor_route TEXT"),
            ("source_doc_type", "ALTER TABLE segment_financials ADD COLUMN source_doc_type TEXT"),
            ("disclosure_date", "ALTER TABLE segment_financials ADD COLUMN disclosure_date TEXT"),
            ("tdnet_doc_id", "ALTER TABLE segment_financials ADD COLUMN tdnet_doc_id TEXT"),
            ("row_type", "ALTER TABLE segment_financials ADD COLUMN row_type TEXT"),
        ]
        for col_name, sql in seg_migrations:
            if col_name not in seg_cols:
                self._conn.execute(sql)
                logger.info(f"[DB] マイグレーション: segment_financials.{col_name} カラム追加")
                # data_source追加時に既存データを一括マーク
                if col_name == "data_source":
                    self._conn.execute(
                        "UPDATE segment_financials SET data_source='excel_legacy' WHERE data_source IS NULL"
                    )
                    logger.info("[DB] 既存segment_financialsを data_source='excel_legacy' にマーク")

        # quarantine マイグレーション (Stage-aware 拡張)
        cur3 = self._conn.execute("PRAGMA table_info(quarantine)")
        q_cols = {row[1] for row in cur3.fetchall()}
        q_migrations = [
            ("failed_stage", "ALTER TABLE quarantine ADD COLUMN failed_stage TEXT DEFAULT ''"),
            ("review_hint", "ALTER TABLE quarantine ADD COLUMN review_hint TEXT DEFAULT ''"),
        ]
        for col_name, sql in q_migrations:
            if col_name not in q_cols:
                self._conn.execute(sql)
                logger.info(f"[DB] マイグレーション: quarantine.{col_name} カラム追加")

    # ----------------------------------------------------------
    # リトライ付き execute（database is locked 対策）
    # ----------------------------------------------------------
    def _exec_retry(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """database is locked 時に最大 _RETRY_MAX 回リトライする"""
        for attempt in range(_RETRY_MAX):
            try:
                return self._conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < _RETRY_MAX - 1:
                    logger.warning(
                        f"[DB] database locked, retry {attempt+1}/{_RETRY_MAX}"
                    )
                    time.sleep(_RETRY_WAIT_SEC * (attempt + 1))
                else:
                    raise
        # unreachable, but for type checker
        raise sqlite3.OperationalError("max retries exceeded")

    # ----------------------------------------------------------
    # audit_log 記録
    # ----------------------------------------------------------
    def _record_audit(
        self,
        *,
        actor: str,
        source: str,
        entity_type: str,
        company_code: str,
        fiscal_year_end: str | None = None,
        quarter: str | None = None,
        field_name: str,
        old_value: str | None,
        new_value: str | None,
        tdnet_disclosure_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        now = _now_jst()
        self._exec_retry(
            """
            INSERT INTO audit_log
                (timestamp, actor, source, entity_type,
                 company_code, fiscal_year_end, quarter,
                 field_name, old_value, new_value,
                 tdnet_disclosure_id, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, actor, source, entity_type,
             company_code, fiscal_year_end, quarter,
             field_name,
             str(old_value) if old_value is not None else None,
             str(new_value) if new_value is not None else None,
             tdnet_disclosure_id, run_id),
        )

    # ----------------------------------------------------------
    # quarterly_results — 差分検出upsert
    # ----------------------------------------------------------
    _QR_NUM_FIELDS = (
        "sales", "gross_profit", "gross_margin", "sga", "operating_profit",
        "profit_before_tax", "net_income",
    )

    def upsert_quarterly_result(
        self,
        company_code: str,
        fiscal_year_end: str,
        quarter: str,
        *,
        sales: float | None = None,
        gross_profit: float | None = None,
        gross_margin: float | None = None,
        sga: float | None = None,
        operating_profit: float | None = None,
        profit_before_tax: float | None = None,
        net_income: float | None = None,
        actor: str = "migration",
        source: str = "migration",
        tdnet_disclosure_id: str | None = None,
        run_id: str | None = None,
        source_doc_id: str | None = None,
        source_url: str | None = None,
        zip_hash: str | None = None,
        field_sources: dict | None = None,
    ) -> str:
        """
        四半期数値をupsertする。

        Returns:
            "inserted" / "updated" / "no_change"
        """
        new_vals = {
            "sales": sales,
            "gross_profit": gross_profit,
            "gross_margin": gross_margin,
            "sga": sga,
            "operating_profit": operating_profit,
            "profit_before_tax": profit_before_tax,
            "net_income": net_income,
        }
        now = _now_jst()

        # 既存レコードを取得
        existing = self.get_quarterly_result(company_code, fiscal_year_end, quarter)

        if existing is None:
            # INSERT
            self._exec_retry(
                """
                INSERT INTO quarterly_results
                    (company_code, fiscal_year_end, quarter,
                     sales, gross_profit, gross_margin, sga, operating_profit,
                     profit_before_tax, net_income,
                     unit, source_doc_id, source_url, zip_hash, parser_version,
                     field_sources, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '百万円', ?, ?, ?, 'v2', ?, ?, ?)
                """,
                (company_code, fiscal_year_end, quarter,
                 sales, gross_profit, gross_margin, sga, operating_profit,
                 profit_before_tax, net_income,
                 source_doc_id, source_url, zip_hash,
                 json.dumps(field_sources) if field_sources else None,
                 now, now),
            )
            # audit_log: INSERT = 全フィールド記録
            if source != "migration":
                for field in self._QR_NUM_FIELDS:
                    val = new_vals[field]
                    if val is not None:
                        self._record_audit(
                            actor=actor, source=source, entity_type="quarter",
                            company_code=company_code,
                            fiscal_year_end=fiscal_year_end, quarter=quarter,
                            field_name=field, old_value=None,
                            new_value=str(val),
                            tdnet_disclosure_id=tdnet_disclosure_id,
                            run_id=run_id,
                        )
            return "inserted"

        # 差分検出
        changes: dict[str, tuple] = {}  # field -> (old, new)
        for field in self._QR_NUM_FIELDS:
            old_val = existing.get(field)
            new_val = new_vals[field]
            if new_val is not None and old_val != new_val:
                changes[field] = (old_val, new_val)

        if not changes:
            return "no_change"

        # UPDATE（変更のある列のみ）
        set_clauses = [f"{f}=?" for f in changes]
        set_clauses.append("updated_at=?")
        vals = [changes[f][1] for f in changes]
        vals.append(now)

        self._exec_retry(
            f"""
            UPDATE quarterly_results
            SET {', '.join(set_clauses)}
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
            """,
            tuple(vals) + (company_code, fiscal_year_end, quarter),
        )

        # audit_log
        if source != "migration":
            for field, (old_v, new_v) in changes.items():
                self._record_audit(
                    actor=actor, source=source, entity_type="quarter",
                    company_code=company_code,
                    fiscal_year_end=fiscal_year_end, quarter=quarter,
                    field_name=field,
                    old_value=str(old_v) if old_v is not None else None,
                    new_value=str(new_v),
                    tdnet_disclosure_id=tdnet_disclosure_id,
                    run_id=run_id,
                )
        return "updated"

    # ----------------------------------------------------------
    # company_memos (C〜L列) — 差分検出upsert
    # ----------------------------------------------------------
    _MEMO_FIELDS = (
        "col_c", "col_d", "col_e", "col_f", "col_g",
        "col_h", "col_i", "col_j", "col_k", "col_l",
    )

    def upsert_company_memo(
        self,
        company_code: str,
        *,
        col_c: str | None = None, col_d: str | None = None,
        col_e: str | None = None, col_f: str | None = None,
        col_g: str | None = None, col_h: str | None = None,
        col_i: str | None = None, col_j: str | None = None,
        col_k: str | None = None, col_l: str | None = None,
        actor: str = "migration",
        source: str = "migration",
        run_id: str | None = None,
    ) -> str:
        """Returns: 'inserted' / 'updated' / 'no_change'"""
        new_vals = {
            "col_c": col_c, "col_d": col_d, "col_e": col_e, "col_f": col_f,
            "col_g": col_g, "col_h": col_h, "col_i": col_i, "col_j": col_j,
            "col_k": col_k, "col_l": col_l,
        }
        now = _now_jst()
        existing = self.get_company_memo(company_code)

        if existing is None:
            self._exec_retry(
                """
                INSERT INTO company_memos
                    (company_code,
                     col_c, col_d, col_e, col_f, col_g,
                     col_h, col_i, col_j, col_k, col_l,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (company_code,
                 col_c, col_d, col_e, col_f, col_g,
                 col_h, col_i, col_j, col_k, col_l,
                 now, now),
            )
            return "inserted"

        # 差分検出
        changes = {}
        for field in self._MEMO_FIELDS:
            old_val = existing.get(field)
            new_val = new_vals[field]
            if new_val is not None and old_val != new_val:
                changes[field] = (old_val, new_val)

        if not changes:
            return "no_change"

        set_clauses = [f"{f}=?" for f in changes]
        set_clauses.append("updated_at=?")
        vals = [changes[f][1] for f in changes]
        vals.append(now)

        self._exec_retry(
            f"""
            UPDATE company_memos SET {', '.join(set_clauses)}
            WHERE company_code=?
            """,
            tuple(vals) + (company_code,),
        )

        if source != "migration":
            for field, (old_v, new_v) in changes.items():
                self._record_audit(
                    actor=actor, source=source, entity_type="memo",
                    company_code=company_code,
                    field_name=field,
                    old_value=old_v, new_value=new_v,
                    run_id=run_id,
                )
        return "updated"

    # ----------------------------------------------------------
    # quarterly_notes (Z列 — 履歴型)
    # ----------------------------------------------------------
    def insert_quarterly_note(
        self,
        company_code: str,
        fiscal_year_end: str,
        quarter: str,
        note: str | None,
        *,
        actor: str = "migration",
        source: str = "migration",
        run_id: str | None = None,
    ) -> str:
        """Returns: 'inserted' / 'skipped'"""
        if note is None or str(note).strip() == "":
            return "skipped"

        str_note = _normalize_note(str(note))
        if not str_note:
            return "skipped"

        # NOTE: 冪等性確保のため、最新のメモ（正規化済み）と完全一致する場合は追加しない
        raw_latest = self.get_latest_note(company_code, fiscal_year_end, quarter)
        latest_note = _normalize_note(raw_latest) if raw_latest else ""

        if latest_note == str_note:
            return "skipped"

        now = _now_jst()
        self._exec_retry(
            """
            INSERT INTO quarterly_notes
                (company_code, fiscal_year_end, quarter, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (company_code, fiscal_year_end, quarter, str_note, now),
        )

        if source != "migration":
            self._record_audit(
                actor=actor, source=source, entity_type="note",
                company_code=company_code,
                fiscal_year_end=fiscal_year_end, quarter=quarter,
                field_name="note",
                old_value=latest_note if latest_note else None,
                new_value=str_note,
                run_id=run_id,
            )
        return "inserted"

    def get_latest_note(
        self, company_code: str, fiscal_year_end: str, quarter: str,
    ) -> str | None:
        cur = self._conn.execute(
            """
            SELECT note FROM quarterly_notes
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
            ORDER BY id DESC LIMIT 1
            """,
            (company_code, fiscal_year_end, quarter),
        )
        row = cur.fetchone()
        return row[0] if row else None

    # ----------------------------------------------------------
    # segment_financials (AA列〜 売上/利益ペア) — 差分検出upsert
    # ----------------------------------------------------------
    def upsert_segment(
        self,
        company_code: str,
        fiscal_year_end: str,
        quarter: str,
        segment_name: str,
        segment_order: int,
        segment_sales: float | None = None,
        segment_profit: float | None = None,
        *,
        unit_raw: str | None = None,
        unit_multiplier: int | None = None,
        raw_profit_label: str = "",
        data_source: str = "migration",
        actor: str = "migration",
        source: str = "migration",
        tdnet_disclosure_id: str | None = None,
        run_id: str | None = None,
        segment_name_norm: str | None = None,
        extractor_route: str | None = None,
        source_doc_type: str | None = None,
        disclosure_date: str | None = None,
        tdnet_doc_id: str | None = None,
        row_type: str | None = None,
    ) -> str:
        """Returns: 'inserted' / 'updated' / 'no_change'"""
        now = _now_jst()

        # ── 百万円単位正規化（INSERT/UPDATE 前に必ず適用）─────────────
        _ctx = f"ticker={company_code} period={fiscal_year_end} q={quarter} seg={segment_name}"
        segment_sales = _to_millions(segment_sales, unit_raw, unit_multiplier, context=_ctx)
        segment_profit = _to_millions(segment_profit, unit_raw, unit_multiplier, context=_ctx)
        # ──────────────────────────────────────────────────────────────

        # 既存チェック
        cur = self._conn.execute(
            """
            SELECT segment_order, segment_sales, segment_profit
            FROM segment_financials
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
              AND segment_name=?
            """,
            (company_code, fiscal_year_end, quarter, segment_name),
        )
        existing = cur.fetchone()

        if existing is None:
            self._exec_retry(
                """
                INSERT INTO segment_financials
                    (company_code, fiscal_year_end, quarter,
                     segment_name, segment_order,
                     segment_sales, segment_profit,
                     raw_profit_label, data_source,
                     segment_name_norm, extractor_route, source_doc_type,
                     disclosure_date, tdnet_doc_id, row_type,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (company_code, fiscal_year_end, quarter,
                 segment_name, segment_order,
                 segment_sales, segment_profit,
                 raw_profit_label, data_source,
                 segment_name_norm, extractor_route, source_doc_type,
                 disclosure_date, tdnet_doc_id, row_type,
                 now, now),
            )
            if source != "migration":
                for fname, val in [("segment_sales", segment_sales),
                                   ("segment_profit", segment_profit)]:
                    if val is not None:
                        self._record_audit(
                            actor=actor, source=source, entity_type="segment",
                            company_code=company_code,
                            fiscal_year_end=fiscal_year_end, quarter=quarter,
                            field_name=f"{segment_name}.{fname}",
                            old_value=None, new_value=str(val),
                            tdnet_disclosure_id=tdnet_disclosure_id,
                            run_id=run_id,
                        )
            return "inserted"

        old_order, old_sales, old_profit = existing
        changes = {}
        if segment_order != old_order:
            changes["segment_order"] = (old_order, segment_order)
        if segment_sales is not None and old_sales != segment_sales:
            changes["segment_sales"] = (old_sales, segment_sales)
        if segment_profit is not None and old_profit != segment_profit:
            changes["segment_profit"] = (old_profit, segment_profit)

        if not changes:
            return "no_change"

        set_clauses = [f"{f}=?" for f in changes]
        set_clauses.append("updated_at=?")
        vals = [changes[f][1] for f in changes]
        vals.append(now)

        self._exec_retry(
            f"""
            UPDATE segment_financials
            SET {', '.join(set_clauses)}
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
              AND segment_name=?
            """,
            tuple(vals) + (company_code, fiscal_year_end, quarter, segment_name),
        )

        if source != "migration":
            for field, (old_v, new_v) in changes.items():
                if field in ("segment_sales", "segment_profit"):
                    self._record_audit(
                        actor=actor, source=source, entity_type="segment",
                        company_code=company_code,
                        fiscal_year_end=fiscal_year_end, quarter=quarter,
                        field_name=f"{segment_name}.{field}",
                        old_value=str(old_v) if old_v is not None else None,
                        new_value=str(new_v),
                        tdnet_disclosure_id=tdnet_disclosure_id,
                        run_id=run_id,
                    )
        return "updated"

    def upsert_segment_provenance_aware(
        self,
        company_code: str,
        fiscal_year_end: str,
        quarter: str,
        segment_name: str,
        segment_order: int,
        segment_sales: float | None = None,
        segment_profit: float | None = None,
        *,
        unit_raw: str | None = None,
        unit_multiplier: int | None = None,
        raw_profit_label: str = "",
        data_source: str = "migration",
        actor: str = "migration",
        source: str = "migration",
        tdnet_disclosure_id: str | None = None,
        run_id: str | None = None,
        segment_name_norm: str | None = None,
        extractor_route: str | None = None,
        source_doc_type: str | None = None,
        disclosure_date: str | None = None,
        tdnet_doc_id: str | None = None,
        row_type: str | None = None,
    ) -> SegmentUpsertResult:
        """自然キー衝突時に filing identity と source priority を検証する。"""
        now = _now_jst()
        context = (
            f"ticker={company_code} period={fiscal_year_end} "
            f"q={quarter} seg={segment_name}"
        )
        segment_sales = _to_millions(
            segment_sales, unit_raw, unit_multiplier, context=context,
        )
        segment_profit = _to_millions(
            segment_profit, unit_raw, unit_multiplier, context=context,
        )

        saved_columns = (
            "segment_order",
            "segment_sales",
            "segment_profit",
            "raw_profit_label",
            "data_source",
            "segment_name_norm",
            "extractor_route",
            "source_doc_type",
            "disclosure_date",
            "tdnet_doc_id",
            "row_type",
        )
        incoming_values = (
            segment_order,
            segment_sales,
            segment_profit,
            raw_profit_label,
            data_source,
            segment_name_norm,
            extractor_route,
            source_doc_type,
            disclosure_date,
            tdnet_doc_id,
            row_type,
        )
        incoming_source = str(data_source or "")
        incoming_filing_id = str(tdnet_doc_id or "").strip()

        existing = self._conn.execute(
            f"""
            SELECT id, {', '.join(saved_columns)}
            FROM segment_financials
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
              AND segment_name=?
            """,
            (company_code, fiscal_year_end, quarter, segment_name),
        ).fetchone()

        if existing is None:
            cursor = self._exec_retry(
                f"""
                INSERT INTO segment_financials
                    (company_code, fiscal_year_end, quarter, segment_name,
                     {', '.join(saved_columns)}, created_at, updated_at)
                VALUES ({', '.join('?' for _ in range(4 + len(saved_columns) + 2))})
                """,
                (
                    company_code,
                    fiscal_year_end,
                    quarter,
                    segment_name,
                    *incoming_values,
                    now,
                    now,
                ),
            )
            row_id = int(cursor.lastrowid)
            self._verify_segment_readback(row_id, saved_columns, incoming_values)
            return SegmentUpsertResult(
                status="inserted",
                row_id=row_id,
                accepted=True,
                reason="segment_inserted",
                existing_source="",
                incoming_source=incoming_source,
            )

        row_id = int(existing[0])
        existing_values = tuple(existing[1:])
        existing_by_column = dict(zip(saved_columns, existing_values))
        existing_source = str(existing_by_column["data_source"] or "")
        existing_filing_id = str(existing_by_column["tdnet_doc_id"] or "").strip()

        if existing_filing_id and incoming_filing_id:
            if existing_filing_id != incoming_filing_id:
                return SegmentUpsertResult(
                    status="rejected_filing_conflict",
                    row_id=row_id,
                    accepted=False,
                    reason="segment_natural_key_filing_conflict",
                    existing_source=existing_source,
                    incoming_source=incoming_source,
                )
        elif not existing_filing_id and not incoming_filing_id:
            if existing_values == incoming_values:
                return SegmentUpsertResult(
                    status="no_change",
                    row_id=row_id,
                    accepted=True,
                    reason="segment_saved_fields_identical",
                    existing_source=existing_source,
                    incoming_source=incoming_source,
                )
            return SegmentUpsertResult(
                status="rejected_filing_identity_unresolved",
                row_id=row_id,
                accepted=False,
                reason="segment_filing_identity_unresolved",
                existing_source=existing_source,
                incoming_source=incoming_source,
            )
        elif existing_filing_id and not incoming_filing_id:
            return SegmentUpsertResult(
                status="rejected_filing_identity_unresolved",
                row_id=row_id,
                accepted=False,
                reason="segment_incoming_filing_identity_missing",
                existing_source=existing_source,
                incoming_source=incoming_source,
            )

        if existing_values == incoming_values:
            return SegmentUpsertResult(
                status="no_change",
                row_id=row_id,
                accepted=True,
                reason="segment_saved_fields_identical",
                existing_source=existing_source,
                incoming_source=incoming_source,
            )

        required_provenance = (
            incoming_source,
            incoming_filing_id,
            str(extractor_route or "").strip(),
            str(source_doc_type or "").strip(),
            str(disclosure_date or "").strip(),
            str(row_type or "").strip(),
        )
        if not all(required_provenance):
            return SegmentUpsertResult(
                status="rejected_filing_identity_unresolved",
                row_id=row_id,
                accepted=False,
                reason="segment_incoming_provenance_incomplete",
                existing_source=existing_source,
                incoming_source=incoming_source,
            )

        if get_priority(incoming_source) > get_priority(existing_source):
            return SegmentUpsertResult(
                status="rejected_lower_priority",
                row_id=row_id,
                accepted=False,
                reason="segment_source_priority_downgrade",
                existing_source=existing_source,
                incoming_source=incoming_source,
            )

        set_clause = ", ".join(f"{column}=?" for column in saved_columns)
        self._exec_retry(
            f"UPDATE segment_financials SET {set_clause}, updated_at=? WHERE id=?",
            (*incoming_values, now, row_id),
        )
        self._verify_segment_readback(row_id, saved_columns, incoming_values)

        if source != "migration":
            for field in ("segment_sales", "segment_profit"):
                index = saved_columns.index(field)
                old_value = existing_values[index]
                new_value = incoming_values[index]
                if old_value != new_value:
                    self._record_audit(
                        actor=actor,
                        source=source,
                        entity_type="segment",
                        company_code=company_code,
                        fiscal_year_end=fiscal_year_end,
                        quarter=quarter,
                        field_name=f"{segment_name}.{field}",
                        old_value=str(old_value) if old_value is not None else None,
                        new_value=str(new_value) if new_value is not None else None,
                        tdnet_disclosure_id=tdnet_disclosure_id,
                        run_id=run_id,
                    )

        return SegmentUpsertResult(
            status="updated",
            row_id=row_id,
            accepted=True,
            reason="segment_provenance_aware_update",
            existing_source=existing_source,
            incoming_source=incoming_source,
        )

    def _verify_segment_readback(
        self,
        row_id: int,
        columns: tuple[str, ...],
        expected: tuple,
    ) -> None:
        actual = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM segment_financials WHERE id=?",
            (row_id,),
        ).fetchone()
        if actual is None or tuple(actual) != expected:
            raise RuntimeError("segment_provenance_readback_mismatch")

    def get_segment_id(
        self,
        *,
        company_code: str,
        fiscal_year_end: str,
        quarter: str,
        segment_name: str,
    ) -> int | None:
        row = self._conn.execute(
            """
            SELECT id FROM segment_financials
            WHERE company_code=? AND fiscal_year_end=? AND quarter=? AND segment_name=?
            """,
            (company_code, fiscal_year_end, quarter, segment_name),
        ).fetchone()
        return int(row[0]) if row else None

    # ----------------------------------------------------------
    # migration_log (Phase0用 — 変更なし)
    # ----------------------------------------------------------
    def insert_log(
        self,
        run_id: str,
        log_level: str,
        log_type: str,
        message: str,
        *,
        sheet_name: str | None = None,
        row_start: int | None = None,
        row_end: int | None = None,
        company_code: str | None = None,
        fiscal_year: str | None = None,
        quarter: str | None = None,
    ) -> None:
        if not should_persist_intermediates():
            return
        now = _now_jst()
        self._exec_retry(
            """
            INSERT INTO migration_log
                (run_id, timestamp, sheet_name,
                 row_start, row_end,
                 company_code, fiscal_year, quarter,
                 log_level, log_type, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, now, sheet_name,
             row_start, row_end,
             company_code, fiscal_year, quarter,
             log_level, log_type, message, now),
        )

    # ----------------------------------------------------------
    # ユーティリティ
    # ----------------------------------------------------------
    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def get_quarterly_result(
        self, company_code: str, fiscal_year_end: str, quarter: str,
    ) -> dict | None:
        cur = self._conn.execute(
            """
            SELECT * FROM quarterly_results
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
            """,
            (company_code, fiscal_year_end, quarter),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_segments(
        self, company_code: str, fiscal_year_end: str, quarter: str,
    ) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT * FROM segment_financials
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
            ORDER BY segment_order
            """,
            (company_code, fiscal_year_end, quarter),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_company_memo(self, company_code: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM company_memos WHERE company_code=?",
            (company_code,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_logs_by_run(self, run_id: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM migration_log WHERE run_id=? ORDER BY id",
            (run_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_audit_log(
        self,
        *,
        company_code: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """audit_logを取得する"""
        conditions = []
        params: list = []
        if company_code:
            conditions.append("company_code=?")
            params.append(company_code)
        if run_id:
            conditions.append("run_id=?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        cur = self._conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ----------------------------------------------------------
    # order_metrics — 受注系メトリクス upsert
    # ----------------------------------------------------------
    def upsert_order_metric(
        self,
        company_code: str,
        fiscal_year_end: str,
        quarter: str,
        metric_name: str,
        *,
        value: float | None = None,
        raw_value: float | None = None,
        unit: str = "",
        confidence: str = "low",
        raw_text: str = "",
        source_doc_id: str = "",
    ) -> str:
        """Returns: 'inserted' / 'updated' / 'no_change'"""
        now = _now_jst()
        cur = self._conn.execute(
            """
            SELECT value, raw_value, unit, confidence
            FROM order_metrics
            WHERE company_code=? AND fiscal_year_end=? AND quarter=? AND metric_name=?
            """,
            (company_code, fiscal_year_end, quarter, metric_name),
        )
        existing = cur.fetchone()

        if existing is None:
            self._exec_retry(
                """
                INSERT INTO order_metrics
                    (company_code, fiscal_year_end, quarter, metric_name,
                     value, raw_value, unit, confidence, raw_text,
                     source_doc_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (company_code, fiscal_year_end, quarter, metric_name,
                 value, raw_value, unit, confidence, raw_text,
                 source_doc_id, now, now),
            )
            return "inserted"

        old_val, old_raw, old_unit, old_conf = existing
        if old_val == value and old_raw == raw_value:
            return "no_change"

        self._exec_retry(
            """
            UPDATE order_metrics
            SET value=?, raw_value=?, unit=?, confidence=?, raw_text=?,
                source_doc_id=?, updated_at=?
            WHERE company_code=? AND fiscal_year_end=? AND quarter=? AND metric_name=?
            """,
            (value, raw_value, unit, confidence, raw_text,
             source_doc_id, now,
             company_code, fiscal_year_end, quarter, metric_name),
        )
        return "updated"

    def get_order_metrics(
        self, company_code: str, fiscal_year_end: str, quarter: str,
    ) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT * FROM order_metrics
            WHERE company_code=? AND fiscal_year_end=? AND quarter=?
            ORDER BY metric_name
            """,
            (company_code, fiscal_year_end, quarter),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ----------------------------------------------------------
    # quarantine — 抽出失敗・曖昧データの保留
    # ----------------------------------------------------------
    def quarantine_record(
        self,
        company_code: str,
        reason: str,
        *,
        fiscal_year_end: str = "",
        quarter: str = "",
        metric_type: str = "",
        detail: str = "",
        source_doc_id: str = "",
        failed_stage: str = "",
        review_hint: str = "",
    ) -> None:
        if not should_persist_intermediates():
            logger.info(
                "[quarantine] %s: %s (DB write skipped — persist OFF)",
                company_code, reason,
            )
            return
        now = _now_jst()
        self._exec_retry(
            """
            INSERT INTO quarantine
                (company_code, fiscal_year_end, quarter, metric_type,
                 reason, detail, source_doc_id,
                 failed_stage, review_hint, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (company_code, fiscal_year_end, quarter, metric_type,
             reason, detail, source_doc_id,
             failed_stage, review_hint, now),
        )
