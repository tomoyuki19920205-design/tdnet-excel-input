from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

viewer = Path(r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_final_materialized_v1.xlsx")
print("[VIEWER]", viewer)
print("[EXISTS]", viewer.exists())
if not viewer.exists():
    raise SystemExit("viewer not found")

def read_xml(z, name):
    data = z.read(name)
    return ET.fromstring(data)

with zipfile.ZipFile(viewer, "r") as z:
    names = set(z.namelist())
    print("[ZIP] entries:", len(names))

    # 1) workbook relationships（外部参照のターゲットがここに出る）
    rel_path = "xl/_rels/workbook.xml.rels"
    if rel_path in names:
        rel = read_xml(z, rel_path)
        # namespace無視で拾う
        rels = []
        for e in rel.iter():
            if e.tag.endswith("Relationship"):
                rid = e.attrib.get("Id")
                typ = e.attrib.get("Type","")
                tgt = e.attrib.get("Target","")
                if "externalLink" in typ or "oleObject" in typ or "connections" in typ or "worksheet" in typ:
                    rels.append((rid, typ, tgt))
        print("\n=== workbook.xml.rels interesting relationships ===")
        for r in rels:
            print(r)
    else:
        print("NO", rel_path)

    # 2) externalLinks/*.xml（外部ブック参照のパスがここに入ってることが多い）
    ext_files = sorted([n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")])
    print("\n=== externalLinks files ===")
    for f in ext_files:
        print(f)

    print("\n=== externalLinks targets (raw) ===")
    for f in ext_files[:50]:
        root = read_xml(z, f)
        # <externalBook r:id="rIdX"> があるので、rIdを拾う
        for e in root.iter():
            if e.tag.endswith("externalBook"):
                rid = e.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                if rid:
                    print(f, "->", rid)

    # 3) definedNames（[data.xlsx] がここに出ることが多い）
    wb_path = "xl/workbook.xml"
    if wb_path in names:
        wb = read_xml(z, wb_path)
        print("\n=== definedNames containing 'data.xlsx' / '[' / 'OneDrive' ===")
        for dn in wb.iter():
            if dn.tag.endswith("definedName"):
                name = dn.attrib.get("name","")
                text = (dn.text or "")
                t = text.lower()
                if ("data.xlsx" in t) or ("onedrive" in t) or ("[" in text):
                    print("**", name, "=", text[:300])
    else:
        print("NO", wb_path)

print("\n[DONE]")
