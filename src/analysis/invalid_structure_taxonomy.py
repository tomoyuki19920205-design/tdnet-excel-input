"""invalid_structure_taxonomy.py — invalid_structure 分類ロジック

Phase B-2 の detail_breakdown_guard の valid/narr/garbage カウントから
failure taxonomy カテゴリを推定する。

カテゴリ:
  A. toc_page_misdetected — 目次ページ誤検出 (本質的主因)
  B. parent_detail_mixed — 親セグメント行と内訳/注記行が混在
  C. no_true_parent_rows — valid=0 でセグメント親行が認識されない
  D. sparse_or_shifted_table — valid=1 で garbage に埋もれている
  E. header_broken — 表構造はあるが列推定に失敗
  F. narrative_table_like — narrative 行が dominant
  G. other
"""


def classify_invalid_structure(
    valid: int,
    narr: int,
    garbage: int,
    bscf: int = 0,
    table_score: float | None = None,
    has_toc_pattern: bool = False,
) -> dict:
    """
    Classify an invalid_structure filing into taxonomy category.

    Returns:
        dict with keys: primary_category, secondary_category, reasons
    """
    primary = "other"
    secondary = ""
    reasons = [f"valid={valid} narr={narr} bscf={bscf} garbage={garbage}"]

    total_non_valid = narr + bscf + garbage

    # A. TOC page misdetection (fundamental cause for most cases)
    if has_toc_pattern:
        primary = "toc_page_misdetected"
        reasons.append("TOC page reference pattern detected")
    # B. parent_detail_mixed: has real segments but mixed with noise
    elif valid >= 2:
        primary = "parent_detail_mixed"
        reasons.append(f"valid>={valid}, mixed with narr={narr} garbage={garbage}")
    # C. no_true_parent_rows: valid=0, noise dominant
    elif valid == 0 and garbage >= 3:
        primary = "no_true_parent_rows"
        reasons.append(f"no valid segments, garbage={garbage} dominant")
    # D. sparse_or_shifted_table: valid=1
    elif valid == 1 and total_non_valid > valid:
        primary = "sparse_or_shifted_table"
        reasons.append(f"single valid segment in {total_non_valid} noise rows")
    # E. header_broken
    elif valid == 0 and garbage == 0 and narr == 0:
        primary = "header_broken"
        reasons.append("zero classified rows: table structure unrecognized")
    # F. narrative_table_like
    elif narr > valid and narr >= 2:
        primary = "narrative_table_like"
        reasons.append(f"narrative={narr} dominant")
    elif valid == 0 and narr >= 1 and garbage >= 1:
        primary = "no_true_parent_rows"
        reasons.append(f"no valid, mixed narr={narr} garbage={garbage}")
    else:
        reasons.append("no matching pattern")

    # Secondary category
    if primary == "parent_detail_mixed" and narr >= 2:
        secondary = "narrative_table_like"
    elif primary == "no_true_parent_rows" and narr >= 2:
        secondary = "narrative_table_like"

    return {
        "primary_category": primary,
        "secondary_category": secondary,
        "reasons": reasons,
    }
