# TDnet Pipeline — 優先タスク Top 10

> **設計目的**: 企業分析速度を極限まで上げる

---

## Priority 1 — Viewer セグメント source badge 修正

| 項目 | 内容 |
|---|---|
| **Why** | `api_latest_financials` に存在しない `source` カラムを select してエラー → 全データ取得失敗。修正は1行 (select から source 削除)。Viewer の基本表示が壊れている |
| **Difficulty** | ★☆☆☆☆ |
| **Files** | `lib/viewer-api.ts` |
| **Status** | 修正済み (未確認) |

---

## Priority 2 — XBRL Backfill Apply (A案完了)

| 項目 | 内容 |
|---|---|
| **Why** | `canonical_segments` に xbrl データが未投入。backfill 実行で 42 wide rows → 79 EAV rows が投入され、xbrl が excel_legacy より自動優先される。**VIEW 変更不要** |
| **Difficulty** | ★☆☆☆☆ (コマンド1つ) |
| **Files** | `tools/backfill_xbrl_to_canonical.py` |
| **Command** | `.\.venv\Scripts\python.exe tools/backfill_xbrl_to_canonical.py --apply` |

---

## Priority 3 — Financials の Canonical VIEW 統一

| 項目 | 内容 |
|---|---|
| **Why** | `api_latest_financials` は `canonical_financials` を参照しているが、source priority / 空行除外の VIEW 再作成がまだ。セグメントと同じ `ROW_NUMBER() + source_priority` パターンを適用すべき |
| **Difficulty** | ★★☆☆☆ |
| **Files** | `migrations/004_recreate_api_latest_financials.sql` (NEW), `lib/viewer-api.ts` |

---

## Priority 4 — セグメント日本語名マッピング

| 項目 | 内容 |
|---|---|
| **Why** | XBRL セグメント名が CamelCase 英語 ("Domestic Beverage Business") で返る。日本語ラベル ("国内飲料事業") へのマッピングがないと分析速度が落ちる |
| **Difficulty** | ★★★☆☆ |
| **Files** | `src/segment/xbrl_segment_extractor.py`, `src/segment/normalize.py` |

---

## Priority 5 — Cache GC (ディスク容量管理)

| 項目 | 内容 |
|---|---|
| **Why** | `data/tdnet_cache/` が無制限に膨張。source.pdf の二重保存は修正済みだが、古い filing の定期削除ポリシーがない。長期運用で数十GBになる |
| **Difficulty** | ★★☆☆☆ |
| **Files** | `tools/cleanup_intermediate_data.py` (拡張), `lib/backfill/cache.py` |

---

## Priority 6 — Supabase Index 最適化

| 項目 | 内容 |
|---|---|
| **Why** | `canonical_segments` / `canonical_financials` の `ticker + period + quarter` 複合インデックスが未確認。Viewer の ticker 検索が遅くなる可能性 |
| **Difficulty** | ★★☆☆☆ |
| **Files** | `migrations/005_add_indexes.sql` (NEW) |

---

## Priority 7 — Viewer セグメント分析機能

| 項目 | 内容 |
|---|---|
| **Why** | セグメントデータは取得できるが、前年同期比・構成比・トレンドグラフがない。数値の並列表示だけでは分析速度が上がらない |
| **Difficulty** | ★★★☆☆ |
| **Files** | `components/SegmentTable.tsx`, `app/globals.css` |

---

## Priority 8 — KPI 自動生成

| 項目 | 内容 |
|---|---|
| **Why** | KPI (営業利益率, ROE, ROIC 等) が手動入力。canonical_financials からの自動算出ロジックを追加すれば分析効率が大幅向上 |
| **Difficulty** | ★★★☆☆ |
| **Files** | `lib/kpi-api.ts` (拡張), `components/KpiTable.tsx`, `lib/viewer-api.ts` |

---

## Priority 9 — パイプライン自動化強化

| 項目 | 内容 |
|---|---|
| **Why** | 日次実行は Windows Task Scheduler だが、失敗時の自動リトライ・エラー集計・成功率グラフがない。Discord 通知は実装済みだが、ダッシュボードがない |
| **Difficulty** | ★★★★☆ |
| **Files** | `tools/pipeline_run.py`, `tools/pipeline_summary_report.py` |

---

## Priority 10 — XBRL Parallel Extraction

| 項目 | 内容 |
|---|---|
| **Why** | 現在は filing を逐次処理。XBRL 抽出は I/O バウンドなので並列化で 3-5x 高速化が見込める。backfill 時間を数時間→数十分に短縮 |
| **Difficulty** | ★★★★☆ |
| **Files** | `lib/backfill/phase2_runner.py`, `lib/backfill/worker.py` |

---

## 完了済みタスク (参考)

| タスク | 状態 |
|---|---|
| TDnet Ingest | ✅ |
| PL 抽出 (PDF+XBRL) | ✅ |
| セグメント抽出 (PDF+XBRL) | ✅ |
| Quarantine + retry | ✅ |
| Canonical DB (financials+segments) | ✅ |
| Source priority VIEW | ✅ |
| XBRL dual-write (sync_segments.py) | ✅ |
| XBRL backfill スクリプト | ✅ |
| PDF 二重保存修正 | ✅ |
| Viewer セグメント source badge | ✅ (修正済み) |
