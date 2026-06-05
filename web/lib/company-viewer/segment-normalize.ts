/**
 * segment_name 正規化ユーティリティ
 *
 * overlay 解決時の照合キーとして使用する。
 * 保存時は元の表示名を保持し、照合は normalized key で行う。
 */

/**
 * segment_name を正規化して照合キーを生成する。
 *
 * - trim
 * - 全角スペース → 半角スペース
 * - 改行・タブ除去
 * - 連続空白 → 単一スペース
 * - 小文字化 (case-insensitive match)
 */
export function normalizeSegmentName(raw: string | null | undefined): string {
    if (!raw) return "";
    let s = String(raw);

    // 全角スペース → 半角
    s = s.replace(/\u3000/g, " ");

    // 改行・タブ → スペース
    s = s.replace(/[\r\n\t]/g, " ");

    // trim
    s = s.trim();

    // 連続スペース → 単一
    s = s.replace(/\s{2,}/g, " ");

    // 小文字化
    s = s.toLowerCase();

    return s;
}

/**
 * overlay 解決用の複合キーを生成する。
 * fiscal_year + quarter + normalized_segment_name + metric
 */
export function buildOverrideKey(
    fiscalYear: number,
    quarter: string,
    segmentName: string,
    metric: string,
): string {
    const norm = normalizeSegmentName(segmentName);
    return `${fiscalYear}|${quarter}|${norm}|${metric}`;
}

// ============================================================
// 表示統合キー — 英日混在・スペース差・語尾差を吸収
// ============================================================

/**
 * segment_name を表示統合用キーに正規化する。
 *
 * 方針（2026-05以降）:
 *   日英統合なし。segment_name のみを最小正規化してキーとする。
 *   NFKC + lower + 空白除去のみ。
 *   意味ベースのセマンティック置換（chemical|化学 → chemical 等）は廃止。
 *   英語名と日本語名は異なる display_key になり、別列として表示される。
 *
 * NOTE: 旧ロジックの normalizeSegmentAliasKey / normalizeSegmentSemanticKey は
 *       互換性のため関数定義を残すが、このキー生成では使用しない。
 */
export function normalizeSegmentDisplayKey(name: string | null | undefined): string {
    if (!name) return "";

    // 1. NFKC 正規化（全角英数→半角）
    let s = name.normalize("NFKC");

    // 2. 小文字化
    s = s.toLowerCase();

    // 3. 全角スペース・空白・タブ・改行を削除
    s = s.replace(/[\s\u3000\t\r\n]+/g, "");

    // 4. 不要記号除去（括弧・中黒等）
    s = s.replace(/[（）()・\-_【】「」『』★☆●○◆◇■□。、．!！?？]+/g, "");

    // 5. 残った空白除去
    s = s.replace(/\s+/g, "");

    return s;
}


/**
 * TDNET英語セグメント名を、EDINET日本語候補テキストへ変換する。
 * FinancialsTable で日本語アンカーへの吸着に使用する。
 * 変換できない場合は "" を返す（呼び出し側でフォールバック）。
 *
 * 変換例:
 *   "HR Platform Business"              → "hrプラットフォーム"
 *   "Local Information Service Business"→ "地域情報サービス"
 *   "Recruiting Business"               → "リクルーティング"
 *   "Human Resource Business"           → "人材サービス"
 *   "Overseas Business"                 → "海外"
 */
export function normalizeSegmentAliasKey(name: string): string {
    if (!name) return "";
    // 全角→半角 + 小文字化 + trim
    const s = name.normalize("NFKC").toLowerCase().trim();

    // フレーズ照合（具体的・長いものを先に）
    const PHRASE_MAP: [RegExp, string][] = [
        [/hr\s*platform/,                  "hrプラットフォーム"],
        [/local\s*information\s*service/,  "地域情報サービス"],
        [/information\s*publishing/,       "情報出版"],
        [/human\s*resources?\b/,           "人材サービス"],
        [/\brecruit(?:ing|ment)?\b/,       "リクルーティング"],
        [/\boverseas\b/,                   "海外"],
        [/real\s*estate/,                  "不動産"],
        [/\blogistics\b/,                  "物流"],
        [/\bfinancial\b/,                  "金融"],
        [/\bretail\b/,                     "小売"],
        [/\bwholesale\b/,                  "卸売"],
        [/\bconstruction\b/,               "建設"],
        [/\bmanufacturing\b/,              "製造"],
        [/\beducation\b/,                  "教育"],
        [/\bhealthcare\b/,                 "医療"],
        [/\bmedical\b/,                    "医療"],
        [/\bmedia\b/,                      "メディア"],
        [/\benergy\b/,                     "エネルギー"],
        [/\benvironment/,                  "環境"],
        [/\bpublishing\b/,                 "出版"],
        [/\badvertis/,                     "広告"],
    ];

    for (const [pattern, candidate] of PHRASE_MAP) {
        if (pattern.test(s)) return candidate;
    }

    // 複合語 includes 判定（単純 regex で捕捉しにくい複数単語の組み合わせ）
    if (s.includes("wiring")) return "配線器具";
    if (
        s.includes("electric") &&
        (s.includes("material") || s.includes("facility") || s.includes("water"))
    ) return "電材";
    if (s.includes("other")) return "その他";

    return "";
}

/**
 * 日英両方のセグメント名から共通の意味キーを返す。
 * jpSemanticAnchorMap 構築と resolveDk での日英統合照合に使用する。
 * 変換できない場合は normalizeSegmentDisplayKey(name) の結果をそのまま返す。
 *
 * 例:
 *   "Wiring Devices"                                → "wiring_devices"
 *   "配線器具"                                       → "wiring_devices"
 *   "Electric Facility Materials And Water Supply"  → "electric_facility_materials_water_supply"
 *   "電材及び管材"                                   → "electric_facility_materials_water_supply"
 */
export function normalizeSegmentSemanticKey(name: string): string {
    const s = normalizeSegmentDisplayKey(name);

    if (s.includes("wiring") || s.includes("配線器具")) {
        return "wiring_devices";
    }
    if (
        s.includes("electric") ||
        s.includes("facility") ||
        s.includes("material") ||
        s.includes("water") ||
        s.includes("supply") ||
        s.includes("電材") ||
        s.includes("管材") ||
        s.includes("電設資材") ||
        s.includes("電気設備資材")
    ) {
        return "electric_facility_materials_water_supply";
    }
    if (s.includes("other") || s.includes("その他")) {
        return "other";
    }
    // 電子デバイス（Electronic Devices / 電子デバイス）
    if (s.includes("electronicdevices") || s.includes("電子デバイス")) {
        return "electronic_devices";
    }
    // 精密成形品（Precision Molding Products / 精密成形品）
    if (s.includes("precisionmolding") || s.includes("精密成形")) {
        return "precision_molding_products";
    }
    // 住環境・生活資材（Housing And Living Materials / 住環境・生活資材）
    if (s.includes("housingliving") || s.includes("住環境") || s.includes("housing&living")) {
        return "housing_living_materials";
    }

    // ── 地域エリア系 semanticKey ─────────────────────────────────

    // 東北エリア
    if (s.includes("tohoku") || s.includes("東北")) {
        return "area_tohoku";
    }
    // 北関東エリア
    if (s.includes("northkanto") || s.includes("北関東")) {
        return "area_north_kanto";
    }
    // 関西エリア
    if (s.includes("kansai") || s.includes("関西")) {
        return "area_kansai";
    }
    // 首都圏エリア
    if (s.includes("metropolitan") || s.includes("首都圏")) {
        return "area_metropolitan";
    }

    // ── 商社・重工系・重工業 semanticKey ───────────────────────────


    // 輸送機・建機（"Transportation And Constructi" 途中切れも対応）
    if (s.includes("transportation") || s.includes("輸送機")) {
        return "transportation_construction";
    }
    // 都市総合開発
    if (s.includes("urban") || s.includes("都市総合開発")) {
        return "urban_development";
    }
    // グリーンインフラ（infrastructure 単独より前に評価）
    if (s.includes("greeninfrastructure") || s.includes("グリーンインフラ")) {
        return "green_infrastructure";
    }
    // サプライチェーン
    if (s.includes("supplychain") || s.includes("サプライチェーン")) {
        return "supply_chain";
    }
    // メディア・デジタル複合（media単独より前に評価）
    if (s.includes("media") && (s.includes("digital") || s.includes("デジタル"))) {
        return "media_digital";
    }
    // デジタルソリューション
    if (s.includes("digital") || s.includes("デジタル")) {
        return "digital_solutions";
    }
    // サーキュラーエコノミー
    if (s.includes("circular") || s.includes("サーキュラー")) {
        return "circular_economy";
    }
    // アフリカ
    if (s.includes("africa") || s.includes("アフリカ")) {
        return "africa";
    }
    // 鉄鋼製品
    if (s.includes("鉄鋼") || s.includes("steel") || s.includes("iron")) {
        return "iron_steel_products";
    }
    // 機械・インフラ複合（プラントプロジェクト含む）
    if (
        s.includes("機械") || s.includes("machinery") ||
        s.includes("プラント") || s.includes("plant") ||
        (s.includes("インフラ") && s.includes("機械")) ||
        (s.includes("infrastructure") && s.includes("machinery"))
    ) {
        return "machinery_infrastructure";
    }
    // インフラ単独
    if (s.includes("インフラ") || s.includes("infrastructure")) {
        return "infrastructure";
    }
    // エネルギー×化学品 / 資源×化学品 → energy に統合（metals/chemicals より前）
    if (
        (s.includes("energy") || s.includes("資源") || s.includes("resource")) &&
        (s.includes("chemical") || s.includes("化学"))
    ) {
        return "energy";
    }
    // 金属・資源（メタル含む）—「資源」単独は化学複合除外後のみ
    if (
        s.includes("金属") || s.includes("メタル") || s.includes("鉱物") ||
        s.includes("資源") ||
        s.includes("mineral") || s.includes("metal")
    ) {
        return "metals_minerals";
    }
    // 注意: "化学" / "chemical" の semantic key 統合は削除済み (2026-05)
    //   理由: 基礎化学品事業・精密化学品事業・Fine/Fundamental/Ferro Chemicals Division 等
    //   "化学/chemical" を含む異なるセグメントが全て "chemicals" に統合されてしまい、
    //   別々のセグメント列が1列に潰れる問題を引き起こすため。
    //   各 segment_name は baseDk (normalize_display_key そのまま) で別列として扱う。

    // 繊維
    if (s.includes("繊維") || s.includes("textile")) {
        return "textile";
    }
    // 食料・食品（foodservice は除外、食料×生活産業複合は lifestyle に任せる）
    if (
        (s.includes("食料") && !s.includes("生活産業")) ||
        s.includes("食品") ||
        (s.includes("food") && !s.includes("foodservice"))
    ) {
        return "food";
    }
    // グローバル部品・ロジスティクス → mobility
    if (s.includes("グローバル部品")) {
        return "mobility";
    }
    // 生活産業・住生活・ライフスタイル・生活×不動産・General Products
    if (
        s.includes("住生活") || s.includes("生活産業") || s.includes("ライフスタイル") ||
        (s.includes("生活") && s.includes("realestate")) ||
        s.includes("lifestyle") || s.includes("generalproducts")
    ) {
        return "lifestyle_general_products_realty";
    }
    // 情報・金融（ICT / Information × Finance の複合）
    if (
        s.includes("ict") ||
        ((s.includes("information") || s.includes("情報")) && s.includes("finance"))
    ) {
        return "information_finance";
    }
    // 第8・第八系
    if (
        s.includes("the8th") || s.includes("第8") ||
        s.includes("第八") || s.includes("eighth")
    ) {
        return "eighth";
    }
    // エネルギートランスフォーメーション → energy に統合
    if (s.includes("energy") && (s.includes("transformation") || s.includes("トランスフォーメーション"))) {
        return "energy";
    }

    return s;
}

/**
 * 表示名を返す。segment_name をそのまま返す。
 *
 * 方針（2026-05以降）:
 *   日本語優先などの選択ロジックを廃止。
 *   names[0]（最初に登録された segment_name）をそのまま返す。
 *   DB の segment_name が表示名になる。
 */
export function pickSegmentDisplayName(names: string[]): string {
    if (names.length === 0) return "";
    return names[0];
}

