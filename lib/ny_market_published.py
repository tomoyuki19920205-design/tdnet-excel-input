"""A usable daily edition with sourced sections and explicit, bounded gaps.

The complete contract remains in ny_market.py. This contract does not promote
missing prices or pending research into verified facts.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from lib.ny_market_research import validate_market_packet
from lib.ny_market_deadline import validate_progress, validate_morning_cutoff, validate_updates, notice
from lib.ny_market_display import SECTOR_LABELS

CONTRACT='ny_market_published_v1'
SECTIONS={
    'indexes':'5指数','sectors':'11業種別騰落','gainers':'話題の値上がり10社',
    'top20':'純粋上昇率Top20','losers':'話題の値下がり10社','earnings':'主要決算',
    'after_hours':'引け後・アフター決算の注目株','news':'主要ニュース',
    'commodities':'商品市況・金利・為替','analysis':'米株全体の分析',
}

def render(payload):
    catalog={s['id']:s for s in payload['publication_sources']}
    def links(row):
        return ' '.join(f"[{catalog[k]['publisher']}]({catalog[k]['url']})" for k in row['source_ids'])
    lines=[f"# 【通常公開版】NY市場モーニングレポート {payload['report_date_jst']}",'',
        '主要セクションを掲載した通常公開版（published）です。全品質条件を満たす完全版（complete）とは区別します。',
        f"対象NY市場営業日：{payload['market_session_date']}。情報基準：{payload['review_progress']['as_of_utc']}（各項目の日時・取引区分を優先）。",'',
        '## 要点',*['- '+s for s in payload['summary_bullets']]]
    def heading(key):
        state=payload['section_status'][key]
        label={'complete':'完成','partial':'一部未確認','unavailable':'取得不能'}[state['status']]
        lines.extend(['','## '+SECTIONS[key],f"確認状況：{label}。{state['note']}"])
    for key,field in [('indexes','indexes'),('sectors','sectors')]:
        heading(key)
        for r in payload['canonical_market_data'][field]:
            name=r['symbol'] if key=='indexes' else SECTOR_LABELS[r['symbol']]
            close=f" {r['close']:,.2f}、" if key=='indexes' else ' '
            lines.append(f"- {name}{close}{r['change_pct']:+.2f}%")
        lines.append('基準：対象営業日の通常取引終値。指数・業種の出典は末尾の市場数値証跡を参照。')
    for key,field in [('gainers','notable_gainers'),('top20','top_gainers_20'),('losers','notable_losers')]:
        heading(key)
        for i,r in enumerate(payload[field],1):
            title=f"{i}. {r['company_name']}（{r['ticker']}） {r['change_pct']:+.2f}%"
            if key=='gainers':title+=' — '+r['company_description']
            lines.extend(['',title,'',f"    {r['catalyst']} {links(r)}"])
        if key=='losers':lines.append('選定は市場への影響・下落幅・材料による10社であり、全上場銘柄の下落率順位ではありません。')
    for key,field in [('earnings','earnings'),('after_hours','after_hours_earnings'),('news','major_news')]:
        heading(key)
        for r in payload[field]:
            lines.extend(['','### '+r['title'],r['summary'],links(r)])
            if r.get('market_impact'):lines.append('市場への含意（分析）：'+r['market_impact'])
            if r.get('missing'):lines.append('未確認・取得不能：'+r['missing'])
    heading('commodities')
    for r in payload['commodities']:
        value=f"{r['price']:,.4f}".rstrip('0').rstrip('.')
        change=f"{r['change_bp']:+.2f}bp" if 'change_bp' in r else f"{r['change_pct']:+.2f}%"
        lines.extend(['',f"- {r['name']}：{value} {r['unit']}、{change}。基準：{r['as_of_utc']}、{r['basis']}。",r['reason'],links(r)])
    heading('analysis')
    for r in payload['market_analysis']:
        lines.extend(['','### '+r['title'],r['summary'],links(r)])
    lines.extend(['','## 未確認情報と補完予定',notice(payload['review_progress']['counts']),
        '保留候補は除外扱いにしていません。次回は保存した判定と出典を使って残件だけ確認します。'])
    for r in payload['unresolved_items']:lines.append('- '+r['field']+'：'+r['reason'])
    lines.extend(['','## 出典と基準日時'])
    for s in catalog.values():lines.append(f"- [{s['id']}：{s['publisher']}]({s['url']}) — {s['available_at']}。{s.get('time_note','')} ")
    lines.extend(['','### 市場数値証跡'])
    for field in ['indexes','sectors']:
        for r in payload['canonical_market_data'][field]:lines.append(f"- [{r['symbol']}]({r['source_identifier']})：{r['session_date']}通常取引。")
    return '\n'.join(lines).strip()

def validate_published(p):
    from lib.ny_market import (NYMarketValidationError, ValidatedNYMarketReport, JSON_FIELDS,
        stable_report_id, report_markdown_sha256, _date, _timestamp, _text, PLATFORM_CITATION_RE)
    def require(condition,message):
        if not condition:raise NYMarketValidationError(message)
    require(p.get('publication_contract_version')==CONTRACT,'published contract required')
    require(p.get('schema_version')=='ny_market_daily_v1' and p.get('report_type')=='ny_market_daily','published schema/type invalid')
    rd=_date(p.get('report_date_jst'),'report_date_jst'); sd=_date(p.get('market_session_date'),'market_session_date')
    require(sd<=rd and p.get('stable_key')==f'ny_market_daily:{rd}','published identity mismatch')
    require(p.get('market_status') in {'open','holiday_or_weekend'},'published market status invalid')
    try:
        indexes,sectors,top,_=validate_market_packet(p)
        _,cutoff=validate_progress(p['review_progress']);validate_morning_cutoff(cutoff,rd)
    except (ValueError,KeyError,TypeError) as exc:raise NYMarketValidationError(str(exc)) from exc
    require(not p['canonical_market_data'].get('unresolved_items'),'major market data unresolved')
    require(5<=len(p.get('summary_bullets',[]))<=8,'published requires 5..8 summary bullets')
    states=p.get('section_status',{})
    require(set(states)==set(SECTIONS),'all traditional sections must be retained')
    for key,s in states.items():
        require(s.get('status') in {'complete','partial','unavailable'} and bool(s.get('note')),'section status and note required')
        require(s.get('as_of_utc')==p['review_progress']['as_of_utc'],'section cutoff differs')
    for key in ['indexes','sectors','analysis']:require(states[key]['status']=='complete','essential market section unfinished')
    catalog={}
    for s in p.get('publication_sources',[]):
        require(s['id'] not in catalog,'duplicate source id')
        # Reuse the archive integrity and historical availability validator.
        evidence={**s,'published_at':s['available_at']}
        try:validate_updates([dict(id=s['id'],title=s['id'],summary='source evidence',section='source',evidence=[evidence])],cutoff)
        except (ValueError,KeyError,OSError) as exc:raise NYMarketValidationError(str(exc)) from exc
        catalog[s['id']]=s
    require(bool(catalog),'source evidence required')
    def sourced(row):
        require(bool(row.get('source_ids')) and set(row['source_ids'])<=set(catalog),'item lacks verified sources')
    require(p.get('index_moves')==indexes and p.get('sector_moves')==list(sectors.values()),'market projection differs')
    for field,n in [('top_gainers_20',20),('notable_gainers',10),('notable_losers',10),('major_news',10)]:
        require(isinstance(p.get(field),list) and len(p[field])==n,f'{field} requires {n} items')
    for i,row in enumerate(p['top_gainers_20']):
        for key,value in top[i].items():require(row.get(key)==value,'Top20 canonical value changed: '+key)
        require(row.get('material_status') in {'verified','unconfirmed','unavailable'},'material status required')
        _text(row.get('catalyst'),'catalyst',3000,15);sourced(row)
    require(p['notable_gainers']==p['top_gainers_20'][:10],'gainers must preserve first ten projections')
    for r in p['notable_gainers']:_text(r.get('company_description'),'company_description',1000,10)
    require(len({r['ticker'] for r in p['notable_losers']})==10,'duplicate loser')
    for r in p['notable_losers']:
        sourced(r);_text(r.get('catalyst'),'loser catalyst',3000,15)
        require(r.get('session_date')==sd and r.get('price_basis')=='regular_close','loser price date/basis differs')
        require(all(isinstance(r.get(k),(int,float)) and not isinstance(r[k],bool) and math.isfinite(r[k]) for k in ['close','previous_close','change_pct']),'invalid loser number')
        require(r['close']>0 and r['previous_close']>0 and r['change_pct']<0,'not a dated loser')
        require(abs((r['close']/r['previous_close']-1)*100-r['change_pct'])<0.015,'loser return mismatch')
        require(r.get('price_source_id') in r['source_ids'],'loser price evidence missing')
        try:
            data=json.loads(Path(catalog[r['price_source_id']]['path']).read_text(encoding='utf8'))['chart']['result'][0]
            require(data['meta']['symbol']==r['ticker'],'loser price issuer differs')
            if r.get('evidence_basis')=='cached_chart_meta':
                meta=data['meta']
                observed=datetime.fromtimestamp(meta['regularMarketTime'],timezone.utc)
                require(observed.date().isoformat()==sd and observed<=cutoff,'loser quote time differs')
                close,previous=meta['regularMarketPrice'],meta['chartPreviousClose']
            elif r.get('evidence_basis')=='daily_unadjusted_close':
                bars={datetime.fromtimestamp(t,timezone.utc).date().isoformat():c for t,c in zip(data['timestamp'],data['indicators']['quote'][0]['close'])}
                prior=max(d for d in bars if d<sd and bars[d] is not None)
                close,previous=bars[sd],bars[prior]
            else:raise ValueError('unsupported loser price evidence basis')
            require(abs(close-r['close'])<1e-8 and abs(previous-r['previous_close'])<1e-8,'loser price differs from archived response')
        except (KeyError,TypeError,ValueError,OSError) as exc:
            raise NYMarketValidationError(str(exc)) from exc
    for field in ['earnings','after_hours_earnings','major_news','market_analysis']:
        require(bool(p.get(field)),field+' empty')
        for r in p[field]:
            sourced(r);_text(r.get('title'),'title',500);_text(r.get('summary'),'summary',10000,60)
    clusters={}
    for r in p['major_news']:
        c=r.get('event_cluster');require(bool(c),'news cluster missing');clusters[c]=clusters.get(c,0)+1
        require(clusters[c]<=2,'duplicate news cluster')
    require(len(p['market_analysis'])>=3 and sum(len(r['summary']) for r in p['market_analysis'])>=600,'substantive market analysis required')
    require(len(p.get('commodities',[]))>=2,'commodity section empty')
    for r in p['commodities']:
        sourced(r)
        require(all(isinstance(r.get(k),(int,float)) and not isinstance(r[k],bool) and math.isfinite(r[k]) for k in ['price','change_pct']),'invalid commodity number')
        t=datetime.fromisoformat(r['as_of_utc'].replace('Z','+00:00'))
        require(t.tzinfo is not None and t<=cutoff and bool(r.get('basis')),'commodity time/basis missing')
    # Mandatory morning macro observations share the existing sourced market section.
    # Older archived editions remain readable; new editions cannot silently omit them.
    macro_symbols={'US2Y','US10Y','US30Y','DXY','USDJPY'}
    macro=[r for r in p['commodities'] if r.get('symbol') in macro_symbols]
    if rd>='2026-11-14' or macro:
        require(len(macro)==5 and {r['symbol'] for r in macro}==macro_symbols,'five daily rates/FX observations required')
        require(len([r for r in p['commodities'] if r.get('symbol') not in macro_symbols])>=2,'two commodities required in addition to rates/FX')
        for r in macro:
            require(r.get('session_date')==sd,'rates/FX date differs from market session')
            require(r['price']>0,'invalid rates/FX level')
            previous=r.get('previous_value')
            require(isinstance(previous,(int,float)) and not isinstance(previous,bool) and math.isfinite(previous) and previous>0,'rates/FX comparison value missing')
            prior=datetime.fromisoformat(r.get('previous_as_of_utc','').replace('Z','+00:00'))
            observed=datetime.fromisoformat(r['as_of_utc'].replace('Z','+00:00'))
            require(prior.tzinfo is not None and prior<observed and observed.date().isoformat()<=sd,'rates/FX comparison time invalid')
            if observed.date().isoformat()!=sd:
                require(r.get('observation_date')==observed.date().isoformat() and bool(r.get('carry_forward_reason')),'older rates/FX observation must disclose its date and reason')
            require(abs((r['price']/previous-1)*100-r['change_pct'])<0.015,'rates/FX percent change mismatch')
            if r['symbol'].startswith('US') and r['symbol']!='USDJPY':
                require(r.get('unit')=='%' and r.get('instrument_type')=='treasury_par_yield','Treasury yield basis required')
                bp=r.get('change_bp')
                require(isinstance(bp,(int,float)) and not isinstance(bp,bool) and math.isfinite(bp) and abs((r['price']-previous)*100-bp)<0.015,'yield basis-point change mismatch')
            else:
                require(r.get('instrument_type')=={'DXY':'cash_dollar_index','USDJPY':'spot_fx'}[r['symbol']],'cash index/spot FX basis required')
    require(bool(p.get('unresolved_items')),'published must disclose gaps to complete contract')
    require(p.get('headline','').startswith('【通常公開版】'),'published label missing')
    markdown=render(p)
    require(p.get('report_markdown')==markdown,'published rendering differs')
    digest=report_markdown_sha256(markdown)
    require(p.get('report_delivery')=={'source_field':'report_markdown','sha256':digest},'published hash differs')
    require(not PLATFORM_CITATION_RE.search(json.dumps(p,ensure_ascii=False)),'platform citation in payload')
    clean={**p,'quality_status':'published','sources':[dict(title=s['id'],publisher=s['publisher'],url=s['url'],published_at=s['available_at']) for s in catalog.values()]}
    report=dict(id=stable_report_id(p['stable_key']),stable_key=p['stable_key'],schema_version=p['schema_version'],report_type=p['report_type'],
        report_date_jst=rd,market_session_date=sd,market_status=p['market_status'],generated_at=_timestamp(p['generated_at'],'generated_at'),
        headline=p['headline'],report_markdown=markdown,quality_status='published')
    for field in JSON_FIELDS:report[field]=json.dumps(clean[field],ensure_ascii=False)
    run=dict(run_id=stable_report_id('run:'+p['stable_key']),stable_key=p['stable_key'],report_date_jst=rd)
    return ValidatedNYMarketReport(report,run,clean)
