from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

targets = [
r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view.xlsx",
r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_formula_fixed.xlsx",
r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_linkfixed.xlsx",
r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_million_display - コピー.xlsx",
r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_million_display.xlsx",
r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_million_display_periodlinked_excel2016_compat_v3.xlsx",
r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_xlookup_fixed.xlsx",
]

NS = {"r":"http://schemas.openxmlformats.org/package/2006/relationships"}

def parse_rels(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out=[]
    for rel in root.findall("r:Relationship", NS):
        out.append({
            "Id": rel.attrib.get("Id",""),
            "Type": rel.attrib.get("Type",""),
            "Target": rel.attrib.get("Target",""),
            "TargetMode": rel.attrib.get("TargetMode",""),
        })
    return out

def scan_one(path):
    p = Path(path)
    if not p.exists():
        return {"file": path, "error":"not found", "targets":[]}
    try:
        with zipfile.ZipFile(p,"r") as z:
            names=set(z.namelist())
            # externalLinks の一覧
            ext_xmls = sorted([n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")])
            # externalLinks の rels から実ファイルターゲットを拾う
            ext_targets=[]
            for x in ext_xmls:
                rels_path = x.replace("externalLinks/", "externalLinks/_rels/") + ".rels"
                if rels_path in names:
                    rels = parse_rels(z.read(rels_path))
                    for r in rels:
                        if r["TargetMode"] == "External":
                            ext_targets.append(r["Target"])
            # workbook rels も一応
            wb_rels_path = "xl/_rels/workbook.xml.rels"
            wb_targets=[]
            if wb_rels_path in names:
                rels = parse_rels(z.read(wb_rels_path))
                for r in rels:
                    if r["TargetMode"] == "External":
                        wb_targets.append(r["Target"])
            return {"file": path, "ext_xmls": ext_xmls, "ext_targets": sorted(set(ext_targets)), "wb_targets": sorted(set(wb_targets)), "error":""}
    except Exception as e:
        return {"file": path, "error":repr(e), "ext_xmls":[], "ext_targets":[], "wb_targets":[]}

results=[scan_one(t) for t in targets]

for r in results:
    print("\n===", r["file"])
    if r["error"]:
        print("ERROR:", r["error"])
        continue
    print("externalLinks xml count:", len(r["ext_xmls"]))
    print("external externalLinks targets:")
    if r["ext_targets"]:
        for t in r["ext_targets"]:
            print("  ", t)
    else:
        print("  (none)")
    if r["wb_targets"]:
        print("workbook external targets:")
        for t in r["wb_targets"]:
            print("  ", t)
