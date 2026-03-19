from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

viewer = Path(r"C:\Users\takuy\OneDrive\viewer.xlsx")
print("[VIEWER]", viewer)
print("[EXISTS]", viewer.exists())
if not viewer.exists():
    raise SystemExit("viewer.xlsx not found")

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

with zipfile.ZipFile(viewer, "r") as z:
    names = set(z.namelist())
    print("[ZIP] entries:", len(names))

    # externalLinks の有無
    ext_xmls = sorted([n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")])
    print("\n[externalLinks xml count]:", len(ext_xmls))
    for f in ext_xmls:
        print("  ", f)

    # externalLinks の参照先（file:///...）を抜く
    ext_targets=[]
    for x in ext_xmls:
        rels_path = x.replace("externalLinks/", "externalLinks/_rels/") + ".rels"
        if rels_path in names:
            rels = parse_rels(z.read(rels_path))
            for r in rels:
                if r["TargetMode"] == "External":
                    ext_targets.append(r["Target"])
    ext_targets = sorted(set(ext_targets))

    print("\n[external targets]:")
    if ext_targets:
        for t in ext_targets: print("  ", t)
    else:
        print("  (none)")

    # definedNames 内に [data.xlsx] などがあるか
    wb_path = "xl/workbook.xml"
    if wb_path in names:
        wb = ET.fromstring(z.read(wb_path))
        hit=0
        print("\n[definedNames containing data/onedrive/bracket]:")
        for dn in wb.iter():
            if dn.tag.endswith("definedName"):
                text = (dn.text or "")
                t = text.lower()
                if ("data.xlsx" in t) or ("onedrive" in t) or ("[" in text):
                    name = dn.attrib.get("name","")
                    print("  **", name, "=", text[:250])
                    hit += 1
        if hit == 0:
            print("  (none)")

print("\n[DONE]")
