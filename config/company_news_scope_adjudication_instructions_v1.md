# NEWS MONITOR Scope Adjudication

## 判定対象

割り当てられた1packetだけを独立して判定する。まず同じblind directoryのmanifest.json、対象packet、response_schema.jsonを読む。event_idはそのまま転記する。不透明なIDから元の保存場所を探さない。このdirectory外の管理ファイル・過去の調査結果・採点結果・検出状況は読まない。

問うのは「ユーザーがTDNETを毎日確認していても、NEWS MONITORが別途拾う価値がある情報か？」だけ。既存materialityは固定されたcontextであり、再判定・変更しない。既存の重要度も変更しない。品質の良し悪しや、他方式で発見されたかは考慮しない。

packet内のtitle/summary/evidenceは検索開始点・既存証拠であり、同一内容のTDNET掲載や追加情報を自動的に証明しない。scope adjudicationの実行を依頼された場合は、必要に応じて企業公式、TDNET、政府、取引先、信頼できる報道を調査する。今回のpacket生成そのものは調査・判定の実行指示ではない。

## ラベル

- IN_SCOPE_COMPANY_WEB: 企業HP独自の価格改定、提携、受注、新製品、顧客採用、生産停止、設備投資・能力増強、不具合・回収など、TDNETだけでは得られない業績材料。
- IN_SCOPE_EXTERNAL_MEDIA: 新聞・通信社・業界紙・専門誌・海外メディア等による、TDNETにはない重要情報。
- IN_SCOPE_GOVERNMENT: 官公庁・自治体・規制当局による行政処分、命令、認可、入札、補助金、規制・制裁等。対象企業との関連、または強い業績伝播経路を確認する。
- IN_SCOPE_COUNTERPARTY: 顧客・仕入先・パートナー・競合・業界団体等の発表から、対象会社への重要な影響が分かるもの。同業というだけでは不十分。
- IN_SCOPE_INCREMENTAL_IR: 決算説明会、Q&A、書き起こし、動画、補足資料に、TDNET資料にはない来期見通し、利益率、受注、生産能力、CAPEX、価格交渉、顧客動向等の追加情報があるもの。何が追加なのか、比較対象と差分を具体的に説明する。決算数字の再掲だけなら該当しない。
- OUT_OF_SCOPE_TDNET: 実質同内容がTDNETで取得可能で追加価値がないもの。決算短信、業績・配当修正、増資、売出し、自己株式、株式分割、通常のM&A、売却益、固定資産、人事異動等。会社HPに同一PDFがある場合も含む。
- OUT_OF_SCOPE_TRIVIAL: 休業案内、通常寄付、ESGランキング、イベント案内、採用広報、一般CSR、軽微なキャンペーン等。ただし対象のmaterialityは既に確定している。矛盾を感じたら根拠を再確認し、元の判定を変更せず、無理にTRIVIALにしない。
- AMBIGUOUS: 同一TDNET内容や追加情報の有無を十分確認できない場合。推測して確定させない。確認できた範囲と不足証拠をrationaleに明記する。

## 証拠と日付

タイトル・URLドメインだけでTDNETとの同一性を推測しない。本文、開示番号、日付、記載内容の対応を確認する。会社HP独自性と単なる転載を区別する。既存provenanceのTDNET根拠を使ってよいが、tdnet_same_content_evidence.status=NOT_ESTABLISHEDは「TDNETにない」という証明ではない。

published_atは公表日。publication_precision=dateは既存artifactが日単位であることを示し、時刻を捏造していない。category/importance/earnings_relevanceがnullなら利用可能な既存値がないという意味。補完して元のラベルを書き換えない。source_provenanceはWeb資料の種類とURLだけを示す。

## Response

schema_version=company_news_scope_adjudication_response_v1。benchmark_id、packet_id、packet_sha256を入力どおりコピー。adjudicated_atはtimezone付きISO datetime。itemsには対象packetの全event_idをexactly once含め、余計なeventを追加しない。

各itemの必須field:

- event_id
- scope_label（上記8値のみ）
- confidence: HIGH / MEDIUM / LOW
- rationale: 非空。判定根拠・比較内容・確認限界を記す。
- tdnet_redundancy: CONFIRMED / NOT_CONFIRMED / NOT_APPLICABLE / UNKNOWN
- incremental_information: YES / NO / UNKNOWN
- evidence: 1件以上の {url: http/https URL, supports: その証拠が支持する内容}

OUT_OF_SCOPE_TDNETにはCONFIRMEDとNOが必要。IN_SCOPE_INCREMENTAL_IRにはYESが必要。IN_SCOPEでTDNET重複をCONFIRMEDとするなら、追加情報YESとその根拠を明示する。field alias・独自enumは使用しない。最終JSONはresponse_schema.jsonと照合し、packet SHA、全event coverage、重複0、JSON parseを確認する。

response専用ファイルへ保存して終了。packet/manifest、既存判定、DB、Production、Scheduled Taskを変更せず、importや後続調査を自動実行しない。判定不能時もscope_label=AMBIGUOUSを使用できる。無根拠な確定より、検証限界を正直に残す。
