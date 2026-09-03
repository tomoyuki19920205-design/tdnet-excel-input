from __future__ import annotations

from copy import deepcopy
import ast
from pathlib import Path
import re

import pytest

from lib.ny_market import NYMarketValidationError, validate_payload
from lib.ny_market_20260903_after_hours_v3 import MIGRATION_PAYLOAD
from lib.ny_market_after_hours import build_after_hours_discovery_plan
from lib.ny_market_display import apply_display_contract
from lib.ny_market_research import attach_market_data_packet_metadata
from tests.ny_market_quality_fixture import payload
from tools.migrate_ny_market_20260903_after_hours_v3 import (
    _payload_sha256,
    _without_after_hours,
    migrate_payload,
)


def _section(markdown: str, heading: str) -> str:
    marker = f"## {heading}\n\n"
    body = markdown.split(marker, 1)[1]
    return body.split("\n## ", 1)[0].strip()


AFTER_HOURS_PRICE_SOURCE = "https://www.investing.com/news/stock-market-news/afterhours-movers-avgo-snow-hpe-ntap-ntsk-chpt-five-tlys-pvh-rare-432SI-4886740"
SNOW_AWS_SOURCE = "https://s26.q4cdn.com/463892824/files/doc_financials/2027/q1/CORRECTED-TRANSCRIPT_-Snowflake-Inc-SNOW-US-Q1-2027-Earnings-Call-27-May-2026-5_00-PM-ET.pdf"
HPE_MEMORY_SOURCE = "https://investors.hpe.com/~/media/Files/H/HP-Enterprise-IR/documents/q1-2026/hpe-q1-26-earnings-transcript.pdf"
NTAP_DATAPELAGO_SOURCE = "https://investors.netapp.com/news/news-details/2026/NetApp-Acquires-DataPelago-Making-Data-AI-Ready-at-the-Infrastructure-Layer/default.aspx"


def _fact(
    summary: str,
    *tokens: str,
    source_url: str,
    announced_at: str | None = None,
    temporal_status: str | None = None,
) -> dict:
    result = {"summary": summary, "evidence_tokens": list(tokens), "source_url": source_url}
    if announced_at is not None:
        result["announced_at"] = announced_at
    if temporal_status is not None:
        result["temporal_status"] = temporal_status
    return result


def _candidate(
    *, ticker: str, name: str, description: str, change: float, primary_url: str,
    source_type: str, reported: list[dict] | None = None,
    consensus: list[dict] | None = None, guidance: list[dict] | None = None,
    guidance_comparison: list[dict] | None = None, kpis: list[dict] | None = None,
    background: list[dict] | None = None, same_day: list[dict] | None = None,
    event_details: list[dict] | None = None, why: str, readthrough: str,
    watch: list[str], why_evidence: list[str], readthrough_evidence: list[str],
    extra_fact_sources: list[dict] | None = None,
    extra_market_sources: list[dict] | None = None,
) -> dict:
    result = {
        "ticker": ticker, "name": name, "description": description, "change": change,
        "primary_url": primary_url, "source_type": source_type,
        "event_type": "material_event" if event_details else "earnings",
        "same_day_developments": same_day or [], "why_moved": why,
        "investment_readthrough": readthrough, "watch_items": watch,
        "qualitative_evidence": {
            "why_moved": why_evidence,
            "investment_readthrough": readthrough_evidence,
        },
        "fact_sources": [
            {"label": "決算・正式発表", "url": primary_url, "source_kind": source_type},
            *(extra_fact_sources or []),
        ],
        "market_context_sources": [{
            "label": "市場予想・時間外株価", "url": AFTER_HOURS_PRICE_SOURCE,
            "source_kind": "financial_media",
        }, *(extra_market_sources or [])],
    }
    if event_details:
        result["event_details"] = event_details
    else:
        result.update({
            "reported_results": reported or [], "consensus_comparison": consensus or [],
            "guidance": guidance or [], "guidance_comparison": guidance_comparison or [],
            "key_kpis": kpis or [], "background_context": background or [],
        })
    return result


SEPTEMBER_3_CANDIDATES = [
    _candidate(
        ticker="AVGO", name="Broadcom", change=-3.5, source_type="company_ir",
        primary_url="https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announces-third-quarter-fiscal-year-2026-financial",
        description="カスタムAIアクセラレーターとデータセンター向けネットワーク半導体、VMwareを含むインフラソフトを展開する企業です。AI半導体の成長と非AI半導体の回復、利益率を同時に見る必要があります。",
        reported=[_fact("Q3売上は$29.59Bで前年比86%増、調整後EPSは$3.32でした。", "$29.59B", "86%", "$3.32", source_url="https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announces-third-quarter-fiscal-year-2026-financial")],
        consensus=[
            {"summary": "調整後EPSは$3.32と市場予想$3.21を上回りました。", "actual": "$3.32", "estimate": "$3.21", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE},
            {"summary": "Q4売上見通し$34.8Bは市場予想$35.05Bを下回りました。", "actual": "$34.8B", "estimate": "$35.05B", "outcome": "miss", "source_url": AFTER_HOURS_PRICE_SOURCE},
        ],
        guidance=[_fact("Q4売上を$34.8B、AI半導体売上を$21.7Bで前年比236%増と見込みました。", "$34.8B", "$21.7B", "236%", source_url="https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announces-third-quarter-fiscal-year-2026-financial")],
        kpis=[_fact("Q3のAI半導体売上は$16.7Bで前年比221%増、前四半期比54%増でした。", "$16.7B", "221%", "54%", source_url="https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announces-third-quarter-fiscal-year-2026-financial")],
        why="Q3はbeatでも、Q4売上見通し$34.8Bが市場予想$35.05Bを下回り、非AI半導体の弱さがAIの急成長を上回る失望材料になりました。",
        readthrough="カスタムASICとネットワーク需要はAI設備投資の裾野拡大を示す一方、汎用半導体には回復の遅れを示すread-throughです。",
        watch=["AI半導体の受注から売上への転換", "非AI半導体の回復", "非GAAP営業利益率66%の維持"],
        why_evidence=["$34.8B", "$35.05B"], readthrough_evidence=["カスタムASIC", "ネットワーク"],
    ),
    _candidate(
        ticker="SNOW", name="Snowflake", change=21.0, source_type="sec",
        primary_url="https://www.sec.gov/Archives/edgar/data/1640147/000164014726000033/fy2027q2earnings.htm",
        description="企業データをクラウド上に蓄積・分析し、AIアプリやエージェントが使うデータ基盤を提供する会社です。利用量課金のため、製品売上とデータ消費、AI機能の実利用が重要です。",
        reported=[_fact("Q2総売上は$1.55B、製品売上は$1.49Bで前年比37%増、NRRは126%、RPOは$9.00Bで30%増でした。", "$1.55B", "$1.49B", "37%", "126%", "$9.00B", "30%", source_url="https://www.sec.gov/Archives/edgar/data/1640147/000164014726000033/fy2027q2earnings.htm")],
        consensus=[{"summary": "調整後EPSは$0.62と市場予想$0.45を上回りました。", "actual": "$0.62", "estimate": "$0.45", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE}],
        guidance_comparison=[{"summary": "FY27製品売上見通しを$5.84Bから$6.07Bへ引き上げました。", "previous": "$5.84B", "current": "$6.07B", "direction": "raised", "source_url": "https://www.sec.gov/Archives/edgar/data/1640147/000164014726000033/fy2027q2earnings.htm"}],
        kpis=[_fact("CoCoは9,100口座、CoWorkは5,800口座へ拡大し、会社は両製品が新規workloadとplatform consumptionを促していると説明しました。", "9,100", "5,800", "platform consumption", source_url="https://www.sec.gov/Archives/edgar/data/1640147/000164014726000033/fy2027q2earnings.htm")],
        background=[_fact("前四半期の5月27日にAWSと5年間$6Bの利用・共同販売契約を発表しており、中期的な利用拡大を支える背景材料です。", "5月27日", "AWS", "5年間", "$6B", source_url=SNOW_AWS_SOURCE, announced_at="2026-05-27", temporal_status="prior_period")],
        why="製品売上の伸びが3四半期連続で加速し、EPSのbeatとFY27製品売上$6.07Bへの上方修正が、AI需要を実課金へ変える力を裏付けました。",
        readthrough="CoCoとCoWorkの利用口座拡大は、AI機能が将来の製品消費増加につながる可能性と、実験段階から本番利用へ移る需要を示しています。",
        watch=["NRR 126%の再加速", "RPO $9.00Bの売上転換", "AI利用増とFCFマージンの両立"],
        why_evidence=["3四半期連続", "$6.07B"], readthrough_evidence=["CoCo", "製品消費"],
        extra_fact_sources=[{"label": "AWS契約・前四半期説明会", "url": SNOW_AWS_SOURCE, "source_kind": "earnings_call"}],
    ),
    _candidate(
        ticker="HPE", name="Hewlett Packard Enterprise", change=-1.0, source_type="sec",
        primary_url="https://www.sec.gov/Archives/edgar/data/1645590/000164559026000078/ex-991x922026x8k.htm",
        description="企業向けサーバー、クラウド基盤と、Juniper統合後のネットワーク製品を提供します。AI需要、ネットワーク成長、利益率と部品供給が焦点です。",
        reported=[_fact("Q3売上は$12.2Bで前年比34%増、調整後EPSは$1.11、非GAAP営業利益率は16.2%でした。", "$12.2B", "34%", "$1.11", "16.2%", source_url="https://www.sec.gov/Archives/edgar/data/1645590/000164559026000078/ex-991x922026x8k.htm")],
        consensus=[{"summary": "調整後EPSは$1.11と市場予想$0.92を上回りました。", "actual": "$1.11", "estimate": "$0.92", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE}],
        guidance=[_fact("FY26調整後EPSを$3.75–$3.85、FCFを少なくとも$3.75Bへ引き上げました。", "$3.75–$3.85", "$3.75B", source_url="https://www.sec.gov/Archives/edgar/data/1645590/000164559026000078/ex-991x922026x8k.htm")],
        kpis=[_fact("Networking売上はJuniper統合を含め75%増、Cloud & AI売上は25%増で、各営業利益率は22.0%と17.0%でした。", "75%", "25%", "22.0%", "17.0%", source_url="https://www.sec.gov/Archives/edgar/data/1645590/000164559026000078/ex-991x922026x8k.htm")],
        background=[_fact("3月9日のFY26 Q1説明会で、メモリコスト上昇が2026年中続く前提を示しており、今回以前からの背景条件です。", "3月9日", "メモリコスト", "2026年", source_url=HPE_MEMORY_SOURCE, announced_at="2026-03-09", temporal_status="prior_period")],
        why="全面的なbeatとraiseでも、Dell決算を受けた事前上昇で期待値が高く、Juniper寄与を含む75%のNetworking成長と部品制約を市場が割り引きました。",
        readthrough="AIサーバーとデータセンターネットワーク需要は強い一方、メモリを含む部品不足が受注を売上へ変える速度とハードウェア利益率を制約します。",
        watch=["Juniper統合後のNetworking有機成長", "メモリ供給と価格転嫁", "Cloud & AI利益率17.0%の持続性"],
        why_evidence=["Juniper", "75%"], readthrough_evidence=["メモリ", "利益率"],
        extra_fact_sources=[{"label": "FY26 Q1説明会・メモリ制約", "url": HPE_MEMORY_SOURCE, "source_kind": "earnings_call"}],
    ),
    _candidate(
        ticker="NTAP", name="NetApp", change=-7.0, source_type="company_ir",
        primary_url="https://investors.netapp.com/news/news-details/2026/NetApp-Reports-First-Quarter-of-Fiscal-Year-2027-Results/default.aspx",
        description="企業向けストレージとハイブリッドクラウドのデータ管理基盤を提供します。AI向けall-flash需要だけでなく、売上をキャッシュへ変える力と在庫効率が重要です。",
        reported=[_fact("Q1売上は$2.03Bで30%増、調整後EPSは$2.58、billingsは$2.06Bで36%増でした。", "$2.03B", "30%", "$2.58", "$2.06B", "36%", source_url="https://investors.netapp.com/news/news-details/2026/NetApp-Reports-First-Quarter-of-Fiscal-Year-2027-Results/default.aspx")],
        consensus=[{"summary": "調整後EPSは$2.58と市場予想$2.11を上回りました。", "actual": "$2.58", "estimate": "$2.11", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE}],
        guidance_comparison=[{"summary": "FY27調整後EPS見通しを$8.70–$9.00から$9.73–$10.03へ引き上げました。", "previous": "$8.70–$9.00", "current": "$9.73–$10.03", "direction": "raised", "source_url": "https://investors.netapp.com/news/news-details/2026/NetApp-Reports-First-Quarter-of-Fiscal-Year-2027-Results/default.aspx"}],
        kpis=[_fact("all-flash売上は$1.3Bで47%増でしたが、FCFは$401Mで35%減、在庫は$198Mから$375Mへ増えました。", "$1.3B", "47%", "$401M", "35%", "$198M", "$375M", source_url="https://investors.netapp.com/news/news-details/2026/NetApp-Reports-First-Quarter-of-Fiscal-Year-2027-Results/default.aspx")],
        background=[_fact("7月16日にAIデータ処理基盤DataPelagoの買収を発表済みで、今回以前からの背景材料としてAI案件への統合が焦点です。", "7月16日", "DataPelago", "買収", source_url=NTAP_DATAPELAGO_SOURCE, announced_at="2026-07-16", temporal_status="prior_period")],
        why="売上・EPSのbeatと通期上方修正よりも、FCFの35%減と在庫の急増がキャッシュ転換の悪化として重く見られ、sell-the-newsになりました。",
        readthrough="all-flashとAIデータ基盤需要はストレージ業界に追い風ですが、ハードウェア増産が運転資本を圧迫する局面では利益成長だけで評価できません。",
        watch=["在庫$375Mの正常化", "FCF $401Mの回復", "DataPelago統合によるAI案件獲得"],
        why_evidence=["FCF", "在庫"], readthrough_evidence=["all-flash", "運転資本"],
        extra_fact_sources=[{"label": "DataPelago買収発表", "url": NTAP_DATAPELAGO_SOURCE, "source_kind": "company_ir"}],
        extra_market_sources=[{"label": "時間外下落・cash flow分析", "url": "https://www.investing.com/news/earnings/netapp-slides-8-despite-beat-on-weaker-cash-flow-concerns-93CH-4886695", "source_kind": "financial_media"}],
    ),
    _candidate(
        ticker="NTSK", name="Netskope", change=16.0, source_type="company_ir",
        primary_url="https://investors.netskope.com/news-releases/news-release-details/netskope-announces-strong-second-quarter-fiscal-2027-financial",
        description="企業のクラウド、Web、AI利用を保護するSASEとデータセキュリティ基盤を提供します。売上より先行するARRと、成長を維持しながら赤字を縮められるかが重要です。",
        reported=[_fact("Q2売上は$220.5Mで29%増、ARRは$899Mで27%増、調整後1株損失は-$0.03でした。", "$220.5M", "29%", "$899M", "27%", "-$0.03", source_url="https://investors.netskope.com/news-releases/news-release-details/netskope-announces-strong-second-quarter-fiscal-2027-financial")],
        consensus=[{"summary": "調整後1株損失は-$0.03と市場予想-$0.07より小幅でした。", "actual": "-$0.03", "estimate": "-$0.07", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE}],
        guidance=[_fact("FY27売上を$888M–$892M、調整後1株損失を-$0.15と見込み、従来より改善しました。", "$888M–$892M", "-$0.15", source_url="https://investors.netskope.com/news-releases/news-release-details/netskope-announces-strong-second-quarter-fiscal-2027-financial")],
        kpis=[_fact("非GAAP営業損失率は前年の-20%から-9%へ改善し、AI Security製品の初期需要も確認しました。", "-20%", "-9%", "AI Security", source_url="https://investors.netskope.com/news-releases/news-release-details/netskope-announces-strong-second-quarter-fiscal-2027-financial")],
        why="ARR 27%成長を保ちながら市場予想より赤字が小さく、通期売上と損失見通しも改善したため、成長と収益性の両立が再評価されました。",
        readthrough="AI Securityの初期需要は、生成AI利用の拡大がSASE、データ保護、ネットワーク最適化への追加支出を生むread-throughです。",
        watch=["net new ARRの再加速", "AI Securityの有料化", "FCFマージン2%の達成"],
        why_evidence=["ARR 27%", "赤字"], readthrough_evidence=["AI Security", "SASE"],
    ),
    _candidate(
        ticker="CHPT", name="ChargePoint", change=16.0, source_type="sec",
        primary_url="https://www.sec.gov/Archives/edgar/data/1777393/000177739326000061/chpt8-kerfy2027q2exx991.htm",
        description="法人・集合住宅・家庭向けEV充電機器とネットワーク課金サービスを提供します。機器売上の回復だけでなく、継続収益、粗利率、キャッシュ消費を見る会社です。",
        reported=[_fact("Q2売上は$116.1Mで18%増、subscription売上は$43.7Mで10%増、調整後EBITDA損失は-$4.8Mでした。", "$116.1M", "18%", "$43.7M", "10%", "-$4.8M", source_url="https://www.sec.gov/Archives/edgar/data/1777393/000177739326000061/chpt8-kerfy2027q2exx991.htm")],
        consensus=[
            {"summary": "調整後1株損失は-$1.35と市場予想-$1.60より小幅でした。", "actual": "-$1.35", "estimate": "-$1.60", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE},
            {"summary": "売上$116.1Mは市場予想$105.43Mを上回りました。", "actual": "$116.1M", "estimate": "$105.43M", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE},
        ],
        guidance=[_fact("Q3売上は$105M–$115Mを見込みました。", "$105M–$115M", source_url="https://www.sec.gov/Archives/edgar/data/1777393/000177739326000061/chpt8-kerfy2027q2exx991.htm")],
        kpis=[_fact("非GAAP粗利率は過去最高の38%、調整後EBITDA損失は前年の-$22.1Mから-$4.8Mへ縮小しました。", "38%", "-$22.1M", "-$4.8M", source_url="https://www.sec.gov/Archives/edgar/data/1777393/000177739326000061/chpt8-kerfy2027q2exx991.htm")],
        same_day=[_fact("Express Soloのearly-access出荷とEatonとの提携拡大を進めました。", "Express Solo", "Eaton", source_url="https://www.sec.gov/Archives/edgar/data/1777393/000177739326000061/chpt8-kerfy2027q2exx991.htm", announced_at="2026-09-02", temporal_status="same_day")],
        why="売上が市場予想$105.43Mを上回り、過去最高の非GAAP粗利率38%とEBITDA損失縮小が、資金消費への懸念を和らげました。",
        readthrough="EV販売台数だけでなく、法人充電網の更新とsubscription比率、Eaton経由の設備販売が収益性改善を左右します。",
        watch=["Q3売上レンジの達成", "subscription売上比率", "調整後EBITDAの黒字化時期"],
        why_evidence=["$105.43M", "38%"], readthrough_evidence=["subscription", "Eaton"],
    ),
    _candidate(
        ticker="FIVE", name="Five Below", change=3.0, source_type="sec",
        primary_url="https://www.sec.gov/Archives/edgar/data/1177609/000117760926000023/q22026fivebelowexhibit991.htm",
        description="若年層向けの低価格雑貨を店舗網で販売するディスカウント小売です。新店拡大だけでなく、既存店売上、商品回転、関税影響後の利益率が重要です。",
        reported=[_fact("Q2売上は$1.26Bで22.9%増、既存店売上は14.1%増、調整後EPSは$1.68でした。", "$1.26B", "22.9%", "14.1%", "$1.68", source_url="https://www.sec.gov/Archives/edgar/data/1177609/000117760926000023/q22026fivebelowexhibit991.htm")],
        consensus=[{"summary": "調整後EPSは$1.68と市場予想$1.39を上回りました。", "actual": "$1.68", "estimate": "$1.39", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE}],
        guidance_comparison=[{"summary": "FY26売上見通しを$5.40B–$5.48Bから$5.63B–$5.71Bへ引き上げました。", "previous": "$5.40B–$5.48B", "current": "$5.63B–$5.71B", "direction": "raised", "source_url": "https://www.sec.gov/Archives/edgar/data/1177609/000117760926000023/q22026fivebelowexhibit991.htm"}],
        same_day=[_fact("取締役会は新たに最大$600Mの自社株買いを承認しました。", "$600M", "自社株買い", source_url="https://www.sec.gov/Archives/edgar/data/1177609/000117760926000023/q22026fivebelowexhibit991.htm", announced_at="2026-09-02", temporal_status="same_day")],
        kpis=[_fact("52店を純増して2,022店となり、既存店と新店の両方が成長へ寄与しました。", "52店", "2,022店", source_url="https://www.sec.gov/Archives/edgar/data/1177609/000117760926000023/q22026fivebelowexhibit991.htm")],
        why="既存店14.1%増とEPS beatに加え、通期売上の上方修正と$600Mの自社株買いが下値を支えましたが、上昇幅は3%にとどまりました。",
        readthrough="低価格・トレンド商品の集客力は消費選別下でも強く、他のディスカウント小売には客数と在庫回転の改善を示します。",
        watch=["下期既存店売上+10%–+12%の持続性", "関税還付後の粗利正常化", "新店生産性"],
        why_evidence=["14.1%", "$600M"], readthrough_evidence=["低価格", "在庫回転"],
    ),
    _candidate(
        ticker="TLYS", name="Tilly’s", change=32.0, source_type="sec",
        primary_url="https://www.sec.gov/Archives/edgar/data/1524025/000162828026060088/q2fy2026earningsrelease.htm",
        description="若者向け衣料・靴・アクセサリーを店舗とECで販売します。店舗縮小下での既存店成長、EC比率、粗利回復が黒字化の鍵です。",
        reported=[_fact("Q2売上は$163.5Mで8.1%増、既存店売上は12.1%増、EPSは$0.27でした。", "$163.5M", "8.1%", "12.1%", "$0.27", source_url="https://www.sec.gov/Archives/edgar/data/1524025/000162828026060088/q2fy2026earningsrelease.htm")],
        consensus=[
            {"summary": "EPSは$0.27と市場予想$0.17を上回りました。", "actual": "$0.27", "estimate": "$0.17", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE},
            {"summary": "Q3 EPS見通し$0.07–$0.12は市場予想-$0.072を上回りました。", "actual": "$0.07–$0.12", "estimate": "-$0.072", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE},
        ],
        guidance=[_fact("Q3売上を$150M–$155M、EPSを$0.07–$0.12の黒字レンジと見込みました。", "$150M–$155M", "$0.07–$0.12", source_url="https://www.sec.gov/Archives/edgar/data/1524025/000162828026060088/q2fy2026earningsrelease.htm")],
        kpis=[_fact("EC売上は20.9%増、粗利率は300bp改善し、13か月連続で既存店売上が前年を上回りました。", "20.9%", "300bp", "13か月", source_url="https://www.sec.gov/Archives/edgar/data/1524025/000162828026060088/q2fy2026earningsrelease.htm")],
        why="既存店の二桁成長と粗利率300bp改善がEPS beatへつながり、Q3も黒字を見込んだことで構造的なturnaround期待が強まりました。",
        readthrough="店舗数を減らしてもECと既存店の生産性が伸びており、若年層アパレルでは値引き抑制と在庫鮮度が利益回復を左右します。",
        watch=["Q3既存店売上+10%–+14%", "商品粗利の改善継続", "通期黒字化"],
        why_evidence=["300bp", "Q3"], readthrough_evidence=["EC", "在庫鮮度"],
    ),
    _candidate(
        ticker="PVH", name="PVH", change=-3.0, source_type="sec",
        primary_url="https://www.sec.gov/Archives/edgar/data/78239/000007823926000055/ex99120262q8k.htm",
        description="Calvin KleinとTommy Hilfigerを世界展開するアパレル企業です。地域別のDTC・卸売動向と、値引き・関税を含むブランド利益率が焦点です。",
        reported=[_fact("Q2売上は$2.097Bで3%減、調整後営業利益率は11.1%、調整後EPSは$3.70でした。", "$2.097B", "3%", "11.1%", "$3.70", source_url="https://www.sec.gov/Archives/edgar/data/78239/000007823926000055/ex99120262q8k.htm")],
        consensus=[
            {"summary": "調整後EPSは$3.70と市場予想$3.08を上回りました。", "actual": "$3.70", "estimate": "$3.08", "outcome": "beat", "source_url": AFTER_HOURS_PRICE_SOURCE},
            {"summary": "FY26 EPS見通しの中央値$11.95は市場予想$12.08を下回りました。", "actual": "$11.95", "estimate": "$12.08", "outcome": "miss", "source_url": AFTER_HOURS_PRICE_SOURCE},
        ],
        guidance=[_fact("FY26調整後EPS見通しを$11.80–$12.10で据え置きました。", "$11.80–$12.10", source_url="https://www.sec.gov/Archives/edgar/data/78239/000007823926000055/ex99120262q8k.htm")],
        kpis=[_fact("EMEA売上は6%減、APACは3%増、e-commerceは4%増と地域差が続きました。", "EMEA", "6%", "APAC", "3%", "4%", source_url="https://www.sec.gov/Archives/edgar/data/78239/000007823926000055/ex99120262q8k.htm")],
        why="EPS beatでも売上は減少し、EMEAの6%減に対してFY26 EPSレンジを据え置いたため、利益の質と欧州需要への懸念が残りました。",
        readthrough="米州・APACのDTC改善だけでは欧州卸売の弱さを埋め切れず、グローバルアパレルでは地域ミックスと販促抑制が重要です。",
        watch=["EMEA卸売の底入れ", "DTC成長と粗利率", "関税還付を除く利益率"],
        why_evidence=["EMEA", "EPS"], readthrough_evidence=["APAC", "DTC"],
    ),
    _candidate(
        ticker="RARE", name="Ultragenyx", change=-40.0, source_type="company_ir",
        primary_url="https://ir.ultragenyx.com/news-releases/news-release-details/ultragenyx-announces-phase-3-aspire-results-angelman-syndrome",
        description="希少疾患向け治療薬を開発・販売するバイオ医薬品企業です。今回は決算ではなく、Angelman症候群向け主力候補apazunersenの臨床価値を左右するPhase 3結果が焦点です。",
        event_details=[_fact("Phase 3 AspireはBayley-4認知raw scoreの主要評価項目とMDRIの重要副次評価項目をともに達成できず、安全性はPhase 1/2と整合しました。", "Bayley-4", "MDRI", "達成できず", source_url="https://ir.ultragenyx.com/news-releases/news-release-details/ultragenyx-announces-phase-3-aspire-results-angelman-syndrome")],
        same_day=[_fact("会社はapazunersenの扱いを再評価し、商業部門を支えながら大幅な費用削減を検討すると表明しました。", "apazunersen", "費用削減", source_url="https://ir.ultragenyx.com/news-releases/news-release-details/ultragenyx-announces-phase-3-aspire-results-angelman-syndrome", announced_at="2026-09-02", temporal_status="same_day")],
        why="主要・重要副次評価項目の双方が未達で、Phase 1/2で見えた有効性を検証できず、apazunersenの成功確率とパイプライン価値が急低下しました。",
        readthrough="Angelman症候群向けASO開発では小規模初期試験から比較試験へ進む際の再現性が課題となり、同領域の評価にも慎重さを促します。",
        watch=["apazunersen継続可否", "費用削減の規模", "UX111の承認判断と商業事業の資金創出"],
        why_evidence=["双方が未達", "apazunersen"], readthrough_evidence=["ASO", "再現性"],
    ),
]


def _september_3_payload() -> dict:
    data = payload()
    data["stable_key"] = "ny_market_daily:2026-09-03"
    data["report_date_jst"] = "2026-09-03"
    data["generated_at"] = "2026-09-03T05:40:00+09:00"
    data["market_session_date"] = "2026-09-02"
    data["canonical_market_data"]["market_session_date"] = "2026-09-02"
    data["report_markdown"] = data["report_markdown"].replace("2026-09-02", "2026-09-03", 1)
    template = deepcopy(data["ticker_research"][-1])
    selected = []
    candidates = []
    for definition in SEPTEMBER_3_CANDIDATES:
        ticker = definition["ticker"]
        name = definition["name"]
        description = definition["description"]
        change = definition["change"]
        source_url = definition["primary_url"]
        source_type = definition["source_type"]
        research = deepcopy(template)
        research.update({
            "ticker": ticker,
            "company_name": name,
            "company_description": description,
            "catalyst": "引け後の正式発表を一次情報で確認",
            "catalyst_type": "earnings_or_material_event",
            "source_url": source_url,
            "source_type": source_type,
            "search_status": "verified_catalyst",
            "search_queries": [f'"{ticker}" 2026-09-02 results', f'"{name}" investor relations 2026-09-02'],
        })
        data["ticker_research"].append(research)
        structured = {
            key: deepcopy(value)
            for key, value in definition.items()
            if key not in {
                "ticker", "name", "description", "change", "primary_url", "source_type"
            }
        }
        item = {
            **deepcopy(research),
            **structured,
            "after_hours_change_pct": change,
            "as_of_utc": "2026-09-02T20:28:00Z",
            "as_of_jst": "2026-09-03T05:28:00+09:00",
            "session": "post_market",
            "display_company_name": name,
            "company_description": description,
            "after_hours_reaction": {
                "change_pct": change,
                "source_url": AFTER_HOURS_PRICE_SOURCE,
            },
            "after_hours_price_source_url": AFTER_HOURS_PRICE_SOURCE,
            "after_hours_price_provider": "Investing.com",
        }
        selected.append(item)
        candidates.append({
            "importance_rank": len(candidates) + 1,
            "ticker": ticker,
            "status": "included",
            "reason_code": "verified_material_event" if ticker == "RARE" else "verified_earnings",
            "reason": "正式発表と信頼できる時間外反応を検証できたため採用。",
            "discovered_by": ["after_hours_movers", "material_events"] if ticker == "RARE" else ["earnings_calendar", "after_hours_movers"],
            "discovery_source_url": item["after_hours_price_source_url"],
            "discovery_source_kind": "secondary_discovery",
            "primary_source_url": source_url,
            "price_source_url": item["after_hours_price_source_url"],
        })
        for evidence in (*item["fact_sources"], *item["market_context_sources"]):
            if not any(source["url"] == evidence["url"] for source in data["sources"]):
                data["sources"].append({
                    "title": evidence["label"],
                    "publisher": name if evidence in item["fact_sources"] else "Market Context",
                    "url": evidence["url"],
                    "published_at": "2026-09-02",
                })
    data["sources"].append({
        "title": "After-hours movers September 2",
        "publisher": "Investing.com",
        "url": selected[0]["after_hours_price_source_url"],
        "published_at": "2026-09-02T20:28:00Z",
    })
    data["after_hours_earnings"] = deepcopy(selected)
    data["after_hours_research"] = deepcopy(selected)
    discovery_runs = [
        {"scope": "earnings_calendar", "query": "US earnings calendar 2026-09-02", "source_url": "https://www.nasdaq.com/market-activity/earnings", "source_kind": "secondary_discovery", "status": "completed"},
        {"scope": "after_hours_movers", "query": "US after-hours movers 2026-09-02", "source_url": selected[0]["after_hours_price_source_url"], "source_kind": "secondary_discovery", "status": "completed"},
        {"scope": "regulatory_filings", "query": "SEC 8-K results 2026-09-02", "source_url": "https://www.sec.gov/edgar/search/", "source_kind": "official_registry", "status": "completed"},
        {"scope": "material_events", "query": "US clinical trial FDA announcements 2026-09-02", "source_url": selected[0]["after_hours_price_source_url"], "source_kind": "secondary_discovery", "status": "completed"},
    ]
    for run in discovery_runs:
        if not any(source["url"] == run["source_url"] for source in data["sources"]):
            data["sources"].append({"title": run["scope"], "publisher": "Discovery", "url": run["source_url"], "published_at": "2026-09-02"})
    data["after_hours_candidate_review"] = {
        "contract_version": "ny_market_after_hours_v2",
        "discovery_method": "broad_discovery_then_primary_verification",
        "market_session_date": "2026-09-02",
        "discovery_started_at": "2026-09-02T20:00:00Z",
        "discovery_completed_at": "2026-09-02T20:28:00Z",
        "discovery_runs": discovery_runs,
        "coverage_status": "normal",
        "discovered_candidate_count": len(candidates),
        "candidates": candidates,
    }
    return apply_display_contract(attach_market_data_packet_metadata(data))


def test_discovery_plan_is_broad_then_verifies_every_candidate_without_truncation():
    tickers = ["AVGO", "SNOW", "HPE", "NTAP", "NTSK", "FIVE", "RARE", "CHPT", "TLYS", "PVH"]
    plan = build_after_hours_discovery_plan(
        "2026-09-02",
        [{"ticker": ticker, "company_name": f"Company {ticker}"} for ticker in tickers],
    )
    assert plan["discovery_method"] == "broad_discovery_then_primary_verification"
    assert len(plan["broad_discovery_queries"]) == 4
    assert plan["market_session_date"] == "2026-09-02"
    assert [item["ticker"] for item in plan["candidate_verification"]] == tickers
    assert all(len(item["verification_queries"]) == 6 for item in plan["candidate_verification"])


def test_two_company_quiet_day_is_valid_and_renders_concise_paragraphs():
    data = payload()
    validate_payload(data)
    body = _section(data["report_markdown"], "引け後・アフター決算の注目株")
    assert len(re.findall(r"(?m)^#### ", body)) == 2
    assert "時間外株価は決算直後の初動" in body
    assert "通常取引終値" not in body
    assert "session=post_market" not in body
    assert "as_of_utc" not in body
    assert "provider" not in body.lower()
    assert "\\*\\*" not in body


def test_quiet_day_requires_completed_broad_discovery_and_cannot_bypass_count():
    missing_discovery = payload()
    del missing_discovery["after_hours_candidate_review"]["discovery_runs"]
    with pytest.raises(NYMarketValidationError, match="discovery_runs"):
        validate_payload(missing_discovery)

    self_declared_normal = payload()
    self_declared_normal["after_hours_candidate_review"]["coverage_status"] = "normal"
    with pytest.raises(NYMarketValidationError, match="derived from selected count"):
        validate_payload(self_declared_normal)

    self_declared_quiet = _september_3_payload()
    self_declared_quiet["after_hours_candidate_review"]["coverage_status"] = "quiet_day"
    with pytest.raises(NYMarketValidationError, match="derived from selected count"):
        validate_payload(self_declared_quiet)


def test_excluded_candidate_requires_a_structured_reason():
    data = payload()
    review = data["after_hours_candidate_review"]
    review["candidates"].append({
        "importance_rank": 3,
        "ticker": "TEST",
        "status": "excluded",
        "reason": "一次情報を確認できませんでした。",
        "discovered_by": ["earnings_calendar"],
        "discovery_source_url": "https://example.com/discovery/earnings-calendar",
        "discovery_source_kind": "secondary_discovery",
    })
    review["discovered_candidate_count"] = 3
    with pytest.raises(NYMarketValidationError, match="reason_code"):
        validate_payload(data)


def test_september_3_fixture_renders_all_ten_verified_candidates_compactly():
    data = _september_3_payload()
    validate_payload(data)
    body = _section(data["report_markdown"], "引け後・アフター決算の注目株")
    assert len(re.findall(r"(?m)^#### ", body)) == 10
    for definition in SEPTEMBER_3_CANDIDATES:
        ticker = definition["ticker"]
        name = definition["name"]
        assert f"#### {name}（{ticker}）— 時間外 約" in body
    for hidden in ("as_of_utc", "as_of_jst", "search_status", "searched_at", "provider=", "通常取引終値"):
        assert hidden not in body
    company_blocks = body.split("\n\n#### ")[1:]
    assert len(company_blocks) == 10
    assert all(len(block.split("\n\n")) == 4 for block in company_blocks)


def test_after_hours_projection_tampering_fails_closed():
    data = payload()
    data["after_hours_earnings"][0]["reported_results"][0]["summary"] = (
        data["after_hours_earnings"][0]["reported_results"][0]["summary"].replace(
            "$30.0B", "$99.0B"
        )
    )
    data = apply_display_contract(data)
    with pytest.raises(NYMarketValidationError, match="canonical after_hours_research"):
        validate_payload(data)


def test_after_hours_description_number_and_session_fail_closed():
    missing_description = payload()
    del missing_description["after_hours_earnings"][0]["company_description"]
    with pytest.raises(NYMarketValidationError, match="company_description"):
        validate_payload(missing_description)

    changed_number = payload()
    changed_number["after_hours_research"][0]["reported_results"][0]["summary"] = (
        changed_number["after_hours_research"][0]["reported_results"][0]["summary"].replace(
            "$30.0B", "$31.0B"
        )
    )
    with pytest.raises(NYMarketValidationError, match="canonical after_hours_research"):
        validate_payload(changed_number)

    wrong_session = payload()
    wrong_session["after_hours_earnings"][0]["session"] = "regular"
    with pytest.raises(NYMarketValidationError, match="post_market"):
        validate_payload(wrong_session)


def test_after_hours_canonical_does_not_require_regular_session_projection_fields():
    data = payload()
    regular_only = {
        "close", "change_pct", "market_cap", "market_cap_method", "catalyst",
        "catalyst_type", "search_status", "searched_at", "share_class_components",
        "search_attempt_count", "search_queries", "searched_sources", "revenue", "eps",
        "one_offs", "price_reaction", "why_stock_moved",
        "forward_implication",
    }
    for field in ("after_hours_earnings", "after_hours_research"):
        for item in data[field]:
            for key in regular_only:
                item.pop(key, None)
    data = apply_display_contract(data)
    validate_payload(data)


def test_after_hours_renderer_preserves_every_other_section():
    data = payload()
    original = deepcopy(data)
    data["after_hours_earnings"][0]["investment_readthrough"] += " 確認用。"
    data["after_hours_research"][0]["investment_readthrough"] += " 確認用。"
    rendered = apply_display_contract(data)
    for heading in ("要点", "話題の値下がり10社", "主要決算"):
        assert _section(rendered["report_markdown"], heading) == _section(
            original["report_markdown"], heading
        )


def test_snowflake_renders_consensus_guide_revision_aws_context_and_analysis():
    data = _september_3_payload()
    body = _section(data["report_markdown"], "引け後・アフター決算の注目株")
    snow = body.split("#### Snowflake（SNOW）", 1)[1].split("\n\n#### ", 1)[0]
    assert "市場予想$0.45" in snow
    assert "$5.84Bから$6.07Bへ引き上げ" in snow
    assert "AWSと5年間$6B" in snow
    assert "AI需要を実課金へ変える力" in snow
    assert "将来の製品消費増加につながる可能性" in snow
    assert "中期的な利用拡大を支える背景材料" in snow


def test_missing_same_day_developments_fails_closed():
    data = payload()
    for field in ("after_hours_earnings", "after_hours_research"):
        del data[field][0]["same_day_developments"]
    with pytest.raises(NYMarketValidationError, match="same_day_developments"):
        validate_payload(data)


def test_prior_period_background_cannot_be_labeled_as_same_day():
    data = _september_3_payload()
    for field in ("after_hours_earnings", "after_hours_research"):
        snow = next(item for item in data[field] if item["ticker"] == "SNOW")
        prior = snow["background_context"].pop()
        snow["same_day_developments"].append(prior)
    with pytest.raises(NYMarketValidationError, match="same-day development"):
        validate_payload(data)


def test_market_consensus_claim_requires_declared_market_source():
    data = payload()
    for field in ("after_hours_earnings", "after_hours_research"):
        data[field][0]["consensus_comparison"][0]["source_url"] = (
            "https://example.com/undeclared-consensus"
        )
    with pytest.raises(NYMarketValidationError, match="matching source list"):
        validate_payload(data)


@pytest.mark.parametrize("missing_field", ["why_moved", "investment_readthrough"])
def test_missing_company_specific_qualitative_analysis_fails_closed(missing_field):
    data = payload()
    for field in ("after_hours_earnings", "after_hours_research"):
        data[field][0][missing_field] = ""
    with pytest.raises(NYMarketValidationError, match=missing_field):
        validate_payload(data)


def test_generic_qualitative_sentence_does_not_pass_quality_gate():
    data = payload()
    for field in ("after_hours_earnings", "after_hours_research"):
        data[field][0]["why_moved"] = "好決算が評価されました。"
    data = apply_display_contract(data)
    with pytest.raises(NYMarketValidationError, match="generic qualitative analysis"):
        validate_payload(data)


def test_material_event_does_not_require_earnings_fields():
    data = _september_3_payload()
    rare = next(item for item in data["after_hours_research"] if item["ticker"] == "RARE")
    assert rare["event_type"] == "material_event"
    for field in (
        "reported_results", "consensus_comparison", "guidance",
        "guidance_comparison", "key_kpis", "background_context",
    ):
        assert field not in rare
    validate_payload(data)


def test_each_company_renders_three_paragraphs_with_clickable_source_links():
    data = _september_3_payload()
    body = _section(data["report_markdown"], "引け後・アフター決算の注目株")
    company_blocks = body.split("\n\n#### ")[1:]
    assert all(len(block.split("\n\n")) == 4 for block in company_blocks)
    assert all("  \n出典：[" in block and "](" in block for block in company_blocks)


def test_historical_migration_uses_production_data_and_preserves_other_markdown():
    source = _september_3_payload()
    source["report_display_contract_version"] = "ny_market_display_v2"
    before = source["report_markdown"]
    migrated = migrate_payload(
        source,
        expected_input_sha256=_payload_sha256(source),
        applied_commit="a" * 40,
    )
    assert _without_after_hours(before) == _without_after_hours(migrated["report_markdown"])
    assert migrated["report_display_contract_version"] == "ny_market_display_v3"
    assert migrated["after_hours_candidate_review"]["contract_version"] == (
        "ny_market_after_hours_v2"
    )
    assert migrated["after_hours_migration"]["migration_source"] == (
        "reviewed_external_research_snapshot_2026-09-03"
    )
    assert migrated["after_hours_migration"]["applied_commit"] == "a" * 40
    validate_payload(migrated)


def test_production_migration_runtime_does_not_import_tests():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "lib/ny_market_20260903_after_hours_v3.py",
        "tools/migrate_ny_market_20260903_after_hours_v3.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(
            name == "tests" or name.startswith("tests.") for name in imported_modules
        )


def test_historical_migration_data_has_no_regular_session_projection_values():
    forbidden = {
        "close", "change_pct", "market_cap", "market_cap_method",
        "share_class_components", "catalyst", "catalyst_type",
    }
    for item in MIGRATION_PAYLOAD["after_hours_research"]:
        assert forbidden.isdisjoint(item)
        assert item["search_status"] == "verified_for_historical_migration"
        assert set(item["searched_sources"]) == {
            source["url"]
            for source in (*item["fact_sources"], *item["market_context_sources"])
        }
