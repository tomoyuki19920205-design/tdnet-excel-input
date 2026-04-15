# EPS高速化SPEC（pdfplumber fallback化 + 並列化）

## 目的
- pdfplumber を最後の fallback に下げる
- filing 単位で EPS 抽出を並列化する
- 通知時点で DB 反映済みを保証する

## 実装対象
- `src/events/forecast_extractor.py`
- EPS抽出を呼ぶイベント処理のオーケストレーション部
- 必要なら小さな補助モジュール追加可
- `tools/verify_eps_revision_detection.py` に timing 集計追加可

## 非対象
- OCR新規導入
- Supabaseスキーマ変更
- 通知文面変更
- EPS抽出ルールの大幅変更

## 要件1: pdfplumber fallback化
抽出順序を以下に固定すること。

1. Native/Regex
2. Prose
3. Note
4. pdfplumber（最後のみ）

軽量経路で信頼できる候補が出たらその時点で終了すること。
軽量経路成功後に pdfplumber を追加実行してはならない。

### 推奨フロー
- `extract_eps_revision()` の中で段階制御する
- `pdfplumber.open()` や table extraction は最後まで遅延させる
- `stage` を結果に必ず持たせる  
  `native | prose | note | pdfplumber | none`

### confidence 判定
共通関数に寄せること。安全側維持。
- before/after のどちらか一方でも十分強いアンカー付きなら採用可
- `%` や配当値の誤採用は禁止
- 弱い単独数値は採用しない
- Wrong Value 0件を壊さない

### 追加ログ
- `stage`
- `fallback_used`
- `fallback_reason`
- `status`（exact / partial / missing / error）

## 要件2: 並列化
- 並列単位は `1 filing = 1 task`
- `ThreadPoolExecutor` を使う
- `max_workers` は設定値化
- デフォルト 5、上限 10

### 推奨構造
- worker側: EPS抽出、timing、結果整形
- メイン側: DB保存、通知

### 重要
- 全件完了待ちしてから通知する方式は禁止
- その filing が終わったら、DB保存成功後に即通知する

## 要件3: DB保存と通知の順序
順序は必ず以下。

1. workerで抽出完了
2. メイン側でDB保存
3. DB保存成功後に通知

禁止:
- DB保存前通知
- DB保存失敗時通知

## 要件4: 設定
- `EPS_ENABLE_PARALLEL=true`
- `EPS_MAX_WORKERS=5`
- `EPS_ENABLE_PDFPLUMBER_FALLBACK=true`

`EPS_MAX_WORKERS` は 1〜10 に丸めること。

## 要件5: エラー耐性
- 1件失敗しても全体停止しない
- future単位で例外を握ってログ化し継続
- 他 filing への波及を防ぐ

## 要件6: 計測
各 filing について以下を出すこと。
- `filing_id`
- `total_sec`
- `text_extract_sec`
- `native_sec`
- `prose_sec`
- `note_sec`
- `pdfplumber_sec`
- `final_stage`
- `fallback_used`
- `status`

### 集計で欲しいもの
- 平均 total_sec
- p50 / p90 / max
- pdfplumber到達率
- lightweightのみで終わった比率
- worker数ごとの throughput

## verify スクリプト追加項目
- `--parallel / --no-parallel`
- `--max-workers`
- stage別集計
- fallback到達率集計

## 受け入れ条件
1. Wrong Value 0件維持
2. Exact Match 70% を原則維持
3. 軽量経路成功時に pdfplumber が呼ばれない
4. 5 worker で複数 filing を同時処理できる
5. 1件失敗で全体停止しない
6. DB保存成功後にのみ通知する
7. 実装前後の timing 比較が出せる

## 実装時の注意
- pdfplumber は削除ではなく遅延実行
- confidence 判定は共通化
- 初回は worker で DB書き込みしない
- まずは「抽出は worker、保存と通知はメイン側」
- 1行サマリログを必ず出す

例:
`[eps_perf] filing=XXXX total=0.213 text=0.081 native=0.004 prose=0.001 note=0.000 pdfplumber=0.000 stage=native fallback=false status=exact`

## 完了時の提出物
1. 変更ファイル一覧
2. 実装要約
3. 50件精度比較
4. 50件速度比較
5. pdfplumber到達率 before/after
6. 5 worker / 10 worker throughput 比較
7. リスク・未解決点
