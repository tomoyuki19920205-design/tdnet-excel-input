# forecast_revision EPS通知追加 + PDF OCR改善 仕様

## 目的
上方修正/下方修正/差異開示の `forecast_revision` について、以下を実装する。

1. Discord通知に EPS の修正前→修正後を追加する
   - 表示例: `EPS: 100円→150円(+50.0%)`
2. PDF抽出精度を改善する
   - 既存のPDFテキスト抽出を維持
   - 抽出失敗または低品質時のみ Google OCR + Ghostscript を使う
3. 既存の `buyback` / `dividend_revision` には影響を出さない

---

## 対象ファイル
- `src/events/common_notify.py`
- `src/events/forecast_extractor.py`

必要なら新規追加:
- `src/events/pdf_ocr.py`
- `tests/events/test_forecast_notify_eps.py`
- `tests/events/test_forecast_extractor_eps.py`
- `tests/events/test_forecast_ocr_fallback.py`

---

## A. Discord通知に EPS を追加

### 要件
- `forecast_revision` 通知本文に EPS 行を追加する
- 表示形式:
  - `EPS: 100円→150円(+50.0%)`
- `previous_eps` と `revised_eps` の両方がある時だけ表示する
- 欠損時は非表示。通知失敗にはしない
- 単位は常に `円`
- 金額系の `億円` フォーマッタは使わない
- 整形ルール:
  - 整数なら `100円`
  - 小数があるなら `100.5円` のように過剰桁を出さない
- 既存の通知順を大きく崩さない
- `buyback` / `dividend_revision` の文面には影響させない

### 実装方針
`common_notify.py` の `forecast_revision` 用フォーマッタに EPS 行を追加する。

追加ヘルパー例:
- `_format_eps_value(value)`
- `_format_eps_change(prev, revised)`

仕様:
- `prev is None or revised is None` の場合は `None` を返す
- `%` は `(revised - prev) / abs(prev) * 100` を基本とする
- `prev == 0` の場合は `%` を無理に出さず、必要なら `EPS: 0円→150円` だけでも可
- 既存通知の fingerprint / dedup / status 更新ロジックは変更しない

### 期待表示例
🔺【上方修正】ABC（1234）
営業利益: 10.0億円→15.0億円(+50.0%)
純利益: 5.0億円→8.0億円(+60.0%)
EPS: 100円→150円(+50.0%)
2026年3月期 通期

---

## B. EPS抽出の強化

### 背景
`ForecastRevisionEvent` に `previous_eps` / `revised_eps` が既にある想定。
ただし extractor 側で EPS を十分拾えていない可能性があるため強化する。

### 要件
`forecast_extractor.py` で EPS のラベル認識と値抽出を改善する。

### 対応する表記揺れ
以下を EPS ラベル候補として扱う:
- `1株当たり当期純利益`
- `１株当たり当期純利益`
- `1株当たり四半期純利益`
- `１株当たり四半期純利益`
- `1株当たり純利益`
- `１株当たり純利益`
- `一株当たり当期純利益`
- `一株当たり四半期純利益`
- `一株当たり純利益`
- `EPS`

### 実装方針
- NFKC 正規化して比較
- 以下を吸収:
  - `1株` / `１株` / `一株`
  - 全角半角ゆれ
  - 余分スペース
- EPS は `円` 指標なので、売上/利益向けの単位換算ロジックを流用しない
- EPS は負値を許容する
- `▲`, `△`, `-` などの負号は EPS にも適用する
- `%` 付き文字列は EPS 候補から除外する
- `previous_eps` / `revised_eps` が両方あれば差分計算も行う

### 注意
- EPS に対して売上/利益向けの異常値ガードをそのまま適用しない
- 小数 EPS を落とさない
- OCR テキストのノイズで `%` や注記番号を誤認しない

---

## C. OCRフォールバック導入

### 目的
TDNET PDF の文字化け/CMap問題でラベル認識や表抽出に失敗するケースを救済する。

### 方針
- OCR は常時使わない
- まず既存の PDF テキスト抽出を実行
- 失敗または低品質時のみ OCR を実行
- `forecast_revision` 限定で導入する
- OCR 失敗でもイベント全体を落とさない

### 推奨構成
新規ファイル:
- `src/events/pdf_ocr.py`

想定関数:
- `rasterize_pdf_with_ghostscript(pdf_path, out_dir) -> list[str]`
- `extract_text_via_google_ocr(image_paths) -> str`
- `should_run_ocr_fallback(raw_text, extracted_event, diagnostics=None) -> bool`
- `score_forecast_result(event) -> int`
- `select_better_result(base, ocr) -> event`

### OCR発火条件
以下のいずれかなら OCR を試す:
1. PDF text が空または極端に短い
2. 文字化け率が高い
3. extractor の結果が `None`
4. extractor は返ったが主要項目がほぼ空
5. subtype が `undecided` で数値も乏しい
6. テーブルラベル検出失敗

### 処理フロー
1. 既存 PDF text から抽出
2. 十分ならそのまま採用
3. 不十分なら OCR 実行
4. OCR text を同じ extractor に再投入
5. 元結果と OCR 結果を score 比較
6. 良い方を採用
7. 同点なら元結果優先

### score例
- `revised_net_income` 非None: +3
- `revised_op` 非None: +3
- `revised_ordinary` 非None: +2
- `revised_sales` 非None: +2
- `previous_net_income` 非None: +2
- `previous_op` 非None: +2
- `revised_eps` 非None: +2
- `previous_eps` 非None: +2
- `subtype != undecided`: +2

数値は固定でなくてよいが、base と ocr を一貫比較できる形にする。

### extraction_source
識別できるようにする:
- `pdf_text`
- `ocr_text`
- `ocr_fallback`

---

## D. Google OCR / Ghostscript 実装要件

### 前提
ユーザーは Google OCR と Ghostscript を導入済み。

### 環境変数
- `ENABLE_GOOGLE_OCR=1`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `GHOSTSCRIPT_EXE`（必要なら）

### 実装要件
- OCR 未設定でもシステムは正常継続
- OCR 例外時は warning ログを出して base 結果で継続
- pipeline 全体を落とさない
- 一時画像は削除する
- 重いので常時実行しない
- まずは `forecast_revision` 限定

### OCR手段
Google Cloud Vision API を使う場合は `document_text_detection` 優先で可。
返却テキストは 1 本の文字列にまとめて既存 extractor に渡す。

---

## E. extractor 構造

理想構造:
- `extract_forecast_revision(...)`
  - base_text を取得
  - `base = _extract_from_text(base_text, source="pdf_text")`
  - `if should_run_ocr_fallback(...):`
    - OCR 実行
    - `ocr = _extract_from_text(ocr_text, source="ocr_text")`
    - `best = select_better_result(base, ocr)`
  - else:
    - `best = base`
  - `return best`

ポイント:
- コアパーサは `_extract_from_text()` に寄せて共通化
- OCR専用の別パーサは作らない
- 入力ソース差だけで比較可能にする

---

## F. ログ要件

INFO で残す:
- `[forecast_ocr] start doc_id=...`
- `[forecast_ocr] rasterized pages=...`
- `[forecast_ocr] ocr_text_len=...`
- `[forecast_ocr] base_score=... ocr_score=... selected=...`
- `[forecast_ocr] skipped reason=...`

目的:
- OCR が走ったか
- OCR が改善したか
- なぜ走らなかったか
を後から追えるようにする。

---

## G. テスト要件

最低限追加:

1. 通知文面 EPS 表示
- `previous_eps=100`, `revised_eps=150` で
  - `EPS: 100円→150円(+50.0%)` が含まれる
- 片方欠損なら EPS 行なし

2. EPS ラベル認識
- `1株当たり当期純利益`
- `１株当たり当期純利益`
- `一株当たり純利益`
- `EPS`
を認識できる

3. EPS 負値
- `-10 -> 20` を落とさない

4. OCRフォールバック発火
- 空文字
- 文字化け
- 抽出失敗
で `should_run_ocr_fallback=True`

5. OCR結果採用
- OCR結果の score が高い場合に OCR を選ぶ

6. OCR失敗の非致命性
- OCR 例外でも pipeline 継続

7. 非対象イベント非影響
- `buyback` / `dividend_revision` 既存テストが落ちない

---

## H. 受け入れ条件

必須:
- `forecast_revision` 通知に EPS が追加される
- 形式は `EPS: 100円→150円(+50.0%)`
- EPS 欠損時は非表示
- OCR は `forecast_revision` でのみ、条件付きフォールバックとして動作
- OCR 未設定環境でも正常動作
- ログで OCR 実行有無が判別可能
- テストが通る

実動確認:
- 既存 text path で成功する PDF
- text path は弱いが OCR で改善する PDF
- OCR が失敗しても base 継続する PDF
の3系統を確認する

---

## I. 実装順

### Phase 1
- `common_notify.py` に EPS 表示追加
- `forecast_extractor.py` の EPS ラベル補強
- 通知/EPS テスト追加

### Phase 2
- `pdf_ocr.py` 追加
- OCR フォールバック導入
- score 比較
- ログ追加

### Phase 3
- 実PDFドライラン
- 改善例の確認
- 必要なら発火条件の微調整

---

## J. 禁止事項
- OCR 失敗で pipeline 全体を raise しない
- 全PDFに無条件で OCR をかけない
- `buyback` / `dividend_revision` にまで一気に広げない
- 通知の fingerprint/dedup ロジックを壊さない

---

## 完了成果物
- 修正コード
- 追加テスト
- ドライラン結果
- Discord表示サンプル
