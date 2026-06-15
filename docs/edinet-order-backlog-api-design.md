# EDINET受注データ API設計

> **作成日**: 2026-06-15  
> **対象リポジトリ**: `company-memo-app`  
> **DB変更**: なし（設計のみ）  
> **実装状態**: 未着手

---

## 0. 現状整理

### 既存の受注系テーブル

| テーブル | 役割 | 現状 |
|---|---|---|
| `order_kpis` | TDnet開示から自動抽出した受注KPI | **既存・Viewer表示済み** |
| `edinet_order_data` | EDINET有報から抽出した受注・受注残・RPO | **今回新設・DB保存済み** |

### order_kpis との違い

| 項目 | `order_kpis` | `edinet_order_data` |
|---|---|---|
| ソース | TDnet（四半期開示） | EDINET有報（年次） |
| 対象 | 受注高・受注残・繰越工事高 | 受注高・受注残・繰越工事高・完成工事高・**RPO** |
| 頻度 | 四半期ごと | 年次（fiscal_year単位） |
| 単位 | 統一済み（million_yen） | `source_unit` + 変換後値の二重保持 |
| 信頼度 | `confidence_score`（数値） | `confidence`（high/medium/low） |
| Viewer表示 | OrderKpiCard で表示済み | **未表示** |

---

## 1. どの API に載せるか

### 結論：**`viewer-api.ts` に新関数 `loadEdinetOrders()` を追加**

#### 選択理由

| 案 | メリット | デメリット | 採否 |
|---|---|---|---|
| **A. `viewer-api.ts` に追加** | 既存パターンと統一。追加コスト最小。RLS 動作確認済みパターン | viewer-api.ts が大きくなる | **採用** |
| B. `lib/edinet-orders-api.ts` 新ファイル | 関心分離が明確 | import 経路が増える。Viewer コンポーネント改修必要 |
| C. Next.js API Route `/api/edinet-orders` | SSR・認証確認が容易 | ネットワーク往復増。既存データ取得は全て client-side | |
| D. `financials` API に混ぜる | - | スキーマ・意味が全く異なる。コンフリクトリスク大 | **不採用** |

> [!IMPORTANT]
> `financials` API（`api_latest_financials_canonical`）とは**絶対に混ぜない**。  
> スキーマ・セマンティクス・期間単位が異なる。

---

## 2. 取得単位

### 結論：**ticker 単位で全年度取得。呼び出し側でフィルタ**

```ts
// 取得: ticker に紐づく全年度・全レコード
loadEdinetOrders(ticker: string): Promise<EdinetOrderRecord[]>
```

#### 理由

- `edinet_order_data` は現在 31社・各1年度のみ。将来複数年度が入っても最大 ~10件程度
- `fiscal_year` 別フィルタは Viewer の表示ロジックで制御する方が柔軟
- `order_kpis` の `loadOrderKpis()` も ticker 単位で全件取得しているため整合的

---

## 3. 返却型定義

### `types/edinet-order.ts`（新規作成）

```ts
// types/edinet-order.ts

/**
 * edinet_order_data テーブルから取得するレコード。
 * segment_name_key は generated column（確認用に含める）。
 * raw_* は表示には使わないが将来のデバッグ・監査用に含める。
 */
export interface EdinetOrderRecord {
    id: number;
    ticker: string;
    company_name: string | null;
    period: string;               // YYYY-MM-DD（決算期末日）
    fiscal_year: number;          // 年度（整数）
    segment_name: string | null;  // null = 連結全体
    segment_name_key: string;     // generated: COALESCE(segment_name, '__ALL__')
    source_type: string;          // "edinet_yuho"
    confidence: "high" | "medium" | "low";
    null_reason: string | null;

    // 百万円統一後（表示用）
    orders_received: number | null;
    order_backlog: number | null;
    construction_carryover: number | null;
    completed_construction: number | null;
    rpo: number | null;

    // 原単位（監査・デバッグ用）
    source_unit: string;          // million_yen / thousand_yen / billion_yen / unknown
    raw_orders_received: number | null;
    raw_order_backlog: number | null;
    raw_rpo: number | null;

    // メタ
    updated_at: string;
}

/**
 * confidence に応じた表示クラス
 */
export function edinetConfidenceClass(confidence: string): string {
    switch (confidence) {
        case "high":   return "confidence-high";
        case "medium": return "confidence-medium";
        case "low":    return "confidence-low";
        default:       return "confidence-unknown";
    }
}

/**
 * confidence に応じた日本語ラベル
 */
export function edinetConfidenceLabel(confidence: string): string {
    switch (confidence) {
        case "high":   return "確度高";
        case "medium": return "確度中";
        case "low":    return "データなし";
        default:       return confidence;
    }
}
```

---

## 4. `viewer-api.ts` に追加する関数

### `loadEdinetOrders(ticker)`

```ts
// lib/viewer-api.ts への追加（既存ファイルの末尾付近）

import type { EdinetOrderRecord } from "@/types/edinet-order";

/**
 * EDINET有価証券報告書から抽出した受注データを取得する。
 *
 * - テーブル: edinet_order_data
 * - RLS: allowed_users の email に含まれるユーザーのみ SELECT 可
 * - 取得単位: ticker 単位・全年度
 * - 順序: period DESC（最新年度が先頭）
 *
 * @param ticker 4桁銘柄コード
 * @returns EdinetOrderRecord[]（テーブル未存在・RLS拒否時は空配列）
 */
export async function loadEdinetOrders(ticker: string): Promise<EdinetOrderRecord[]> {
    const t = normalizeTicker(ticker);
    if (!t) return [];

    try {
        const { data, error } = await supabase
            .from("edinet_order_data")
            .select(
                "id,ticker,company_name,period,fiscal_year," +
                "segment_name,segment_name_key,source_type,confidence,null_reason," +
                "orders_received,order_backlog,construction_carryover," +
                "completed_construction,rpo," +
                "source_unit,raw_orders_received,raw_order_backlog,raw_rpo," +
                "updated_at"
            )
            .eq("ticker", t)
            .order("period", { ascending: false })
            .order("fiscal_year", { ascending: false })
            .limit(20);   // 年次なので最大 ~10期分もあれば十分

        if (error) {
            console.warn("[edinet_order_data] skip:", error.message);
            return [];
        }

        return (data ?? []) as EdinetOrderRecord[];
    } catch (err) {
        console.warn("[edinet_order_data] exception:", err);
        return [];
    }
}
```

### Supabase クエリ仕様

| 項目 | 内容 |
|---|---|
| テーブル | `edinet_order_data` |
| 認証 | anon key + RLS（allowed_users 条件） |
| select | 表示用カラム + raw_* + meta（`segment_name_key` 含む） |
| filter | `ticker = eq.{t}` |
| order | `period DESC` → `fiscal_year DESC` |
| limit | 20件（年次・複数年度対応） |
| エラー時 | 空配列で継続（Viewer は graceful degradation） |

---

## 5. Viewer 表示案

### 表示コンポーネント：`OrderKpiCard` の下に新設 or 拡張

#### 案A：`EdinetOrderCard` コンポーネント新規追加

```
┌─────────────────────────────────────────┐
│  受注データ（有報）          EDINET 2025  │
├─────────────────────────────────────────┤
│  受注高          1,773,567 百万円  ●     │
│  受注残高                    —          │
│  繰越工事高      2,514,070 百万円  ●     │
│  完成工事高      1,457,617 百万円  ●     │
│  RPO（残存義務）              —         │
├─────────────────────────────────────────┤
│  confidence: 高  source: EDINET有報      │
└─────────────────────────────────────────┘
```

#### 案B：`OrderKpiCard` に EDINET データを補助表示

既存 `order_kpis`（TDnet）と並べて、"有報確認値" として副表示。

#### 推奨：**案A（EdinetOrderCard 新規）**

- `order_kpis` とセマンティクスが異なる（年次 vs 四半期）
- RPO という新しいカテゴリを追加できる
- low データの扱いを独立して制御できる

---

## 6. low データ表示方針

| confidence | 表示方針 |
|---|---|
| `high` | 値をそのまま表示 |
| `medium` | 値を表示 + 「確度中」バッジ（注意促し） |
| `low` | 「データなし」と表示。値は表示しない |

### low データの null_reason 対応

| null_reason | 表示テキスト |
|---|---|
| `no_table_found` | 有報にデータなし |
| `unit_unclear` | 単位不明（要手動確認） |
| `null` | — |

```ts
// 実装イメージ
function renderOrderValue(value: number | null, confidence: string): string {
    if (confidence === "low") return "—";
    if (value === null) return "—";
    return value.toLocaleString() + " 百万円";
}
```

---

## 7. segment 対応将来設計

### 現状

- 現在 `segment_name = NULL`（連結全体のみ）で格納
- `segment_name_key = '__ALL__'`（generated column）

### 将来的な segment 対応計画

```
edinet_order_data
  (ticker='1802', period='2025-03-31', segment_name=NULL)        ← 現在格納済み（連結全体）
  (ticker='1802', period='2025-03-31', segment_name='建設事業')  ← 将来追加予定
  (ticker='1802', period='2025-03-31', segment_name='不動産事業') ← 将来追加予定
```

| 段階 | 内容 | 時期 |
|---|---|---|
| Phase 1（現在） | 連結全体のみ（`segment_name=NULL`） | 完了 |
| Phase 2 | セグメント別行を追加（`segment_name` 指定） | 抽出精度改善後 |
| Phase 3 | Viewer にセグメント切替UI | Phase 2 完了後 |

### Viewer 表示での segment 対応方針（Phase 1 時点）

- `segment_name_key = '__ALL__'` の行のみ取得・表示
- 将来 segment 行追加後は Viewer 側でフィルタ or 折りたたみ表示

---

## 8. RLS 確認事項

実装前に以下を確認する：

```sql
-- edinet_order_data の RLS policy 確認
SELECT policyname, cmd, qual
FROM pg_policies
WHERE tablename = 'edinet_order_data';

-- expected: SELECT policy = allowed_users の email に含まれるユーザーのみ
```

- `anon` ユーザー（未ログイン）は SELECT 不可
- `authenticated` ユーザーでも allowed_users に含まれない場合は SELECT 不可
- Viewer は認証済みユーザーのみアクセス可能のため、この制約は問題なし

---

## 9. 実装順序

```
[Step 1] types/edinet-order.ts 作成
[Step 2] lib/viewer-api.ts に loadEdinetOrders() 追加
[Step 3] components/EdinetOrderCard.tsx 作成（表示コンポーネント）
[Step 4] components/CompanyViewer.tsx に EdinetOrderCard を追加
[Step 5] 動作確認（1812 鹿島建設 で orders_received=1,773,567 が表示されるか）
[Step 6] confidence=low の表示確認（サンコール・ツガミ・SCREEN HD）
[Step 7] RPO 表示確認（SWCC=1,997、東京エレクトロン=225,019）
```

---

## 10. 未決定事項（実装前に確認）

> [!IMPORTANT]
> 以下はにゃもーんに確認が必要な設計判断事項です。

1. **表示場所**: `OrderKpiCard` の下 / 隣 / 別セクション のどれが良いか
2. **コンポーネント名**: `EdinetOrderCard` か別の名前か
3. **年度切替**: fiscal_year が複数ある場合、年度セレクタを出すか・最新のみか
4. **RPO のラベル**: 「RPO」「残存履行義務」「未充足の履行義務」のどれにするか
5. **低 confidence の扱い**: 非表示 vs バッジ付き表示 vs グレーアウト表示

---

## 11. commit 予定

| 対象ファイル | 変更内容 |
|---|---|
| `types/edinet-order.ts` | [NEW] 型定義 |
| `lib/viewer-api.ts` | [MODIFY] `loadEdinetOrders()` 追加 |
| `components/EdinetOrderCard.tsx` | [NEW] 表示コンポーネント |
| `components/CompanyViewer.tsx` | [MODIFY] EdinetOrderCard 組み込み |

> [!NOTE]
> **本ドキュメントはAPI設計のみ。実装はにゃもーんの承認後に実施。**  
> DB変更・Viewer変更・デプロイはまだ行っていない。
