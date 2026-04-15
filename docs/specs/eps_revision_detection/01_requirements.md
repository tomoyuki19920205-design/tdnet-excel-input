# 要件定義

## 目的
EPS縦ブロック抽出の改善効果を実開示で検証する。

## 実装
- tools/verify_eps_revision_detection.py を追加
- 旧ロジック vs 新ロジック比較を実装

## CLI
- --limit
- --input-manifest
- --only-failures
- --save-jsonl
- --save-csv
- --debug

## 保存項目
- ticker
- company_name
- title
- old_prev_eps / old_new_eps
- new_prev_eps / new_new_eps
- expected_prev_eps / expected_new_eps
- old_status / new_status / diff_status

## status
- exact_match
- partial_match
- prev_only
- new_only
- both_missing
- false_positive
- wrong_value

## 必須指標
- exact_match_rate
- false_positive_rate
- improved_count
- regressed_count
