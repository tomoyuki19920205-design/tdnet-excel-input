# 業績期待Aの決算持ち越し根拠オーバーレイ v1.1

- 入力は四半期予測可能性v3、計上タイミングリスクv1、明示score manifest。A/B/M/D/Cは再採点していない。
- 個別再生成済みの明示入力（1418・2337）を統合採点より優先し、その他は統合採点を使用。更新時刻による選択はしていない。
- 判定は業績期待Aの利用可能性だけを示す。還元期待は別ルートであり、Aを復活させない。

## 件数
- earnings_route_status: {'conditional': 6, 'insufficient': 5, 'blocked': 2, 'usable': 2}
- historical_q_signal_weight: {'reduced': 6, 'unknown': 5, 'zero': 2, 'full': 2}

## 完成行に基づく判定一覧
- 1418: timing=medium / visibility=none / weight=reduced / status=conditional
- 198A: timing=low / visibility=not_applicable / weight=unknown / status=insufficient
- 205A: timing=high / visibility=none / weight=zero / status=blocked
- 2164: timing=unknown / visibility=unknown / weight=unknown / status=insufficient
- 2168: timing=low / visibility=not_applicable / weight=reduced / status=conditional
- 2337: timing=high / visibility=none / weight=zero / status=blocked
- 2449: timing=medium / visibility=none / weight=reduced / status=conditional
- 244A: timing=medium / visibility=none / weight=reduced / status=conditional
- 2484: timing=low / visibility=not_applicable / weight=full / status=usable
- 280A: timing=unknown / visibility=unknown / weight=unknown / status=insufficient
- 3547: timing=low / visibility=not_applicable / weight=full / status=usable
- 3558: timing=low / visibility=not_applicable / weight=reduced / status=conditional
- 3697: timing=medium / visibility=none / weight=reduced / status=conditional
- 4197: timing=unknown / visibility=unknown / weight=unknown / status=insufficient
- 9238: timing=unknown / visibility=unknown / weight=unknown / status=insufficient

## 重点確認
- 2337はhigh / noneのためzero / blocked。採点値は明示個別入力の47 / 75 / 43 / 53 / 77。
- 1418はstructural_changeの優先規則によりreduced / conditional。
- 2484はstable / structural_trendかつlowのためfull / usable。
- 3547はstable / stableかつlowのためfull / usable。
- 3558はhighly_lumpy、2168はstructural_changeのため、ともにreduced / conditional。
- 3697は完成行のmedium / none / reduced / conditionalに従う。
