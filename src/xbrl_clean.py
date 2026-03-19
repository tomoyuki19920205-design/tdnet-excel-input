# ============================================================
# xbrl_clean.py — XBRL/iXBRL バイト列のテキスト正規化
# ============================================================
#
# 責務: バイト列 → 正規化済み str への変換のみ。
#       パース処理は含めない（呼び出し側の責務）。
# ============================================================
from __future__ import annotations

import logging
import re

logger = logging.getLogger("tdnet")

# XML 1.0 非許容制御文字:
#   U+0000 - U+0008
#   U+000B (VT)
#   U+000C (FF)
#   U+000E - U+001F
# 許容される制御文字: U+0009 (TAB), U+000A (LF), U+000D (CR)
_CTRL_CHAR_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f]"
)

# BOM signatures
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"

# XML宣言からencoding属性を抽出するパターン
_XML_ENCODING_RE = re.compile(r'encoding=["\']([^"\']+)["\']')

# フォールバックエンコーディング一覧（優先順）
_FALLBACK_ENCODINGS = ["cp932", "shift_jis", "euc-jp", "iso-8859-1"]


def _detect_xml_declared_encoding(raw: bytes) -> str | None:
    """
    バイト列の先頭200バイトからXML宣言のencoding属性を抽出する。

    Returns:
        encoding名 (例: "UTF-8", "Shift_JIS") or None
    """
    head_ascii = raw[:200].decode("ascii", errors="ignore")
    match = _XML_ENCODING_RE.search(head_ascii)
    if match:
        return match.group(1)
    return None


def read_xbrl_bytes(raw: bytes, *, debug: bool = False) -> str:
    """
    バイト列をUTF-8文字列に正規化する。

    処理内容:
      1. BOM検出: UTF-8 / UTF-16 BOM があればそれに基づきデコード
      2. BOMなし: XML宣言のencoding属性を参照してデコード
      3. 上記で失敗: cp932 / Shift_JIS / EUC-JP / ISO-8859-1 を順に試行
      4. XML 1.0 非許容制御文字を除去（除去数をログ出力）

    ログレベル:
      - INFO: 除去数サマリ（0文字の場合は出力しない）
      - DEBUG (debug=True): 除去した各文字のコードポイント

    Args:
        raw: 生バイト列
        debug: True の場合、除去した文字のコードポイントも詳細ログ出力

    Returns:
        正規化済み str

    Raises:
        UnicodeDecodeError: どのエンコーディングでもデコード不可能な場合
    """
    # --- Step 1: BOM検出 & デコード ---
    text: str | None = None

    if raw.startswith(_BOM_UTF8):
        text = raw[3:].decode("utf-8")
    elif raw.startswith(_BOM_UTF16_LE):
        text = raw.decode("utf-16-le")
        if text.startswith("\ufeff"):
            text = text[1:]
    elif raw.startswith(_BOM_UTF16_BE):
        text = raw.decode("utf-16-be")
        if text.startswith("\ufeff"):
            text = text[1:]
    else:
        # --- Step 2: BOMなし → XML宣言encoding参照 → フォールバック ---
        declared_enc = _detect_xml_declared_encoding(raw)

        if declared_enc:
            # XML宣言のencodingで試行
            try:
                text = raw.decode(declared_enc)
                logger.debug(
                    f"[XBRL_CLEAN] XML宣言encoding '{declared_enc}' でデコード成功"
                )
            except (UnicodeDecodeError, LookupError):
                logger.info(
                    f"[XBRL_CLEAN] XML宣言encoding '{declared_enc}' でデコード失敗、"
                    f"フォールバックを試行"
                )

        if text is None:
            # UTF-8 を最優先で試行
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # フォールバックエンコーディングを順に試行
                for enc in _FALLBACK_ENCODINGS:
                    try:
                        text = raw.decode(enc)
                        logger.info(
                            f"[XBRL_CLEAN] フォールバック '{enc}' でデコード成功"
                        )
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue

        if text is None:
            # 全フォールバック失敗 → UnicodeDecodeError を発生させる
            raw.decode("utf-8")  # 意図的に例外を発生させる

    # --- Step 3: XML 1.0 非許容制御文字の除去 ---
    removed_chars = _CTRL_CHAR_RE.findall(text)
    removed_count = len(removed_chars)

    if removed_count > 0:
        logger.info(
            f"[XBRL_CLEAN] 制御文字 {removed_count}文字 除去 "
            f"(対象範囲: U+0000-U+0008, U+000B, U+000C, U+000E-U+001F)"
        )
        if debug:
            code_points = [f"U+{ord(c):04X}" for c in removed_chars]
            logger.debug(
                f"[XBRL_CLEAN] 除去文字詳細: {code_points}"
            )
        text = _CTRL_CHAR_RE.sub("", text)

    return text


def detect_encoding_info(raw: bytes) -> dict:
    """
    バイト列のエンコーディング情報を返す（probe表示用）。

    Returns:
        {
            "head_hex": str,       # 先頭16バイトのhex表現
            "has_bom": bool,       # BOM有無
            "bom_type": str|None,  # "UTF-8" / "UTF-16-LE" / "UTF-16-BE" / None
            "encoding_guess": str, # 推定エンコーディング
        }
    """
    head = raw[:16]
    head_hex = " ".join(f"{b:02x}" for b in head)

    has_bom = False
    bom_type = None
    encoding_guess = "UTF-8"

    if raw.startswith(_BOM_UTF8):
        has_bom = True
        bom_type = "UTF-8"
        encoding_guess = "UTF-8 (BOM付き)"
    elif raw.startswith(_BOM_UTF16_LE):
        has_bom = True
        bom_type = "UTF-16-LE"
        encoding_guess = "UTF-16-LE"
    elif raw.startswith(_BOM_UTF16_BE):
        has_bom = True
        bom_type = "UTF-16-BE"
        encoding_guess = "UTF-16-BE"
    else:
        # XML宣言からencoding推定
        declared_enc = _detect_xml_declared_encoding(raw)
        if declared_enc:
            encoding_guess = declared_enc

    return {
        "head_hex": head_hex,
        "has_bom": has_bom,
        "bom_type": bom_type,
        "encoding_guess": encoding_guess,
    }
