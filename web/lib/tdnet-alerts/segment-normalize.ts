/**
 * segment-normalize.ts
 *
 * ビュアー表示用のセグメント表記統合ロジック。
 * DB・Supabase のデータは変更せず、表示時のみ使用する。
 */

import type { SegmentRow } from "./queries";

// ================================================================
// normalizeSegmentDisplayKey
// ================================================================

/**
 * segment_name を表示統合キーに正規化する。
 *
 * 同じセグメントが表記揺れ（全角スペース・英日混在・略語差）で
 * 別列にならないよう、比較・グルーピング用キーを生成する。
 */
export function normalizeSegmentDisplayKey(name: string | null | undefined): string {
  if (!name) return "";

  // 1. Unicode NFKC 正規化
  let s = name.normalize("NFKC");

  // 2. 小文字化
  s = s.toLowerCase();

  // 3. 全角スペース・半角スペース・タブ・改行を削除
  s = s.replace(/[\s\u3000\t\r\n]+/g, "");

  // 4. 全角 ＆ → &
  s = s.replace(/＆/g, "&");

  // 5. "and" → "&"
  s = s.replace(/\band\b/g, "&");
  // スペース除去後も "and" が残るケースに対応 (例: "mobilityandtelematics")
  // → 後の表記揺れ置換で対応

  // 6. 不要語を除去（比較キーから除外）
  const stopWords = [
    "分野", "事業", "セグメント", "セクター",
    "sector", "business", "division",
    "services", "service",
  ];
  for (const w of stopWords) {
    s = s.split(w).join("");
  }

  // 7. 表記揺れを統一語に置換
  // ── モビリティ系 ──
  s = s.replace(/automotive|automobile|自動車|車載|モビリティ|mobility/g, "mobility");

  // ── テレマティクス ──
  s = s.replace(/telematics|テレマティクス/g, "telematics");

  // ── エンタテインメント ──
  s = s.replace(/entertainment|エンタテインメント|エンターテインメント/g, "entertainment");

  // ── ソリューション ──
  s = s.replace(/solutions|solution|ソリューションズ|ソリューション/g, "solutions");

  // ── セーフティ ──
  s = s.replace(/safety|セーフティ|安全/g, "safety");

  // ── セキュリティ ──
  s = s.replace(/security|セキュリティ/g, "security");

  // ── その他 ──
  s = s.replace(/^other$|その他/g, "other");

  // 8. "mobilityandtelematics" → "mobility&telematics" など
  //    "and" を "&" に寄せる（スペース除去後のケース）
  s = s.replace(/mobilityandtelematics/g, "mobility&telematics");
  s = s.replace(/([a-z\u3040-\u30ff\u4e00-\u9fff])and([a-z\u3040-\u30ff\u4e00-\u9fff])/g, "$1&$2");

  // 9. 最終クリーンアップ: 残った空白・記号除去
  s = s.replace(/[\s\u3000]+/g, "");

  return s;
}


// ================================================================
// pickSegmentDisplayName
// ================================================================

const _JP_RE = /[\u3040-\u30ff\u4e00-\u9fff\uff01-\uffee]/;

/**
 * 同じ segment_display_key グループ内の名前群から表示名を1つ選ぶ。
 *
 * 優先: 日本語含む → 長い → 先頭
 */
export function pickSegmentDisplayName(names: string[]): string {
  if (names.length === 0) return "";
  if (names.length === 1) return names[0];

  const jpNames = names.filter((n) => _JP_RE.test(n));
  const pool = jpNames.length > 0 ? jpNames : names;

  // 最も長い名前を返す
  return pool.reduce((best, cur) => (cur.length >= best.length ? cur : best), pool[0]);
}


// ================================================================
// buildSegmentViewData
// ================================================================

export interface SegmentViewRow {
  display_key: string;
  display_name: string;
  segment_sales: number | null;
  segment_profit: number | null;
  period: string | null;
  quarter: string | null;
  data_source: string | null;
}

/**
 * rawRows（Supabase から取得した SegmentRow[]）を
 * 表示統合キー単位で集約し、ビュアー描画用データを返す。
 *
 * - 同じ ticker/period/quarter/display_key に複数行あれば最後の行を採用（合算しない）
 * - 表示名は pickSegmentDisplayName で決定
 * - rawRows が変わらない限り再計算しない（呼び出し元で useMemo を使うこと）
 */
export function buildSegmentViewData(rawRows: SegmentRow[]): SegmentViewRow[] {
  if (rawRows.length === 0) return [];

  // キー → { 名前一覧, 最後に採用する row }
  const keyMap = new Map<
    string,
    { names: string[]; row: SegmentRow }
  >();

  for (const row of rawRows) {
    const dk = normalizeSegmentDisplayKey(row.segment_name);
    if (!dk) continue;

    const existing = keyMap.get(dk);
    if (!existing) {
      keyMap.set(dk, { names: [row.segment_name], row });
    } else {
      // 名前候補を追加（重複排除）
      if (!existing.names.includes(row.segment_name)) {
        existing.names.push(row.segment_name);
      }
      // updated_at がある場合は新しい方、なければ後勝ち
      const cur = existing.row as SegmentRow & { updated_at?: string };
      const nxt = row as SegmentRow & { updated_at?: string };
      if (nxt.updated_at && cur.updated_at && nxt.updated_at > cur.updated_at) {
        existing.row = row;
      } else if (!cur.updated_at || !nxt.updated_at) {
        existing.row = row; // updated_at なければ後勝ち
      }
    }
  }

  const result: SegmentViewRow[] = [];
  for (const [dk, { names, row }] of keyMap.entries()) {
    result.push({
      display_key: dk,
      display_name: pickSegmentDisplayName(names),
      segment_sales: row.segment_sales,
      segment_profit: row.segment_profit,
      period: row.period,
      quarter: row.quarter,
      data_source: row.data_source,
    });
  }

  return result;
}
