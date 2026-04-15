PDFセグメント抽出 v2 パイプラインの改善作業です。



現状:



\* Phase 1,2,3,3.5 実装済み

\* 同一manifest30件比較で native成功率 5/30 → 14/30 に改善

\* 候補生成強化は有効

\* 現在の主ボトルネックは pl\_guard（PL表を誤選択）

\* 回帰1件、列マッピング失敗2件あり



今回の目的:



\* 「数値が多い表」ではなく「セグメント表らしい表」を選ぶ



対象:



\* table\_scoring.py の score\_segment\_table()



禁止:



\* min\_table\_score の一律緩和

\* OCR変更

\* guard全体緩和



