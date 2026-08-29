# Company News Work Task Instructions — slot01

このタスクは1社・1回だけのtransport smoke testです。次の銘柄へ進んだり、定期実行を設定したりしないでください。

## 実行手順

1. `slots/slot01/assignment.json` を読み、`status` が `ready` であること、ticker、company_name、search_from、search_to、assignment_idを確認する。
2. 指定企業について、search_fromからsearch_toまでに公開された新しい定性情報をWebで調査する。
3. 会社IR、主要ニュース、受注、需要、値上げ、原材料・労務・エネルギー・物流コスト、設備投資、新工場、新製品、M&A、顧客、supplier、競合・業界・規制の変化を確認する。
4. 決算・業績に意味がありそうな新情報だけを、既存の`company_news_v1` JSONへ変換する。重要情報がなければ`items: []`とする。eventを無理に作らない。
5. `output_directory` に `work_slot01_<assignment_id>.json` という名前でJSONを1ファイルだけ保存する。
6. 保存後は終了する。

## 禁止事項

- `assignment.json`を書き換えない。
- `ingest_company_news.py`、`sync_company_news.py`、bridge、その他のスクリプトを実行しない。
- `decision_db.db`を直接読書きしない。
- Supabaseへ直接接続・writeしない。
- `data/news_work`と`data/news_inbox`以外のローカルファイルを変更しない。
- 記事全文、HTML、script、推測値を保存しない。
- search_fromより前の既知の背景情報を新しいeventとして保存しない。

## company_news_v1出力要件

- `run_id`はassignment_idと完全一致させる。
- `ticker`はassignmentのtickerと完全一致させる。
- `collector_type`は`chatgpt_work`、`task_id`はassignment_idとする。
- `checked_at`、`published_at`はタイムゾーン付きISO-8601にする。published_atを確認できない情報はeventにしない。
- `source_url`は原典または確認した記事の`http/https` URLを必須とする。
- directionは株価方向ではなく企業業績への定性的方向で、`positive|negative|mixed|neutral|unknown`から選ぶ。
- importanceは`high|medium|low`、earnings_relevanceは`direct|likely|general|context|unknown`から選ぶ。
- summaryは短い事実要約、why_it_mattersは業績との関係、evidence_excerptは短い根拠だけを書く。
- temporal_scopeとvalidityを明示し、期限切れ情報をcurrentにしない。

```json
{
  "schema_version": "company_news_v1",
  "run_id": "<assignment_id>",
  "ticker": "<ticker>",
  "checked_at": "2026-08-29T12:00:00+09:00",
  "collector_type": "chatgpt_work",
  "collector_version": "desktop_work_bridge_v1",
  "analysis_version": "company_news_v1",
  "task_id": "<assignment_id>",
  "sources_checked_count": 0,
  "items": []
}
```
