# ============================================================
# segment_extractor_ocr.py — OCRテキストからセグメント抽出
# ============================================================
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .layout_reconstruct import split_to_lines, find_all_anchor_regions
from .text_normalize import extract_numbers

logger = logging.getLogger("tdnet")

# セグメント表アンカーキーワード
_SEGMENT_ANCHORS = [
    "報告セグメント",
    "事業セグメント",
    "セグメント情報",
    "セグメント別",
    "事業別",
    "セグメントの業績",
    "セグメント利益",
]

# 売上系ヘッダーキーワード（アンカー周辺に存在するか確認用）
_SALES_HINTS = [
    "売上高", "売上収益", "営業収益", "収益", "売上",
    "外部顧客", "事業収益",
]

# 利益系ヘッダーキーワード
_PROFIT_HINTS = [
    "セグメント利益", "営業利益", "利益", "損益",
]

# 除外ラベル（部分一致）
_SKIP_LABELS = {
    "合計", "総計", "調整額", "消去", "消去又は全社",
    "全社", "配賦不能", "セグメント間", "内部取引",
    "調整", "小計",
}

# 除外ラベル（完全一致）
_SKIP_EXACT = {"計"}

# ヘッダー行の判定キーワード
_HEADER_KEYWORDS = [
    "報告セグメント", "セグメント情報", "セグメント別", "事業セグメント",
    "百万円", "千円", "億円", "単位",
    "前年同期", "増減", "前期", "当期",
]

# ── region棄却用: 目次/表紙/注記見出しキーワード ──
_TOC_REJECT_KEYWORDS = [
    "連結貸借対照表",
    "連結損益計算書",
    "連結包括利益計算書",
    "連結キャッシュ・フロー",
    "連結株主資本等変動計算書",
    "注記事項",
    "中間連結",
    "四半期連結",
    "目次",
    "ページ",
]

# ── region採用に必要な表ヘッダー候補 ──
_TABLE_HEADER_EVIDENCE = [
    "売上高", "売上収益", "営業収益", "営業利益",
    "セグメント利益", "セグメント損益",
    "外部顧客への売上高", "外部顧客への売上収益",
    "利益又は損失",
]


@dataclass
class OcrSegment:
    """OCRから抽出されたセグメント1件"""
    segment_name: str
    segment_order: int
    segment_sales: int | None = None
    segment_profit: int | None = None


@dataclass
class OcrSegmentResult:
    """OCRセグメント抽出結果"""
    segments: list[OcrSegment] = field(default_factory=list)
    success: bool = False
    reason: str = ""


def _is_skip_label(name: str) -> bool:
    """除外ラベルかどうか判定"""
    stripped = name.strip()
    if stripped in _SKIP_EXACT:
        return True
    for skip in _SKIP_LABELS:
        if skip in stripped:
            return True
    return False


def _is_header_line(line: str) -> bool:
    """ヘッダー/見出し行かどうか判定"""
    for kw in _HEADER_KEYWORDS:
        if kw in line:
            return True
    return False


def _detect_columns(region_text: str) -> tuple[bool, bool]:
    """領域テキストから売上列・利益列の存在を判定"""
    has_sales = any(kw in region_text for kw in _SALES_HINTS)
    has_profit = any(kw in region_text for kw in _PROFIT_HINTS)
    return has_sales, has_profit


def _extract_segment_name(line: str) -> str | None:
    """行からセグメント名候補を抽出する。"""
    # パターン1: 先頭の非数値テキスト
    m = re.match(r'^([^\d△▲\-－]+)', line)
    if m:
        name = m.group(1).strip()
        if len(name) >= 2:
            return name

    # パターン2: 最初のトークンが日本語
    tokens = line.split()
    if tokens:
        first = tokens[0]
        if re.match(r'^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3000-\u303Fー・]+$', first) and len(first) >= 2:
            return first

    return None


def _score_region(region: list[str]) -> int:
    """
    regionがセグメント表らしいかスコアリングする。

    加点:
      +3: 表ヘッダー候補（売上高/営業利益等）を含む行がある
      +2: 数値を含む行が3行以上ある
      +1: 数値を含む行が1行以上ある

    減点:
      -2: 目次/注記見出しキーワードが2つ以上ある
      -3: 数値を含む行が0
    """
    region_text = "\n".join(region)
    score = 0

    # 表ヘッダー証拠
    header_hits = sum(1 for kw in _TABLE_HEADER_EVIDENCE if kw in region_text)
    if header_hits >= 2:
        score += 5
    elif header_hits >= 1:
        score += 3

    # 数値行カウント
    num_lines = sum(1 for line in region if re.search(r'\d{3}', line))
    if num_lines >= 3:
        score += 2
    elif num_lines >= 1:
        score += 1
    else:
        score -= 3

    # 目次/注記の汚染
    toc_hits = sum(1 for kw in _TOC_REJECT_KEYWORDS if kw in region_text)
    if toc_hits >= 3:
        score -= 4
    elif toc_hits >= 2:
        score -= 2

    return score


def _select_best_region(
    lines: list[str],
) -> list[str] | None:
    """
    全アンカー候補からスコアで最適なregionを選択する。
    スコア0以下のregionは棄却。
    """
    candidates = find_all_anchor_regions(
        lines, _SEGMENT_ANCHORS, before=3, after=25,
    )

    if not candidates:
        return None

    best_region = None
    best_score = -999
    best_kw = ""
    best_idx = -1

    for region, anchor_idx, kw in candidates:
        score = _score_region(region)
        logger.info(
            f"[segment-ocr] candidate: anchor_idx={anchor_idx}, "
            f"kw={kw!r}, score={score}, lines={len(region)}"
        )
        if score > best_score:
            best_score = score
            best_region = region
            best_kw = kw
            best_idx = anchor_idx

    if best_score <= 0:
        logger.info(
            f"[segment-ocr] all candidates rejected (best_score={best_score}, "
            f"kw={best_kw!r}, idx={best_idx})"
        )
        return None

    logger.info(
        f"[segment-ocr] selected: anchor_idx={best_idx}, "
        f"kw={best_kw!r}, score={best_score}"
    )
    return best_region


def extract_segments_from_ocr_text(ocr_text: str) -> OcrSegmentResult:
    """
    OCRテキストからセグメント情報を抽出する。

    改善版:
    1. 全アンカー候補を検出
    2. 各regionをスコアリング（表ヘッダー証拠/数値行/目次汚染）
    3. 最高スコアのregionを選択（スコア0以下は棄却）
    4. セグメント名 + 数値を解析

    成功条件: セグメント2件以上 & (売上2件以上 or 利益2件以上)
    """
    lines = split_to_lines(ocr_text)
    if not lines:
        return OcrSegmentResult(reason="no_lines")

    # アンカー検出 + スコアリングで最適region選択
    region = _select_best_region(lines)
    if region is None:
        logger.info("[segment-ocr] no valid anchor region found")
        return OcrSegmentResult(reason="no_valid_segment_region")

    logger.info(f"[segment-ocr] anchor found, region={len(region)} lines")

    # デバッグ: 全region行をダンプ
    for ri, rline in enumerate(region):
        logger.debug(f"[segment-ocr] region[{ri:2d}]: {rline}")

    region_text = "\n".join(region)
    has_sales, has_profit = _detect_columns(region_text)
    logger.info(f"[segment-ocr] has_sales={has_sales}, has_profit={has_profit}")

    # ── パス1: 横型テーブル（名前+数値が同一行） ──
    segments: list[OcrSegment] = []
    order = 0

    for line in region:
        stripped = line.strip()

        if not stripped or len(stripped) > 100:
            continue
        if _is_header_line(stripped):
            continue

        seg_name = _extract_segment_name(stripped)
        nums = extract_numbers(stripped)

        logger.debug(
            f"[segment-ocr] LINE: name={seg_name!r}, "
            f"nums={nums}, raw={stripped!r}"
        )

        if seg_name is None or not nums:
            continue
        if _is_skip_label(seg_name):
            continue

        seg_sales, seg_profit = _assign_values(nums, has_sales, has_profit)

        order += 1
        segments.append(OcrSegment(
            segment_name=seg_name,
            segment_order=order,
            segment_sales=seg_sales,
            segment_profit=seg_profit,
        ))
        logger.info(
            f"[segment-ocr] HORIZONTAL: [{order}] {seg_name} "
            f"sales={seg_sales}, profit={seg_profit}"
        )

    # 横型で十分なら返す
    if _check_success(segments):
        return _build_result(segments)

    # ── パス2: 縦型テーブル復元（名前列と数値列が分離） ──
    logger.info(
        f"[segment-ocr] horizontal insufficient (seg={len(segments)}), "
        f"trying vertical reconstruction"
    )
    vertical_segments = _reconstruct_vertical_table(region, has_sales, has_profit)

    if vertical_segments and _check_success(vertical_segments):
        return _build_result(vertical_segments)

    # 両方不十分
    all_segs = vertical_segments if len(vertical_segments) > len(segments) else segments
    sales_count = sum(1 for s in all_segs if s.segment_sales is not None)
    profit_count = sum(1 for s in all_segs if s.segment_profit is not None)
    logger.info(
        f"[segment-ocr] insufficient: segments={len(all_segs)}, "
        f"sales={sales_count}, profit={profit_count}"
    )
    return OcrSegmentResult(
        segments=all_segs,
        reason=f"insufficient: seg={len(all_segs)}, sales={sales_count}, profit={profit_count}",
    )


def _assign_values(
    nums: list[int], has_sales: bool, has_profit: bool,
) -> tuple[int | None, int | None]:
    """数値リストを売上/利益に割り当てる。"""
    seg_sales: int | None = None
    seg_profit: int | None = None

    if has_sales and has_profit:
        if len(nums) >= 2:
            seg_sales = nums[0]
            seg_profit = nums[1]
        elif len(nums) == 1:
            seg_sales = nums[0]
    elif has_sales:
        seg_sales = nums[0]
    elif has_profit:
        seg_profit = nums[0]
    else:
        seg_sales = nums[0]
        if len(nums) >= 2:
            seg_profit = nums[1]

    return seg_sales, seg_profit


def _check_success(segments: list[OcrSegment]) -> bool:
    """成功条件: セグメント2件以上 & (売上2件以上 or 利益2件以上)"""
    if len(segments) < 2:
        return False
    sales_count = sum(1 for s in segments if s.segment_sales is not None)
    profit_count = sum(1 for s in segments if s.segment_profit is not None)
    return sales_count >= 2 or profit_count >= 2


def _build_result(segments: list[OcrSegment]) -> OcrSegmentResult:
    """成功結果を生成"""
    sales_count = sum(1 for s in segments if s.segment_sales is not None)
    profit_count = sum(1 for s in segments if s.segment_profit is not None)
    logger.info(
        f"[segment-ocr] segments extracted count={len(segments)}, "
        f"sales={sales_count}, profit={profit_count}"
    )
    return OcrSegmentResult(segments=segments, success=True)


# ── セグメント名バリデーション用 ──
# 助詞・助動詞（文章判定用）
_SENTENCE_PARTICLES = re.compile(r'[はがのをにでともからながらけどしてへより]')
# 装飾文字
_DECORATION_CHARS = re.compile(r'[≪≫《》【】「」『』◆■□●○★☆※◇▼▽〔〕＜＞]')
# 文中複合パターン（文章の強指標）
_SENTENCE_COMPOUND = re.compile(
    r'(?:について|における|ための|として|による|に関する'
    r'|に対する|に伴う|を含む|を除く|であり|であった'
    r'|ている|ており|のうち|に基づ)'
)
# セグメント名に含まれやすいキーワード（長めでも許可）
_SEGMENT_NAME_HINTS = [
    "事業", "部門", "サービス", "セグメント",
    "部", "本部", "カンパニー", "グループ",
    "不動産", "建設", "製造", "流通", "金融",
    "海外", "国内", "日本", "アジア", "欧州", "米国",
]


def _is_valid_segment_name(text: str) -> bool:
    """
    テキストがセグメント名として有効かどうかを判定する。

    有効:
    - 日本語2〜15文字程度の短い名前（東日本、関東、駐車場事業 等）
    - 「事業」「部門」等のヒントを含む短文

    無効:
    - 文章（助詞を含む長文、複合パターン「について」等）
    - 英字のみ4文字以下（LO, AC, NOTE 等のOCRノイズ）
    - 装飾タイトル（≪...≫ 〔...〕 等）
    """
    # 装飾文字を含む → タイトル行
    if _DECORATION_CHARS.search(text):
        return False

    # ASCII英字のみで4文字以下 → OCRノイズ (LO, AC, RE, NOTE, ITEM 等)
    if re.match(r'^[A-Za-z]{1,4}$', text):
        return False

    # 文中複合パターン → 文章
    if _SENTENCE_COMPOUND.search(text):
        return False

    # セグメント名ヒントを含む → 長めでもOK（最大20文字）
    if any(hint in text for hint in _SEGMENT_NAME_HINTS):
        return len(text) <= 20

    # 15文字超 → 長すぎ
    if len(text) > 15:
        return False

    # 助詞が2個以上 → 文章
    particles = _SENTENCE_PARTICLES.findall(text)
    if len(particles) >= 2:
        return False

    return True


def _is_name_only_line(line: str) -> str | None:
    """
    テキストのみ（数値なし）の行からセグメント名候補を返す。
    数値を含む場合はNone。
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 30:
        return None
    # 数字を含む行は名前行ではない
    if re.search(r'\d', stripped):
        return None
    # ヘッダー行は除外
    if _is_header_line(stripped):
        return None
    # 除外ラベル
    if _is_skip_label(stripped):
        return None
    # セグメント名バリデーション
    if not _is_valid_segment_name(stripped):
        logger.debug(f"[segment-ocr] REJECT name: {stripped!r}")
        return None
    # 2文字以上
    if len(stripped) >= 2:
        return stripped
    return None


def _is_number_only_line(line: str) -> list[int] | None:
    """
    数値のみ（または数値中心）の行から数値リストを返す。
    名前テキストが主体の場合はNone。
    """
    stripped = line.strip()
    if not stripped:
        return None
    nums = extract_numbers(stripped)
    if not nums:
        return None
    # 行の大半が数値/記号で構成されているか確認
    text_without_nums = re.sub(r'[△▲\-－\d,.\s]+', '', stripped)
    if len(text_without_nums) > 5:
        # テキスト部分が長い → 名前+数値の混合行（横型で処理済み）
        return None
    return nums


def _reconstruct_vertical_table(
    region: list[str],
    has_sales: bool,
    has_profit: bool,
) -> list[OcrSegment]:
    """
    縦型テーブル復元: 名前ブロックと数値ブロックを検出しペアリングする。

    OCRが表を縦に読み取った場合:
        東日本      ←名前ブロック
        関東
        東海
        487         ←数値ブロック
        3095
        246

    名前ブロックと数値ブロックを連続行として収集し、
    インデックスで対応付ける。
    """
    # Phase 1: 各行を分類
    classified: list[tuple[str, str | None, list[int] | None]] = []
    #  (type, name_or_none, nums_or_none)
    #  type: "name", "nums", "skip"

    for line in region:
        stripped = line.strip()
        if not stripped or len(stripped) > 100:
            classified.append(("skip", None, None))
            continue
        if _is_header_line(stripped):
            classified.append(("skip", None, None))
            continue

        name = _is_name_only_line(stripped)
        if name is not None:
            classified.append(("name", name, None))
            continue

        nums = _is_number_only_line(stripped)
        if nums is not None:
            classified.append(("nums", None, nums))
            continue

        classified.append(("skip", None, None))

    # デバッグ: 分類結果
    for ci, (ctype, cname, cnums) in enumerate(classified):
        if ctype != "skip":
            logger.debug(
                f"[segment-ocr] CLASSIFIED[{ci:2d}]: type={ctype}, "
                f"name={cname!r}, nums={cnums}"
            )

    # Phase 2: 連続する名前ブロックと数値ブロックを抽出
    name_blocks: list[list[str]] = []
    num_blocks: list[list[list[int]]] = []

    current_names: list[str] = []
    current_nums: list[list[int]] = []

    for ctype, cname, cnums in classified:
        if ctype == "name":
            if current_nums:
                # 数値ブロックが終わった → 保存
                num_blocks.append(current_nums)
                current_nums = []
            current_names.append(cname)  # type: ignore
        elif ctype == "nums":
            if current_names:
                # 名前ブロックが終わった → 保存
                name_blocks.append(current_names)
                current_names = []
            current_nums.append(cnums)  # type: ignore
        else:
            # skip: ブロック区切り（短い隙間は許容）
            pass

    # 残りを保存
    if current_names:
        name_blocks.append(current_names)
    if current_nums:
        num_blocks.append(current_nums)

    logger.info(
        f"[segment-ocr] vertical: name_blocks={len(name_blocks)}, "
        f"num_blocks={len(num_blocks)}"
    )

    if not name_blocks or not num_blocks:
        return []

    # Phase 3: 最大の名前ブロックと直後の数値ブロックをペアリング
    # 名前ブロックのうち3件以上のものを候補とする
    best_names: list[str] = []
    best_nums: list[list[int]] = []

    for ni, nb in enumerate(name_blocks):
        if len(nb) < 3:
            continue
        # 対応する数値ブロックを探す（名前ブロックの次にある数値ブロック）
        # name_blocks[ni] に対応する num_blocks を探す
        # 単純に: 同じインデックスか、次のnum_blockを使う
        nbi = min(ni, len(num_blocks) - 1)
        candidate_nums = num_blocks[nbi]
        if len(nb) > len(best_names) and len(candidate_nums) >= 3:
            best_names = nb
            best_nums = candidate_nums

    if len(best_names) < 3 or len(best_nums) < 3:
        logger.info(
            f"[segment-ocr] vertical: no valid block pair "
            f"(names={len(best_names)}, nums={len(best_nums)})"
        )
        return []

    # ペアリング（短い方に合わせる）
    pair_count = min(len(best_names), len(best_nums))
    logger.info(
        f"[segment-ocr] vertical pairing: "
        f"names={len(best_names)}, nums={len(best_nums)}, pairs={pair_count}"
    )

    segments: list[OcrSegment] = []
    for i in range(pair_count):
        name = best_names[i]
        nums = best_nums[i]

        if _is_skip_label(name):
            continue

        seg_sales, seg_profit = _assign_values(nums, has_sales, has_profit)

        segments.append(OcrSegment(
            segment_name=name,
            segment_order=i + 1,
            segment_sales=seg_sales,
            segment_profit=seg_profit,
        ))
        logger.info(
            f"[segment-ocr] VERTICAL: [{i + 1}] {name} "
            f"sales={seg_sales}, profit={seg_profit}"
        )

    return segments
