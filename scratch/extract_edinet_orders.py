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
                        if '受注高' in ttext or '当期受注高' in ttext or '受注額' in ttext or '繰越工事高' in ttext or '受注実績' in ttext or '受注工事高' in ttext:
                            table_count += 1
                            rows = table.find_all('tr')
                            if not rows: continue
                            
                            col_map = {}
                            unit_str = None
                            header_row_idx = -1
                            header_cols_count = 0
                            
                            for i, row in enumerate(rows[:5]):
                                row_text = norm(row.get_text())
                                if '千円' in row_text: unit_str = '千円'
                                elif '百万円' in row_text: unit_str = '百万円'
                                elif '億円' in row_text: unit_str = '億円'
                                
                                cells = row.find_all(['td', 'th'])
                                has_order_col_in_this_row = False
                                col_idx = 0
                                
                                current_row_cols_count = 0
                                for cell in cells:
                                    colspan = int(cell.get('colspan', 1))
                                    current_row_cols_count += colspan
                                    ctext = norm(cell.get_text())
                                    
                                    # パーセント表記や比率列は絶対に金額カラムとして採用しない
                                    if any(x in ctext for x in ['%', '％', '比', '率', '増減']):
                                        pass
                                    elif '受注工事高' in ctext or ctext.startswith('受注高') or ctext.startswith('当期受注高') or ctext.startswith('受注額'):
                                        col_map['orders_received'] = col_idx
                                        has_order_col_in_this_row = True
                                    elif '受注残高' in ctext or '受注残' in ctext or '次期繰越工事高' in ctext or '当期受注残高' in ctext:
                                        col_map['order_backlog'] = col_idx
                                        has_order_col_in_this_row = True
                                    elif '繰越工事高' in ctext or '期末繰越' in ctext:
                                        if '前期' not in ctext and '期首' not in ctext:
                                            col_map['construction_carryover'] = col_idx
                                            has_order_col_in_this_row = True
                                    elif ('当期売上高' in ctext or '完成工事高' in ctext):
                                        col_map['completed_construction'] = col_idx
                                        
                                    col_idx += colspan
                                        
                                # ヘッダー行は一度見つけたら上書きしない
                                if has_order_col_in_this_row and header_row_idx == -1:
                                    header_row_idx = i
                                    header_cols_count = current_row_cols_count
                            
                            if not col_map or header_row_idx == -1:
                                continue
                                
                            result['source_tag'] = name_attr
                            result['source_type'] = 'table'
                            if unit_str: result['unit'] = unit_str
                            elif '千円' in ttext: result['unit'] = '千円'
                            elif '百万円' in ttext: result['unit'] = '百万円'
                            elif '億円' in ttext: result['unit'] = '億円'
                            
                            found_valid_row = False
                            for row in rows[header_row_idx+1:]:
                                cells = row.find_all(['td', 'th'])
                                if not cells: continue
                                first_cell = norm(cells[0].get_text())
                                
                                is_target_row = False
                                if '合計' in first_cell or first_cell == '計' or '受注実績' in first_cell or '全社' in first_cell:
                                    is_target_row = True
                                    
                                if company == '鹿島建設' and '合計' in first_cell:
                                    is_target_row = False
                                    
                                if is_target_row:
                                    col_vals = []
                                    for c in cells:
                                        colspan = int(c.get('colspan', 1))
                                        col_vals.extend([norm(c.get_text())] * colspan)
                                        
                                    shift = 0
                                    if len(col_vals) > header_cols_count:
                                        shift = len(col_vals) - header_cols_count
                                    elif len(col_vals) < header_cols_count:
                                        pad = header_cols_count - len(col_vals)
                                        col_vals = [''] * pad + col_vals
                                        
                                    try:
                                        temp_res = {}
                                        
                                        def safe_parse(idx):
                                            actual_idx = idx + shift
                                            if 0 <= actual_idx < len(col_vals):
                                                val_str = col_vals[actual_idx]
                                                if any(x in val_str for x in ['%', '％']):
                                                    return None
                                                return parse_number(val_str)
                                            return None

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
                                            found_valid_row = True
                                            header_strs = [norm(td.get_text())[:10] for td in rows[header_row_idx].find_all(['td','th'])]
                                            row_strs = [v[:15] for v in col_vals]
                                            result['snippet'] = f"Header: {'|'.join(header_strs)}\nRow: {'|'.join(row_strs)}"
                                    except Exception as e:
                                        pass
                                        
                            if found_valid_row:
                                found_order = True
                                if result['unit'] is None:
                                    result['confidence'] = 'low'
                                    result['notes'] += "Unit not found. "
                                elif table_count > 1:
                                    result['confidence'] = 'medium'
                                    result['notes'] += f"Multiple tables found ({table_count}), picked last. "
                                else:
                                    result['confidence'] = 'high'
                                    
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
    
    out_json = r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\orders_extracted_30_v2.json'
    out_md = r'C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\orders_extracted_30_v2_summary.md'
    
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
        f.write("# 30社 DRY RUN 抽出結果サマリー (v2)\n\n")
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
