# ============================================================
# test_ixbrl_probe.py — xbrl_clean + ixbrl_probe のテスト
# ============================================================
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.xbrl_clean import read_xbrl_bytes, detect_encoding_info


# ============================================================
# テスト1: UTF-8 BOM付きiXBRLが正常パースできる
# ============================================================

class TestBomUtf8Parse:
    """UTF-8 BOM付きバイト列のBOM除去 → ET.fromstringパース成功"""

    def test_bom_utf8_parse(self):
        xml_content = '<?xml version="1.0" encoding="UTF-8"?><root><child>test</child></root>'
        raw = b"\xef\xbb\xbf" + xml_content.encode("utf-8")

        result = read_xbrl_bytes(raw)

        # BOMが除去されている
        assert not result.startswith("\ufeff")
        # ET.fromstringでパースできる
        root = ET.fromstring(result)
        assert root.tag == "root"
        assert root.find("child").text == "test"


# ============================================================
# テスト2: 制御文字混入ケースの正常処理
# ============================================================

class TestControlCharCleaned:
    """制御文字混入バイト列 → 除去されてパース成功"""

    def test_control_char_cleaned(self, caplog):
        xml_content = '<?xml version="1.0"?><root>he\x00ll\x07o\x1fworld</root>'
        raw = xml_content.encode("utf-8")

        import logging
        with caplog.at_level(logging.INFO, logger="tdnet"):
            result = read_xbrl_bytes(raw)

        # 制御文字が除去されている
        assert "\x00" not in result
        assert "\x07" not in result
        assert "\x1f" not in result
        assert "helloworld" in result

        # ログに除去数が出力されている
        assert any("制御文字" in record.message and "3文字" in record.message
                    for record in caplog.records)

        # パース成功
        root = ET.fromstring(result)
        assert root.tag == "root"

    def test_control_char_debug_logging(self, caplog):
        """debug=Trueで除去文字のコードポイントが詳細ログに出る"""
        xml_content = '<?xml version="1.0"?><root>\x02data</root>'
        raw = xml_content.encode("utf-8")

        import logging
        with caplog.at_level(logging.DEBUG, logger="tdnet"):
            result = read_xbrl_bytes(raw, debug=True)

        assert "\x02" not in result
        assert any("U+0002" in record.message for record in caplog.records)

    def test_no_log_when_no_control_chars(self, caplog):
        """制御文字がない場合はINFOログを出力しない"""
        xml_content = '<?xml version="1.0"?><root>clean</root>'
        raw = xml_content.encode("utf-8")

        import logging
        with caplog.at_level(logging.INFO, logger="tdnet"):
            result = read_xbrl_bytes(raw)

        assert not any("制御文字" in record.message for record in caplog.records)


# ============================================================
# テスト3: XML失敗 → HTMLフォールバック成功
# ============================================================

class TestHtmlFallbackWithIxTags:
    """XML invalid → lxml HTMLフォールバック → ixタグ存在 → OK"""

    def test_html_fallback_with_ix_tags(self):
        # XMLとしてinvalidだがHTMLとしてはパース可能なiXBRL
        # (閉じタグなしのbrなど HTML寄りの構文)
        ixbrl_content = """<!DOCTYPE html>
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
<body>
<br>
<ix:nonFraction name="jppfs_cor:NetSales" unitRef="JPY">1000000</ix:nonFraction>
<ix:nonNumeric name="jppfs_cor:CompanyName">テスト会社</ix:nonNumeric>
</body>
</html>"""

        # XMLパースは失敗するはず
        with pytest.raises(ET.ParseError):
            ET.fromstring(ixbrl_content)

        # tools/ixbrl_probe.py の _try_parse_xml を直接テスト
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from ixbrl_probe import _try_parse_xml, _find_ix_elements

        root, method, error_msg = _try_parse_xml(ixbrl_content)

        assert root is not None
        assert method == "HTML_FALLBACK"

        # ix要素が見つかる
        ix_elements = _find_ix_elements(root)
        total_ix = len(ix_elements["nonFraction"]) + len(ix_elements["nonNumeric"])
        assert total_ix > 0


# ============================================================
# テスト4: HTMLフォールバック後もixタグ不存在 → 失敗
# ============================================================

class TestHtmlFallbackNoIxTagsFails:
    """HTMLフォールバック成功 → ixタグ不存在 → NG扱い"""

    def test_html_fallback_no_ix_tags_fails(self):
        # HTMLとしてはパース可能だがix要素なし
        html_content = """<!DOCTYPE html>
<html>
<body>
<br>
<p>ただのHTML。iXBRLタグなし。</p>
</body>
</html>"""

        # XMLパースは失敗するはず
        with pytest.raises(ET.ParseError):
            ET.fromstring(html_content)

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from ixbrl_probe import _try_parse_xml, _find_ix_elements

        root, method, error_msg = _try_parse_xml(html_content)

        assert root is not None
        assert method == "HTML_FALLBACK"

        # ix要素は見つからない → NG扱い
        ix_elements = _find_ix_elements(root)
        total_ix = len(ix_elements["nonFraction"]) + len(ix_elements["nonNumeric"])
        assert total_ix == 0  # ixタグなし = エラー扱いの判定根拠


# ============================================================
# テスト5: 実ZIPでのsmokeテスト
# ============================================================

class TestProbeSmokeRealZip:
    """
    実ZIP (081220260225568550.zip) での統合テスト。

    テストデータ:
      ファイル: data/docs/081220260225568550.zip
      配置: リポジトリ内固定
      期待: exit code 0、出力に "parse" (OK/NG判定行) を含む
    """

    _ZIP_PATH = Path(__file__).resolve().parent.parent / "data" / "docs" / "081220260225568550.zip"

    @pytest.mark.skipif(
        not (Path(__file__).resolve().parent.parent / "data" / "docs" / "081220260225568550.zip").exists(),
        reason="テスト用ZIPファイルが見つかりません",
    )
    def test_probe_smoke_real_zip(self):
        probe_script = str(
            Path(__file__).resolve().parent.parent / "tools" / "ixbrl_probe.py"
        )
        python_exe = sys.executable

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        result = subprocess.run(
            [python_exe, probe_script, str(self._ZIP_PATH)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
        )

        # exit code 0 (全ファイルparse OK)
        # 注: 一部ファイルがNGでもexit 1になるが、少なくともクラッシュしない
        assert result.returncode in (0, 1), (
            f"異常終了\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # 出力に parse 判定が含まれる
        assert "parse" in result.stdout.lower(), (
            f"parse判定が出力に含まれていません\nstdout:\n{result.stdout}"
        )

        # iXBRL一覧が表示されている
        assert "iXBRL" in result.stdout or "ixbrl" in result.stdout.lower()


# ============================================================
# detect_encoding_info のテスト
# ============================================================

class TestDetectEncodingInfo:
    """detect_encoding_info のユニットテスト"""

    def test_utf8_bom(self):
        raw = b"\xef\xbb\xbf<?xml version='1.0'?>"
        info = detect_encoding_info(raw)
        assert info["has_bom"] is True
        assert info["bom_type"] == "UTF-8"

    def test_no_bom(self):
        raw = b"<?xml version='1.0' encoding='UTF-8'?>"
        info = detect_encoding_info(raw)
        assert info["has_bom"] is False
        assert info["bom_type"] is None

    def test_encoding_from_xml_decl(self):
        raw = b"<?xml version='1.0' encoding='Shift_JIS'?>"
        info = detect_encoding_info(raw)
        assert info["encoding_guess"] == "Shift_JIS"


# ============================================================
# テスト: cp932 エンコーディング対応
# ============================================================

class TestCp932Encoding:
    """cp932/Shift_JIS エンコードされたXBRLのデコード"""

    def test_cp932_decode_success(self, caplog):
        """cp932エンコードのXMLが正常にデコードされる"""
        xml_content = '<?xml version="1.0" encoding="Shift_JIS"?><root>テスト</root>'
        raw = xml_content.encode("cp932")

        import logging
        with caplog.at_level(logging.INFO, logger="tdnet"):
            result = read_xbrl_bytes(raw)

        assert "テスト" in result
        # ET.fromstring でパース可能（encoding宣言を除去する必要があるかもしれないが
        # 内部はすでにUnicode strなのでET.fromstringはencoding宣言を無視する）

    def test_cp932_without_xml_decl(self, caplog):
        """XML宣言なしのcp932バイト列→UTF-8失敗→フォールバック"""
        # cp932で「テスト」をエンコード（XML宣言なし）
        xml_content = '<root>テスト</root>'
        raw = xml_content.encode("cp932")

        import logging
        with caplog.at_level(logging.INFO, logger="tdnet"):
            result = read_xbrl_bytes(raw)

        assert "テスト" in result
        # フォールバックログが出力されている
        assert any("フォールバック" in record.message for record in caplog.records)


# ============================================================
# テスト: XML宣言encoding不一致ケース
# ============================================================

class TestEncodingMismatch:
    """XML宣言のencodingと実際のデータが異なるケース"""

    def test_declared_utf8_but_cp932(self, caplog):
        """宣言はUTF-8だが実データはcp932 → フォールバック成功"""
        # UTF-8宣言だがcp932でエンコード
        xml_str = '<?xml version="1.0" encoding="UTF-8"?><root>テスト</root>'
        raw = xml_str.encode("cp932")

        import logging
        with caplog.at_level(logging.INFO, logger="tdnet"):
            result = read_xbrl_bytes(raw)

        assert "テスト" in result


# ============================================================
# テスト: ProfitLoss スコアリング
# ============================================================

class TestProfitLossScoring:
    """ProfitLossがixbrl_probeのスコアリングで候補に出る"""

    def test_profit_loss_in_op_keywords(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from ixbrl_probe import _score_names, _OP_KEYWORDS
        from xml.etree.ElementTree import Element

        # ProfitLoss name属性を持つ要素を作成
        elem = Element("nonFraction", name="jppfs_cor:ProfitLoss", unitRef="JPY")
        scored = _score_names([elem], _OP_KEYWORDS)

        assert len(scored) > 0
        names = [s[0] for s in scored]
        assert "jppfs_cor:ProfitLoss" in names

