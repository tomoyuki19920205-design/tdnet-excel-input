# forecast_revision EPS + OCR 改善仕様

## 目的
上方修正開示からEPSを抽出し、Discord通知に表示する。

---

## 対応内容

### 1. EPS抽出
- forecast_extractor.py を修正
- _find_eps_from_lines() を使用してEPS取得

対象ラベル:
- 1株当たり当期純利益
- １株当たり当期純利益
- 1株当たり純利益
- １株当たり純利益
- 一株当たり純利益
- EPS
- earnings per share

OCR分断対応:
- "1 株 当 た り" → "1株当たり"
- "E P S" → "EPS"
- "円 銭" → "円銭"

---

### 2. OCRフォールバック
- score=0 の場合は OCR を実行
- normalize → 既存パーサ → 簡易行パーサ の順

---

### 3. EPSログ追加
INFOレベルで出力:

logger.info(
    f"[forecast_ocr] EPS prev={event.previous_eps} rev={event.revised_eps}"
)

---

### 4. Discord通知
common_notify.py に追加:

if event.previous_eps and event.revised_eps:
    change = (event.revised_eps / event.previous_eps - 1) * 100
    lines.append(f"EPS: {event.previous_eps}円→{event.revised_eps}円({change:+.1f}%)")

---

## ゴール
- selected=ocr が出る
- EPSがログに出る
- 通知に EPS: 100円→150円(+50.0%) が表示される
