# Realtime PL 数値未抽出障害 — 2026-07-31

## Final Judgment

`PASS_REALTIME_PL_EXTRACTION_6268_6503_ROOT_CAUSE_FIXED_BACKLOG_REPAIRED_PRODUCTION_PATH_VERIFIED`

## 結論

IFRS XBRL の標準 fact 名 `NetSalesIFRS` と `OperatingProfitLossIFRS` が
リアルタイム抽出器の別名表に無かったため、PL fact が存在しても売上高が
`None` となり、PL 一式が保存されなかった。通知カードはセグメント・本文だけで
生成可能なため、PL 空欄のまま success として保存された。同日分を横断した結果、
同一原因の欠損は 4 件であり、限定 manifest によりすべて補修した。

## 対象開示と補修 manifest

| ticker | 対象 event ID | TDNET 時刻 (JST) | canonical filing ID | XBRL disclosure ID | period | 補修結果 |
| --- | --- | --- | --- | --- | --- | --- |
| 6268 (ナブテスコ) | `35c928ca-5ea6-42fb-ac5b-f7f581726e46` | 2026-07-31 16:00 | `4286b976286b6fc5f45d9c986f05a10af10ce9617927a46677e2fd953129d42b` | `20260731505000` | 2026-12-31 2Q 累計・連結 | success, 1 row updated |
| 6503 (三菱電機) | `a028e2a3-c561-4ce6-a730-271565ab73ef` | 2026-07-31 15:30 | `f997b092da3fc256817a5707765fcf4044b051adc8dfc57a984e164edb251c4d` | `20260721596543` | 2027-03-31 1Q 累計・連結 | success, 1 row updated |
| 6758 | `02bbb9a6-0b84-41c2-a842-3a842cae22c6` | 2026-07-31 12:00 | `c45448eb968911c909e1b34316aa7266379d38334f745c998e8cf22b086a0ec3` | `20260730504252` | 2027-03-31 1Q 累計・連結 | success, 1 row updated |
| 6762 | `0ac8bad0-e558-4998-a435-166311c9a2bc` | 2026-07-31 15:30 | `52e5b86f47918a0d691b404da1e422ab6d853a15a83469c5a4bff7cb8426d912` | `20260730503470` | 2027-03-31 1Q 累計・連結 | success, 1 row updated |

各行は `id`, `ticker`, `disclosed_at`, `dedupe_key`, `pdf_url` の5キーで厳密に
1件一致させた。PDF URL は順に `140120260731505000.pdf`,
`140120260721596543.pdf`, `140120260730504252.pdf`,
`140120260730503470.pdf`。再通知は行っていない。

## 6268・6503の元資料照合

保存済み公式 XBRL ZIP を production の `extract_earnings_data` で再解析した。
両資料に `jpigp_cor:NetSalesIFRS` と `jpigp_cor:OperatingProfitLossIFRS` があり、
current/prior の duration context と unit を抽出器が選択した。

| ticker | 売上高（当期 / 前年同期） | 営業利益（当期 / 前年同期） | 保存・カード表示 |
| --- | --- | --- | --- |
| 6268 | 167,401 / 143,272 百万円 | 15,846 / 9,194 百万円 | 売上 1,674億円 (+17%)、営業利益 158億円 (+72%) |
| 6503 | 1,497,109 / 1,312,896 百万円 | 139,501 / 111,972 百万円 | 売上 14,971億円 (+14%)、営業利益 1,395億円 (+25%) |

6268 は粗利 52,221 百万円、販管費 37,314 百万円も canonical に保存した。
6503 は販管費 355,008 百万円を保存した。いずれも XBRL の連結・四半期累計値である。

## 段階別 trace と原因

1. TDNET metadata、PDF、XBRL ZIP は取得済みだった。
2. XBRL ZIP の展開・fact/context 選択も成功していた。
3. `src.events.summary_financials._XBRL_TAG_MAP` と `src.extractor._XBRL_TAG_MAP` に、
   上記2タグの別名が無かった。
4. 売上高が `None` のため `extract_earnings_data` は PL current/prior を組み立てず、
   canonical financials は zero rows、payload は `text_extract_status=empty` になった。
5. 通知生成は metadata・segments・narrative を成功条件として許容するため、カードは
   挿入されたが PL 行は無かった。これは financial extraction 層の silent success である。

直接原因は alias 未対応、根本原因は IFRS の同義標準タグに対する taxonomy alias coverage
の不足である。数値の誤保存、重複イベント、空値による既存正常値の上書きは確認されなかった。

## 影響範囲

2026-07-31 JST の earnings event は 299 件。補修前に PL 欠損は 5 件あり、保存済み XBRL を
同じ修正版 extractor で read-only 再解析して `NetSalesIFRS`/`OperatingProfitLossIFRS` により
復元可能だったものは 6268、6503、6758、6762 の4件だった。残る 8473、2768、8053 はこの
alias 条件では復元不能であり、本件の補修 manifest から除外した。

補修後、同日について `same_alias_recoverable_remaining=[]` を確認した。4件の canonical PL と
既存カード本文を再取得し、各 event ID は1件のみ、`text_extract_status=ok`、売上・営業利益行ありを確認した。

## 修正・補修経路

* `src/events/summary_financials.py`: `NetSalesIFRS -> sales` と
  `OperatingProfitLossIFRS -> operating_profit` を追加。
* `src/extractor.py`: backfill/realtime 共通の同じ別名を追加。
* `tests/test_summary_financials_ifrs.py`: 最小 iXBRL ZIP の current/prior context を用い、
  新しい2タグから売上・営業利益が抽出されることを追加検証。
* `tools/repair_realtime_ifrs_pl.py`: explicit `--dry-run` / `--apply`、限定 ticker 指定、
  5キー同一性ガード、production extractor・canonical writer・partial event update を使う
  再実行可能な補修ツールを追加。許可列は payload、primary metric、表示本文だけである。

`--dry-run` 後に `--apply` を実行した。6268/6503 と影響確認で追加した6758/6762は各1行ずつのみ
更新され、既存正常 financials への一括再抽出は行っていない。

## Viewer・通知カード・本番相当確認

Viewer が参照する Supabase event record と canonical financials を production 接続で read-back した。
6268/6503 のカード本文にはそれぞれ上表の売上・営業利益行が存在し、canonical 行は sales と
operating_profit（及び存在する補助PL）を `millions_jpy` で保持する。通知再送はしていない。

補修は production と同じ extractor、canonical writer、Supabase guarded updater を対象 filing にだけ
適用した bounded production replay であり、exit code は0。各 dry-run と apply で identity match は1、
apply の affected rows は各1だった。ロック取得を伴う scheduler の全量再実行は不要な新規処理を避けるため
実施していない。終了時に `state/locks` は空で、Python 残留プロセス数は0だった。

## テスト・非破壊確認

`python -m pytest tests/test_summary_financials_ifrs.py tests/test_xbrl_attachment.py tests/test_pipeline_run.py tests/test_scheduler_realtime_deadline.py -q`
は **66 passed**。`py_compile tools/repair_realtime_ifrs_pl.py` と `git diff --check` も成功した。

作業ツリーには本件以前から多数の未追跡・既存変更があるため、それらは変更していない。commit は作成していない
（ユーザーから要求なし）。今回の report と実装は未コミットである。

## 残課題

なし（8473、2768、8053は同一 alias 原因ではなく、本件の安全な修復対象外）。
