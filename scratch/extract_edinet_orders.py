import os
import json
import zipfile
import re
import unicodedata
from bs4 import BeautifulSoup

CACHE_DIR = r'C:\Users\takuy\OneDrive\tdnet-excel-input\data\edinet_cache'
SURVEY_JSON = r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\survey_detail.json'

def norm(text):
    return unicodedata.normalize('NFKC', text).replace(' ', '').replace('\n', '').replace('\u3000', '')

def parse_number(s):
    if not s:
        return None
    s = norm(s)
    s = s.replace(',', '').replace('△', '-').replace('▲', '-')
    match = re.search(r'(-?\d+)', s)
    if match:
        return int(match.group(1))
    return None

def extract_from_company(target):
    ticker = target["ticker"]
    company = target["company"]
    doc_id = target["doc_id"]
    
    result = {
        "ticker": ticker,
        "company": company,
        "doc_id": doc_id,
        "source_type": None,
        "source_tag": None,
        "unit": None,
        "orders_received": None,
        "order_backlog": None,
        "construction_carryover": None,
        "completed_construction": None,
        "rpo": None,
        "snippet": "",
        "confidence": "low",
        "notes": ""
    }
    
    zip_path = os.path.join(CACHE_DIR, doc_id, 'xbrl.zip')
    if not os.path.exists(zip_path):
        result["notes"] = "ZIP not found"
        return result
        
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        htm_files = [n for n in names if 'PublicDoc' in n and n.endswith('.htm')]
        
        found_order = False
        
        for fname in htm_files:
            raw = z.read(fname)
            try: text = raw.decode('utf-8')
            except: text = raw.decode('cp932', errors='replace')
            
            soup = BeautifulSoup(text, 'lxml')
            
            for elem in soup.find_all(True):
                name_attr = elem.get('name', '')
                if not name_attr or 'textblock' not in name_attr.lower():
                    continue
                
                # HTML Table 解析
                if 'ManagementAnalysisOfFinancialPosition' in name_attr or 'SignificantAccountingPolicies' in name_attr:
                    tables = elem.find_all('table')
                    table_count = 0
                    for table in tables:
                        ttext = norm(table.get_text())
                        if any(x in ttext for x in ['受注高', '当期受注高', '受注額', '繰越工事高', '受注実績', '受注工事高']):
                            table_count += 1
                            rows = table.find_all('tr')
                            if not rows: continue
                            
                            # 軽量グリッドパーサー
                            grid = {}
                            max_col = 0
                            for r_idx, row in enumerate(rows):
                                cells = row.find_all(['td', 'th'])
                                c_idx = 0
                                for cell in cells:
                                    while grid.get((r_idx, c_idx)) is not None:
                                        c_idx += 1
                                    rowspan = int(cell.get('rowspan', 1))
                                    colspan = int(cell.get('colspan', 1))
                                    ctext = norm(cell.get_text())
                                    for r in range(rowspan):
                                        for c in range(colspan):
                                            grid[(r_idx + r, c_idx + c)] = ctext
                                    c_idx += colspan
                                    if c_idx > max_col:
                                        max_col = c_idx
                            
                            # ヘッダー領域の特定
                            start_row_idx = -1
                            for r_idx in range(min(15, len(rows))):
                                row_text = "".join([grid.get((r_idx, c), "") for c in range(max_col)])
                                if any(x in row_text for x in ['受注', '繰越', '完成']):
                                    start_row_idx = r_idx
                                    break
                                    
                            if start_row_idx == -1:
                                continue
                                
                            header_depth = 1
                            for cell in rows[start_row_idx].find_all(['td', 'th']):
                                rs = int(cell.get('rowspan', 1))
                                if rs > header_depth:
                                    header_depth = rs
                                    
                            end_header_idx = start_row_idx + header_depth - 1
                            
                            # カラムマッピング
                            col_map = {}
                            for c in range(max_col):
                                col_text = " ".join([grid.get((r, c), "") for r in range(start_row_idx, end_header_idx + 1)])
                                
                                if any(x in col_text for x in ['%', '％', '比', '率', '増減']):
                                    continue
                                    
                                if any(x in col_text for x in ['受注残高', '受注残', '期末受注残高', '当期受注残高']):
                                    col_map['order_backlog'] = c
                                elif any(x in col_text for x in ['次期繰越工事高', '次期繰越高', '期末繰越高', '繰越工事高']):
                                    if '前期' not in col_text and '期首' not in col_text:
                                        col_map['construction_carryover'] = c
                                elif any(x in col_text for x in ['受注工事高', '受注高', '当期受注高', '受注実績', '受注額']):
                                    col_map['orders_received'] = c
                                elif any(x in col_text for x in ['完成工事高', '当期完成工事高', '当期売上高']):
                                    col_map['completed_construction'] = c
                                    
                            if not col_map:
                                continue
                                
                            result['source_tag'] = name_attr
                            result['source_type'] = 'table'
                            
                            # 単位取得
                            if '千円' in ttext: result['unit'] = '千円'
                            elif '百万円' in ttext: result['unit'] = '百万円'
                            elif '億円' in ttext: result['unit'] = '億円'
                            
                            # 単位フォールバック (IHI対応)
                            if not result['unit']:
                                etext = norm(elem.get_text())
                                u_match = re.search(r'単位[:：]?\s*(千円|百万円|億円)', etext)
                                if u_match:
                                    result['unit'] = u_match.group(1)
                                else:
                                    units_found = [u for u in ['千円', '百万円', '億円'] if u in etext]
                                    if len(units_found) == 1:
                                        result['unit'] = units_found[0]
                            
                            best_score = -1
                            best_row_idx = -1
                            
                            for r_idx in range(end_header_idx + 1, len(rows)):
                                # Ensure row has at least one valid number in mapped columns
                                has_num = False
                                for mapped_c in col_map.values():
                                    if parse_number(grid.get((r_idx, mapped_c), "")) is not None:
                                        has_num = True
                                        break
                                if not has_num:
                                    continue
                                
                                row_header = ""
                                for c in range(max_col):
                                    val = norm(grid.get((r_idx, c), ""))
                                    if c in col_map.values():
                                        if parse_number(val) is not None and not any(x in val for x in ['期', '年', '月', '日']):
                                            break
                                    row_header += val
                                    
                                if any(x in row_header for x in ['前年', '前期', '前第', '増減', '構成', '比', '%', '％']):
                                    continue
                                
                                # 鹿島対応：数値をクリーンアップして純粋なテキストで判定
                                clean_header = re.sub(r'[\d,\.\s△▲\+\-]', '', row_header)
                                
                                score = 0
                                if '合計' in clean_header:
                                    if company == '鹿島建設':
                                        score = 0
                                    else:
                                        score = 100
                                elif clean_header == '計' or clean_header.endswith('計'):
                                    score = 90
                                elif '全社' in clean_header:
                                    score = 80
                                elif '当連結会計年度' in clean_header:
                                    score = 70
                                elif '当事業年度' in clean_header:
                                    score = 60
                                elif re.search(r'第\d+期', row_header): # clean_headerは数字が消えるのでrow_headerを使う
                                    score = 50
                                elif '当期' in clean_header:
                                    score = 40
                                else:
                                    score = 10
                                    
                                if score >= best_score and score > 0:
                                    best_score = score
                                    best_row_idx = r_idx
                                    
                            if best_row_idx != -1:
                                try:
                                    temp_res = {}
                                    def safe_parse(c_idx):
                                        val_str = grid.get((best_row_idx, c_idx), "")
                                        if any(x in val_str for x in ['%', '％']):
                                            return None
                                        return parse_number(val_str)

                                    if 'orders_received' in col_map:
                                        temp_res['orders_received'] = safe_parse(col_map['orders_received'])
                                    if 'order_backlog' in col_map:
                                        temp_res['order_backlog'] = safe_parse(col_map['order_backlog'])
                                    if 'construction_carryover' in col_map:
                                        temp_res['construction_carryover'] = safe_parse(col_map['construction_carryover'])
                                    if 'completed_construction' in col_map:
                                        temp_res['completed_construction'] = safe_parse(col_map['completed_construction'])
                                    
                                    if any(v is not None for v in temp_res.values()):
                                        result.update(temp_res)
                                        found_order = True
                                        header_strs = [norm(grid.get((start_row_idx, c), "")) for c in range(max_col)]
                                        row_strs = [norm(grid.get((best_row_idx, c), ""))[:15] for c in range(max_col)]
                                        result['snippet'] = f"Header: {'|'.join(header_strs)}\nRow: {'|'.join(row_strs)}"
                                except Exception as e:
                                    pass
                                    
                            if found_order:
                                if result['unit'] is None:
                                    result['confidence'] = 'low'
                                    result['notes'] += "Unit not found. "
                                elif table_count > 1:
                                    result['confidence'] = 'medium'
                                    result['notes'] += f"Multiple tables found ({table_count}), picked last. "
                                else:
                                    result['confidence'] = 'high'
                                break
                                    
                # RPO 自然言語解析
                if not found_order and ('NotesRevenue' in name_attr or 'Revenue' in name_attr or 'jpcrp_cor' in name_attr):
                    etext = norm(elem.get_text())
                    if '残存履行義務' in etext or '履行義務' in etext or '受注高' in etext:
                        matches = []
                        for m in re.finditer(r'履行義務は([\d,]+)(百万円|千円|億円|円)', etext):
                            matches.append(m)
                        for m in re.finditer(r'総額は([\d,]+)(百万円|千円|億円|円)', etext):
                            matches.append(m)
                        for m in re.finditer(r'残存履行義務に配分した取引価格[^\d]*([\d,]+)(百万円|千円|億円|円)', etext):
                            matches.append(m)
                        for m in re.finditer(r'受注高は[\D]*([\d,]+)(百万円|千円|億円|円)', etext):
                            matches.append(("orders_received", m))
                            
                        if matches:
                            last_match = matches[-1]
                            if type(last_match) == tuple and last_match[0] == "orders_received":
                                m_obj = last_match[1]
                                val = parse_number(m_obj.group(1))
                                unit_val = m_obj.group(2)
                                if result['orders_received'] is None:
                                    result['orders_received'] = val
                                    result['source_tag'] = name_attr
                                    result['source_type'] = 'text'
                                    result['unit'] = unit_val
                                    idx = etext.find(m_obj.group(0))
                                    result['snippet'] = etext[max(0, idx-30):idx+30]
                                    result['confidence'] = 'medium'
                                    result['notes'] += "orders_received extracted from text. "
                                    found_order = True
                            else:
                                rpo_val = parse_number(last_match.group(1))
                                unit_val = last_match.group(2)
                                
                                if result['rpo'] is None:
                                    result['rpo'] = rpo_val
                                    result['source_tag'] = name_attr
                                    result['source_type'] = 'text'
                                    result['unit'] = unit_val
                                    idx = etext.find(last_match.group(0))
                                    result['snippet'] = etext[max(0, idx-30):idx+30]
                                    result['confidence'] = 'medium'
                                    result['notes'] += "RPO extracted from text. "
                                    found_order = True
            
            if found_order:
                break
                
    if result['orders_received'] is None and result['order_backlog'] is None and result['construction_carryover'] is None and result['rpo'] is None:
        result['confidence'] = 'low'
        result['notes'] += "No valid values extracted."
        
    return result

if __name__ == '__main__':
    with open(SURVEY_JSON, 'r', encoding='utf-8') as f:
        survey_data = json.load(f)
        
    targets = []
    for d in survey_data:
        if d.get("doc_id"):
            targets.append({
                "ticker": d["ticker"],
                "company": d["company"],
                "doc_id": d["doc_id"]
            })
            
    print(f"Target companies: {len(targets)}")
    
    results = []
    for t in targets:
        res = extract_from_company(t)
        results.append(res)
    
    out_json = r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\orders_extracted_30_v4.json'
    out_md = r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\orders_extracted_30_v4_summary.md'
    
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        
    success_count = sum(1 for r in results if r['confidence'] in ('high', 'medium') or r['orders_received'] or r['order_backlog'] or r['rpo'] or r['construction_carryover'])
    fail_count = len(results) - success_count
    
    source_type_count = {}
    conf_count = {}
    low_conf = []
    manual_check = []
    
    for r in results:
        st = str(r['source_type'])
        source_type_count[st] = source_type_count.get(st, 0) + 1
        
        conf = r['confidence']
        conf_count[conf] = conf_count.get(conf, 0) + 1
        
        if conf == 'low':
            low_conf.append(f"{r['ticker']} {r['company']}: {r['notes']}")
        if conf == 'low' or ('Multiple tables' in r['notes']):
            manual_check.append(f"{r['ticker']} {r['company']}")
            
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write("# 30社 DRY RUN 抽出結果サマリー (v4)\n\n")
        f.write(f"- **実行対象社数**: {len(targets)}社\n")
        f.write(f"- **成功社数**: {success_count}社\n")
        f.write(f"- **失敗社数**: {fail_count}社\n\n")
        
        f.write("## Source Type別件数\n")
        for k, v in source_type_count.items():
            f.write(f"- {k}: {v}\n")
            
        f.write("\n## Confidence別件数\n")
        for k, v in conf_count.items():
            f.write(f"- {k}: {v}\n")
            
        f.write("\n## 低Confidenceの会社一覧\n")
        for c in low_conf:
            f.write(f"- {c}\n")
            
        f.write("\n## 手動確認推奨 (複数表など)\n")
        for c in manual_check:
            f.write(f"- {c}\n")
            
        f.write("\n## 抽出結果一覧\n")
        f.write("| ticker | company | type | unit | orders | backlog | carryover | rpo | conf |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            ord_v = r['orders_received'] if r['orders_received'] is not None else '-'
            bklg_v = r['order_backlog'] if r['order_backlog'] is not None else '-'
            c_v = r['construction_carryover'] if r['construction_carryover'] is not None else '-'
            rpo_v = r['rpo'] if r['rpo'] is not None else '-'
            f.write(f"| {r['ticker']} | {r['company']} | {r['source_type']} | {r['unit']} | {ord_v} | {bklg_v} | {c_v} | {rpo_v} | {r['confidence']} |\n")
            
    print(f"Complete. Saved to {out_json} and {out_md}")
